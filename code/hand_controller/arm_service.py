"""臂 + 双手执行服务（当前是 mock，:8013）。

仓库里只运行这一份执行端服务。真硬件实现由硬件同学替换 ``_execute``，
HTTP 协议保持不变；同时保留 kit 的动作别名，方便手#2 组按 kit 的验收
命令联调，而不把 kit 自带的第二份 arm_service 放到 8013 上。

POST /pose?name=press_sos&hand=1   执行动作（hand=2 驱动手#2）
POST /pose/comfort_rhythm?hand=2    kit 兼容写法，映射到 gentle_hold
POST /press_sos                     kit 兼容快捷路由
POST /handover_med                  kit 兼容快捷路由，映射到 deliver_medicine
GET  /status                       在线状态 + 最近动作

运行：python -m hand_controller.arm_service [--port 8013]
"""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from config import utf8_console
from hand_controller.poses import POSES, label
from hand_controller.rh56_backend import RH56Backend
from hand_controller.rh56f2_driver import RH56F2, RH56Error

HISTORY = []
HISTORY_LIMIT = 200
_HISTORY_LOCK = threading.Lock()

MOCK_SERIAL = os.environ.get("MOCK_SERIAL", "1").strip() != "0"
ARM_BACKEND = os.environ.get("ARM_BACKEND", "mock").strip().lower()
_REAL_BACKEND = None
_BACKEND_ERROR = None
_BACKEND_LOCK = threading.Lock()
ASYNC_POSES = {"gentle_hold", "wave", "nod_like", "clench_open", "count_five"}


def _record_history(entry):
    """Bound and serialize the action log shared by request/worker threads."""
    with _HISTORY_LOCK:
        HISTORY.append(entry)
        del HISTORY[:-HISTORY_LIMIT]


def _recent_history(limit=20):
    with _HISTORY_LOCK:
        return list(HISTORY[-limit:])

# 外部 kit 的动作名只在 HTTP 边界做映射，主脑内部和 HISTORY 始终使用仓库
# 的规范名。这样不会把另一套服务的命名渗进状态机或 UI。
POSE_ALIASES = {
    "rock": "rps_rock",
    "comfort_rhythm": "gentle_hold",
    "thumbs_up": "sign_family_received",
    "handover_med": "deliver_medicine",
}
PATH_ALIASES = {
    "/press_sos": "press_sos",
    "/handover_med": "deliver_medicine",
    "/reset": "home",
    "/card_up": "hold_card",
    "/thumbs": "thumbs_up",
    "/wave": "wave",
}


def _ensure_real_backend():
    """Open the two RH56 links once, lazily, and fail closed on setup errors."""
    global _REAL_BACKEND, _BACKEND_ERROR
    if MOCK_SERIAL:
        return None
    with _BACKEND_LOCK:
        if _REAL_BACKEND is not None:
            return _REAL_BACKEND
        if _BACKEND_ERROR is not None:
            raise RH56Error(_BACKEND_ERROR)
        opened = []
        try:
            port1 = os.environ.get("HAND1_PORT", "COM5")
            port2 = os.environ.get("HAND2_PORT", "COM6")
            hand1 = RH56F2(
                port1,
                int(os.environ.get("HAND1_ID", "1")),
                mock=False,
            )
            opened.append(hand1)
            if port2.lower() == port1.lower():
                # One RS485 bus can carry both slave IDs.  Reuse its serial
                # object instead of opening the same device twice.
                hand2 = RH56F2(
                    port2, int(os.environ.get("HAND2_ID", "2")),
                    mock=False, serial_obj=hand1.ser, confirmed=True,
                )
                hand2._io_lock = hand1._io_lock
            else:
                hand2 = RH56F2(
                    port2, int(os.environ.get("HAND2_ID", "2")),
                    mock=False,
                )
            opened.append(hand2)
            _REAL_BACKEND = RH56Backend(hand1, hand2)
            return _REAL_BACKEND
        except Exception as exc:  # noqa: BLE001: service must report the real reason
            for hand in opened:
                try:
                    hand.close()
                except Exception:
                    pass
            _BACKEND_ERROR = str(exc)
            raise RH56Error(_BACKEND_ERROR) from exc


def _vendor_arm(pose):
    """Dispatch arm-only work to the Panthera slot, never to a fake driver."""
    if ARM_BACKEND != "vendor_sdk":
        return {"ok": False, "error": "%s requires ARM_BACKEND=vendor_sdk" % pose,
                "backend": ARM_BACKEND}
    from hand_controller.arm_backend_vendor import execute
    waypoint = {
        "press_sos": "press_sos",
        "deliver_medicine": "handover_med",
        "sign_coming": "sign_coming",
        "home": "reset",
    }.get(pose, pose)
    return execute(waypoint)


