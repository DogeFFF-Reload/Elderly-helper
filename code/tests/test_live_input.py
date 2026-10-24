"""Bounded capture and cleanup using in-memory cameras; no device is opened."""
import unittest
from unittest.mock import patch

from vision.live_input import LiveInput


class LiveInputTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.released = self.estimator_closed = False

        class Frame:
            shape = (480, 640, 3)

        owner = self

        class Capture:
            def isOpened(self):
                return True

            def read(self):
                owner.now += .05
                return True, Frame()

            def release(self):
                owner.released = True

        class Estimator:
            def __call__(self, frame):
                return {"head": [.5, .2, .99]}

            def close(self):
                owner.estimator_closed = True

        self.capture = Capture()
        self.live = LiveInput(camera=0, duration=.25, fps=10,
                              estimator_factory=lambda **kw: Estimator(),
                              capture_factory=lambda n: self.capture)

    def advance(self, seconds):
        self.now += seconds

    def test_pc_source_is_bounded_uses_real_timestamps_and_releases_resources(self):
        with patch("vision.live_input.time.monotonic", side_effect=lambda: self.now), \
                patch("vision.live_input.time.sleep", side_effect=self.advance):
            with self.live:
                frames = list(self.live)
        self.assertEqual(len(frames), 2)
        self.assertAlmostEqual(frames[0][0], .05)
        self.assertAlmostEqual(frames[1][0], .20)
        self.assertEqual(frames[0][3], 4/3)
        self.assertTrue(self.released)
        self.assertTrue(self.estimator_closed)

    def test_failed_open_and_read_both_release_capture_and_estimator(self):
        for opened in (False, True):
            with self.subTest(opened=opened):
                self.released = self.estimator_closed = False
                self.capture.isOpened = lambda: opened
                self.capture.read = lambda: (False, None)
                with self.assertRaises(RuntimeError):
                    with self.live:
                        list(self.live)
                self.assertTrue(self.released)
                self.assertTrue(self.estimator_closed)

    def test_validation_and_import_do_not_load_camera_or_pose_model(self):
        for options in ({}, {"camera": 0, "phone_stream": "http://invalid/"},
                        {"camera": 0, "duration": float("inf")}, {"camera": 0, "fps": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                LiveInput(**options)


if __name__ == "__main__":
    unittest.main()
