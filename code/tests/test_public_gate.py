"""公网门（Cloudflare 隧道进来的请求）、手板拉家属回话、摔倒现场照片。"""
import http.client
import io
import json
import threading
import unittest
import wave

from helpers import make_bus

import master_service

TUNNEL = {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Visitor": '{"scheme":"https"}', "Host": "demo.trycloudflare.com"}


def wav_bytes(secs):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * secs))
    return buf.getvalue()


class GateBase(unittest.TestCase):
    TOKEN = "k-123"

    def setUp(self):
        self.bus, self.clock, self.arm = make_bus(public_token=self.TOKEN)
        self.server = master_service.serve(self.bus, "127.0.0.1", 0)
        self.server.stop_ticker.set()   # 测试里不要后台 tick，时间全由假时钟控制
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, public=False, key=None, ctype="application/json"):
        headers = dict(TUNNEL) if public else {}
        if key:
            headers["X-CareMate-Token"] = key
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = ctype
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        ct = r.getheader("Content-Type", "")
        return r.status, (json.loads(raw.decode("utf-8")) if "json" in ct else raw), ct, r


class PublicGateTest(GateBase):
    def test_lan_is_unchanged(self):
        self.assertEqual(self.req("GET", "/ui/console")[0], 200)
        self.assertEqual(self.req("GET", "/api/poll?since=0")[0], 200)
        self.assertEqual(self.req("POST", "/api/estop", {"on": False})[0], 200)

    def test_public_family_page_open_but_data_needs_key(self):
        self.assertEqual(self.req("GET", "/ui/family/", public=True)[0], 200)
        self.assertEqual(self.req("GET", "/ui/family/manifest.webmanifest", public=True)[0], 200)
        self.assertEqual(self.req("GET", "/ui/shared/bus.js", public=True)[0], 200)
        self.assertEqual(self.req("GET", "/api/poll?since=0", public=True)[0], 401)
        self.assertEqual(self.req("GET", "/api/poll?since=0", public=True, key="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/api/poll?since=0", public=True, key=self.TOKEN)[0], 200)
        self.assertEqual(self.req("GET", "/api/poll?since=0&key=" + self.TOKEN, public=True)[0], 200)

    def test_public_cannot_touch_robot_or_operator(self):
        for path in ("/ui/console", "/ui/monitor", "/api/nodes", "/api/params"):
            self.assertEqual(self.req("GET", path, public=True, key=self.TOKEN)[0], 403, path)
        for path, body in (("/mock/voice", {"text": "把药递给我"}), ("/mock/fall", {}), ("/mock/button", {"key": "ok"}),
                           ("/api/estop", {"on": True}), ("/api/reset", {}), ("/api/params", {"ask_wait_day": 9}),
                           ("/api/fall_photo", {})):
            self.assertEqual(self.req("POST", path, body, public=True, key=self.TOKEN)[0], 403, path)
        self.assertNotIn("deliver_medicine", self.arm.poses())
        self.assertFalse(self.bus.estop)

    def test_public_cannot_act_as_elder(self):
        self.bus.saw_client("elder")
        self.bus.saw_client("family:daughter")
        self.assertEqual(self.req("POST", "/api/call/start", {"from": "elder", "to": "daughter"}, public=True, key=self.TOKEN)[0], 403)
        self.assertEqual(self.req("POST", "/api/dial120", {}, public=True, key=self.TOKEN)[0], 403)
        self.assertEqual(self.req("POST", "/api/dial120", {"by": "elder"}, public=True, key=self.TOKEN)[0], 403)
        st, r, _, _ = self.req("POST", "/api/dial120", {"by": "daughter"}, public=True, key=self.TOKEN)
        self.assertEqual((st, r["ok"]), (200, True))
        st, r, _, _ = self.req("POST", "/api/call/start", {"from": "daughter", "to": "elder"}, public=True, key=self.TOKEN)
        self.assertEqual((st, r["ok"]), (200, True))

    def test_public_root_goes_to_family(self):
        st, _, _, r = self.req("GET", "/", public=True)
        self.assertEqual((st, r.getheader("Location")), (302, "/ui/family"))
        st, _, _, r = self.req("GET", "/")
        self.assertEqual((st, r.getheader("Location")), (302, "/ui/console"))


