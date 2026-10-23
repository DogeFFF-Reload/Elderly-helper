"""语音链路与总线之间的小工具：UTF-8 发送 + 订阅事件流。只用标准库。"""
import json
import threading
import time
import urllib.request


def post_json(url, payload, timeout=3.0):
    """统一按 UTF-8 发送（避免 Windows 中文乱码），返回解析后的 JSON。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url, timeout=3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class EventFollower:
    """后台轮询 /api/events，只把启动之后的新事件交给 on_event(ev)。

    总线重启（事件 id 从头开始）时自动对齐，不会漏也不会重放历史。
    """

    def __init__(self, server, on_event, types=None, interval=0.3):
        self.server = server.rstrip("/")
        self.on_event = on_event
        self.types = set(types) if types else None
        self.interval = interval
        self.since = None
        self.connected = False
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def poll_once(self):
        if self.since is None:
            self.since = get_json("%s/api/events?since=%d" % (self.server, 1 << 60))["last_id"]
            return []
        data = get_json("%s/api/events?since=%d" % (self.server, self.since))
        if data["last_id"] < self.since:
            self.since = data["last_id"]
            return []
        out = []
        for ev in data["events"]:
            self.since = max(self.since, ev["id"])
            if self.types is None or ev["type"] in self.types:
                out.append(ev)
        return out

    def _loop(self):
        while not self._stop.is_set():
            try:
                for ev in self.poll_once():
                    self.on_event(ev)
                self.connected = True
            except Exception:
                self.connected = False
            time.sleep(self.interval)
