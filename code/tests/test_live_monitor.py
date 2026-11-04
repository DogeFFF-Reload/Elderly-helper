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

    def execute(self, args, *, monitor_factory, close_monitor, log):
        self.assertFalse(close_monitor)
        self.options = args
        self.monitor = monitor_factory(host="127.0.0.1", port=8015, mode="off", input_kind="phone_mjpeg")
        return {"ok": True, "status": "completed"}

    def start(self, argv=(), answers=("http://phone.invalid:8081/", "admin"), **options):
        answers = iter(answers)
        defaults = dict(prompt=lambda _: next(answers), secret=lambda _: "private-password",
                        execute=self.execute, settings=self.settings, log=self.logs.append,
                        monitor_factory=lambda **kw: type("Monitor", (), {
                            "address": ("127.0.0.1", kw["port"]), "close": lambda self: None})(),
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

    def test_error_view_stays_available_until_explicit_retry_then_old_server_closes(self):
        monitors, passwords, browsers = [], [], []

        class Monitor:
            address = ("127.0.0.1", 8015)
            closed = False

            def close(self):
                self.closed = True

        def factory(**_options):
            if monitors:
                self.assertTrue(monitors[-1].closed, "Retry must release the previous listening port")
            monitor = Monitor()
            monitors.append(monitor)
            return monitor

        def execute(args, *, monitor_factory, close_monitor, log):
            self.assertFalse(close_monitor)
            monitor_factory()
            passwords.append(args.phone_password)
            return {"ok": len(passwords) > 1, "status": "error" if len(passwords) == 1 else "completed"}

        answers = iter(("http://phone.invalid/", "admin", "", "", "admin"))
        secrets = iter(("first-password", "correct-password"))

        def prompt(message):
            if "重新输入" in message:
                self.assertFalse(monitors[-1].closed, "Error page must stay reachable while correcting login")
            return next(answers)

        result = self.start(prompt=prompt, secret=lambda _: next(secrets), execute=execute, monitor_factory=factory,
                            open_browser=lambda url: browsers.append(url) or True)
        self.assertEqual(result, 0)
        self.assertEqual(passwords, ["first-password", "correct-password"])
        self.assertEqual(len(browsers), 1)
        self.assertTrue(all(m.closed for m in monitors))
        self.assertNotIn("correct-password", self.settings.read_text(encoding="utf-8") + "".join(self.logs))

    def test_quitting_after_login_failure_closes_monitor_without_any_automatic_retry(self):
        calls, closed = [], []

        def execute(args, *, monitor_factory, **_options):
            calls.append(args)
            monitor_factory(host="127.0.0.1", port=8015)
            return {"ok": False, "status": "error"}

        self.assertEqual(self.start(answers=("http://phone.invalid/", "admin", "q"), execute=execute,
                                    monitor_factory=lambda **kw: type("Monitor", (), {
                                        "address": ("127.0.0.1", 8015), "close": lambda self: closed.append(True)})()), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
