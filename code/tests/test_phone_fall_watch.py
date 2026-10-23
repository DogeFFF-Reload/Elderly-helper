"""手机摄像头保底哨兵：图像能量和唯一主脑上报入口。"""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

from camera import phone_fall_watch as watch


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


if __name__ == "__main__":
    unittest.main()
