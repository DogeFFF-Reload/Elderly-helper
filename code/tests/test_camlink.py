"""视频看护服务：钥匙 / 本机限制、信令往返、骨架进原跌倒规则。只用本机回环，不开摄像头、不起隧道。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from vision.camlink.server import CamLink, KP_NAMES, clean_kp, make_handler

TUNNEL = {"Cf-Connecting-Ip": "203.0.113.9"}


def standing(y0=0.2):
    kp = {}
    xs = {"l": 0.45, "r": 0.55}
    for side, x in xs.items():
        kp[side + "_shoulder"] = [x, y0 + 0.15, 0.99]
        kp[side + "_elbow"] = [x, y0 + 0.3, 0.99]
        kp[side + "_wrist"] = [x, y0 + 0.42, 0.99]
        kp[side + "_hip"] = [x, y0 + 0.45, 0.99]
        kp[side + "_knee"] = [x, y0 + 0.6, 0.99]
        kp[side + "_ankle"] = [x, y0 + 0.75, 0.99]
    kp["head"] = [0.5, y0, 0.99]
    kp["neck"] = [0.5, y0 + 0.15, 0.99]
    return kp


class CamLinkHttpTest(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.link = CamLink(clock=lambda: self.now[0], token="secret-key")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.link))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def req(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        h = dict(headers or {})
        if data is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_tunnel_requests_need_the_key_and_reach_only_phone_endpoints(self):
        self.assertEqual(self.req("GET", "/cam", headers=TUNNEL)[0], 403)
        self.assertEqual(self.req("GET", "/cam?k=wrong", headers=TUNNEL)[0], 403)
        st, page = self.req("GET", "/cam?k=secret-key", headers=TUNNEL)
        self.assertEqual(st, 200)
        self.assertIn("开始看护".encode(), page)
        for path in ("/", "/api/status", "/api/sig/offer", "/vendor/qrcode.js"):
            self.assertEqual(self.req("GET", path + "?k=secret-key", headers=TUNNEL)[0], 404, path)
        self.assertEqual(self.req("POST", "/api/pose", {"kp": None}, headers=TUNNEL)[0], 404)
        self.assertEqual(self.req("POST", "/api/sig/answer", {"id": "x", "sdp": "v=0" * 10}, headers=TUNNEL)[0], 404)
        self.assertEqual(self.req("POST", "/api/sig/offer", {"sdp": "v=0" * 10}, headers=TUNNEL)[0], 403)

    def test_signaling_round_trip_like_a_call(self):
        offer = "v=0 offer " * 5
        st, body = self.req("POST", "/api/sig/offer?k=secret-key", {"sdp": offer}, headers=TUNNEL)
        self.assertEqual(st, 200)
        sid = json.loads(body)["id"]
        status = json.loads(self.req("GET", "/api/status")[1])
        self.assertTrue(status["phone_online"] and status["pending_offer"])
        got = json.loads(self.req("GET", "/api/sig/offer")[1])
        self.assertEqual((got["id"], got["sdp"]), (sid, offer))
        self.assertIsNone(json.loads(self.req("GET", "/api/sig/answer?k=secret-key&id=" + sid, headers=TUNNEL)[1])["sdp"])
        self.assertEqual(self.req("POST", "/api/sig/answer", {"id": sid, "sdp": "v=0 answer " * 5})[0], 200)
        ans = json.loads(self.req("GET", "/api/sig/answer?k=secret-key&id=" + sid, headers=TUNNEL)[1])
        self.assertEqual(ans["sdp"], "v=0 answer " * 5)
        self.assertEqual(json.loads(self.req("GET", "/api/sig/offer")[1]), {})
        self.assertEqual(self.req("POST", "/api/phone/bye?k=secret-key", headers=TUNNEL)[0], 200)

    def test_one_call_goes_to_one_monitor_page(self):
        self.req("POST", "/api/sig/offer?k=secret-key", {"sdp": "v=0 offer " * 5}, headers=TUNNEL)
        self.assertIn("sdp", json.loads(self.req("GET", "/api/sig/offer")[1]))
        self.assertEqual(json.loads(self.req("GET", "/api/sig/offer")[1]), {})   # 第二个页面拿不到
        self.now[0] += 11
        self.assertIn("sdp", json.loads(self.req("GET", "/api/sig/offer")[1]))   # 第一个页面没回应，放给别人
        self.assertFalse(json.loads(self.req("GET", "/api/status")[1])["phone_online"])

    def test_stale_offer_expires_and_phone_goes_offline(self):
        self.req("POST", "/api/sig/offer?k=secret-key", {"sdp": "v=0 offer " * 5}, headers=TUNNEL)
        self.now[0] += 120
        self.assertEqual(json.loads(self.req("GET", "/api/sig/offer")[1]), {})
        self.assertFalse(json.loads(self.req("GET", "/api/status")[1])["phone_online"])

    def test_pose_goes_through_original_fall_rules(self):
        st, body = self.req("POST", "/api/pose", {"kp": standing(), "aspect": 16 / 9})
        self.assertEqual(st, 200)
        state = json.loads(body)
        self.assertIn(state["status"], ("observing", "waiting"))
        self.assertFalse(state["event"])
        st, body = self.req("POST", "/api/pose", {"kp": None})
        self.assertEqual(json.loads(body)["status"], "no_person")
        self.assertEqual(self.req("POST", "/api/pose", "not json")[0], 200)  # 非字典正文当没检测到人

    def test_bad_bodies_are_rejected(self):
        self.assertEqual(self.req("POST", "/api/sig/offer?k=secret-key", {"sdp": "x"}, headers=TUNNEL)[0], 400)
        self.assertEqual(self.req("POST", "/api/sig/answer", {"id": "nope", "sdp": "v=0 answer " * 5})[0], 404)


class CleanKpTest(unittest.TestCase):
    def test_only_complete_finite_14_points_pass(self):
        kp = standing()
        self.assertEqual(set(clean_kp(kp)), set(KP_NAMES))
        broken = dict(kp, head=[0.5, "x", 1])
        self.assertIsNone(clean_kp(broken))
        self.assertIsNone(clean_kp(dict(kp, head=[0.5, 9e9, 1])))
        missing = dict(kp)
        missing.pop("l_ankle")
        self.assertIsNone(clean_kp(missing))
        self.assertIsNone(clean_kp([1, 2, 3]))


class LongPollTest(unittest.TestCase):
    """长轮询：对方一发，挂着的一方立刻返回，不用等下一轮轮询。"""

    def later(self, delay, fn):
        t = threading.Timer(delay, fn)
        t.start()
        self.addCleanup(t.cancel)

    def test_waiting_monitor_gets_offer_as_soon_as_phone_posts(self):
        import time
        link = CamLink(token="k")
        self.later(0.2, lambda: link.post_offer("v=0 offer " * 5))
        t0 = time.monotonic()
        got = link.take_offer(wait=5)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(got["sdp"], "v=0 offer " * 5)
        self.assertIsNone(link.take_offer(wait=0.1))   # 已被领走

    def test_waiting_phone_gets_answer_and_replaced_call_ends_wait(self):
        import time
        link = CamLink(token="k")
        sid = link.post_offer("v=0 offer " * 5)
        self.later(0.2, lambda: link.post_answer(sid, "v=0 answer " * 5))
        t0 = time.monotonic()
        self.assertEqual(link.get_answer(sid, wait=5), "v=0 answer " * 5)
        self.assertLess(time.monotonic() - t0, 2.0)
        old = link.post_offer("v=0 offer " * 5)
        self.later(0.2, lambda: link.post_offer("v=0 newer " * 5))
        with self.assertRaises(KeyError):
            link.get_answer(old, wait=5)

    def test_wait_without_anything_times_out_quietly(self):
        link = CamLink(token="k")
        self.assertIsNone(link.take_offer(wait=0.2))


if __name__ == "__main__":
    unittest.main()
