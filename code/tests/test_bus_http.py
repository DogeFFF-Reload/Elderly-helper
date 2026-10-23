"""HTTP 层：中文编码、三块屏可访问、家属端接口、路径穿越防护。"""
import http.client
import json
import threading
import unittest
import urllib.parse

from helpers import make_bus

import master_service


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus, cls.clock, cls.arm = make_bus()
        cls.server = master_service.serve(cls.bus, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_ticker.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.bus.reset()

    def req(self, method, path, body=None, ctype="application/json; charset=utf-8"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers={"Content-Type": ctype} if body is not None else {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def post(self, path, obj, encoding="utf-8"):
        status, data = self.req("POST", path, json.dumps(obj, ensure_ascii=False).encode(encoding))
        return json.loads(data.decode("utf-8"))

    def test_voice_utf8(self):
        self.assertEqual(self.post("/mock/voice", {"text": "把药递给我"})["intent"], "deliver")

    def test_voice_gbk_body_still_understood(self):
        # Windows PowerShell 不带 charset 直接发中文时常见
        self.assertEqual(self.post("/mock/voice", {"text": "把药递给我"}, encoding="gbk")["intent"], "deliver")

    def test_fall_room_query(self):
        self.post("/mock/fall?room=" + urllib.parse.quote("客厅"), {})
        self.assertEqual(self.bus.fsm.room, "客厅")
        self.post("/mock/button?key=ok", {})
        self.assertEqual(self.bus.fsm.state, "IDLE")

    def test_three_screens_served(self):
        for page, title in (("monitor", "大屏"), ("family", "家属端"), ("console", "操作台")):
            status, data = self.req("GET", "/ui/" + page)
            self.assertEqual(status, 200, page)
            self.assertIn(title, data.decode("utf-8"))
        self.assertEqual(self.req("GET", "/ui/shared/bus.js")[0], 200)

    def test_path_traversal_blocked(self):
        self.assertEqual(self.req("GET", "/ui/../config.py")[0], 404)
        self.assertEqual(self.req("GET", "/ui/%2e%2e/config.py")[0], 404)

    def test_poll_shape(self):
        self.post("/mock/voice", {"text": "我没事"})
        status, data = self.req("GET", "/api/poll?since=0")
        d = json.loads(data)
        self.assertEqual({"state", "events", "last_id"}, set(d))
        self.assertEqual(d["events"][-1]["type"], "say")

    def test_params_endpoint(self):
        status, data = self.req("GET", "/api/params")
        d = json.loads(data)
        self.assertEqual(d["params"]["ask_wait_day"], 5.0)
        self.assertEqual(d["limits"]["night_start_hour"], [0, 23])
        r = self.post("/api/params", {"ask_wait_day": 7})
        self.assertEqual(r["changed"], {"ask_wait_day": 7.0})
        self.assertFalse(self.post("/api/params", {"llm_api_key": "sk-x"})["ok"])
        self.post("/api/params", {"ask_wait_day": 5})

    def test_box_protocol(self):
        get = lambda: json.loads(self.req("GET", "/api/box?id=box1")[1])  # noqa: E731
        self.assertEqual((get()["led"], get()["pattern"]), ("green", "breath"))
        self.assertTrue(self.bus.snapshot()["box"]["online"])
        self.post("/mock/fall", {"room": "卧室"})
        self.assertEqual(get()["led"], "yellow")
        self.post("/mock/button?key=ok", {})
        self.assertEqual(get()["state"], "IDLE")
        self.clock.advance(4)
        self.assertFalse(self.bus.snapshot()["box"]["online"], "3 秒没轮询 = 离线")

    def test_family_endpoints(self):
        self.post("/api/family/message", {"contact": "daughter", "text": "妈，记得按时吃药哈"})
        self.assertTrue(self.post("/api/echo/authorize", {"contact": "daughter"})["ok"])
        self.assertTrue(self.bus.style.is_authorized("daughter"))
        self.post("/api/echo/revoke", {"contact": "daughter"})
        self.assertFalse(self.bus.style.is_authorized("daughter"))
        self.assertTrue(self.post("/api/night", {"mode": "on"})["night"])


if __name__ == "__main__":
    unittest.main()
