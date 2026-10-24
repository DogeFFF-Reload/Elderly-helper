"""Phone-to-pose adapter: loopback synthetic JPEGs and fake pose estimates only."""
import argparse
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from camera.phone_source import PhoneFrameSource
from gesture.fall_rules import STANDING, FallDetector
from test_phone_calibrate import PacedCamera
from vision import fall_bridge, phone_input, synth
from vision.kpio import read_keypoints


class FakeEstimator:
    def __init__(self, complexity=0, missing=False):
        self.missing = missing
        self.closed = False
        self.frames = []

    def __call__(self, frame):
        self.frames.append(frame)
        return None if self.missing else {k: [*v, .99] for k, v in STANDING.items()}

    def close(self):
        self.closed = True


@unittest.skipUnless(importlib.util.find_spec("cv2") and importlib.util.find_spec("numpy"), "Optional vision image dependencies")
class PhonePoseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PacedCamera)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        PacedCamera.requests = []
        PacedCamera.disconnect_after = None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "pose.jsonl"
        self.args = argparse.Namespace(phone_stream=self.base, phone_username="collector_user",
                                       phone_password="collector_secret", record_keypoints=str(self.path),
                                       duration=2, fps_cap=20, frames=3, preview=False, rules=None,
                                       set=None, bed=None, complexity=0, room="test")

    def observe(self, estimator=None):
        estimator = estimator or FakeEstimator()
        with patch.object(fall_bridge, "_post", side_effect=AssertionError("No bus posts allowed")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("No master requests allowed")):
            result = phone_input.observe_phone(self.args, fall_bridge.run,
                                              estimator_factory=lambda **_kw: estimator, log=lambda *_args: None)
        summary = json.loads(self.path.with_suffix(".summary.json").read_text(encoding="utf-8"))
        return result, summary, estimator

    def test_authenticated_phone_decodes_to_bgr_with_real_aspect_and_monotonic_time(self):
        camera = PhoneFrameSource(self.base, mjpeg=True, username="collector_user", password="collector_secret")
        estimator = FakeEstimator()
        with patch("cv2.VideoCapture", side_effect=AssertionError("No physical camera access")):
            frames = list(phone_input.phone_source(camera, estimator, duration=2, max_frames=3, fps=20))
        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0][2].shape, (64, 96, 3))
        self.assertEqual(frames[0][3], 1.5)
        self.assertTrue(all(b[0] > a[0] for a, b in zip(frames, frames[1:])))
        self.assertTrue(all(method == "GET" and path == "/" for method, path in PacedCamera.requests))

    def test_observation_saves_replayable_coordinates_without_credentials_or_photos(self):
        result, summary, estimator = self.observe()
        self.assertEqual(result, 0)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["frames"], 3)
        self.assertEqual(summary["person_frames"], 3)
        self.assertEqual(summary["usable_pose_frames"], 3)
        meta, rows = read_keypoints(self.path)
        self.assertEqual(meta["source"], "phone:mjpeg")
        self.assertEqual(meta["aspect"], 1.5)
        self.assertEqual(len(rows), 3)
        self.assertTrue(estimator.closed)
        for path in Path(self.temp.name).iterdir():
            text = path.read_text(encoding="utf-8")
            for sensitive in (self.base, "collector_user", "collector_secret"):
                self.assertNotIn(sensitive, text)
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 2)

    def test_missing_people_is_not_reported_as_success(self):
        result, summary, _ = self.observe(FakeEstimator(missing=True))
        self.assertEqual(result, 1)
        self.assertEqual(summary["status"], "no_usable_pose")
        self.assertEqual(summary["person_frames"], 0)
        self.assertEqual(summary["local_suspected_count"], 0)
        _, rows = read_keypoints(self.path)
        self.assertTrue(all(kp is None for _, kp in rows))

    def test_disconnect_preserves_partial_coordinate_file(self):
        PacedCamera.disconnect_after = .03
        self.args.frames = 20
        result, summary, estimator = self.observe()
        self.assertEqual(result, 1)
        self.assertEqual(summary["status"], "error")
        self.assertGreater(summary["frames"], 0)
        self.assertLess(summary["frames"], 20)
        self.assertTrue(self.path.is_file())
        self.assertTrue(estimator.closed)

    def test_wrong_password_is_not_printed_or_saved(self):
        self.args.phone_password = "secret_wrong_password"
        result, summary, _ = self.observe()
        self.assertEqual(result, 1)
        self.assertIn("登录失败", summary["message"])
        self.assertNotIn("secret_wrong_password", json.dumps(summary))
        self.assertEqual(summary["frames"], 0)

    def test_synthetic_fall_rule_is_observed_locally_and_recorded(self):
        frames = ((t, kp, None, 4 / 3) for t, kp in synth.build("fall"))
        with patch.object(phone_input, "phone_source", return_value=frames):
            result, summary, _ = self.observe()
        self.assertEqual(result, 0)
        self.assertEqual(summary["local_suspected_count"], 1)
        self.assertEqual(len(summary["local_suspected_events"]), 1)
        self.assertEqual(PacedCamera.requests, [], "Synthetic skeleton test must not connect even to loopback")

    def test_preview_stop_closes_input_generator(self):
        closed = []
        def source():
            try:
                for n in range(10):
                    yield n * .1, STANDING, None, 4 / 3
            finally:
                closed.append(True)
        class Preview:
            def show(self, *_args):
                return False
        fall_bridge.run(source(), FallDetector(), phone_input.ObservationPoster(), "room", preview=Preview(), log=lambda *_: None)
        self.assertEqual(closed, [True])

    def test_phone_cli_observes_without_constructing_bus_poster(self):
        argv = ["bridge", "--phone-stream", self.base, "--phone-username", "collector_user", "--phone-password", "collector_secret"]
        with patch.object(sys, "argv", argv), patch.object(phone_input, "observe_phone", return_value=0) as observe, \
                patch.object(fall_bridge, "BusPoster") as bus:
            self.assertEqual(fall_bridge.main(), 0)
        observe.assert_called_once()
        bus.assert_not_called()

    def test_invalid_phone_limits_rejected_before_model_or_camera(self):
        for flags in (("--duration", "nan"), ("--duration", "0"), ("--fps-cap", "-1"), ("--frames", "-1"), ("--loop",)):
            with self.subTest(flags=flags), patch.object(sys, "argv", ["bridge", "--phone-stream", self.base, *flags]), \
                    patch.object(phone_input, "observe_phone") as observe, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                fall_bridge.main()
            observe.assert_not_called()

    def test_invalid_jpeg_stops_without_pose_estimation(self):
        with self.assertRaises(phone_input.CollectionError):
            phone_input.decode_frame(b"not an image")


if __name__ == "__main__":
    unittest.main()
