"""猜拳的主脑一侧：一局的流程、判输赢、资源锁、接口和参数。视觉一侧（手势分类、投票）见 test_rps_engine。"""
import random
import unittest

from helpers import events, make_bus, said

from master_service import RPS_NAMES, dispatch_post, rps_judge


class JudgeTest(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(rps_judge("rock", "scissors"), "win")
        self.assertEqual(rps_judge("scissors", "paper"), "win")
        self.assertEqual(rps_judge("paper", "rock"), "win")
        self.assertEqual(rps_judge("rock", "paper"), "lose")
        for m in RPS_NAMES:
            self.assertEqual(rps_judge(m, m), "draw")


class RoundTest(unittest.TestCase):
    def setUp(self):
        self.bus, self.clock, self.arm = make_bus()
        self.bus.rng = random.Random(7)

    def test_robot_commits_first_and_reveals_late(self):
        self.assertEqual(self.bus.voice("我们来猜拳吧")["intent"], "rps")
        view = self.bus.snapshot()["rps"]
        self.assertEqual(view["phase"], "countdown")
        self.assertIsNone(view["robot"], "揭晓前大屏/操作台看不到机器人出什么")
        robot = self.bus.rps["robot"]
        self.clock.advance(3.6, self.bus)
        self.assertEqual(self.arm.sent, [("rps_" + robot, 1)])
        r = self.bus.rps_gesture(1, "rock")
        self.assertEqual(r["result"], rps_judge("rock", robot))
        self.assertIn("您出石头" if r["result"] != "draw" else "都出了石头", said(self.bus)[-1])

    def test_wrong_round_ignored(self):
        self.bus.voice("石头剪刀布")
        self.assertFalse(self.bus.rps_gesture(99, "rock")["ok"])
        self.assertFalse(self.bus.rps_gesture("abc", "rock")["ok"])
        self.assertIsNotNone(self.bus.rps)

    def test_unclear_gesture_still_reveals(self):
        self.bus.voice("石头剪刀布")
        self.bus.rps_gesture(1, None)
        self.assertIn("没看清", said(self.bus)[-1])
        self.assertEqual(len(self.arm.sent), 1, "没看清也要揭晓机器人出的")

    def test_no_vision_times_out(self):
        self.bus.voice("石头剪刀布")
        self.clock.advance(10, self.bus)
        self.assertIsNone(self.bus.rps)
        self.assertIn("摄像头", said(self.bus)[-1])
        self.assertTrue(any("超时" in e["text"] for e in events(self.bus, "rps")))

    def test_fall_cancels_round(self):
        self.bus.voice("石头剪刀布")
        self.bus.fall("客厅")
        self.assertIsNone(self.bus.rps)
        self.clock.advance(10, self.bus)
        self.assertFalse(any("超时" in e["text"] for e in events(self.bus, "rps")))

    def test_suspended_while_safety_chain_holds_arm(self):
        self.bus.fall("卧室")
        self.clock.advance(5.2, self.bus)
        self.assertEqual(self.bus.fsm.state, "SOS")
        before = list(self.arm.sent)
        self.bus.voice("石头剪刀布")
        self.assertIsNone(self.bus.rps, "保命链占着臂，猜拳要挂起")
        self.clock.advance(6, self.bus)
        self.assertEqual(self.arm.sent, before, "手#1 不能在举牌/按 SOS 时去出拳")


class InterfaceTest(unittest.TestCase):
    def test_route_and_params(self):
        bus, clock, arm = make_bus()
        bus.voice("石头剪刀布")
        r = dispatch_post(bus, "/api/rps/gesture", {"round": 1, "gesture": "paper"})
        self.assertTrue(r["ok"])
        self.assertTrue(bus.set_params({"rps_reveal_delay": 2})["ok"])
        self.assertFalse(bus.set_params({"rps_timeout": 999})["ok"], "超出 LIMITS 要拒绝")

    def test_reveal_poses_exist_for_arm_service(self):
        from hand_controller.poses import POSES
        for move in RPS_NAMES:
            self.assertIn("rps_" + move, POSES)


if __name__ == "__main__":
    unittest.main()
