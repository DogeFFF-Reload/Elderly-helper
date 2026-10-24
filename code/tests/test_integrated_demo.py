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


if __name__=="__main__":
    unittest.main()
