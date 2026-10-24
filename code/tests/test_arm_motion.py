"""臂（Panthera-HT）：正运动学、物理安全（限位 / 限速 / 禁区 / 按压限力）、臂线程的排队与打断、
示教文件、Panthera 后端对 SDK 的调用、kit 路由映射、和主脑端到端。全部 mock，不碰硬件。"""
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import events, make_bus

from hand_controller import arm_safety as S
from hand_controller import arm_selftest, arm_service, voice_cmd
from hand_controller.arm_backend import REST, ArmError, MockArm, PantheraArm, make_backend
from hand_controller.arm_kinematics import GRIPPER_TCP, body_points, tool_position
from hand_controller.arm_teach import dump_skills, save_point
from hand_controller.poses import JOINTS, POSES
from hand_controller.rh56f2_driver import RH56F2
from master_service import ArmClient


def wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def cfg_with(**changes):
    cfg = S.load_config("gripper")
    cfg.update(changes)
    return cfg


def zone_around(p, r=0.03, name="测试禁区"):
    return {"name": name, "min": [c - r for c in p], "max": [c + r for c in p]}


class RecordingHand(RH56F2):
    """mock 手，记下每一次下发的开合度。"""

    def __init__(self, hand_id):
        super().__init__("", hand_id, mock=True)
        self.seen = []

    def move(self, joints, speed=None):
        self.seen.append(dict(joints))
        return super().move(joints, speed)


def fresh_hands():
    arm_service._HANDS.clear()
    hands = {h: RecordingHand(h) for h in (1, 2)}
    arm_service._HANDS.update({h: arm_service.HandWorker(h, d) for h, d in hands.items()})
    return hands


