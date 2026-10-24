"""HTTP 层：中文编码、三块屏可访问、家属端接口、路径穿越防护。"""
import http.client
import io
import json
import threading
import unittest
import urllib.parse
from types import SimpleNamespace

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

    def test_invalid_body_cannot_acknowledge_sos(self):
        for body in (b"{broken", b"[]", b"null", b'{"contact": NaN}', b'{"contact": Infinity}'):
            with self.subTest(body=body):
                self.post("/mock/fall", {"room": "客厅"})
                self.bus.voice("起不来")
                status, data = self.req("POST", "/api/family/ack", body)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(data)["ok"])
                self.assertEqual(self.bus.fsm.state, "SOS")
                self.bus.reset()

    def test_ack_requires_explicit_valid_contact(self):
        self.post("/mock/fall", {"room": "客厅"})
        self.bus.voice("起不来")
        for body in ({}, {"contact": "unknown"}):
            self.assertFalse(self.post("/api/family/ack", body)["ok"])
            self.assertEqual(self.bus.fsm.state, "SOS")
        self.assertTrue(self.post("/api/family/ack", {"contact": "daughter"})["ok"])
        self.assertEqual(self.bus.fsm.state, "HELP_COMING")

    def test_oversized_body_rejected(self):
        status, data = self.req("POST", "/api/family/ack", b" " * (master_service.MAX_JSON_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertFalse(json.loads(data)["ok"])

    def test_bad_cursor_returns_json_error_and_server_survives(self):
        for endpoint in ("/api/poll", "/api/events"):
            for since in ("bad", "-1", "1.2"):
                status, data = self.req("GET", endpoint + "?since=" + since)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(data)["ok"])
        self.assertEqual(self.req("GET", "/api/state")[0], 200)


class BodyReadTest(unittest.TestCase):
    def test_bad_lengths_rejected_before_read(self):
        for length, status in (("no", 400), ("-1", 400), (str(master_service.MAX_JSON_BYTES + 1), 413)):
            handler = SimpleNamespace(headers={"Content-Length": length}, rfile=io.BytesIO(b"{}"))
            with self.assertRaises(master_service.RequestBodyError) as cm:
                master_service._read_json(handler)
            self.assertEqual(cm.exception.status, status)
            self.assertEqual(handler.rfile.tell(), 0)

    def test_truncated_body_rejected(self):
        handler = SimpleNamespace(headers={"Content-Length": "10"}, rfile=io.BytesIO(b"{}"))
        with self.assertRaises(master_service.RequestBodyError):
            master_service._read_json(handler)


if __name__ == "__main__":
    unittest.main()
