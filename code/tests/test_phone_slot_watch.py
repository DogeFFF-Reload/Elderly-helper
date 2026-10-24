"""iPhone 取物架格位确认：框格、空架参考、有 / 空判定（抗亮度变化、抗一闪）、回传主脑。"""
import io
import json
import os
import random
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import make_bus  # noqa: F401  （把 code/ 放进 sys.path）

from camera import phone_slot_watch as W

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None

ROIS = {"tray": [0.05, 0.4, 0.2, 0.3], "1": [0.3, 0.4, 0.2, 0.3], "2": [0.55, 0.4, 0.2, 0.3]}


def shelf(items=(), brightness=0, seed=1):
    """一张 320×240 的"架子"：木纹似的随机条纹；items 里的格位放一个深色方块。"""
    rnd = random.Random(seed)
    img = Image.new("L", (320, 240))
    d = ImageDraw.Draw(img)
    for x in range(0, 320, 4):
        d.rectangle([x, 0, x + 3, 240], fill=110 + rnd.randint(-15, 15))
    for name in items:
        x, y, w, h = ROIS[name]
        d.rectangle([(x + w * 0.2) * 320, (y + h * 0.2) * 240, (x + w * 0.8) * 320, (y + h * 0.9) * 240], fill=30)
    if brightness:
        img = img.point(lambda p: max(0, min(255, p + brightness)))
    return img


def jpeg(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


@unittest.skipIf(Image is None, "没装 Pillow")
class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.rois = {k: tuple(v) for k, v in ROIS.items()}
        empty = shelf()
        self.refs = {k: W.crop_cell(empty, b) for k, b in self.rois.items()}

    def cells(self, img):
        return {k: W.crop_cell(W.decode(jpeg(img)), b) for k, b in self.rois.items()}

    def test_occupied_vs_empty(self):
        j = W.SlotJudge(self.refs, thresh=14)
        state, scores = j.update(self.cells(shelf(items=("1",))))
        self.assertEqual(state, {"tray": False, "1": True, "2": False})
        self.assertGreater(scores["1"], 14 * 2)

    def test_lighting_change_is_not_an_item(self):
        j = W.SlotJudge(self.refs, thresh=14)
        state, _ = j.update(self.cells(shelf(brightness=40)))
        self.assertEqual(set(state.values()), {False})

    def test_needs_consecutive_frames_to_change(self):
        j = W.SlotJudge(self.refs, thresh=14, conn=3)
        j.update(self.cells(shelf(items=("2",))))
        for _ in range(2):
            self.assertTrue(j.update(self.cells(shelf()))[0]["2"], "拿走一下（手挡了一闪）先不改口")
        self.assertFalse(j.update(self.cells(shelf()))[0]["2"])

    def test_bad_roi_rejected(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for bad in ({"1": [0.5, 0.5, 0.6, 0.2]}, {"1": [0.1, 0.1]}, {"_说明": "x"}):
            path = os.path.join(tmp, "slots.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bad, f)
            with self.assertRaises(ValueError):
                W.load_rois(path)


class _Phone(BaseHTTPRequestHandler):
    frame = b""
    posts = []

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(self.frame)))
        self.end_headers()
        self.wfile.write(self.frame)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).posts.append((self.path, json.loads(body)))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@unittest.skipIf(Image is None, "没装 Pillow")
class EndToEndTest(unittest.TestCase):
    """假 iPhone（单帧 JPEG）+ 假主脑在同一个本地 HTTP 服务上：存网格图 → 拍空架 → 看 → 回传。"""

    def setUp(self):
        _Phone.posts = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Phone)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = tempfile.mkdtemp()
        self.rois = os.path.join(self.tmp, "slots.json")
        with open(self.rois, "w", encoding="utf-8") as f:
            json.dump(ROIS, f)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp)

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = W.main(["--shot-url", self.url + "/shot.jpg", "--interval", "0"] + list(args))
        return code, out.getvalue()

    def test_three_steps(self):
        _Phone.frame = jpeg(shelf())
        grid = os.path.join(self.tmp, "grid.jpg")
        self.assertEqual(self.run_main("--rois", self.rois, "--snapshot", grid)[0], 0)
        self.assertTrue(os.path.exists(grid))
        ref = os.path.join(self.tmp, "ref")
        self.assertEqual(self.run_main("--rois", self.rois, "--capture-empty", ref)[0], 0)
        self.assertEqual(sorted(os.listdir(ref)), ["slot_1.png", "slot_2.png", "slot_tray.png"])

        _Phone.frame = jpeg(shelf(items=("tray", "2")))
        code, out = self.run_main("--rois", self.rois, "--ref", ref, "--master", self.url, "--arm", "1", "--frames", "2")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(_Phone.posts), 2)
        path, body = _Phone.posts[-1]
        self.assertEqual(path, "/api/fetch/slots")
        self.assertEqual(body["slots"], {"tray": True, "1": False, "2": True})
        self.assertEqual(body["src"], "phone_slots")

    def test_observe_mode_never_posts(self):
        _Phone.frame = jpeg(shelf())
        ref = os.path.join(self.tmp, "ref")
        self.run_main("--rois", self.rois, "--capture-empty", ref)
        code, out = self.run_main("--rois", self.rois, "--ref", ref, "--master", self.url, "--frames", "1")
        self.assertEqual((code, _Phone.posts), (0, []))
        self.assertIn("[SLOTS]", out)

    def test_missing_reference_is_a_clear_error(self):
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            code, _ = self.run_main("--rois", self.rois)
        self.assertEqual(code, 2)
        self.assertIn("--capture-empty", err.getvalue())

    def test_bus_accepts_what_the_watcher_posts(self):
        bus, clock, arm = make_bus()
        bus.fetch_slots({"tray": True, "1": False, "2": True})
        bus.voice("把水杯递给我")
        self.assertEqual(arm.sent, [])


if __name__ == "__main__":
    unittest.main()
