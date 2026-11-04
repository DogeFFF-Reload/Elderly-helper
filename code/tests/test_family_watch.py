"""User entry, safe replay gating and fake live sources against actual alarm HTTP."""
import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from family_alarm_receiver import AlarmReceiver
from vision import family_watch as watch, synth
from vision.family_alarm import AlarmClient
from vision.fall_bridge import keypoint_source


class FamilyWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        connect = socket.create_connection

        def loopback_only(address, *args, **kw):
            self.assertEqual(address[0], "127.0.0.1")
            return connect(address, *args, **kw)

        guard = patch("socket.create_connection", side_effect=loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

    def args(self, *options):
        return watch.parser().parse_args([*options, "--output", str(self.root / "result")])

    def test_default_off_synthetic_records_locally_without_transport(self):
        args = self.args("--synthetic", "fall")
        client = AlarmClient(transport=lambda *a, **k: self.fail("Off mode network access"))
        report = watch.execute(args, client=client, log=lambda _: None)
        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "off")
        self.assertEqual(report["events"][0]["status"], "disabled")
        self.assertFalse(report["images_sent"])
        self.assertFalse(report["actuator_connected"])
        self.assertFalse(report["master_connected"])
        self.assertEqual(json.loads((args.output / "summary.json").read_text(encoding="utf-8")), report)

    def test_live_replay_is_rejected_before_execute_devices_or_transport(self):
        for source in (["--synthetic", "fall"], ["--keypoints", "missing.jsonl"]):
            with self.subTest(source=source), patch.object(watch, "execute") as execute:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    watch.main([*source, "--mode", "live"])
                execute.assert_not_called()
            with self.assertRaises(ValueError):
                watch.execute(self.args(*source, "--mode", "live"))

    def test_dry_synthetic_refall_sends_two_unique_dry_events_but_stores_none(self):
        with AlarmReceiver() as receiver:
            args = self.args("--synthetic", "refall", "--mode", "dry", "--family", receiver.url)
            report = watch.execute(args, log=lambda _: None)
            self.assertTrue(report["ok"])
            self.assertEqual(len(report["events"]), 2)
            self.assertTrue(all(e["status"] == "dry_accepted" for e in report["events"]))
            self.assertEqual(len({e["event_id"] for e in report["events"]}), 2)
            self.assertEqual(receiver.alarms, [])

    def fake_live(self, **options):
        self.live_options = options
        owner = self

        class FakeLive:
            def __enter__(self):
                owner.live_opened = True
                return self

            def __iter__(self):
                yield from keypoint_source(synth.build("fall"), 4/3, realtime=False)

            def __exit__(self, *_args):
                owner.live_closed = True

        return FakeLive()

    def test_fake_pc_live_source_sends_one_alarm_and_closes(self):
        with AlarmReceiver() as receiver:
            args = self.args("--camera", "0", "--mode", "live", "--family", receiver.url)
            report = watch.execute(args, live_factory=self.fake_live, log=lambda _: None)
            self.assertTrue(report["ok"])
            self.assertTrue(self.live_opened and self.live_closed)
            self.assertEqual(len(receiver.alarms), 1)
            self.assertEqual(report["events"][0]["status"], "bedside_accepted")
            self.assertIsNone(report["phone_received"])

    def test_fake_phone_live_source_and_credentials_do_not_appear_in_saved_results(self):
        with AlarmReceiver() as receiver:
            args = self.args("--phone-stream", "http://phone.invalid/stream?token=private-token",
                             "--phone-username", "private-user", "--phone-password", "private-password",
                             "--mode", "live", "--family", receiver.url)
            report = watch.execute(args, live_factory=self.fake_live, log=lambda _: None)
            self.assertTrue(report["ok"])
            self.assertEqual(report["input_kind"], "phone_mjpeg")
            self.assertEqual(self.live_options["password"], "private-password")
            stored = "".join(p.read_text(encoding="utf-8") for p in args.output.iterdir())
            for value in ("private-token", "private-user", "private-password", "phone.invalid"):
                self.assertNotIn(value, stored)

    def test_failed_preflight_stops_before_opening_input(self):
        args = self.args("--camera", "0", "--mode", "live")
        client = AlarmClient(mode="live", transport=lambda *a, **k: {"ok": True, "ver": "5.6"})
        report = watch.execute(args, client=client, live_factory=lambda **kw: self.fail("Device opened"),
                               log=lambda _: None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "error")

    def test_partial_input_error_preserves_delivery_and_does_not_claim_success(self):
        class BrokenLive:
            def __enter__(self):
                return self

            def __iter__(self):
                yield from keypoint_source(synth.build("fall"), 4/3, realtime=False)
                raise RuntimeError("secret-phone-url")

            def __exit__(self, *_args):
                pass

        with AlarmReceiver() as receiver:
            args = self.args("--camera", "0", "--mode", "live", "--family", receiver.url)
            report = watch.execute(args, live_factory=lambda **kw: BrokenLive(), log=lambda _: None)
            self.assertFalse(report["ok"])
            self.assertEqual(report["status"], "error")
            self.assertEqual(len(receiver.alarms), 1)
            self.assertEqual(report["events"][0]["status"], "bedside_accepted")
            self.assertNotIn("secret-phone-url", json.dumps(report))

    def test_check_link_only_uses_dry_and_never_opens_camera(self):
        with AlarmReceiver() as receiver:
            args = self.args("--check-link", "--mode", "live", "--family", receiver.url)
            report = watch.execute(args, live_factory=lambda **kw: self.fail("Camera opened"),
                                   log=lambda _: None)
            self.assertTrue(report["ok"])
            self.assertEqual(report["status"], "link_checked")
            self.assertEqual(receiver.alarms, [])

    def test_missing_model_system_exit_is_recorded_as_input_error_without_raw_message(self):
        class MissingModel:
            def __enter__(self):
                raise SystemExit("secret-model-path")

            def __exit__(self, *_args):
                pass

        args = self.args("--phone-stream", "http://phone.invalid/stream")
        report = watch.execute(args, live_factory=lambda **kw: MissingModel(), log=lambda _: None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error_type"], "SystemExit")
        self.assertNotIn("secret-model-path", json.dumps(report))

    def test_explicit_interface_test_can_reuse_failed_event_id(self):
        with AlarmReceiver() as receiver:
            for suffix in ("first", "retry"):
                result = watch.main(["--send-test", "--event-id", "same-failed-id", "--mode", "live",
                                     "--family", receiver.url, "--output", str(self.root / suffix)])
                self.assertEqual(result, 0)
            self.assertEqual(len(receiver.alarms), 1)
            report = json.loads((self.root / "retry/summary.json").read_text(encoding="utf-8"))
            self.assertTrue(report["events"][0]["dedup"])

    def test_bad_or_empty_input_is_not_completed(self):
        path = self.root / "empty.jsonl"
        path.write_text('{"t":0,"kp":null}\n', encoding="utf-8")
        report = watch.execute(self.args("--keypoints", str(path)), log=lambda _: None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "no_usable_input")

    def test_camera_errors_explain_login_or_stream_problem_without_exposing_other_exceptions(self):
        from camera.phone_calibrate import CAMERA_ERROR_MESSAGES, CollectionError
        for label, text in (("auth", CAMERA_ERROR_MESSAGES["auth"]),
                            ("page", CAMERA_ERROR_MESSAGES["page"]),
                            ("secret", "secret-password-and-camera-url")):
            with self.subTest(label=label):
                logs = []
                args = watch.parser().parse_args(["--phone-stream", "http://phone.invalid/",
                                                 "--output", str(self.root / label)])
                client = AlarmClient(transport=lambda *_a, **_kw: self.fail("Off mode accessed bedside"))
                class Broken:
                    def __enter__(self):
                        raise CollectionError(text)

                    def __exit__(self, *_args):
                        pass
                report = watch.execute(args, client=client, live_factory=lambda **_kw: Broken(), log=logs.append)
                if label != "secret":
                    self.assertEqual(report["message"], text)
                    self.assertIn(text, "".join(logs))
                else:
                    self.assertNotIn(text, json.dumps(report) + "".join(logs))

    def test_launcher_can_keep_error_page_available_after_camera_rejection(self):
        from http.client import HTTPConnection
        from camera.phone_calibrate import CAMERA_ERROR_MESSAGES, CollectionError
        from vision.web_monitor import WebMonitor
        monitor = WebMonitor(port=0)
        self.addCleanup(monitor.close)

        class Rejected:
            def __enter__(self):
                raise CollectionError(CAMERA_ERROR_MESSAGES["auth"])

            def __exit__(self, *_args):
                pass

        args = self.args("--phone-stream", "http://phone.invalid/", "--web-monitor")
        report = watch.execute(args, live_factory=lambda **_kw: Rejected(), monitor_factory=lambda **_kw: monitor,
                               close_monitor=False, log=lambda _: None)
        self.assertFalse(report["ok"])
        self.assertFalse(monitor.closed)
        connection = HTTPConnection("127.0.0.1", monitor.address[1], timeout=2)
        try:
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            status = json.loads(response.read())
            self.assertEqual(status["run_status"], "error")
            self.assertEqual(status["input_message"], CAMERA_ERROR_MESSAGES["auth"])
            self.assertFalse(status["frame_ready"])
        finally:
            connection.close()
        monitor.update(input_message="secret-credential")
        self.assertNotIn("secret-credential", json.dumps(monitor.snapshot()))

    def test_launcher_help_works_from_unrelated_directory_without_model_or_devices(self):
        launcher = Path(__file__).resolve().parents[2] / "run_family_alarm.py"
        result = subprocess.run([sys.executable, "-X", "utf8", str(launcher), "--help"],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--phone-stream", result.stdout)
        self.assertIn("--mode", result.stdout)


if __name__ == "__main__":
    unittest.main()
