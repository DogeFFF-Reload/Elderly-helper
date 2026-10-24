"""Run ONLY the actual bedside alarm definitions on loopback, never import service.

The real module opens serial/SDKs at top level. AST extraction compiles an
explicit allowlist of alarm functions and HTTP handler definitions, with a
temporary alarm directory. Every other route is blocked in this test server.
"""
import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading


class AlarmReceiver:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        source = Path(__file__).resolve().parents[2] / "bedside_family/service_handboard/arm_service.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"alarms_load", "alarm_new", "alarm_incoming", "alarms_ack", "Handler"}
        selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name in names]
        assert {node.name for node in selected} == names
        version = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign) and any(
                           isinstance(t, ast.Name) and t.id == "SVC_VER" for t in node.targets))
        lock = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_alarm_lock" for t in node.targets))
        assert (isinstance(lock.value, ast.Call) and not lock.value.args and not lock.value.keywords
                and isinstance(lock.value.func, ast.Attribute) and lock.value.func.attr in ("Lock", "RLock")
                and isinstance(lock.value.func.value, ast.Name) and lock.value.func.value.id == "threading")
        self.namespace = {"os": os, "json": json, "BaseHTTPRequestHandler": BaseHTTPRequestHandler,
                          "SVC_VER": version, "threading": threading, "_alarm_mem": [],
                          "AL_DIR": self.temp.name, "ALARMS_JSON": self.temp.name + "/alarms.json",
                          "_log": lambda *args: None}
        exec(compile(ast.Module(body=[lock, *selected], type_ignores=[]), str(source), "exec"), self.namespace)
        self.requests, self.drop_next = [], False
        owner = self

        class IsolatedHandler(self.namespace["Handler"]):
            def do_GET(self):
                assert self.path in ("/fall_alarm", "/alarms"), "Non-alarm route forbidden"
                super().do_GET()

            def do_POST(self):
                assert self.path in ("/fall_alarm", "/alarms/ack"), "Hardware route forbidden"
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(json.loads(raw))
                self.rfile = io.BytesIO(raw)
                super().do_POST()

            def _send(self, obj, code=200):
                if owner.drop_next and self.path == "/fall_alarm" and not obj.get("dry"):
                    owner.drop_next = False
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                super()._send(obj, code)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), IsolatedHandler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        return self

    @property
    def alarms(self):
        return self.namespace["_alarm_mem"]

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()
