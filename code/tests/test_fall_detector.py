"""跌倒规则（时序）+ 骨架录制回放 + 视觉桥发总线。"""
import os
import math
import json
import tempfile
import threading
import time
import unittest

from helpers import make_bus

import master_service
from gesture.fall_rules import STANDING, FallDetector, features
from vision import synth
from vision.fall_bridge import BusPoster, keypoint_source, run
from vision.kpio import KeypointWriter, load_rules, parse_set, read_keypoints


def alarms(name, fps=15.0, noise=0.004, seed=0, rules=None):
    sc = synth.SCENARIOS[name]
    merged = dict(sc.get("rules", {}))
    merged.update(rules or {})
    d = FallDetector(merged)
    return [ev for t, kp in synth.build(name, fps, noise, seed) for ev in [d.update(t, kp)] if ev]


class ScenarioTest(unittest.TestCase):
    def test_all_scenarios(self):
        for name, sc in synth.SCENARIOS.items():
            self.assertEqual([e["kind"] for e in alarms(name)], sc["expect"], name)

    def test_robust_to_fps_and_noise(self):
        for fps in (6, 10, 30):
            for seed in range(4):
                for name, sc in synth.SCENARIOS.items():
                    got = [e["kind"] for e in alarms(name, fps=fps, noise=0.01, seed=seed)]
                    self.assertEqual(got, sc["expect"], "%s fps=%s seed=%s" % (name, fps, seed))

    def test_fall_alarm_latency(self):
        t = alarms("fall")[0]["t"]
        self.assertTrue(3.5 <= t <= 5.0, "跌倒开始于 2.0s，应在落地后约 2 秒报警，实际 %.2f" % t)

    def test_stricter_hold_misses_short_recording(self):
        self.assertEqual(alarms("slow_floor", rules={"slow_hold": 30}), [])

    def test_bed_needs_zone_only_when_low(self):
        # 床比地面高，不配床区也不会被当成“低处”；配了床区则更保险
        self.assertEqual(alarms("bed", rules={"bed_zones": []}), [])


class FeatureTest(unittest.TestCase):
    def test_bad_required_point_is_unavailable_not_a_crash(self):
        for point in ([], [0.5], "bad", 4, [float("nan"), 0.5], [0.5, float("inf")],
                      [0.5, 0.5, float("nan")], [0.5, 0.5, -0.1], [True, 0.5], [10 ** 500, 0.5]):
            with self.subTest(point=point):
                kp = dict(STANDING, l_hip=point)
                self.assertIsNone(features(kp))

    def test_unrelated_bad_joint_does_not_hide_valid_torso(self):
        kp = dict(STANDING, l_wrist=[], head=[float("nan"), 0.5])
        self.assertIsNotNone(features(kp))
        self.assertIsNone(features(kp)["head_y"])

    def test_bad_pose_does_not_poison_next_valid_frame(self):
        d = FallDetector()
        d.update(0.0, STANDING)
        for t, kp in ((0.1, ["bad"]), (0.2, dict(STANDING, l_hip=[float("nan"), 0.5]))):
            self.assertIsNone(d.update(t, kp))
            self.assertIsNone(d.last["f"])
            self.assertEqual(d.state, d.WATCH)
        d.update(0.3, STANDING)
        self.assertTrue(math.isfinite(d.last["f"]["hip_y"]))
        self.assertTrue(math.isfinite(d.standing_hip))

    def test_neck_from_shoulders_and_visibility(self):
        kp = {k: [x, y, 0.9] for k, (x, y) in STANDING.items()}
        del kp["neck"]
        self.assertAlmostEqual(features(kp)["angle"], 0.0, delta=1)
        kp["l_hip"][2] = 0.1
        self.assertIsNone(features(kp), "髋部看不清就不判")

    def test_aspect_restores_true_angle(self):
        kp = {"neck": [0.40, 0.40], "l_hip": [0.50, 0.50], "r_hip": [0.50, 0.50]}
        self.assertAlmostEqual(features(kp, aspect=1.0)["angle"], 45.0, delta=0.5)
        self.assertGreater(features(kp, aspect=16 / 9)["angle"], 55.0)

    def test_rearm_needs_standing(self):
        d = FallDetector()
        frames = synth.build("fall")
        for t, kp in frames:
            d.update(t, kp)
        self.assertEqual(d.state, FallDetector.FALLEN)
        t = frames[-1][0]
        up = {k: [x, y, 0.99] for k, (x, y) in STANDING.items()}
        d.update(t + 0.1, up)
        d.update(t + 1.0, up)
        self.assertEqual(d.state, FallDetector.FALLEN, "站起来不到 2 秒还不重新布防")
        d.update(t + 2.3, up)
        self.assertEqual(d.state, FallDetector.WATCH)


