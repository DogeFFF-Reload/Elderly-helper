"""Timed phone collection: loopback synthetic MJPEG only, no real devices/audio."""
import base64
import array
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import zipfile

from PIL import Image

from camera import phone_calibrate as calibration
from camera import phone_fall_watch as watch
from camera.phone_source import PhoneFrameSource


def jpeg(level):
    buffer = io.BytesIO()
    Image.new("RGB", (96, 64), (level, level, level)).save(buffer, format="JPEG")
    return buffer.getvalue()


def wav_bytes(values=(0, 50, -50, 1000, -1000, 30000, -30000), width=2):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(width)
        target.setframerate(22050)
        target.writeframes(array.array("h", values).tobytes())
    return buffer.getvalue()


class SpeechVolumeTest(unittest.TestCase):
    def test_gain_boosts_quiet_audio_preserving_wave_format_without_overflow(self):
        original = wav_bytes()
        louder = calibration.amplify_wave(original, 3)
        with wave.open(io.BytesIO(original)) as a, wave.open(io.BytesIO(louder)) as b:
            self.assertEqual(a.getparams(), b.getparams())
            before = array.array("h", a.readframes(a.getnframes()))
            after = array.array("h", b.readframes(b.getnframes()))
        self.assertEqual(after[0], 0)
        self.assertGreater(after[1], 2 * before[1])
        self.assertLess(after[2], 2 * before[2])
        self.assertGreater(after[3], 2 * before[3])
        self.assertTrue(all(-32767 <= n <= 32767 for n in after))
        self.assertGreater(after[5], 0)
        self.assertLess(after[6], 0)

    def test_unit_gain_preserves_original_samples_and_unsupported_format_fails(self):
        original = wav_bytes()
        with wave.open(io.BytesIO(original)) as a, \
                wave.open(io.BytesIO(calibration.amplify_wave(original, 1))) as b:
            self.assertEqual(a.readframes(a.getnframes()), b.readframes(b.getnframes()))
        with self.assertRaises(calibration.CollectionError):
            calibration.amplify_wave(wav_bytes(width=1), 3)

    def test_sound_check_needs_no_camera_and_plays_once(self):
        with patch.object(calibration, "WindowsSpeaker") as factory, \
                patch.object(calibration, "PhoneFrameSource") as source, \
                patch.object(calibration, "collect") as collect, \
                patch("builtins.input") as confirm, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(calibration.main(["--sound-check"]), 0)
        factory.assert_called_once_with(gain=3)
        factory.return_value.assert_called_once_with(calibration.SOUND_CHECK_TEXT)
        source.assert_not_called()
        collect.assert_not_called()
        confirm.assert_not_called()

    def test_cancel_after_sound_check_never_opens_camera(self):
        with patch.object(calibration, "WindowsSpeaker"), \
                patch.object(calibration, "PhoneFrameSource") as source, \
                patch.object(calibration, "collect") as collect, \
                patch("builtins.input", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            result = calibration.main(["--stream-url", "http://127.0.0.1:1/"])
        self.assertEqual(result, 130)
        source.assert_not_called()
        collect.assert_not_called()

    def test_confirmed_sound_check_passes_same_speaker_into_collection(self):
        with patch.object(calibration, "WindowsSpeaker") as factory, \
                patch.object(calibration, "PhoneFrameSource") as source, \
                patch.object(calibration, "collect", return_value=(Path("unused.zip"), {"status": "complete"})) as collect, \
                patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            result = calibration.main(["--stream-url", "http://127.0.0.1:1/", "--voice-gain", "4"])
        self.assertEqual(result, 0)
        factory.assert_called_once_with(gain=4)
        source.assert_called_once()
        self.assertIs(collect.call_args.kwargs["speaker"], factory.return_value)

    def test_sound_check_failure_never_opens_camera(self):
        with patch.object(calibration, "WindowsSpeaker") as factory, \
                patch.object(calibration, "PhoneFrameSource") as source, \
                patch("builtins.input") as confirm, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.side_effect = calibration.CollectionError("播放失败")
            self.assertEqual(calibration.main(["--sound-check"]), 1)
        source.assert_not_called()
        confirm.assert_not_called()

    def test_invalid_gain_and_incompatible_modes_rejected_without_audio(self):
        for flags in (("--voice-gain", "nan"), ("--voice-gain", "9"), ("--voice-gain", "0"), ("--no-voice",)):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(calibration, "WindowsSpeaker") as speaker, self.assertRaises(SystemExit):
                calibration.main(["--sound-check", *flags])
            speaker.assert_not_called()


class PacedCamera(BaseHTTPRequestHandler):
    authorization = "Basic " + base64.b64encode(b"collector_user:collector_secret").decode("ascii")
    pictures = [jpeg(0), jpeg(240)]
    requests = []
    disconnect_after = None

    def do_GET(self):  # noqa: N802
        self.__class__.requests.append(("GET", self.path))
        if self.headers.get("Authorization") != self.authorization:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test phone"')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        start = time.monotonic()
        n = 0
        try:
            while True:
                elapsed = time.monotonic() - start
                if self.disconnect_after is not None and elapsed > self.disconnect_after:
                    return
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + self.pictures[n % 2] + b"\r\n")
                self.wfile.flush()
                n += 1
                time.sleep(0.004)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):  # noqa: N802
        self.__class__.requests.append(("POST", self.path))
        self.send_error(500)

    def log_message(self, *_args):
        pass