class NoTokenTest(GateBase):
    TOKEN = ""

    def test_public_closed_without_token(self):
        st, r, _, _ = self.req("GET", "/api/poll?since=0", public=True, key="anything")
        self.assertEqual(st, 403)
        self.assertIn("public_token", r["error"])
        self.assertEqual(self.req("GET", "/api/poll?since=0")[0], 200)


class BoardPullTest(GateBase):
    def test_pull_family_voice_replies_in_order(self):
        a = self.bus.voice_upload("daughter", "elder", "audio/wav", wav_bytes(1.0), None)
        self.bus.voice_upload("elder", "son", "audio/wav", wav_bytes(1.0), None)   # 老人发出去的不算回话
        b = self.bus.voice_upload("son", "elder", "audio/wav", wav_bytes(2.0), None)
        self.bus.family_message("daughter", "文字不在 replies 里")
        st, r, _, _ = self.req("GET", "/api/family_replies?elder=hand-A&after=0", public=True, key=self.TOKEN)
        self.assertEqual(st, 200)
        self.assertEqual([x["id"] for x in r["replies"]], [str(a["voice_id"]), str(b["voice_id"])])
        first = r["replies"][0]
        self.assertEqual((first["from"], first["duration"]), ("女儿", 1.0))
        self.assertEqual(first["wav_url"], "https://demo.trycloudflare.com/api/voice_msg?id=%d&key=%s" % (a["voice_id"], self.TOKEN))
        st, r2, _, _ = self.req("GET", "/api/family_replies?after=%s" % b["voice_id"], public=True, key=self.TOKEN)
        self.assertEqual(r2["replies"], [])
        # wav_url 自带钥匙，不加请求头也能取
        st, data, ct, _ = self.req("GET", first["wav_url"].replace("https://demo.trycloudflare.com", ""), public=True)
        self.assertEqual((st, ct), (200, "audio/wav"))
        self.assertEqual(data, wav_bytes(1.0))
        # 拉过 = 床头盒在线，消息标成已送到床头
        self.assertTrue(self.bus.comms.snapshot()["online"]["box"])
        self.assertTrue(next(m for m in self.bus.messages if m.get("voice_id") == a["voice_id"])["delivered_box"])

    def test_pull_on_lan_has_no_key_and_bad_after_is_error(self):
        a = self.bus.voice_upload("daughter", "elder", "audio/wav", wav_bytes(1.0), None)
        st, r, _, _ = self.req("GET", "/api/family_replies?after=")
        self.assertTrue(r["replies"][0]["wav_url"].endswith("/api/voice_msg?id=%d" % a["voice_id"]))
        st, r, _, _ = self.req("GET", "/api/family_replies?after=abc")
        self.assertFalse(r["ok"])

    def test_board_multipart_push_through_tunnel(self):
        boundary = "XyZ123"
        body = b"".join([
            b"--XyZ123\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"msg_20261024_1930.wav\"\r\nContent-Type: audio/wav\r\n\r\n",
            wav_bytes(2.0), b"\r\n",
            b"--XyZ123\r\nContent-Disposition: form-data; name=\"duration\"\r\n\r\n2.0\r\n",
            b"--XyZ123\r\nContent-Disposition: form-data; name=\"elder\"\r\n\r\nhand-A\r\n--XyZ123--\r\n"])
        ctype = "multipart/form-data; boundary=" + boundary
        self.assertEqual(self.req("POST", "/api/voice_msg", body, public=True, ctype=ctype)[0], 401)
        st, r, _, _ = self.req("POST", "/api/voice_msg", body, public=True, key=self.TOKEN, ctype=ctype)
        self.assertEqual((st, r["ok"], r["to"]), (200, True, ["daughter", "son"]))
        self.assertEqual({m["via"] for m in self.bus.messages if m.get("kind") == "voice"}, {"hand-A"})


