"""Explicit pure-core extraction: never import hardware service or driver modules."""
import ast
import json
import os
from pathlib import Path
import re
import threading
import types


def load_service(directory):
    root = Path(__file__).resolve().parents[1] / "service_handboard"
    module = types.ModuleType("isolated_bedside_core")
    ns = module.__dict__
    ns.update(os=os, json=json, _log=lambda *a: None, _alarm_mem=[],
              AL_DIR=directory, ALARMS_JSON=os.path.join(directory, "alarms.json"),
              MSG_DIR=os.path.join(directory, "messages"))
    poses = ast.parse((root / "poses.py").read_text(encoding="utf-8"))
    # Data definitions only; no imports of serial drivers, SDKs or services.
    data = [node for node in poses.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for node in data:
        for item in ast.walk(node):
            if isinstance(item, ast.Call):
                assert (isinstance(item.func, ast.Name) and item.func.id == "set"
                        and len(item.args) == 1 and not item.keywords
                        and isinstance(item.args[0], ast.Name)
                        and item.args[0].id in ("POSE_VOCAB", "SEQUENCES"))
    for node in data:
        if isinstance(node, ast.AnnAssign):
            node.annotation = ast.Name(id="object", ctx=ast.Load())
    exec(compile(ast.fix_missing_locations(ast.Module(body=data, type_ignores=[])),
                 str(root / "poses.py"), "exec"), ns)
    source = root / "arm_service.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        for name in names & {"VOICE_OPEN", "_alarm_serial_ms"}:
            ns[name] = ast.literal_eval(node.value)
        if "WAYPOINTS" in names:
            assert isinstance(node.value, ast.Dict)
            # The test only needs the whitelist keys, never executable action bodies.
            ns["WAYPOINTS"] = {ast.literal_eval(key): None for key in node.value.keys}
        if "_ID_OK" in names:
            assert isinstance(node.value, ast.Call)
            ns["_ID_OK"] = re.compile(ast.literal_eval(node.value.args[0]))
        if "_alarm_lock" in names:
            assert (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == "threading"
                    and node.value.func.attr in ("Lock", "RLock"))
            ns["_alarm_lock"] = getattr(threading, node.value.func.attr)()
    names = {"alarm_new", "alarm_incoming", "alarms_load", "alarms_ack", "_voice_open_match", "_msg_paths"}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), ns)
    return module