class CalibrationStatisticsTest(unittest.TestCase):
    def test_statistics_include_every_comparison_and_linear_p95(self):
        rows = [{"stage_s": n * 0.5, "energy": energy}
                for n, energy in enumerate(("", 0, 2, 4, 6, 8))]
        stats = calibration.phase_statistics(rows, 3, 0.5)
        self.assertEqual(stats["samples"], 6)
        self.assertEqual(stats["comparisons"], 5)
        self.assertEqual(stats["energy_mean"], 4)
        self.assertEqual(stats["energy_median"], 4)
        self.assertAlmostEqual(stats["energy_p95"], 7.6)
        self.assertTrue(stats["sampling_ok"])

    def test_sparse_or_empty_data_are_not_marked_good(self):
        stats = calibration.phase_statistics([], 20, 0.5)
        self.assertFalse(stats["sampling_ok"])
        self.assertIsNone(stats["energy_max"])
        rows = [{"stage_s": n * 0.1, "energy": 0} for n in range(35)]
        stats = calibration.phase_statistics(rows, 20, 0.5)
        self.assertGreaterEqual(stats["samples"], stats["minimum_samples"])
        self.assertFalse(stats["sampling_ok"], "Long missing tail must invalidate otherwise adequate sample count")

    def test_invalid_time_parameters_rejected_before_camera_access(self):
        for flags in (("--duration", "nan"), ("--interval", "0"), ("--rounds", "0"),
                      ("--prep", "-1"), ("--duration", "0.01")):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(calibration, "collect") as collect, self.assertRaises(SystemExit):
                calibration.main(["--stream-url", "http://127.0.0.1:1/", *flags, "--no-voice"])
            collect.assert_not_called()

    def test_stream_discards_preparation_frames_and_closes_worker(self):
        class Source:
            mjpeg = True
            closed = False

            def frames(self):
                try:
                    for n in range(200):
                        yield str(n).encode("ascii")
                        time.sleep(0.002)
                finally:
                    self.closed = True

        source = Source()
        reader = calibration.LatestFrame(source, 0.03)
        reader.start()
        try:
            first = reader.get()
            time.sleep(0.04)  # stand-in for spoken preparation
            boundary = time.monotonic()
            newer = reader.get(after=first[0], not_before=boundary)
            self.assertGreater(newer[0], first[0] + 1)
            self.assertGreaterEqual(newer[2], boundary)
        finally:
            reader.close()
        self.assertFalse(reader.thread.is_alive())
        self.assertTrue(source.closed)

    @unittest.skipUnless(os.name == "nt", "Windows speech launcher")
    def test_speech_passes_text_as_data_without_real_audio(self):
        text = "中文提示；`任意文本`\n$()"
        with patch.object(calibration.subprocess, "run") as run, patch.object(calibration, "play_wave") as play:
            run.return_value.returncode = 0
            run.return_value.stdout = base64.b64encode(wav_bytes())
            calibration.WindowsSpeaker()(text)
        args, kwargs = run.call_args
        self.assertEqual(kwargs["input"], text.encode("utf-8"))
        self.assertEqual(args[0][-2], "-EncodedCommand")
        script = base64.b64decode(args[0][-1]).decode("utf-16-le")
        self.assertNotIn(text, script)
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertIn("zh-*", script)
        self.assertIn("$speaker.Volume = 100", script)
        self.assertIn("$speaker.SetOutputToWaveStream", script)
        self.assertFalse(kwargs.get("shell", False))
        play.assert_called_once()