class FallPhotoTest(GateBase):
    JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 50 + b"\xff\xd9"

    def test_photo_shown_during_incident_and_deleted_after_ok(self):
        self.bus.fall("卧室")
        st, r, _, _ = self.req("POST", "/api/fall_photo?src=fall_bridge", self.JPEG, ctype="image/jpeg")
        self.assertEqual((st, r["ok"]), (200, True))
        snap = self.bus.snapshot()["fall_photo"]
        self.assertEqual((snap["id"], snap["src"]), (r["id"], "fall_bridge"))
        self.assertNotIn("data", snap)
        st, data, ct, _ = self.req("GET", "/api/fall_photo?id=%d" % r["id"], public=True, key=self.TOKEN)
        self.assertEqual((st, ct, data), (200, "image/jpeg", self.JPEG))
        self.assertEqual(self.req("GET", "/api/fall_photo?id=%d" % r["id"], public=True)[0], 401)
        self.bus.button("ok")
        self.assertIsNone(self.bus.snapshot()["fall_photo"])
        self.assertEqual(self.req("GET", "/api/fall_photo?id=%d" % r["id"])[0], 404)

    def test_photo_expires_when_idle_and_bad_type_rejected(self):
        r = self.bus.fall_photo_upload(self.JPEG, "image/jpeg", "test")
        self.assertTrue(r["ok"])
        self.clock.advance(31, self.bus)   # 没进保命链（空闲）30 秒后删
        self.assertIsNone(self.bus.snapshot()["fall_photo"])
        self.assertFalse(self.bus.fall_photo_upload(b"hello", "text/plain")["ok"])
        self.assertFalse(self.bus.fall_photo_upload(b"", "image/jpeg")["ok"])


class VisionPhotoHookTest(unittest.TestCase):
    """视觉桥：跌倒事件送达后补发那一帧；照片发不出去不影响报警。"""

    def _poster(self, raw_fails=False):
        from vision.fall_bridge import BusPoster
        calls, raws = [], []

        def post(url, payload=None):
            calls.append(url)
            return {"ok": True, "state": "ASKING"}

        def post_raw(url, data, ctype):
            if raw_fails:
                raise OSError("photo endpoint down")
            raws.append((url, data, ctype))
            return {"ok": True}
        return BusPoster("http://bus", post=post, post_raw=post_raw, log=lambda *_: None), calls, raws

    def test_photo_follows_fall_once(self):
        poster, calls, raws = self._poster()
        poster.fall("卧室", b"\xff\xd8jpeg")
        poster.drain()
        self.assertTrue(any("/mock/fall" in u for u in calls))
        self.assertEqual(raws, [("http://bus/api/fall_photo?src=fall_bridge", b"\xff\xd8jpeg", "image/jpeg")])
        poster.fall("卧室")   # 下一次没带照片就不发
        poster.drain()
        self.assertEqual(len(raws), 1)

    def test_photo_failure_does_not_block_alarm(self):
        poster, calls, raws = self._poster(raw_fails=True)
        poster.fall("客厅", b"\xff\xd8jpeg")
        poster.drain()
        self.assertEqual(sum("/mock/fall" in u for u in calls), 1)

    def test_run_only_passes_photo_to_posters_that_accept_it(self):
        from gesture.fall_rules import FallDetector
        from vision import synth
        from vision.fall_bridge import keypoint_source, run

        class OldPoster:   # phone_input / rehearsal 的 poster：fall 只收 room
            def __init__(self):
                self.rooms = []

            def skeleton(self, kp, fallen):
                pass

            def fall(self, room):
                self.rooms.append(room)
        p = OldPoster()
        run(keypoint_source(synth.build("fall"), 4 / 3, realtime=False), FallDetector(), p, "卧室", log=lambda *_: None)
        self.assertEqual(p.rooms, ["卧室"])


if __name__ == "__main__":
    unittest.main()