class RecordingTest(unittest.TestCase):
    def test_invalid_rule_values_fail_before_processing(self):
        bad = ({"hip_smooth": 0}, {"confirm_hold": -1}, {"slow_hold": float("nan")},
               {"fall_window": float("inf")}, {"cancel_hold": "x"}, {"recover_hold": True},
               {"min_visibility": 2}, {"floor_y": -0.1}, {"lying_angle": 100},
               {"upright_angle": 60, "lying_angle": 50}, {"bed_zones": "bad"},
               {"bed_zones": [[0.8, 0.2, 0.4, 0.5]]}, {"bed_zones": [[0, 0, 2, 1]]},
               {"bed_zones": [[0, 0, 0.5]]}, {"unknown": 1})
        for rules in bad:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                FallDetector(rules)
        for aspect in (0, -1, float("nan"), float("inf"), "bad"):
            with self.subTest(aspect=aspect), self.assertRaises(ValueError):
                FallDetector(aspect=aspect)

    def test_cli_invalid_rules_fail_clearly(self):
        for sets, beds in ((["hip_smooth=0"], None), (["slow_hold=NaN"], None),
                           (None, ["0.8,0.2,0.4,0.5"]), (None, ["bad,0,1,1"])):
            with self.subTest(sets=sets, beds=beds), self.assertRaises(SystemExit):
                load_rules(sets=sets, beds=beds)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rules.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
            with self.assertRaises(SystemExit):
                load_rules(path)

    def test_roundtrip_has_no_images(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fall_01.jsonl")
            w = KeypointWriter(path, "camera:0")
            frames = synth.build("fall")
            for t, kp in frames:
                w.write(t, kp, 16 / 9)
            w.write(9.0, None, 16 / 9)
            w.close()
            meta, back = read_keypoints(path)
            self.assertAlmostEqual(meta["aspect"], 1.7778, places=3)
            self.assertEqual(len(back), len(frames) + 1)
            self.assertIsNone(back[-1][1])
            raw = open(path, encoding="utf-8").read()
            self.assertNotIn("image", raw)

    def test_rule_overrides(self):
        self.assertEqual(parse_set(["lying_angle=50", "confirm_hold=1.5"]), {"lying_angle": 50, "confirm_hold": 1.5})
        with self.assertRaises(SystemExit):
            parse_set(["no_such_rule=1"])
        self.assertEqual(load_rules(beds=["0.1,0.2,0.3,0.4"])["bed_zones"], [[0.1, 0.2, 0.3, 0.4]])


class FakePost:
    def __init__(self, fail_first=0):
        self.calls = []
        self.fail_first = fail_first
        self.lock = threading.Lock()

    def __call__(self, url, payload=None, timeout=2.0):
        with self.lock:
            if self.fail_first:
                self.fail_first -= 1
                raise OSError("connection refused")
            self.calls.append((url, payload))
        return {"ok": True, "state": "ASKING"}


class BridgeTest(unittest.TestCase):
    def test_run_reports_fall_once(self):
        post = FakePost()
        poster = BusPoster("http://bus", post=post, log=lambda *_: None)
        found = run(keypoint_source(synth.build("fall"), 4 / 3, realtime=False), FallDetector(), poster, "客厅",
                    log=lambda *_: None)
        poster.drain()
        self.assertEqual(len(found), 1)
        falls = [u for u, _ in post.calls if "/mock/fall" in u]
        self.assertEqual(falls, ["http://bus/mock/fall?room=%E5%AE%A2%E5%8E%85"])
        skel = [p for u, p in post.calls if u.endswith("/api/skeleton")]
        self.assertTrue(skel and set(skel[0]) == {"keypoints", "fallen"})

    def test_fall_event_survives_bus_outage(self):
        post = FakePost(fail_first=2)
        poster = BusPoster("http://bus", post=post, log=lambda *_: None)
        poster.fall("卧室")
        poster.drain(timeout=5)
        self.assertTrue(any("/mock/fall" in u for u, _ in post.calls), "总线恢复后跌倒事件仍要送达")


class LiveBusTest(unittest.TestCase):
    def test_synthetic_fall_reaches_state_machine(self):
        bus, clock, arm = make_bus()
        server = master_service.serve(bus, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            poster = BusPoster(base, log=lambda *_: None)
            frames = synth.build("fall")
            run(keypoint_source(frames, 4 / 3, realtime=False), FallDetector(), poster, "卧室", log=lambda *_: None)
            poster.drain()
            deadline = time.time() + 3
            while bus.fsm.state == "IDLE" and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(bus.fsm.state, "ASKING")
            self.assertEqual(bus.skeleton["pose"], "live")
        finally:
            server.stop_ticker.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