class CalibrationCollectionTest(unittest.TestCase):
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
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        PacedCamera.requests = []
        PacedCamera.disconnect_after = None

    def source(self, password="collector_secret"):
        return PhoneFrameSource(self.base, mjpeg=True, username="collector_user", password=password)

    def collect(self, **kwargs):
        defaults = dict(output=self.temporary.name, rounds=1, duration=0.24, prep=0.02,
                        interval=0.03, frame_timeout=0.4)
        defaults.update(kwargs)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = calibration.collect(self.source(), **defaults)
        return result

    @staticmethod
    def rows(archive):
        with zipfile.ZipFile(archive) as bundle:
            name = next(name for name in bundle.namelist() if name.endswith("samples.csv"))
            return list(csv.DictReader(io.StringIO(bundle.read(name).decode("utf-8-sig"))))

    def test_three_rounds_archive_all_samples_and_never_post(self):
        with patch.object(watch, "post_fall") as post:
            archive, summary = self.collect(rounds=3)
        post.assert_not_called()
        self.assertEqual(summary["status"], "complete", summary)
        self.assertEqual(len(summary["phases"]), 9)
        rows = self.rows(archive)
        self.assertEqual(len(rows), sum(p["statistics"]["samples"] for p in summary["phases"]))
        for phase in summary["phases"]:
            subset = [r for r in rows if int(r["round"]) == phase["round"] and r["stage"] == phase["stage"]]
            self.assertEqual(subset[0]["baseline"], "1")
            self.assertEqual(subset[0]["energy"], "", "No comparisons across stage/preparation boundaries")
            self.assertEqual(subset[0]["delta_s"], "")
            energies = [float(r["energy"]) for r in subset[1:]]
            self.assertEqual(len(energies), phase["statistics"]["comparisons"])
            self.assertEqual(max(energies), phase["statistics"]["energy_max"])
            self.assertTrue(all(r["baseline"] == "0" for r in subset[1:]))
            self.assertTrue(all(0 <= e <= 21.25 for e in energies))
            self.assertEqual(len({r["source_sequence"] for r in subset}), len(subset))
            self.assertTrue(all(0 <= float(r["stage_s"]) < 0.24 for r in subset))
        self.assertTrue(all(method == "GET" and path == "/" for method, path in PacedCamera.requests))
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(len(bundle.namelist()), 4)
            content = "\n".join(bundle.read(name).decode("utf-8-sig") for name in bundle.namelist())
            for sensitive in ("collector_user", "collector_secret", self.base):
                self.assertNotIn(sensitive, content)
            self.assertNotIn("threshold", summary)
            summary_name = next(n for n in bundle.namelist() if n.endswith("summary.json"))
            self.assertEqual(json.loads(bundle.read(summary_name)), summary)

    def test_disconnect_saves_partial_and_marks_remaining_stages_not_started(self):
        PacedCamera.disconnect_after = 0.13
        archive, summary = self.collect()
        self.assertTrue(archive.is_file())
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["phases"][0]["status"], "error")
        self.assertTrue(self.rows(archive))
        self.assertEqual(summary["phases"][1]["status"], "not_started")

    def test_ctrl_c_saves_partial_data(self):
        original = calibration.energy_profile
        calls = 0

        def interrupt(before, after):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt
            return original(before, after)

        with patch.object(calibration, "energy_profile", side_effect=interrupt):
            archive, summary = self.collect()
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["phases"][0]["status"], "interrupted")
        self.assertEqual(len(self.rows(archive)), 3)
        self.assertTrue(archive.is_file())

    def test_wrong_password_and_voice_failure_do_not_claim_completion(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            archive, summary = calibration.collect(self.source("dont_print_this_secret"),
                                                    output=self.temporary.name)
        self.assertEqual(summary["status"], "error")
        self.assertIn("登录失败", summary["message"])
        self.assertNotIn("dont_print_this_secret", output.getvalue())
        self.assertFalse(self.rows(archive))
        with patch.object(calibration.LatestFrame, "start") as start, \
                patch.object(calibration, "WindowsSpeaker", side_effect=AssertionError("Unexpected speaker construction")):
            def broken_voice(_text):
                raise calibration.CollectionError("语音不可用")
            _, summary = self.collect(speaker=broken_voice)
        start.assert_not_called()
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["phases"][0]["status"], "not_started")

    def test_cli_no_voice_rounds_and_exit_code(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(calibration, "WindowsSpeaker") as speaker:
            result = calibration.main([
                "--stream-url", self.base, "--username", "collector_user", "--password", "collector_secret",
                "--no-voice", "--rounds", "1", "--duration", "0.24", "--prep", "0.01", "--interval", "0.03",
                "--output", self.temporary.name,
            ])
        speaker.assert_not_called()
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(len(list(Path(self.temporary.name).glob("*.zip"))), 1)

    def test_short_sampling_coverage_returns_insufficient_not_success(self):
        # A delayed decoder is a realistic way for a client to miss sampling slots.
        original = calibration.to_small_gray

        def slow_decode(picture):
            time.sleep(0.055)
            return original(picture)

        with patch.object(calibration, "to_small_gray", side_effect=slow_decode):
            archive, summary = self.collect()
        self.assertTrue(archive.is_file())
        self.assertEqual(summary["status"], "insufficient")
        self.assertTrue(all(p["status"] == "insufficient" for p in summary["phases"]))
        self.assertTrue(self.rows(archive))

    def tail_timeout_session(self, source_error=False, full_timeout=False):
        class Clock:
            now = 0.0

        clock = Clock()

        class Reader:
            calls = 0

            def __init__(self, *_args):
                pass

            def start(self):
                pass

            def check(self, _timeout):
                if source_error and self.calls >= 8:
                    raise calibration.CollectionError("source disconnected")

            def get(self, after=0, not_before=0.0, timeout=3.0):
                self.calls += 1
                if self.calls == (3 if full_timeout else 8):
                    clock.now += timeout
                    raise calibration.FrameWaitTimeout("wait expired")
                clock.now = max(clock.now, not_before) + .0001
                return self.calls, jpeg(0), clock.now, calibration.utc_now()

            def close(self):
                pass

        def wait(deadline, *_args):
            clock.now = max(clock.now, deadline)

        with patch.object(calibration, "LatestFrame", Reader), \
                patch.object(calibration.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(calibration, "wait_until", side_effect=wait):
            return self.collect(duration=1.2 if full_timeout else .24)

    def test_phase_deadline_is_not_reported_as_camera_failure(self):
        archive, summary = self.tail_timeout_session()
        self.assertEqual(summary["status"], "complete", summary)
        self.assertEqual(summary["phases"][0]["statistics"]["samples"], 6)
        with zipfile.ZipFile(archive) as bundle:
            events = next(name for name in bundle.namelist() if name.endswith("events.jsonl"))
            self.assertIn(b"phase_tail_without_sample", bundle.read(events))

    def test_tail_expiry_does_not_hide_source_error(self):
        _, summary = self.tail_timeout_session(source_error=True)
        self.assertEqual(summary["status"], "error")
        self.assertIn("source disconnected", summary["message"])

    def test_full_frame_timeout_still_aborts(self):
        _, summary = self.tail_timeout_session(full_timeout=True)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["message"], "wait expired")


if __name__ == "__main__":
    unittest.main()
