"""X5 床头盒中继（elder_link/relay.py）：老人留言先存盘再转发、家属留言下行播放、HTTP 接口。

主脑用 master_service.serve 起真服务（假时钟），中继直接调 Relay 的方法；
播放器换成记录路径的假函数，并把 ANNOUNCE_WAIT 临时改成 0，测试不 sleep。
"""
import http.client
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from helpers import make_bus

import master_service
from elder_link import relay as relay_module


def pcm_bytes(seconds):
    """16 kHz / 16 bit / 单声道裸 PCM（静音）。"""
    return b"\x00" * int(relay_module.RATE * relay_module.WIDTH * seconds)


def start_master(bus):
    server = master_service.serve(bus, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    return server, server.server_address[1]


def stop_master(server):
    server.stop_ticker.set()
    server.shutdown()
    server.server_close()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def hold_clock(bus, clock, clients, seconds, step=1.0):
    """推进假时钟；每秒给 clients 里的页面续命（床头盒要一直在线）。"""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t = min(end, clock.t + step)
        for name in clients:
            bus.saw_client(name)
        bus.tick()


def wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class RelayBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._announce_wait = relay_module.ANNOUNCE_WAIT
        relay_module.ANNOUNCE_WAIT = 0.0

    @classmethod
    def tearDownClass(cls):
        relay_module.ANNOUNCE_WAIT = cls._announce_wait

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus, self.clock, self.arm = make_bus()
        self.server, self.port = start_master(self.bus)
        self.addCleanup(stop_master, self.server)
        self.master = "http://127.0.0.1:%d" % self.port
        self.played = []
        self.relay = relay_module.Relay(self.tmp.name, self.master, player=self.player)

    def player(self, path):
        self.played.append(str(path))
        return True

    def new_relay(self):
        return relay_module.Relay(self.tmp.name, self.master, player=self.player)

    def voice_messages(self, sender=None, **match):
        return [m for m in self.bus.messages if m.get("kind") == "voice"
                and (sender is None or m.get("from") == sender)
                and all(m.get(k) == v for k, v in match.items())]


class UploadForwardTest(RelayBase):
    def test_a_raw_pcm_is_saved_and_forwarded(self):
        item = self.relay.add(pcm_bytes(3), "application/octet-stream")
        self.assertEqual(item["duration"], 3.0)
        self.assertEqual(item["to"], "all")
        path = Path(self.tmp.name) / (item["id"] + ".wav")
        self.assertTrue(path.is_file())
        self.assertAlmostEqual(relay_module.wav_info(path.read_bytes()), 3.0, places=3)

        self.assertEqual(self.relay.forward_once(), 1)
        msgs = self.voice_messages(sender="elder")
        self.assertEqual(len(msgs), 2)
        self.assertEqual({m["to"] for m in msgs}, {"daughter", "son"})
        self.assertEqual({m["via"] for m in msgs}, {"hand-A"})
        self.assertEqual({m["voice_id"] for m in msgs}, {msgs[0]["voice_id"]})

        index = json.loads((Path(self.tmp.name) / "index.json").read_text(encoding="utf-8"))
        self.assertTrue(index[-1]["forwarded"])
        self.assertEqual(index[-1]["voice_id"], msgs[0]["voice_id"])
        self.assertEqual(self.relay.forward_once(), 0)

    def test_b_retries_after_master_comes_back(self):
        self.relay.master = "http://127.0.0.1:%d" % free_port()
        self.relay.add(pcm_bytes(1), "application/octet-stream")

        self.assertEqual(self.relay.forward_once(), 0)
        status = self.relay.status()
        self.assertEqual(status["pending"], 1)
        self.assertFalse(status["master"])

        self.relay.master = self.master
        self.relay.retry_at = 0
        self.assertEqual(self.relay.forward_once(), 1)
        self.assertEqual(self.relay.status()["pending"], 0)
        self.assertEqual(len(self.voice_messages(sender="elder")), 2)

    def test_c_invalid_audio_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.relay.add(b"", "application/octet-stream")
        with self.assertRaises(ValueError):
            self.relay.add(b"\x00" * (relay_module.MAX_BYTES + 1), "application/octet-stream")
        with self.assertRaises(ValueError):
            self.relay.add(pcm_bytes(1), "audio/wav")
        with self.assertRaises(ValueError):
            self.relay.add(pcm_bytes(0.2), "application/octet-stream")


class DownlinkTest(RelayBase):
    def test_d_box_plays_family_voice_and_marks_heard(self):
        self.assertIsNone(self.relay.poll_once())   # 报到：带 client=elder-box

        # 用合法 WAV，中继存下来的文件名才是 .wav
        wav = relay_module.pcm_to_wav(pcm_bytes(1.5))
        up = self.bus.voice_upload("daughter", "elder", "audio/wav", wav, None)
        self.assertTrue(up["ok"])
        self.assertEqual(self.bus.snapshot()["comms"]["play"]["target"], "box")

        vid = self.relay.poll_once()
        self.assertEqual(vid, up["voice_id"])
        self.assertTrue(wait_until(lambda: bool(self.played)))
        self.assertTrue(self.played[0].endswith(".wav"))

        def heard():
            msgs = self.voice_messages(voice_id=up["voice_id"])
            return bool(msgs) and msgs[0]["heard"]

        self.assertTrue(wait_until(heard))

        before = len(self.played)
        self.bus.saw_client("elder")
        up2 = self.bus.voice_upload("daughter", "elder", "audio/wav", wav, None)
        self.assertTrue(up2["ok"])
        self.assertEqual(self.bus.snapshot()["comms"]["play"]["target"], "page")
        self.assertIsNone(self.relay.poll_once())
        self.assertEqual(len(self.played), before)

    def test_e_restart_does_not_play_an_old_request(self):
        self.bus.saw_client("elder-box")
        up = self.bus.voice_upload("daughter", "elder", "audio/wav",
                                   relay_module.pcm_to_wav(pcm_bytes(1.5)), None)
        self.assertTrue(up["ok"])
        self.assertEqual(self.bus.snapshot()["comms"]["play"]["target"], "box")

        hold_clock(self.bus, self.clock, ("elder-box",), 61)
        fresh = self.new_relay()
        self.assertIsNone(fresh.play_seq)
        self.assertIsNone(fresh.poll_once())
        self.assertEqual(self.played, [])


class RelayHttpTest(RelayBase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), relay_module.make_handler(self.relay))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.http_port = self.httpd.server_address[1]

    def req(self, method, path, body=None, ctype=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        h = dict(headers or {})
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data, status = resp.read(), resp.status
        rh = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return status, data, rh

    def test_f_http_surface(self):
        status, data, _ = self.req("POST", "/messages", pcm_bytes(1.0),
                                   ctype="application/octet-stream")
        self.assertEqual(status, 200)
        first = json.loads(data.decode("utf-8"))
        self.assertTrue(first["ok"])
        self.assertTrue(first["id"])
        self.assertEqual(first["duration"], 1.0)

        status, data, _ = self.req("POST", "/messages", pcm_bytes(2.0),
                                   ctype="application/octet-stream")
        second = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(second["duration"], 2.0)

        status, data, _ = self.req("GET", "/messages")
        self.assertEqual(status, 200)
        listing = json.loads(data.decode("utf-8"))["messages"]
        self.assertEqual(listing[0]["id"], second["id"])
        self.assertEqual([i["id"] for i in listing].index(first["id"]), 1)

        status, data, rh = self.req("GET", "/messages/%s.wav" % first["id"])
        self.assertEqual(status, 200)
        self.assertEqual(rh.get("content-type"), "audio/wav")
        stored = (Path(self.tmp.name) / (first["id"] + ".wav")).read_bytes()
        self.assertEqual(data, stored)

        self.assertEqual(self.req("GET", "/messages/../x.wav")[0], 404)
        self.assertEqual(self.req("GET", "/messages/..%2Fx.wav")[0], 404)

        status, data, rh = self.req("GET", "/messages/inbox")
        self.assertEqual(status, 200)
        self.assertTrue(rh.get("content-type", "").startswith("text/html"))
        self.assertIn("老人留言", data.decode("utf-8"))

        status, data, _ = self.req("GET", "/status")
        self.assertEqual(status, 200)
        status_body = json.loads(data.decode("utf-8"))
        self.assertTrue({"ok", "box", "recording", "pending", "master", "unheard"} <= set(status_body))
        self.assertEqual(status_body["box"], "hand-A")

    def test_f_record_start_without_arecord_is_503(self):
        if shutil.which("arecord"):
            self.skipTest("本机装了 arecord，/record/start 会真的开始录音")
        status, data, _ = self.req("POST", "/record/start")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(data.decode("utf-8"))["ok"])

    def test_f_dial120_and_play(self):
        status, data, _ = self.req("POST", "/dial120")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data.decode("utf-8"))["ok"])
        notes = [n for n in self.bus.notifications if n["kind"] == "120"]
        self.assertEqual(len(notes), 2)
        self.assertEqual({n["contact"] for n in notes}, {"daughter", "son"})

        status, data, _ = self.req("POST", "/play")
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertFalse(body["ok"])
        self.assertIn("没有家属留言", body["error"])


if __name__ == "__main__":
    unittest.main()