def skills_file(tmp, edit):
    """拷一份 arm_skills.json 到临时目录，edit(raw) 改完返回路径。"""
    with open(S.SKILLS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    edit(raw)
    path = os.path.join(tmp, "arm_skills.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump_skills(raw))
    return path


def point_q(name):
    return S.load_config("gripper")["points"][name]["q"]


# ---------- 正运动学 ----------

class KinematicsTest(unittest.TestCase):
    def test_zero_pose_is_urdf_offsets_summed(self):
        x, y, z = tool_position(REST)
        self.assertAlmostEqual(x, 0.0181985 - 0.26 + 0.23 + 0.07 + 0.02495 + GRIPPER_TCP, places=6)
        self.assertAlmostEqual(y, -0.000450862561756094, places=9)
        self.assertAlmostEqual(z, 0.0584 + 0.0535 + 0.06 + 0.034989137438241 - 0.0375, places=6)

    def test_joint1_turns_about_vertical_axis(self):
        x0, y0, z0 = tool_position(REST)
        x, y, z = tool_position([math.pi / 2, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(x, -y0, places=6)
        self.assertAlmostEqual(y, x0, places=6)
        self.assertAlmostEqual(z, z0, places=6)

    def test_joint2_lifts_upper_arm(self):
        """J2 绕 +y 转 90°：大臂（零位朝后 0.26 m）竖起来，肘到了肩的正上方。"""
        elbow = body_points([0, math.pi / 2, 0, 0, 0, 0])["elbow"]
        self.assertAlmostEqual(elbow[0], 0.0181985, places=5)
        self.assertAlmostEqual(elbow[2], 0.0584 + 0.0535 + 0.26, places=5)

    def test_wrong_joint_count(self):
        with self.assertRaises(ValueError):
            tool_position([0, 0, 0])


# ---------- 物理安全（纯函数） ----------

class SafetyTest(unittest.TestCase):
    def test_limits_are_the_user_mandated_ones(self):
        """和 X5 上 Follower.yaml 一致（说明书收紧 15%，J1 收紧 20%）。谁要放宽，先找硬件负责人。"""
        lo = [round(math.degrees(x)) for x in S.JOINT_LOWER]
        hi = [round(math.degrees(x)) for x in S.JOINT_UPPER]
        self.assertEqual(lo, [-144, 0, 0, -76, -76, -153])
        self.assertEqual(hi, [144, 170, 195, 76, 76, 153])

    def test_each_joint_out_of_range_rejected(self):
        for i in range(6):
            for bad in (S.JOINT_LOWER[i] - 0.01, S.JOINT_UPPER[i] + 0.01):
                q = [0.0] * 6
                q[i] = bad
                with self.assertRaises(S.SafetyError) as cm:
                    S.check_limits(q)
                self.assertIn("J%d" % (i + 1), str(cm.exception))

    def test_duration_caps_fastest_joint(self):
        cfg = cfg_with()
        sp = cfg["speed"]
        q1 = [1.2, 0, 0, 0, 0, 0]
        self.assertAlmostEqual(S.duration(REST, q1, cfg), 1.2 / sp["max_joint_vel"])
        self.assertAlmostEqual(S.duration(REST, q1, cfg, slow=True), 1.2 / sp["near_person_vel"])
        self.assertGreater(sp["max_joint_vel"], sp["near_person_vel"])
        self.assertLess(sp["max_joint_vel"], 1.0, "不能超过 SDK 配置的关节限速 1 rad/s")
        self.assertEqual(S.duration(REST, [0.01, 0, 0, 0, 0, 0], cfg), sp["min_seconds"])

    def test_keep_out_target_rejected(self):
        target = point_q("handover")
        cfg = cfg_with(keep_out=[zone_around(tool_position(target, GRIPPER_TCP), name="老人脸")])
        with self.assertRaises(S.SafetyError) as cm:
            S.check_segment(REST, target, cfg, "handover")
        self.assertIn("老人脸", str(cm.exception))

    def test_keep_out_checked_along_path_not_just_endpoints(self):
        """J1 从 -1 转到 +1：两头都在禁区外，中间扫过正前方的禁区。"""
        cfg = cfg_with(keep_out=[zone_around(tool_position(REST))])
        a, b = [-1.0, 0, 0, 0, 0, 0], [1.0, 0, 0, 0, 0, 0]
        S.check_pose(a, cfg)
        S.check_pose(b, cfg)
        with self.assertRaises(S.SafetyError):
            S.check_segment(a, b, cfg)

    def test_whole_body_checked_not_only_tool(self):
        """末端在禁区外，但肘进了禁区，也要拒绝。"""
        q = [0, math.pi / 2, 0, 0, 0, 0]
        cfg = cfg_with(floor_z=-1.0, keep_out=[zone_around(body_points(q)["elbow"])])
        with self.assertRaises(S.SafetyError) as cm:
            S.check_pose(q, cfg)
        self.assertIn("elbow", str(cm.exception))

    def test_path_through_table_rejected(self):
        """零位直接把大臂竖起来（J3 不动），半路小臂会扫到桌面：起点终点都合法也要拒绝。"""
        cfg = cfg_with()
        with self.assertRaises(S.SafetyError) as cm:
            S.check_segment(REST, [0, math.pi / 2, 0, 0, 0, 0], cfg)
        self.assertIn("桌面", str(cm.exception))

    def test_below_table_rejected(self):
        cfg = cfg_with(floor_z=tool_position(REST)[2] + 0.01)
        with self.assertRaises(S.SafetyError) as cm:
            S.check_pose(REST, cfg)
        self.assertIn("桌面", str(cm.exception))

    def test_untaught_points_refused_for_real_arm(self):
        with self.assertRaises(S.SafetyError) as cm:
            S.check_skill("press_sos", REST, cfg_with(), require_taught=True)
        self.assertIn("arm_teach", str(cm.exception))


class ShippedSkillsTest(unittest.TestCase):
    """arm_skills.json 本身：示教完改了点位，跑这一套就知道有没有点进了禁区、超了限位。"""

    def test_every_skill_is_a_pose_and_hand1_shape_exists(self):
        for name in S.skill_names():
            self.assertIn(name, POSES)
            self.assertEqual(POSES[name]["hand"], 1, "臂的动作都是 hand=1")
            self.assertIn(name, JOINTS, "末端是手#1 时 grip close 要用 JOINTS[%s]" % name)

    def test_master_sends_only_known_arm_skills(self):
        # return_medicine 只从 T5AI 大屏（还药键 / 语音）来，主脑不发
        self.assertEqual(S.skill_names(), {"home", "deliver_medicine", "return_medicine", "press_sos",
                                           "sign_family_received"})

    def test_all_skills_safe_from_rest_and_after_each_other(self):
        for effector in ("gripper", "hand1"):
            cfg = S.load_config(effector)
            for a in cfg["skills"]:
                end = S.check_skill(a, REST, cfg)
                for b in cfg["skills"]:
                    S.check_skill(b, end, cfg)

    def test_rest_is_zero_and_taught(self):
        rest = S.load_config("gripper")["points"]["rest"]
        self.assertEqual(rest["q"], REST)
        self.assertTrue(rest["taught"])

    def test_press_uses_reduced_torque(self):
        cfg = S.load_config("gripper")
        self.assertLessEqual(cfg["points"]["sos_press"].get("torque_scale", 1.0), 0.5)

    def test_points_reaching_the_person_are_slow(self):
        cfg = S.load_config("gripper")
        for name in ("handover", "show_sign"):
            self.assertTrue(cfg["points"][name].get("near_person"), name)

    def test_skills_end_at_rest_except_sign(self):
        cfg = S.load_config("gripper")
        for name in ("home", "deliver_medicine", "return_medicine", "press_sos"):
            self.assertEqual(S.skill_moves(name, cfg)[-1], "rest", name)


class SelftestFileTest(unittest.TestCase):
    """arm_selftest.json：上电后、示教前在真臂上跑的小角度动作。"""

    PATH = os.path.join(os.path.dirname(S.SKILLS_FILE), "arm_selftest.json")

    def test_same_action_names_as_main_file(self):
        self.assertEqual(S.skill_names(self.PATH), S.skill_names())

    def test_every_point_small_and_taught(self):
        for name, p in S.load_config("gripper", self.PATH)["points"].items():
            self.assertTrue(p["taught"], name)
            self.assertLessEqual(max(map(abs, p["q"])), math.radians(20) + 1e-3, "%s 离 rest 超过 20°" % name)

    def test_slower_than_main_file_and_safe_in_any_order(self):
        for effector in ("gripper", "hand1"):
            cfg = S.load_config(effector, self.PATH)
            self.assertLessEqual(cfg["speed"]["max_joint_vel"], S.load_config(effector)["speed"]["max_joint_vel"])
            for a in cfg["skills"]:
                end = S.check_skill(a, REST, cfg, require_taught=True)
                for b in cfg["skills"]:
                    S.check_skill(b, end, cfg, require_taught=True)

    def test_canonical_format(self):
        with open(self.PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(dump_skills(json.loads(text)), text)

    def test_real_arm_accepts_selftest_without_teaching(self):
        b = MockArm(time_scale=0)
        b.mock = False
        w = arm_service.ArmWorker(b, effector="gripper", skills_path=self.PATH)
        self.addCleanup(w.stop)
        b.has_gripper = True
        self.assertEqual(w.request("press_sos"), (200, ""))
        self.assertTrue(w.wait_idle())
        self.assertEqual(b.moves[-1][0], REST)


# ---------- 示教文件 ----------

class TeachFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_shipped_file_is_in_canonical_format(self):
        with open(S.SKILLS_FILE, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(dump_skills(json.loads(text)), text,
                         "arm_skills.json 要保持 arm_teach.dump_skills 的格式，示教时的 diff 才只有一行")

    def test_save_point_changes_one_line_and_keeps_fields(self):
        path = os.path.join(self.tmp, "arm_skills.json")
        shutil.copy(S.SKILLS_FILE, path)
        with open(path, encoding="utf-8") as f:
            before = f.read().splitlines()
        p = save_point("sos_press", [0.1, 1.6, 1.0, -0.6, -0.9, 0.01234567], path)
        self.assertTrue(p["taught"])
        self.assertEqual(p["torque_scale"], 0.3, "示教不能丢掉按压限力")
        self.assertEqual(p["q"][-1], 0.0123)
        with open(path, encoding="utf-8") as f:
            after = f.read().splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
        cfg = S.load_config("gripper", path)
        self.assertTrue(cfg["points"]["sos_press"]["taught"])

    def test_save_new_point(self):
        path = os.path.join(self.tmp, "arm_skills.json")
        shutil.copy(S.SKILLS_FILE, path)
        save_point("water_cup", [0.2] * 6, path)
        self.assertEqual(S.load_config("hand1", path)["points"]["water_cup"]["q"], [0.2] * 6)


# ---------- 臂线程 ----------

class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.hands = fresh_hands()
        self.addCleanup(arm_service._HANDS.clear)

    def worker(self, gate=False, effector=None, skills_path=None, **kw):
        b = MockArm(time_scale=0, **kw)
        if gate:
            b.gate = threading.Event()
            self.addCleanup(b.gate.set)
        w = arm_service.ArmWorker(b, effector=effector, skills_path=skills_path)
        self.addCleanup(w.stop)
        return w, b

    def targets(self, b, finished=True):
        return [m[0] for m in b.moves if m[3] == finished]

    def test_deliver_runs_waypoints_in_order(self):
        w, b = self.worker()
        self.assertEqual(w.request("deliver_medicine"), (200, ""))
        self.assertTrue(w.wait_idle())
        self.assertEqual(self.targets(b), [point_q(n) for n in
                                           ("above_tray", "pick_med", "above_tray", "handover", "rest")])
        self.assertEqual((w.done, w.aborted, w.at), (1, 0, "rest"))

    def test_moves_respect_speed_limit(self):
        w, b = self.worker()
        w.request("deliver_medicine")
        self.assertTrue(w.wait_idle())
        sp = w.cfg["speed"]
        q = REST
        for target, secs, _, _ in b.moves:
            span = max(abs(x - y) for x, y in zip(q, target))
            self.assertGreaterEqual(secs + 1e-9, span / sp["max_joint_vel"])
            self.assertGreaterEqual(secs, sp["min_seconds"])
            if target == point_q("handover"):
                self.assertGreaterEqual(secs + 1e-9, span / sp["near_person_vel"], "递到老人手边要慢")
            q = target

    def test_first_move_after_start_is_slow(self):
        """刚开机 / 被打断后不知道臂在哪，第一段按靠近老人的限速走。"""
        w, b = self.worker()
        w.request("home")
        self.assertTrue(w.wait_idle())
        w.request("press_sos")
        self.assertTrue(w.wait_idle())
        sp = w.cfg["speed"]
        target, secs, _, _ = b.moves[1]   # home 之后的第一段：rest → above_sos，at = rest，正常速
        self.assertAlmostEqual(secs, max(sp["min_seconds"], max(map(abs, target)) / sp["max_joint_vel"]))
        w2, b2 = self.worker()
        w2.request("press_sos")
        self.assertTrue(w2.wait_idle())
        target, secs, _, _ = b2.moves[0]
        self.assertAlmostEqual(secs, max(sp["min_seconds"], max(map(abs, target)) / sp["near_person_vel"]))

    def test_press_uses_reduced_torque(self):
        w, b = self.worker()
        w.request("press_sos")
        self.assertTrue(w.wait_idle())
        torque = {tuple(t): ts for t, _, ts, _ in b.moves}
        self.assertEqual(torque[tuple(point_q("sos_press"))], 0.3)
        self.assertEqual(torque[tuple(point_q("above_sos"))], 1.0)

    def test_safety_preempts_deliver(self):
        w, b = self.worker(gate=True)
        self.assertEqual(w.request("deliver_medicine")[0], 200)
        self.assertTrue(wait_until(lambda: w.running == "deliver_medicine"))
        self.assertEqual(w.request("press_sos")[0], 200)
        self.assertTrue(wait_until(lambda: w.running == "press_sos"), w.status())
        b.gate.set()
        self.assertTrue(w.wait_idle())
        self.assertEqual(w.aborted, 1)
        self.assertGreaterEqual(b.holds, 1, "被打断要停住保持")
        self.assertEqual(self.targets(b, finished=False), [point_q("above_tray")])
        self.assertEqual(self.targets(b)[-1], point_q("rest"))
        self.assertNotIn(point_q("handover"), self.targets(b), "递药被打断后不能接着做")

    def test_sos_sequence_queues_behind_each_other(self):
        w, b = self.worker(gate=True)
        self.assertEqual(w.request("press_sos")[0], 200)
        self.assertEqual(w.request("sign_family_received")[0], 200)
        b.gate.set()
        self.assertTrue(w.wait_idle())
        self.assertEqual(self.targets(b)[-1], point_q("show_sign"))
        self.assertEqual((w.done, w.aborted), (2, 0))

    def test_busy_rules(self):
        w, b = self.worker(gate=True)
        w.request("press_sos")
        self.assertTrue(wait_until(lambda: w.running == "press_sos"))
        code, why = w.request("press_sos")
        self.assertEqual(code, 409)
        self.assertIn("已经在做", why)
        self.assertEqual(w.request("sign_family_received")[0], 200)
        self.assertEqual(w.request("home")[0], 200, "保命动作做完再回家，可以排")
        code, why = w.request("deliver_medicine")
        self.assertEqual(code, 409)
        self.assertIn("排着", why)

    def test_safety_drops_queued_normal_actions(self):
        w, b = self.worker(gate=True)
        w.request("home")
        self.assertTrue(wait_until(lambda: w.running == "home"))
        w.request("deliver_medicine")
        w.request("press_sos")
        b.gate.set()
        self.assertTrue(w.wait_idle())
        self.assertNotIn(point_q("pick_med"), self.targets(b), "排着的递药应被保命动作作废")

    def test_stop_aborts_holds_and_drops_queue(self):
        w, b = self.worker(gate=True)
        w.request("deliver_medicine")
        self.assertTrue(wait_until(lambda: w.running == "deliver_medicine"))
        w.request("home")
        w.stop()
        self.assertTrue(w.wait_idle(1.0), "急停后臂线程要马上空下来")
        self.assertEqual(w.aborted, 1)
        self.assertGreaterEqual(b.holds, 1)
        self.assertEqual(self.targets(b), [], "排着的 home 不能在急停后补做")
        self.assertIsNone(w.at)
        b.gate.set()
        self.assertEqual(w.request("home")[0], 200, "急停之后照常接收")
        self.assertTrue(w.wait_idle())

    def test_keep_out_violation_rejected_before_moving(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        handover = tool_position(point_q("handover"), GRIPPER_TCP)
        path = skills_file(tmp, lambda raw: raw["keep_out"].append(zone_around(handover, name="老人脸")))
        w, b = self.worker(effector="gripper", skills_path=path, has_gripper=True)
        code, why = w.request("deliver_medicine")
        self.assertEqual(code, 422)
        self.assertIn("老人脸", why)
        self.assertEqual(b.moves, [])
        self.assertEqual(w.request("press_sos")[0], 200, "别的动作不受影响")

    def test_execution_rechecks_from_actual_position(self):
        """预检之后臂被推到了别处：执行时按实际位置再查，查不过就停住、记错，不硬走。"""
        pushed = [-1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        mid = [(a + b) / 2 for a, b in zip(pushed, point_q("above_sos"))]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = skills_file(tmp, lambda raw: raw["keep_out"].append(
            zone_around(body_points(mid, S.load_config("hand1")["tool_length"])["tool"], 0.02)))
        w, b = self.worker(skills_path=path)
        hand_block = threading.Event()
        self.addCleanup(hand_block.set)
        real_move = self.hands[1].move
        self.hands[1].move = lambda joints, speed=None: (hand_block.wait(2.0), real_move(joints, speed))[1]
        self.assertEqual(w.request("press_sos"), (200, ""), "从 rest 出发是安全的")
        self.assertTrue(wait_until(lambda: arm_service.hands()[1].status()["busy"]), "卡在第一步 grip")
        b.q = list(pushed)
        hand_block.set()
        self.assertTrue(w.wait_idle())
        self.assertEqual(w.aborted, 1)
        self.assertIn("禁区", w.last_error)
        self.assertEqual(self.targets(b), [])
        self.assertGreaterEqual(b.holds, 1)

    def test_untaught_points_refused_on_real_arm(self):
        w, b = self.worker()
        b.mock = False
        code, why = w.request("press_sos")
        self.assertEqual(code, 422)
        self.assertIn("示教", why)
        self.assertEqual(w.request("home")[0], 200, "rest 示教过，回家可以")

    def test_tripped_arm_refuses_with_503(self):
        w, b = self.worker()
        b.tripped = "电机层越界锁定"
        code, why = w.request("press_sos")
        self.assertEqual(code, 503)
        self.assertIn("越界锁定", why)

    def test_gripper_effector(self):
        w, b = self.worker(has_gripper=True)
        w.request("deliver_medicine")
        self.assertTrue(w.wait_idle())
        self.assertEqual(w.effector, "gripper")
        self.assertEqual(b.grips, [False, True, False])
        self.assertEqual(self.hands[1].seen, [], "末端是夹爪时不碰手#1")

    def test_hand1_effector_uses_pose_shapes(self):
        w, b = self.worker()
        w.request("deliver_medicine")
        self.assertTrue(w.wait_idle())
        self.assertEqual(w.effector, "hand1")
        self.assertEqual(self.hands[1].seen, [JOINTS["release"], JOINTS["deliver_medicine"], JOINTS["release"]])
        self.assertEqual(w.cfg["tool_length"], S.load_config("hand1")["tool_length"])

    def test_hand1_effector_needs_ready_hand(self):
        arm_service._HANDS[1] = arm_service.HandWorker(1, RH56F2("", 1, mock=False))
        w, b = self.worker()
        code, why = w.request("press_sos")
        self.assertEqual(code, 503)
        self.assertIn("手#1", why)

    def test_status_fields(self):
        w, b = self.worker()
        w.request("home")
        st = w.status()
        for field in ("backend", "mock", "effector", "busy", "queued", "done", "aborted", "joints_deg", "tool_m",
                      "untaught", "last_error", "settle"):
            self.assertIn(field, st)
        self.assertTrue(w.wait_idle())
        self.assertEqual(w.status()["settle"], {"point": "rest", "err_deg": [0.0] * 6})
        self.assertIn("sos_press", st["untaught"])
        self.assertNotIn("rest", st["untaught"])


# ---------- 退出前回 rest ----------

class InterruptedArm(MockArm):
    """回 rest 的路上被第二次 Ctrl+C 打断。"""

    def move(self, q, seconds, abort, torque_scale=1.0):
        raise KeyboardInterrupt


class ParkTest(unittest.TestCase):
    def setUp(self):
        fresh_hands()
        self.addCleanup(arm_service._HANDS.clear)
        self.lines = []

    def worker(self, backend, skills_path=None):
        w = arm_service.ArmWorker(backend, skills_path=skills_path)
        self.addCleanup(w.stop)
        return w

    def test_returns_to_rest_slowly(self):
        start = point_q("above_sos")
        b = MockArm(time_scale=0, q=start)
        w = self.worker(b)
        self.assertTrue(arm_service.park(w, out=self.lines.append))
        target, secs, _, finished = b.moves[-1]
        self.assertEqual((target, finished), (REST, True))
        self.assertAlmostEqual(secs, S.duration(start, REST, w.cfg, slow=True))
        self.assertIn("回 rest", self.lines[0])

    def test_already_at_rest_does_not_move(self):
        b = MockArm(time_scale=0, q=[0.01] * 6)
        self.assertTrue(arm_service.park(self.worker(b), out=self.lines.append))
        self.assertEqual(b.moves, [])

    def test_unsafe_path_stays_put(self):
        start = [-1.0, 0, 0, 0, 0, 0]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = skills_file(tmp, lambda raw: raw["keep_out"].append(
            zone_around(tool_position([-0.5, 0, 0, 0, 0, 0], S.load_config("hand1")["tool_length"]))))
        # 直走会穿过禁区：分段绕开（先抬起 / 分组归位），每一段都要过 check_segment
        b = MockArm(time_scale=0, q=start)
        w = self.worker(b, path)
        self.assertTrue(arm_service.park(w, out=self.lines.append))
        self.assertGreater(len(b.moves), 1)
        cur = start
        for target, *_ in b.moves:
            S.check_segment(cur, target, w.cfg)
            cur = target
        # 禁区罩住 rest 本身：哪条路都到不了，原地不动
        path2 = skills_file(tmp, lambda raw: raw["keep_out"].append(
            zone_around(tool_position([0, 0, 0, 0, 0, 0], S.load_config("hand1")["tool_length"]))))
        b2 = MockArm(time_scale=0, q=start)
        self.lines.clear()
        self.assertFalse(arm_service.park(self.worker(b2, path2), out=self.lines.append))
        self.assertEqual(b2.moves, [])
        self.assertIn("找不到安全的回 rest 路线", self.lines[0])

    def test_second_ctrl_c_holds(self):
        b = InterruptedArm(time_scale=0, q=point_q("above_sos"))
        self.assertFalse(arm_service.park(self.worker(b), out=self.lines.append))
        self.assertEqual(b.holds, 1)
        self.assertIn("停住", self.lines[-1])

    def test_locked_arm_not_moved(self):
        b = MockArm(time_scale=0, q=point_q("above_sos"))
        b.tripped = "电机层越界锁定"
        self.assertFalse(arm_service.park(self.worker(b), out=self.lines.append))
        self.assertEqual(b.moves, [])


# ---------- 后端 ----------

class FakeRobot:
    """只实现 PantheraArm 用到的那几个 SDK 方法，盯调用方式。"""

    motor_count = 6
    max_torque = [21.0, 36.0, 36.0, 21.0, 10.0, 10.0]
    joint_limits = {"lower": list(S.JOINT_LOWER), "upper": list(S.JOINT_UPPER)}

    def __init__(self, reach_after=2, accept=True, errors=None):
        self.has_gripper = True
        self._hard_limit_tripped = False
        self.pos = [0.0] * 6
        self.calls = []
        self.reach_after = reach_after
        self.accept = accept
        self.errors = errors or (lambda n: [0.2] * 6)   # 第 n 次查到位时各关节的偏差
        self.checks = 0

    def send_get_motor_state_cmd(self):
        self.calls.append("query")

    def motor_send_cmd(self):
        self.calls.append("send")

    def get_current_pos(self):
        return list(self.pos)

    def moveJ(self, pos, duration, max_tqu=None, iswait=False):
        self.calls.append(("moveJ", list(pos), duration, list(max_tqu), iswait))
        self.target = list(pos)
        return self.accept

    def check_position_reached(self, target, tolerance=0.1):
        self.checks += 1
        if self.checks >= self.reach_after:
            self.pos = list(target)
            return True, [0.0] * 6
        errors = self.errors(self.checks)
        return all(e <= tolerance for e in errors), errors

    def Joint_Pos_Vel(self, pos, vel, max_tqu=None, iswait=False):
        """和 SDK 一样：目标超出第 1 层限位就拒绝（返回 False），什么都不发。"""
        if any(not lo <= x <= hi for x, lo, hi in zip(pos, self.joint_limits["lower"], self.joint_limits["upper"])):
            return False
        self.calls.append(("hold", list(pos), list(vel)))
        return True

    def set_brake(self):
        self.calls.append("brake")

    def gripper_open(self):
        self.calls.append("open")

    def gripper_close(self):
        self.calls.append("close")


class PantheraBackendTest(unittest.TestCase):
    def backend(self, **kw):
        b = PantheraArm(scripts_dir="unused")
        b.robot = FakeRobot(**kw)
        return b

    def test_move_calls_moveJ_non_blocking_with_scaled_torque(self):
        b = self.backend()
        self.assertTrue(b.move([0.1] * 6, 2.0, threading.Event(), torque_scale=0.3))
        name, pos, secs, tqu, iswait = b.robot.calls[0]
        self.assertEqual((name, pos, secs, iswait), ("moveJ", [0.1] * 6, 2.0, False))
        self.assertEqual([round(t, 3) for t in tqu], [6.3, 10.8, 10.8, 6.3, 3.0, 3.0])

    def test_sdk_rejection_is_an_error(self):
        b = self.backend(accept=False)
        with self.assertRaises(ArmError):
            b.move([0.1] * 6, 1.0, threading.Event())

    def test_abort_returns_false_without_waiting(self):
        b = self.backend(reach_after=10 ** 9)
        abort = threading.Event()
        abort.set()
        started = time.monotonic()
        self.assertFalse(b.move([0.1] * 6, 5.0, abort))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_limit_trip_during_move(self):
        b = self.backend(reach_after=10 ** 9)
        b.robot._hard_limit_tripped = True
        with self.assertRaises(ArmError):
            b.move([0.1] * 6, 1.0, threading.Event())
        self.assertFalse(b.ready()[0])

    def test_blocked_arm_times_out(self):
        b = self.backend(reach_after=10 ** 9)
        with mock.patch("hand_controller.arm_backend.SETTLE", 0.05):
            with self.assertRaises(ArmError) as cm:
                b.move([0.1] * 6, 0.05, threading.Event())
        self.assertIn("挡住", str(cm.exception))

    def test_settled_gravity_sag_counts_as_reached(self):
        """实测 J3 抬 15° 停在差 2.5° 的地方不动了：停稳、差得不多就算到，偏差记下来，不等满 SETTLE。"""
        sag = [0.0, 0.02, 0.06, 0.1, 0.0, 0.0]
        b = self.backend(reach_after=10 ** 9, errors=lambda n: sag)
        started = time.monotonic()
        with mock.patch("hand_controller.arm_backend.STILL_SECONDS", 0.05):
            self.assertTrue(b.move([0.1] * 6, 0.05, threading.Event()))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(b.last_errors, sag)

    def test_still_moving_is_not_settled(self):
        """偏差还在一点点变小（还在走）就接着等，到容差内才算到。"""
        b = self.backend(reach_after=10 ** 9, errors=lambda n: [max(0.0, 0.14 - 0.005 * n)] + [0.0] * 5)
        with mock.patch("hand_controller.arm_backend.STILL_SECONDS", 0.05):
            self.assertTrue(b.move([0.1] * 6, 0.01, threading.Event()))
        self.assertLessEqual(max(b.last_errors), 0.08)

    def test_large_settled_error_is_blocked(self):
        b = self.backend(reach_after=10 ** 9, errors=lambda n: [0.0, 0.0, 0.3, 0.0, 0.0, 0.0])
        with mock.patch("hand_controller.arm_backend.SETTLE", 0.2), \
                mock.patch("hand_controller.arm_backend.STILL_SECONDS", 0.05):
            with self.assertRaises(ArmError) as cm:
                b.move([0.1] * 6, 0.05, threading.Event())
        self.assertIn("17.2°", str(cm.exception))

    def test_hold_targets_current_position(self):
        b = self.backend()
        b.robot.pos = [0.3] * 6
        b.hold()
        self.assertEqual(b.robot.calls[-1][:2], ("hold", [0.3] * 6))

    def test_hold_at_rest_with_slightly_negative_reading(self):
        """收拢时 J2 读到 -0.04°：夹到 0 再保持，不能被 SDK 当越界拒掉（那样 /stop 就没停住）。"""
        b = self.backend()
        b.robot.pos = [0.0, math.radians(-0.04), 0.0005, 0.0, 0.0, 0.0]
        b.hold()
        self.assertEqual(b.robot.calls[-1][:2], ("hold", [0.0, 0.0, 0.0005, 0.0, 0.0, 0.0]))

    def test_hold_refuses_real_limit_violation(self):
        b = self.backend()
        b.robot.pos = [0.0, math.radians(-3), 0.0, 0.0, 0.0, 0.0]
        with self.assertRaises(ArmError) as cm:
            b.hold()
        self.assertIn("超出关节限位", str(cm.exception))
        self.assertFalse([c for c in b.robot.calls if c[0] == "hold"])

    def test_hold_rejected_by_sdk_is_an_error(self):
        b = self.backend()
        b.robot.Joint_Pos_Vel = lambda pos, vel, max_tqu=None, iswait=False: False
        with self.assertRaises(ArmError):
            b.hold()

    def test_sdk_never_entered_from_two_threads(self):
        """臂线程在走（轮询到位）、HTTP 线程在查 /status（读关节）：SDK 调用必须一个一个来。"""
        b = self.backend(reach_after=5)
        r = b.robot
        state = {"inside": 0, "overlap": 0}

        def guarded(fn):
            def call(*a, **kw):
                state["inside"] += 1
                if state["inside"] > 1:
                    state["overlap"] += 1
                time.sleep(0.003)
                try:
                    return fn(*a, **kw)
                finally:
                    state["inside"] -= 1
            return call

        for name in ("send_get_motor_state_cmd", "motor_send_cmd", "get_current_pos", "moveJ",
                     "check_position_reached", "Joint_Pos_Vel"):
            setattr(r, name, guarded(getattr(r, name)))
        stop = threading.Event()

        def poll_status():   # 比主脑（每 2 秒一次）密得多，但每次之间歇一下，不至于把锁抢光
            while not stop.is_set():
                b.joints()
                time.sleep(0.002)
        t = threading.Thread(target=poll_status)
        t.start()
        try:
            self.assertTrue(b.move([0.1] * 6, 0.01, threading.Event()))
            b.hold()
        finally:
            stop.set()
            t.join()
        self.assertEqual(state["overlap"], 0)

    def test_grip_and_brake(self):
        b = self.backend()
        b.grip(True)
        b.grip(False)
        b.brake()
        self.assertEqual(b.robot.calls[-3:], ["close", "open", "brake"])
        b.robot.has_gripper = False
        with self.assertRaises(ArmError):
            b.grip(True)

    def test_missing_sdk_reports_why(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        b = PantheraArm(scripts_dir=tmp)
        self.addCleanup(lambda: tmp in sys.path and sys.path.remove(tmp))
        ok, why = b.connect()
        self.assertFalse(ok)
        self.assertIn("连不上臂", why)
        w = arm_service.ArmWorker(b)
        self.addCleanup(w.stop)
        code, why = w.request("press_sos")
        self.assertEqual(code, 503)
        self.assertFalse(w.status()["connected"])

    def test_make_backend(self):
        self.assertIsInstance(make_backend("mock"), MockArm)
        self.assertIsInstance(make_backend("panthera"), PantheraArm)
        with self.assertRaises(ValueError):
            make_backend("ur5")


# ---------- HTTP：主脑的接口 + kit 的写法 ----------

def start_service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), arm_service.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
    return "http://127.0.0.1:%d" % server.server_address[1], stop


def post_body(url, path, data):
    req = urllib.request.Request(url + path, data=data, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def call(url, path, method="POST"):
    req = urllib.request.Request(url + path, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.hands = fresh_hands()
        arm_service.HISTORY.clear()
        self.mock = MockArm(time_scale=0)
        self.arm = arm_service.use_arm(arm_service.ArmWorker(self.mock))
        self.url, stop = start_service()
        self.addCleanup(stop)

    def tearDown(self):
        if self.mock.gate:
            self.mock.gate.set()
        self.arm.stop()
        self.arm.wait_idle(1.0)
        for w in arm_service._HANDS.values():
            w.stop()
        arm_service._HANDS.clear()
        arm_service.HISTORY.clear()

    def test_arm_pose_answers_at_once(self):
        self.mock.gate = threading.Event()
        started = time.monotonic()
        code, body = call(self.url, "/pose?name=deliver_medicine&hand=1")
        self.assertLess(time.monotonic() - started, 0.5, "主脑 1.5 秒就算超时，收到就得回")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["mock"])
        self.assertTrue(wait_until(lambda: self.arm.running == "deliver_medicine"))
        self.assertEqual(arm_service.HISTORY[-1]["pose"], "deliver_medicine")

    def test_rejections_come_back_with_reason(self):
        self.mock.gate = threading.Event()
        call(self.url, "/pose?name=deliver_medicine&hand=1")
        code, body = call(self.url, "/pose?name=deliver_medicine&hand=1")
        self.assertEqual(code, 409)
        self.assertIn("已经在做", body["error"])
        code, body = call(self.url, "/pose?name=finger_fist&hand=1")
        self.assertEqual(code, 409, "手#1 在臂上，臂忙的时候不接别的手势")
        self.assertIn("手#1", body["error"])
        code, body = call(self.url, "/pose?name=gentle_hold&hand=2")
        self.assertEqual(code, 200, "手#2 不受臂影响")

    def test_stop_endpoint_stops_arm(self):
        self.mock.gate = threading.Event()
        call(self.url, "/pose?name=deliver_medicine&hand=1")
        self.assertTrue(wait_until(lambda: self.arm.running == "deliver_medicine"))
        code, body = call(self.url, "/stop")
        self.assertEqual(code, 200, body)
        self.assertTrue(self.arm.wait_idle(1.0))
        self.assertEqual(self.arm.aborted, 1)
        self.assertGreaterEqual(self.mock.holds, 1)

    def test_status_includes_arm(self):
        code, body = call(self.url, "/status", "GET")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["mock"])
        self.assertEqual(body["arm"]["backend"], "mock")
        self.assertIn("joints_deg", body["arm"])
        self.assertEqual(set(body["hands"]), {"1", "2"})
        self.assertIn("loaded", body["voice"])

    def test_hand_only_poses_still_go_to_hands(self):
        code, body = call(self.url, "/pose?name=finger_fist&hand=1")
        self.assertEqual(code, 200, body)
        arm_service.hands()[1].drain()
        self.assertEqual(self.hands[1].seen[-1], JOINTS["finger_fist"])
        self.assertEqual(self.mock.moves, [])

    # ---- caremate_hand2_kit 的写法 ----

    def test_kit_comfort_rhythm_defaults_to_hand2(self):
        code, body = call(self.url, "/pose/comfort_rhythm")
        self.assertEqual(code, 200, body)
        self.assertEqual((body["pose"], body["hand"], body["alias_of"]), ("gentle_hold", 2, "comfort_rhythm"))
        call(self.url, "/stop")

    def test_kit_hand_names(self):
        for kit, hand, pose in (("thumbs_up?hand=2", 2, "thumbs_up"), ("rock", 1, "rps_rock"),
                                ("fingers_3?hand=1", 1, "finger_count_3"), ("hold_card?hand=2", 2, "sign_coming"),
                                ("release?hand=2", 2, "release")):
            code, body = call(self.url, "/pose/" + kit)
            self.assertEqual(code, 200, (kit, body))
            self.assertEqual((body["pose"], body["hand"]), (pose, hand), kit)
        self.assertEqual(self.mock.moves, [], "/pose/<名字> 只动手")

    def test_kit_pose_path_never_moves_arm(self):
        for kit in ("press_sos?hand=1", "home", "deliver_medicine"):
            code, body = call(self.url, "/pose/" + kit)
            self.assertEqual(code, 400, (kit, body))
            self.assertIn("会动臂", body["error"])
        self.assertEqual(self.mock.moves, [])

    def test_kit_unknown_names(self):
        code, body = call(self.url, "/pose/no_such_pose")
        self.assertEqual(code, 200, "kit 的约定：未知名字回 200 + ok:false")
        self.assertFalse(body["ok"])
        self.assertIn("no_such_pose", body["error"])
        code, _ = call(self.url, "/handover_item")
        self.assertEqual(code, 404)
        self.assertEqual(arm_service.HISTORY, [])

    def test_kit_sequences_and_card_route(self):
        for path, pose, hand in (("/pose/wave", "wave", 1), ("/wave", "wave", 1), ("/card_up", "sign_coming", 2)):
            code, body = call(self.url, path)
            self.assertEqual(code, 200, (path, body))
            self.assertEqual((body["pose"], body["hand"]), (pose, hand), path)
            arm_service.hands()[hand].drain()
        self.assertEqual(self.mock.moves, [], "挥手、举牌（手#2）都只动手")

    def test_kit_routes_map_to_arm_skills(self):
        for route, pose in (("/press_sos", "press_sos"), ("/handover_med", "deliver_medicine"),
                            ("/return_med", "return_medicine")):
            code, body = call(self.url, route)
            self.assertEqual(code, 200, (route, body))
            self.assertEqual((body["pose"], body["alias_of"]), (pose, route))
            self.assertTrue(self.arm.wait_idle())
        # 10-25 起旧大屏的「归位」/reset 改成零位待机点（手张开、全部关节回 0°）
        code, body = call(self.url, "/reset")
        self.assertEqual((code, body["pose"]), (200, "teach_start"), body)
        self.assertTrue(self.arm.wait_idle())
        self.assertEqual(self.mock.moves[-1][0], REST)
        code, body = call(self.url, "/thumbs")
        self.assertEqual((code, body["pose"]), (200, "thumbs_up"))

    # ---- T5AI 大屏的语音键 /voice_cmd（识别换成假的，只验关键词 → 动作这条路） ----

    def voice(self, heard, body=b"\0" * 16000, unk=0.0):
        with mock.patch.object(voice_cmd, "recognize", return_value=(heard, unk)):
            return post_body(self.url, "/voice_cmd", body)

    def test_voice_moves_arm_like_the_button(self):
        code, body = self.voice("递药")
        self.assertEqual(code, 200, body)
        self.assertEqual((body["pose"], body["hand"], body["keyword"]), ("deliver_medicine", 1, "递药"))
        self.assertTrue(self.arm.wait_idle())
        self.assertEqual(self.mock.moves[-1][0], REST)
        self.assertEqual(arm_service.HISTORY[-1]["requested"], "voice:递药")
        code, body = self.voice("帮我还药")
        self.assertEqual((code, body["pose"]), (200, "return_medicine"))

    def test_voice_hand_gestures_do_not_move_arm(self):
        for heard in ("猜拳", "挥手", "点赞", "手指操"):
            code, body = self.voice(heard)
            self.assertEqual(code, 200, (heard, body))
            self.assertEqual(body["hand"], 1)
            arm_service.hands()[1].drain()
        self.assertIn(arm_service.HISTORY[0]["pose"], ("rps_rock", "rps_scissors", "rps_paper"))
        self.assertEqual(self.mock.moves, [])

    def test_voice_longest_keyword_wins(self):
        self.assertEqual(voice_cmd.match("按铃铛"), "按铃铛")
        self.assertEqual(voice_cmd.match("做手指操"), "手指操")
        self.assertEqual(voice_cmd.match("猜拳"), "猜拳")
        self.assertIsNone(voice_cmd.match("今天天气"))

    def test_voice_grammar_splits_words_missing_from_model(self):
        vocab = {"药盒", "手指", "操", "按", "铃", "递", "药"}
        known = vocab.__contains__
        self.assertEqual(voice_cmd.segment("递药", known), "递 药", "小模型没有「递药」这个词")
        self.assertEqual(voice_cmd.segment("药盒", known), "药盒")
        self.assertEqual(voice_cmd.segment("手指操", known), "手指 操")
        self.assertIsNone(voice_cmd.segment("按铃铛", known))
        g = voice_cmd.grammar(known)
        self.assertIn("按 铃", g)
        self.assertNotIn("按铃铛", " ".join(g))
        self.assertEqual(g[-1], "[unk]")
        self.assertEqual(voice_cmd.match("递 药".replace(" ", "")), "递药")

    def test_voice_not_understood_does_nothing(self):
        for heard, why in (("", "没听清"), ("今天天气", "没听懂")):
            code, body = self.voice(heard)
            self.assertEqual(code, 200, body)
            self.assertFalse(body["ok"])
            self.assertIn(why, body["error"])
        # X5 实测：「今天天气不错」被 grammar 硬凑成「紧急」，[unk] 占 0.68；指令最多 0.41（「我们来猜拳」）
        code, body = self.voice("紧急", unk=0.68)
        self.assertEqual((code, body["ok"]), (200, False), body)
        self.assertIn("大半没听懂", body["error"])
        self.assertEqual(arm_service.HISTORY, [])
        self.assertEqual(self.mock.moves, [])
        code, body = self.voice("猜拳", unk=0.41)
        self.assertTrue(body["ok"], body)

    def test_voice_rejects_short_audio_and_reports_missing_asr(self):
        code, body = self.voice("递药", b"\0" * 100)
        self.assertEqual(code, 400, body)
        with mock.patch.object(voice_cmd, "recognize", side_effect=voice_cmd.VoiceError("没有模型目录")):
            code, body = post_body(self.url, "/voice_cmd", b"\0" * 16000)
        self.assertEqual(code, 503, body)
        self.assertIn("没有模型目录", body["error"])
        self.assertEqual(arm_service.HISTORY, [])

    def test_voice_accepts_wav(self):
        pcm = bytes(range(256)) * 64
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        self.assertEqual(voice_cmd.strip_wav(buf.getvalue()), pcm)
        self.assertEqual(voice_cmd.strip_wav(pcm), pcm)

    # ---- 真臂自检脚本（这里用 mock 臂按真实节奏的 1/10 跑） ----

    def selftest_worker(self, skills_path):
        self.mock = MockArm(time_scale=0.1, has_gripper=True)
        self.arm = arm_service.use_arm(arm_service.ArmWorker(self.mock, effector="gripper", skills_path=skills_path))
        self.assertEqual(self.arm.ready()[0], 200)   # arm_service 起来时就会 ready() 一次

    def test_selftest_driver_runs_through(self):
        self.selftest_worker(SelftestFileTest.PATH)
        lines, prompts = [], []
        self.assertTrue(arm_selftest.run(self.url, confirm=prompts.append, out=lines.append, poll=0.01,
                                         allow_mock=True, timeout=10))
        self.assertEqual(len(prompts), 5, "每次动臂前都要等人确认")
        self.assertIn("自检通过", "\n".join(lines))
        self.assertEqual((self.arm.aborted, self.arm.at), (1, "rest"), "第 4 步急停一次，最后回 rest")
        self.assertEqual(self.mock.grips, [False, True, False])

    def test_selftest_refuses_without_selftest_file(self):
        self.selftest_worker(None)
        with self.assertRaises(arm_selftest.SelftestError) as cm:
            arm_selftest.run(self.url, confirm=lambda m: None, out=lambda s: None, allow_mock=True)
        self.assertIn("ARM_SKILLS_FILE", str(cm.exception))
        self.assertEqual(self.mock.moves, [])

    def test_selftest_refuses_mock_arm_by_default(self):
        self.selftest_worker(SelftestFileTest.PATH)
        with self.assertRaises(arm_selftest.SelftestError) as cm:
            arm_selftest.run(self.url, confirm=lambda m: None, out=lambda s: None)
        self.assertIn("ARM_BACKEND=panthera", str(cm.exception))

    # ---- 和主脑端到端 ----

    def test_fall_escalation_moves_the_arm(self):
        bus, clock, _ = make_bus(arm=ArmClient(self.url), arm_url=self.url)
        bus.fall("卧室")
        clock.advance(6, bus)
        bus.family_ack("daughter")
        bus.arm.drain()
        self.assertTrue(self.arm.wait_idle(3.0), self.arm.status())
        done = [m[0] for m in self.mock.moves if m[3]]
        self.assertIn(point_q("sos_press"), done, "臂真的按了 SOS")
        self.assertEqual(done[-1], point_q("show_sign"), "最后举着牌")
        self.assertEqual((self.arm.done, self.arm.aborted), (2, 0))
        self.assertFalse([e for e in events(bus, "system") if e["level"] == "danger"])
        call(self.url, "/stop")


if __name__ == "__main__":
    unittest.main()
