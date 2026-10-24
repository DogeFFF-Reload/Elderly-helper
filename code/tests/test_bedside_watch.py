"""Bedside deployment config and complete fake-phone -> loopback-alarm -> web view."""
import contextlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from family_alarm_receiver import AlarmReceiver
from test_phone_deploy import BASH
from test_web_monitor import eventually
from vision import bedside_watch as bedside, family_watch, synth
from vision.family_alarm import AlarmClient
from vision.fall_bridge import keypoint_source
from vision.web_monitor import WebMonitor


ROOT = Path(__file__).resolve().parents[2]


class BedsideWatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="caremate bedside ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "deploy/bedside_vision.json"
        self.path.parent.mkdir()
        self.values = {"phone_stream": "http://phone.invalid/camera/stream?token=private-token"}
        connect = socket.create_connection

        def loopback_only(address, *args, **kwargs):
            self.assertEqual(address[0], "127.0.0.1", "No real device connections")
            return connect(address, *args, **kwargs)

        guard = patch("socket.create_connection", side_effect=loopback_only)
        guard.start()
        self.addCleanup(guard.stop)

    def configuration(self, **values):
        self.path.write_text(json.dumps(dict(self.values, **values)), encoding="utf-8")
        return bedside.load_configuration(self.path)

    def test_default_is_off_local_bedside_and_continuous_with_config_relative_paths(self):
        values = self.configuration()
        args = bedside.watch_arguments(values)
        self.assertEqual(args.mode, "off")
        self.assertEqual(args.family, "http://127.0.0.1:8013")
        self.assertEqual(args.monitor_host, "127.0.0.1")
        self.assertTrue(args.continuous and args.web_monitor)
        self.assertFalse(args.preview)
        self.assertEqual(args.output.parent, self.root / "code/data/bedside_vision")
        self.assertFalse(args.output.exists())

    def test_check_config_has_no_model_device_network_or_output_side_effects(self):
        self.configuration()
        output = io.StringIO()
        with patch.object(family_watch, "execute", side_effect=AssertionError("No launch")), \
                patch.object(AlarmClient, "preflight", side_effect=AssertionError("No network")), \
                patch("vision.live_input.LiveInput.__enter__", side_effect=AssertionError("No input")), \
                contextlib.redirect_stdout(output):
            result = bedside.main(["--config", str(self.path), "--check-config"])
        self.assertEqual(result, 0)
        self.assertIn("配置检查通过", output.getvalue())
        for secret in ("phone.invalid", "private-token"):
            self.assertNotIn(secret, output.getvalue())
        self.assertFalse((self.root / "code").exists())

    def test_invalid_config_fails_before_services_and_never_prints_untrusted_values(self):
        for override in ({"phone_stream": ""}, {"continuous": "true"}, {"mode": "bad-secret"},
                         {"monitor_port": True}, {"fps_cap": 0}, {"duration": float("inf")},
                         {"monitor_host": "secret-host"}, {"family": "http://user:secret@127.0.0.1:1"},
                         {"phone_stream": "http://user:secret@phone.invalid/"},
                         {"phone_stream": "http://phone.invalid:bad-secret/"},
                         {"extra": "private-token"}, {"output_root": ""}):
            with self.subTest(override=override):
                self.path.write_text(json.dumps(dict(self.values, **override)), encoding="utf-8")
                output = io.StringIO()
                with patch.object(family_watch, "execute", side_effect=AssertionError("No launch")), \
                        contextlib.redirect_stdout(output):
                    result = bedside.main(["--config", str(self.path), "--check-config"])
                self.assertEqual(result, 2)
                for secret in ("secret", "private-token", "phone.invalid"):
                    self.assertNotIn(secret, output.getvalue())

    def test_runtime_check_does_not_start_watch_or_need_configuration(self):
        with patch.object(bedside, "check_runtime") as runtime, \
                patch.object(family_watch, "execute", side_effect=AssertionError("No launch")):
            self.assertEqual(bedside.main(["--check-runtime"]), 0)
            runtime.assert_called_once()

    def test_failed_alarm_preflight_closes_web_server_before_any_input_is_opened(self):
        values = self.configuration(mode="live")
        args = bedside.watch_arguments(values)
        monitors = []

        def monitor_factory(**kw):
            kw["port"] = 0
            monitor = WebMonitor(**kw)
            monitors.append(monitor)
            return monitor

        client = AlarmClient(mode="live", transport=lambda *a, **kw: {"ok": True, "ver": "5.6"})
        report = family_watch.execute(args, client=client, monitor_factory=monitor_factory,
                                      live_factory=lambda **kw: self.fail("Real input opened"), log=lambda _: None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "error")
        self.assertTrue(monitors[0].closed)
        self.assertFalse(monitors[0].http.is_alive())

    def test_complete_fake_phone_alarm_and_monitor_share_one_input_and_close_cleanly(self):
        observations, monitors, input_options = {}, [], []
        owner = self

        def monitor_factory(**kw):
            kw["port"] = 0
            monitor = WebMonitor(**kw)
            monitors.append(monitor)
            return monitor

        def read(path):
            connection = HTTPConnection("127.0.0.1", monitors[0].address[1], timeout=2)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                owner.assertEqual(response.status, 200)
                return response.read()
            finally:
                connection.close()

        class Phone:
            def __enter__(self):
                observations["opened"] = True
                return self

            def __iter__(self):
                yield from keypoint_source(synth.build("fall"), 4/3, realtime=False)
                eventually(lambda: monitors[0].snapshot()["frame_ready"])
                eventually(lambda: monitors[0].snapshot()["latest_alarm"]["status"] == "bedside_accepted")
                observations["page"] = read("/")
                observations["frame"] = read("/frame.jpg")
                observations["state"] = json.loads(read("/api/status"))

            def __exit__(self, *_exc):
                observations["closed"] = True

        def live_factory(**kw):
            input_options.append(kw)
            return Phone()

        with AlarmReceiver() as receiver:
            values = self.configuration(mode="live", family=receiver.url)
            report = family_watch.execute(bedside.watch_arguments(values), live_factory=live_factory,
                                          monitor_factory=monitor_factory, log=lambda _: None)
            self.assertTrue(report["ok"], report)
            self.assertEqual(len(receiver.alarms), 1)
        self.assertEqual(len(input_options), 1)
        self.assertTrue(input_options[0]["continuous"])
        self.assertTrue(observations["opened"] and observations["closed"])
        self.assertTrue(observations["frame"].startswith(b"\xff\xd8"))
        self.assertEqual(observations["state"]["latest_alarm"]["status"], "bedside_accepted")
        self.assertEqual(observations["state"]["alarm_count"], 1)
        self.assertIsNone(observations["state"]["phone_received"])
        self.assertTrue(report["images_sent"])
        self.assertFalse(report["alarm_images_sent"])
        self.assertEqual(report["monitor_frames_served"], 1)
        self.assertFalse(report["actuator_connected"] or report["master_connected"])
        self.assertTrue(monitors[0].closed)
        self.assertFalse(monitors[0].http.is_alive() or monitors[0].worker.is_alive())
        text = "".join(p.read_text(encoding="utf-8") for p in Path(values["output_root"]).rglob("*.json*"))
        for secret in ("phone.invalid", "private-token"):
            self.assertNotIn(secret, text)
        self.assertFalse(list(Path(values["output_root"]).rglob("*.jpg")))

    def test_launcher_help_from_unrelated_directory_does_not_require_model_or_hardware(self):
        result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "run_bedside_vision.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-config", result.stdout)
        self.assertIn("--check-runtime", result.stdout)

    @unittest.skipUnless(BASH, "Bash required for installer routing")
    def test_installer_uses_new_unit_and_does_not_start_or_replace_hardware_services(self):
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / "fake-bin").mkdir()
        python = self.root / ".venv/bin/python"
        python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$TASK_PY_ARGS"\n',
                          encoding="utf-8", newline="\n")
        sudo = self.root / "fake-bin/sudo"
        sudo.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TASK_COMMANDS"\n'
                        'if [ "$1" = install ]; then cp "$4" "$TEST_CAPTURED_UNIT"; fi\n',
                        encoding="utf-8", newline="\n")
        python.chmod(0o755)
        sudo.chmod(0o755)
        self.configuration()
        shutil.copyfile(ROOT / "deploy/install_bedside_vision.sh", self.root / "deploy/install_bedside_vision.sh")
        env = dict(os.environ, PATH=str(self.root / "fake-bin") + os.pathsep + os.environ["PATH"],
                   TASK_PY_ARGS=str(self.root / "pyargs"), TASK_COMMANDS=str(self.root / "commands"),
                   TEST_CAPTURED_UNIT=str(self.root / "unit"), SUDO_USER="mock_operator")
        result = subprocess.run([BASH, str(self.root / "deploy/install_bedside_vision.sh")], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = (self.root / "commands").read_text(encoding="utf-8")
        self.assertIn("caremate-bedside-vision.service", commands)
        for forbidden in ("enable", "start", "stop", "restart", "caremate-arm", "caremate.service"):
            self.assertNotIn(forbidden, commands)
        unit = (self.root / "unit").read_text(encoding="utf-8")
        self.assertIn('run_bedside_vision.py" --config', unit)
        self.assertIn('EnvironmentFile="-', unit)
        self.assertIn("User=mock_operator", unit)
        self.assertIn("--check-config", (self.root / "pyargs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
