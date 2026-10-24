"""Complete visual demo: loopback HTTP, fake actuator and no camera/model."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import master_service
from vision import integrated_demo as demo, synth
from vision.kpio import KeypointWriter


class IntegratedDemoTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.connect=socket.create_connection

        def loopback_only(address,*args,**kwargs):
            self.assertEqual(address[0],"127.0.0.1")
            return self.connect(address,*args,**kwargs)

        guard=patch("socket.create_connection",side_effect=loopback_only)
        guard.start()
        self.addCleanup(guard.stop)
        arm=patch.object(master_service, "ArmClient",
                         side_effect=AssertionError("No real actuator client"))
        arm.start()
        self.addCleanup(arm.stop)

    def recording(self,name="sit"):
        path=self.root/"session"/"keypoints.jsonl"
        writer=KeypointWriter(path,"pc_camera:0")
        for t,kp in synth.build(name):
            writer.write(t,kp,4/3)
        writer.close()
        return path

    def run_demo(self,**kwargs):
        out=self.root/"output"
        out.mkdir()
        with patch.dict(sys.modules,{"cv2":None,"mediapipe":None}):
            return demo.run_full_demo(out,**kwargs)

    def test_full_flow_uses_visual_events_and_every_existing_demo_scene(self):
        report=self.run_demo()
        self.assertTrue(report["ok"],report.get("checks"))
        self.assertEqual(len(report["checks"]),10)
        self.assertEqual([len(r["events"]) for r in report["visual_inputs"]],[0,1,1])
        self.assertEqual([r["kind"] for r in report["visual_inputs"]],
                         ["synthetic_normal","synthetic_cancel","synthetic_rescue"])
        poses=[a["pose"] for a in report["mock_actions"]]
        for pose in ("deliver_medicine","press_sos","gentle_hold","sign_family_received","sign_coming"):
            self.assertIn(pose,poses)
        self.assertEqual(poses.count("press_sos"),1)
        self.assertEqual(report["final_state"],"IDLE")
        self.assertFalse(report["camera_opened"])

    def test_saved_normal_recording_stays_distinct_from_synthetic_rescue(self):
        path=self.recording()
        original=path.read_bytes()
        report=self.run_demo(keypoints=path,normal_confirmed=True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["visual_inputs"][0]["kind"],"recorded")
        self.assertEqual(report["recording"]["sha256"],hashlib.sha256(original).hexdigest())
        self.assertEqual(path.read_bytes(),original)
        self.assertTrue(report["normal_confirmed"])

    def test_recording_with_event_stops_before_synthetic_rescue(self):
        report=self.run_demo(keypoints=self.recording("fall"))
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["normal_keeps_idle"])
        self.assertEqual([r["kind"] for r in report["visual_inputs"]],["recorded"])

    def test_confirmation_requires_matching_hash_and_fixed_camera(self):
        path=self.recording()
        self.assertFalse(demo.confirmation_matches(path))
        data={"normal_activities_completed":True,"camera_fixed":True,
              "keypoints_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
        sidecar=path.with_name("operator_confirmation.json")
        sidecar.write_text(json.dumps(data),encoding="utf-8")
        self.assertTrue(demo.confirmation_matches(path))
        data["keypoints_sha256"]="wrong"
        sidecar.write_text(json.dumps(data),encoding="utf-8")
        self.assertFalse(demo.confirmation_matches(path))

    def test_cli_headless_exports_report_and_prevents_overwrite(self):
        out=self.root/"cli"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main(["--headless","--output",str(out)]),0)
        report=json.loads((out/"report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        self.assertIn("完整流程通过",(out/"report.html").read_text(encoding="utf-8"))
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            demo.main(["--headless","--output",str(out)])

    def test_native_view_close_preserves_partial_report(self):
        class ClosedViewer:
            def show(self,_target):
                return False
        report=self.run_demo(viewer=ClosedViewer())
        self.assertFalse(report["ok"])
        self.assertTrue(report["interrupted"])
        self.assertTrue((self.root/"output"/"report.json").is_file())

    def live_source(self, name="sit", *, fail=False):
        class SimulatedInput:
            kind = "pc_camera"
            source_name = "synthetic:live_transport_test"
            duration, fps = 60, 10
            open_attempted = opened = closed = False

            def __enter__(self):
                self.open_attempted = self.opened = True
                return self

            def __iter__(self):
                for t, kp in synth.build(name):
                    yield t, kp, "memory-only-frame", 4/3
                    if fail and t >= .5:
                        raise ConnectionError("http://private_user:private_secret@invalid/")

            def __exit__(self, *_exc):
                self.closed = True
        return SimulatedInput()

    def test_live_coordinates_enter_full_mock_workflow_and_are_archived(self):
        live = self.live_source()
        report = self.run_demo(live_input=live)
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(report["checks"]), 10)
        self.assertEqual(report["visual_inputs"][0]["kind"], "live")
        self.assertTrue(report["visual_inputs"][0]["completed"])
        self.assertFalse(report["normal_confirmed"])
        self.assertTrue(live.closed)
        self.assertTrue((self.root/"output"/"live_keypoints.jsonl").is_file())
        import zipfile
        with zipfile.ZipFile(self.root/"output.zip") as archive:
            self.assertIn("live_keypoints.jsonl", archive.namelist())

    def test_live_disconnect_saves_partial_coordinates_and_redacts_error(self):
        live = self.live_source(fail=True)
        report = self.run_demo(live_input=live)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "ConnectionError")
        self.assertTrue(live.closed)
        self.assertGreater(report["visual_inputs"][0]["input_quality"]["frames"], 0)
        self.assertEqual(report["visual_inputs"][0]["input_quality"]["state"], "input_error")
        self.assertEqual(len(report["visual_inputs"]), 1)
        self.assertNotIn("press_sos", [a["pose"] for a in report["mock_actions"]])
        for path in (self.root/"output").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("private_secret", text)
            self.assertNotIn("private_user", text)

    def test_live_suspected_event_is_preserved_and_stops_scripted_rescue(self):
        live = self.live_source("fall")
        report = self.run_demo(live_input=live)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["normal_keeps_idle"])
        self.assertEqual(len(report["visual_inputs"]), 1)
        self.assertEqual(len(report["visual_inputs"][0]["events"]), 1)
        self.assertEqual(len(report["visual_inputs"][0]["receipts"]), 1)
        self.assertTrue(live.closed)

    def test_live_no_person_does_not_pass_or_continue_synthetic_rescue(self):
        live = self.live_source()
        live.__class__.__iter__ = lambda self: iter([(i/10, None, None, 4/3) for i in range(10)])
        report = self.run_demo(live_input=live)
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["visual_input_usable"])
        self.assertEqual(len(report["visual_inputs"]), 1)
        self.assertEqual(report["final_state"], "IDLE")

    def test_live_view_closed_during_capture_closes_input_and_keeps_partial_report(self):
        class ClosedDuringInput:
            def show(self, target):
                return target.frame is None
        live = self.live_source()
        report = self.run_demo(live_input=live, viewer=ClosedDuringInput())
        self.assertFalse(report["ok"])
        self.assertTrue(report["interrupted"])
        self.assertTrue(live.closed)
        self.assertGreater(report["visual_inputs"][0]["input_quality"]["frames"], 0)

    def test_live_frames_are_not_paced_twice(self):
        live = self.live_source()
        with demo.MockMaster() as master:
            target = demo.DemoTarget(master, viewer=type("View", (), {"show": lambda *a: True})())
            result = {}
            with patch.object(demo.time, "sleep", side_effect=AssertionError("Live input already paced")):
                demo.play_live(target, live, self.root, result)
        self.assertTrue(result["completed"])
        self.assertTrue(live.closed)

    def test_cli_rejects_conflicting_inputs_and_invalid_live_limits_without_opening(self):
        for args in (["--camera", "--keypoints", "x"], ["--camera", "--normal-confirmed"],
                     ["--camera", "-1"], ["--camera", "--duration", "nan"],
                     ["--camera", "--fps-cap", "0"], ["--camera", "--duration", "601"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    patch.dict(sys.modules, {"cv2": None, "mediapipe": None}), self.assertRaises(SystemExit):
                demo.main(args)


if __name__=="__main__":
    unittest.main()
