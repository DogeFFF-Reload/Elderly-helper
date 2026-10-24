"""One-click live viewer: fake input/browser only, never a camera or bedside."""
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

from vision import live_monitor


class LiveMonitorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / "connection.json"
        guard = patch.object(socket, "create_connection", side_effect=AssertionError("No device connection"))
        guard.start()
        self.addCleanup(guard.stop)
        self.options = self.browser = None
        self.logs = []

    def execute(self, args, *, monitor_factory, log):
        self.options = args
        self.monitor = monitor_factory(host="127.0.0.1", port=8015, mode="off", input_kind="phone_mjpeg")
        return {"ok": True, "status": "completed"}

    def start(self, argv=(), answers=("http://phone.invalid:8081/", "admin"), **options):
        answers = iter(answers)
        defaults = dict(prompt=lambda _: next(answers), secret=lambda _: "private-password",
                        execute=self.execute, settings=self.settings, log=self.logs.append,
                        monitor_factory=lambda **kw: type("Monitor", (), {"address": ("127.0.0.1", kw["port"])})(),
                        open_browser=lambda address: setattr(self, "browser", address) or True)
        return live_monitor.main(list(argv), **dict(defaults, **options))

    def test_phone_login_continuous_observe_only_and_browser_open_after_server_starts(self):
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.options.mode, "off")
        self.assertTrue(self.options.continuous and self.options.web_monitor)
        self.assertFalse(self.options.synthetic or self.options.keypoints or self.options.preview)
        self.assertEqual(self.options.phone_password, "private-password")
        self.assertEqual(self.browser, "http://127.0.0.1:8015/")
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"phone_stream": "http://phone.invalid:8081/"})
        self.assertNotIn("private-password", "".join(self.logs) + self.settings.read_text(encoding="utf-8"))

    def test_existing_address_reuses_default_and_no_auth_does_not_request_password(self):
        self.settings.write_text('{"phone_stream":"http://phone.invalid/"}', encoding="utf-8")
        self.assertEqual(self.start(answers=("", "-"), secret=lambda _: self.fail("Unneeded password prompt")), 0)
        self.assertIsNone(self.options.phone_username)
        self.assertIsNone(self.options.phone_password)
        self.assertEqual(self.options.phone_stream, "http://phone.invalid/")

    def test_bad_or_credential_url_stops_before_password_browser_and_execute(self):
        for url in ("rtsp://phone.invalid/live", "http://admin:private-password@phone.invalid/"):
            with self.subTest(url=url):
                self.assertEqual(self.start(answers=(url,), secret=lambda _: self.fail("Password requested"),
                                            execute=lambda *_a, **_kw: self.fail("Started")), 2)
                self.assertIsNone(self.browser)
        self.assertNotIn("private-password", "".join(self.logs))

    def test_query_tokens_are_not_persisted_and_browser_can_be_disabled(self):
        self.assertEqual(self.start(["--phone-stream", "http://phone.invalid/?token=private-token", "--no-browser"],
                                    answers=("admin",)), 0)
        self.assertFalse(self.settings.exists())
        self.assertIsNone(self.browser)
        self.assertNotIn("private-token", "".join(self.logs))

    def test_cancel_before_connection_and_browser_failure_are_handled(self):
        self.assertEqual(self.start(secret=lambda _: (_ for _ in ()).throw(KeyboardInterrupt())), 130)
        self.assertIsNone(self.options)
        self.assertEqual(self.start(open_browser=lambda _: (_ for _ in ()).throw(OSError("secret"))), 0)
        self.assertNotIn("secret", "".join(self.logs))

    def test_help_from_unrelated_directory_needs_no_camera_or_model(self):
        launcher = Path(__file__).resolve().parents[2] / "run_live_monitor.py"
        result = subprocess.run([sys.executable, "-X", "utf8", str(launcher), "--help"],
                                cwd=self.temp.name, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--phone-stream", result.stdout)
        self.assertIn("仅观察", result.stdout)


if __name__ == "__main__":
    unittest.main()
