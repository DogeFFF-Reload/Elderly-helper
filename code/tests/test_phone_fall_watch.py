"""手机摄像头保底哨兵：图像能量和唯一主脑上报入口。"""
import io
import itertools
import base64
import contextlib
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from camera import phone_fall_watch as watch
from camera.phone_source import PhoneFrameSource, iter_mjpeg


class _CaptureHandler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self.__class__.calls.append((self.path, body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):
        pass


class PhoneWatchTest(unittest.TestCase):
    def test_static_frame_has_no_motion(self):
        frame = Image.new("L", (watch.W, watch.H), 80)
        energy, cy, rows = watch.energy_profile(frame, frame.copy())
        self.assertEqual(energy, 0.0)
        self.assertAlmostEqual(cy, 0.5, places=2)
        self.assertEqual(len(rows), watch.H // 4)

    def test_motion_energy_and_bottom_centroid(self):
        before = Image.new("L", (watch.W, watch.H), 0)
        after = before.copy()
        for y in range(20, 32):
            for x in range(10, 38):
                after.putpixel((x, y), 255)
        energy, cy, _ = watch.energy_profile(before, after)
        self.assertGreater(energy, 1)
        self.assertGreater(cy, 0.65)

    def test_phone_frame_conversion(self):
        src = Image.new("RGB", (96, 64), (20, 40, 60))
        buf = io.BytesIO()
        src.save(buf, format="JPEG")
        small = watch.to_small_gray(buf.getvalue())
        self.assertEqual(small.size, (watch.W, watch.H))
        self.assertEqual(small.mode, "L")

    def test_post_fall_only_hits_master_mock_route(self):
        _CaptureHandler.calls = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            self.assertEqual(watch.post_fall(base, room="客厅", src="phone_watch"), 200)
            self.assertEqual(len(_CaptureHandler.calls), 1)
            path, body = _CaptureHandler.calls[0]
            parsed = urlparse(path)
            self.assertEqual(parsed.path, "/mock/fall")
            self.assertEqual(parse_qs(parsed.query), {"room": ["客厅"], "src": ["phone_watch"]})
            self.assertEqual(json.loads(body), {})
        finally:
            server.shutdown()
            server.server_close()


def _jpeg(level):
    buf = io.BytesIO()
    Image.new("RGB", (96, 64), (level, level, level)).save(buf, format="JPEG")
    return buf.getvalue()


class _PhoneHandler(BaseHTTPRequestHandler):
    """Local stand-in for the authenticated iPhone server; no devices used."""
    requests = []
    pictures = [_jpeg(level) for level in (0, 240, 0, 240, 0, 240)]
    authorization = "Basic " + base64.b64encode(b"admin:admin").decode("ascii")

    def do_GET(self):  # noqa: N802
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != self.authorization:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="IP Camera for iOS"')
            self.end_headers()
            return
        self.send_response(200)
        if self.path == "/shot.jpg":
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(self.pictures[0])
        elif self.path == "/html":
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>Not a video</html>")
        else:
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--BoundaryString")
            self.end_headers()
            try:
                for picture in self.pictures:
                    self.wfile.write(b"--BoundaryString\r\nContent-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(picture + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def log_message(self, *_args):
        pass


class _OneByteReader(io.BytesIO):
    def read1(self, _size):
        return self.read(1)


class IPhoneStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PhoneHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        _PhoneHandler.requests = []

    def camera(self, suffix="/", **kwargs):
        return PhoneFrameSource(self.base + suffix, username="admin", password="admin", **kwargs)

    def test_authenticated_stream_root_without_shot_path(self):
        with contextlib.closing(self.camera(mjpeg=True).frames()) as frames:
            dark = watch.to_small_gray(next(frames))
            light = watch.to_small_gray(next(frames))
            self.assertEqual(dark.getpixel((0, 0)), 0)
            self.assertGreater(light.getpixel((0, 0)), 230)
        self.assertEqual(_PhoneHandler.requests[-1], ("/", _PhoneHandler.authorization))

    def test_android_snapshot_still_works(self):
        with contextlib.closing(self.camera("/shot.jpg").frames()) as frames:
            picture = watch.to_small_gray(next(frames))
        self.assertEqual(picture.getpixel((0, 0)), 0)
        self.assertEqual(_PhoneHandler.requests[-1][0], "/shot.jpg")

    def test_camera_ignores_broken_proxy_configuration(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            with contextlib.closing(self.camera(mjpeg=True).frames()) as frames:
                self.assertEqual(watch.to_small_gray(next(frames)).size, (watch.W, watch.H))

    def test_split_markers_and_headers_without_content_length(self):
        a, b = _PhoneHandler.pictures[:2]
        raw = b"--BoundaryString\r\nContent-Type: image/jpeg\r\n\r\n" + a + b"\r\n--BoundaryString\r\n\r\n" + b
        frames = iter_mjpeg(_OneByteReader(raw))
        self.assertEqual(next(frames), a)
        self.assertEqual(next(frames), b)
        with self.assertRaises(ConnectionError):
            next(frames)

    def test_multiple_frames_in_one_chunk(self):
        a, b = _PhoneHandler.pictures[:2]
        frames = iter_mjpeg(io.BytesIO(a + b))
        self.assertEqual(next(frames), a)
        self.assertEqual(next(frames), b)

    def test_truncated_stream_is_reported(self):
        with self.assertRaises(ConnectionError):
            next(iter_mjpeg(io.BytesIO(b"header\r\n\xff\xd8unfinished")))

    def test_oversized_frame_is_rejected(self):
        with patch("camera.phone_source.MAX_FRAME_BYTES", 16):
            with self.assertRaises(ValueError):
                next(iter_mjpeg(io.BytesIO(b"\xff\xd8" + b"x" * 32)))

    def test_html_page_is_not_treated_as_video(self):
        with contextlib.closing(self.camera("/html", mjpeg=True).frames()) as frames:
            with self.assertRaisesRegex(ValueError, "text/html"):
                next(frames)

    def test_observe_mode_never_posts_even_when_motion_triggers(self):
        argv = ["watch", "--stream-url", self.base + "/", "--username", "admin", "--password", "admin",
                "--frames", "6", "--interval", "0.000001", "--arm", "0",
                "--conn", "1", "--cooldown", "0", "--thresh", "0"]
        output = io.StringIO()
        # Windows may deliver this entire buffered stream in one clock tick.
        # Make sampling deterministic so every deliberately alternating frame counts.
        with patch.object(watch.time, "monotonic", side_effect=itertools.count(0, .01).__next__), \
             patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), patch.object(watch, "post_fall") as post:
            self.assertEqual(watch.main(), 0)
        post.assert_not_called()
        self.assertIn("[FALL 试机]", output.getvalue())
        self.assertIn("已观察 6 帧", output.getvalue())

    def test_wrong_password_exits_without_printing_password(self):
        argv = ["watch", "--stream-url", self.base + "/", "--username", "admin", "--password", "wrong_secret", "--arm", "0"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(watch.main(), 1)
        self.assertIn("登录失败", output.getvalue())
        self.assertNotIn("wrong_secret", output.getvalue())

    def test_url_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            PhoneFrameSource("http://admin:secret@127.0.0.1:8081/", mjpeg=True)


if __name__ == "__main__":
    unittest.main()