def _execute(pose, hand):
    """Execute one canonical action and return an honest backend result."""
    if MOCK_SERIAL:
        print("[arm mock] hand#%d → %s（%s）" % (hand, pose, label(pose)), flush=True)
        return {"ok": True, "mock": True, "backend": "mock", "did": "pose:" + pose}
    if pose in ("press_sos", "deliver_medicine", "sign_coming", "home"):
        return _vendor_arm(pose)
    return _ensure_real_backend().execute(pose, hand)


def _execute_async(pose, hand):
    """Run a long RH56 sequence and retain a failure for /status inspection."""
    try:
        result = _execute(pose, hand)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    if result is not None and not result.get("ok", False):
        _record_history({"ts": time.time(), "pose": pose, "hand": hand,
                         "ok": False, "async": True,
                         "error": result.get("error", "sequence failed")})


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/status":
            if not MOCK_SERIAL:
                try:
                    hands = _ensure_real_backend().status()
                except Exception as exc:  # noqa: BLE001: expose hardware state to preflight
                    return self._json(503, {"ok": False, "mock": False,
                                            "backend": "rh56_serial",
                                            "arm_backend": ARM_BACKEND,
                                            "error": str(exc), "recent": _recent_history()})
                return self._json(200, {"ok": True, "mock": False,
                                       "backend": "rh56_serial",
                                       "arm_backend": ARM_BACKEND,
                                       "hands": hands, "recent": _recent_history()})
            return self._json(200, {"ok": _BACKEND_ERROR is None,
                                   "mock": True, "backend": "mock",
                                   "arm_backend": ARM_BACKEND,
                                   "error": _BACKEND_ERROR, "recent": _recent_history()})
        self._json(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        requested = None
        canonical = None
        kit_pose_route = False
        if url.path == "/pose":
            q = parse_qs(url.query)
            requested = q.get("name", [""])[0]
        elif url.path.startswith("/pose/"):
            kit_pose_route = True
            requested = unquote(url.path[len("/pose/"):]).strip("/")
            q = parse_qs(url.query)
        elif url.path in PATH_ALIASES:
            requested = url.path.lstrip("/")
            canonical = PATH_ALIASES[url.path]
            q = parse_qs(url.query)
        else:
            return self._json(404, {"ok": False})
        name = canonical or POSE_ALIASES.get(requested, requested)
        if name not in POSES:
            # The hardware kit's standalone contract uses HTTP 200 with
            # ``ok:false`` for an unknown /pose/<name>.  The master-facing
            # query form stays a real HTTP error so ArmClient records a
            # rejection instead of counting it as a successful action.
            if kit_pose_route:
                return self._json(200, {"ok": False, "error": "unknown pose: " + str(requested)})
            return self._json(400, {"ok": False, "error": "unknown pose: " + str(requested)})
        try:
            hand = int(q.get("hand", [POSES[name].get("hand", 1)])[0])
        except (TypeError, ValueError):
            return self._json(400, {"ok": False, "error": "invalid hand"})
        if hand not in (1, 2):
            return self._json(400, {"ok": False, "error": "hand must be 1 or 2"})
        if name == "gentle_hold":
            hand = 2
        # The comfort rhythm is intentionally slow.  In real-hand mode run it
        # off the request thread so the master receives its accepted response
        # before ArmClient's 1.5s transport timeout.
        if not MOCK_SERIAL and name in ASYNC_POSES:
            try:
                _ensure_real_backend()
            except RH56Error as exc:
                _record_history({"ts": time.time(), "pose": name, "hand": hand,
                                "requested": requested, "ok": False, "error": str(exc)})
                return self._json(503, {"ok": False, "error": str(exc)})
            threading.Thread(target=_execute_async, args=(name, hand), daemon=True).start()
            result = {"ok": True, "queued": "pose:" + name, "backend": "rh56_serial"}
        else:
            try:
                result = _execute(name, hand)
            except Exception as exc:  # noqa: BLE001: expose setup/protocol failures as HTTP errors
                result = {"ok": False, "error": str(exc)}
        if result is None:  # test doubles and old hardware adapters return None on success
            result = {"ok": True}
        if not result.get("ok", False):
            _record_history({"ts": time.time(), "pose": name, "hand": hand,
                             "requested": requested, "ok": False,
                             "error": result.get("error", "backend rejected")})
            return self._json(503, result)
        _record_history({"ts": time.time(), "pose": name, "hand": hand,
                         "requested": requested, "ok": True})
        body = {"ok": True, "pose": name, "requested": requested, "hand": hand,
                "mock": MOCK_SERIAL}
        body.update({k: v for k, v in result.items() if k not in body})
        self._json(200, body)

    def log_message(self, *args):
        pass


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    args = ap.parse_args()
    print("arm_service on http://%s:%d (MOCK_SERIAL=%s, ARM_BACKEND=%s)" %
          (args.host, args.port, "1" if MOCK_SERIAL else "0", ARM_BACKEND), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
