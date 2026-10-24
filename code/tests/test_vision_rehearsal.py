"""Replay uses real master HTTP on loopback; the actuator only records calls."""
import contextlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from gesture.fall_rules import FallDetector, STANDING
from vision import rehearsal, synth
from vision.fall_bridge import run
from vision.input_quality import InputQuality
from vision.phone_input import ObservationPoster
from vision.kpio import KeypointWriter, read_keypoints


class QualityTest(unittest.TestCase):
    def quality(self, frames):
        detector, poster = FallDetector(), ObservationPoster()
        quality = InputQuality(log=lambda *_: None)
        events = run(((t, kp, None, 4 / 3) for t, kp in frames), detector, poster, "test",
                     log=lambda *_: None, quality=quality)
        return quality, detector, events

    def test_no_person_and_partial_pose_are_distinct(self):
        partial = {k: [*v, .1] for k, v in STANDING.items()}
        q, _, _ = self.quality([(0, None), (.1, partial), (.2, STANDING)])
        self.assertEqual([s["state"] for s in q.transitions], ["no_person", "pose_unavailable", "observing"])
        self.assertEqual((q.frames, q.person_frames, q.usable_frames), (3, 2, 1))

    def test_candidate_not_confirmed_by_missing_video_time(self):
        _, frames, _, _ = next(c for c in rehearsal.builtin_cases() if c[0] == "input_gap")
        q, detector, events = self.quality(frames)
        self.assertEqual(events, [])
        self.assertEqual(q.gaps, 1)
        self.assertEqual(detector.state, FallDetector.WATCH)

    def test_already_reported_fall_is_not_rearmed_by_gap(self):
        frames = synth.build("fall")
        frames += [(t + 15, kp) for t, kp in synth.build("fall") if t > 3]
        q, _, events = self.quality(frames)
        self.assertEqual(len(events), 1)
        self.assertEqual(q.gaps, 1)

    def test_fresh_frames_without_person_keep_occlusion_policy(self):
        _, _, events = self.quality(synth.build("fall_occluded"))
        self.assertEqual([e["kind"] for e in events], ["rapid"])

    def test_invalid_timestamps_and_aspects_fail_explicitly(self):
        for times in ((0, 0), (1, .5), (0, float("nan")), (0, float("inf"))):
            with self.subTest(times=times), self.assertRaises(ValueError):
                self.quality([(t, STANDING) for t in times])
        q = InputQuality(log=lambda *_: None)
        with self.assertRaises(ValueError):
            q.before_frame(0, 0, FallDetector())


class RehearsalTest(unittest.TestCase):
    def setUp(self):
        self.network = socket.create_connection
        self.targets = []

        def loopback_only(address, *args, **kwargs):
            self.assertEqual(address[0], "127.0.0.1", "Offline demo must not contact any external host")
            self.targets.append(address)
            return self.network(address, *args, **kwargs)

        self.guard = patch("socket.create_connection", side_effect=loopback_only)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.arm_guard = patch.object(rehearsal.master_service, "ArmClient", side_effect=AssertionError("No actuator client"))
        self.arm_guard.start()
        self.addCleanup(self.arm_guard.stop)

    def case(self, name="fall", frames=None, expected=None):
        return rehearsal.run_case(name, frames if frames is not None else synth.build(name),
                                  expected=expected, log=lambda *_: None)

    def test_builtin_matrix_and_all_master_steps(self):
        for name, frames, rules, expected in rehearsal.builtin_cases():
            with self.subTest(name=name):
                result = rehearsal.run_case(name, frames, rules=rules, expected=expected, log=lambda *_: None)
                self.assertTrue(result["ok"], result["checks"])
                if expected:
                    self.assertEqual(result["states"], ["IDLE", "ASKING", "SOS", "HELP_COMING"])
                    self.assertEqual(sum(a["pose"] == "press_sos" for a in result["mock_actions"]), 1)
                else:
                    self.assertEqual(result["mock_actions"], [])
        self.assertTrue(self.targets)

    def test_unlabeled_file_is_not_classified_by_its_name(self):
        result = self.case("normal_fall_filename", synth.build("sit"))
        self.assertIsNone(result["expected_events"])
        self.assertEqual(result["source_kind"], "unlabeled_recording")
        self.assertNotIn("expected_events", result["checks"])
        self.assertTrue(result["ok"])

    def test_bad_and_empty_recordings_cannot_pass(self):
        for frames in ([], [(0, None)], [(0, STANDING), (0, STANDING)], [(float("inf"), STANDING)]):
            with self.subTest(frames=frames):
                result = self.case("invalid", frames)
                self.assertFalse(result["ok"])
                self.assertEqual(result["mock_actions"], [])

    def test_source_disconnect_is_failure_with_partial_recording(self):
        def frames():
            yield 0, STANDING
            raise ConnectionError("secret camera URL")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.jsonl"
            result = rehearsal.run_case("disconnected", frames(), recording=path, log=lambda *_: None)
            self.assertFalse(result["ok"])
            self.assertEqual(result["input_error"], "ConnectionError")
            self.assertEqual(result["input_quality"]["state"], "input_error")
            self.assertNotIn("secret camera URL", json.dumps(result))
            self.assertEqual(len(read_keypoints(path)[1]), 1)

    def test_hostile_environment_does_not_enable_llm_or_hardware(self):
        with patch.dict("os.environ", {"CAREMATE_LLM_MODE": "llm", "CAREMATE_ARM_URL": "http://unreachable.invalid", "ARM_BACKEND": "panthera"}):
            with rehearsal.MockMaster() as master:
                self.assertEqual(master.bus.cfg["llm_mode"], "scripted")
                self.assertTrue(master.bus.arm.status()["mock"])
                master.request("/mock/fall", {})

    def test_master_port_released_on_exit(self):
        with rehearsal.MockMaster() as master:
            port = master.port
        with self.assertRaises(OSError):
            self.network(("127.0.0.1", port), timeout=.2)

    def test_cli_exports_self_contained_report_and_replayable_synthetic_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rehearsal.main(["--scenario", "fall", "--output", str(out)]), 0)
            report = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["cases"][0]["source_kind"], "synthetic")
            meta, frames = read_keypoints(out / "keypoints" / "fall.jsonl")
            self.assertEqual(meta["source"], "synthetic:fall")
            self.assertTrue(frames)
            with zipfile.ZipFile(out.with_suffix(".zip")) as z:
                self.assertIsNone(z.testzip())
                self.assertIn("report.html", z.namelist())

    def test_cli_replays_phone_recording_without_model_or_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "phone.jsonl"
            writer = KeypointWriter(path, "phone:mjpeg")
            for t, kp in synth.build("fall"):
                writer.write(t, kp, 4 / 3)
            writer.close()
            out = Path(tmp) / "replay"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rehearsal.main(["--keypoints", str(path), "--output", str(out)]), 0)
            result = json.loads((out / "report.json").read_text(encoding="utf-8"))["cases"][0]
            self.assertIsNone(result["expected_events"])
            self.assertEqual(len(result["receipts"]), 1)
            self.assertEqual(result["states"][-1], "HELP_COMING")

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "keep.txt").write_text("keep")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rehearsal.main(["--output", tmp])
            self.assertEqual((Path(tmp) / "keep.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
