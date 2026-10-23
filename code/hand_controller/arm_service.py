"""臂 + 双手执行服务（mock，:8013）。真硬件实现由硬件同学替换 _execute，接口不变。

POST /pose?name=press_sos&hand=1   执行动作（hand=2 驱动手#2）
GET  /status                       在线状态 + 最近动作

运行：python -m hand_controller.arm_service [--port 8013]
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import utf8_console
from hand_controller.poses import POSES, label

HISTORY = []


def _execute(pose, hand):
    # 真硬件：查 poses → RH56F2 寄存器写入 + 臂轨迹；这里只打印
    print("[arm mock] hand#%d → %s（%s）" % (hand, pose, label(pose)), flush=True)


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
            return self._json(200, {"ok": True, "mock": True, "recent": HISTORY[-10:]})
        self._json(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/pose":
            return self._json(404, {"ok": False})
        q = parse_qs(url.query)
        name = q.get("name", [""])[0]
        hand = int(q.get("hand", ["1"])[0])
        if name not in POSES:
            return self._json(400, {"ok": False, "error": "unknown pose: " + name})
        _execute(name, hand)
        HISTORY.append({"ts": time.time(), "pose": name, "hand": hand})
        self._json(200, {"ok": True, "pose": name, "hand": hand, "mock": True})

    def log_message(self, *args):
        pass


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    args = ap.parse_args()
    print("arm_service (mock) on http://%s:%d" % (args.host, args.port), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
