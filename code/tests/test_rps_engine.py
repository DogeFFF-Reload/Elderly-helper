"""猜拳：手势分类（转手、远近、画面比例都不影响）+ 判输赢 + 投票 + 总线一局。"""
import math
import random
import unittest

from helpers import events, make_bus, said

from gesture.rps_engine import MOVES, classify, judge, vote

FINGER_DIRS = {"index": -20, "middle": -5, "ring": 10, "pinky": 25}  # 相对手掌方向的角度
FINGER_IDX = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12), "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}
SHAPES = {"rock": set(), "scissors": {"index", "middle"}, "paper": {"index", "middle", "ring", "pinky"},
          "point": {"index"}}


def hand(shape, rotate=0.0, scale=0.25, center=(0.5, 0.5), aspect=1.0):
    """合成 MediaPipe 21 点手（手腕在 center，手指朝上），再旋转、缩放、按画面比例压扁 x。"""
    pts = [None] * 21
    pts[0] = (0.0, 0.0)
    for i, (x, y) in zip(range(1, 5), [(-0.2, -0.15), (-0.32, -0.3), (-0.4, -0.42), (-0.45, -0.52)]):
        pts[i] = (x, y)
    for name, (mcp, pip, dip, tip) in FINGER_IDX.items():
        a = math.radians(FINGER_DIRS[name] - 90)
        d = lambda r, da=0.0: (r * math.cos(a + da), r * math.sin(a + da))  # noqa: E731
        pts[mcp] = d(0.35)
        if name in SHAPES[shape]:
            pts[pip], pts[dip], pts[tip] = d(0.55), d(0.68), d(0.80)
        else:  # 弯回掌心
            pts[pip], pts[dip], pts[tip] = d(0.48), d(0.40, 0.35), d(0.30, 0.45)
    r = math.radians(rotate)
    out = []
    for x, y in pts:
        xr, yr = x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r)
        out.append([center[0] + xr * scale / aspect, center[1] + yr * scale, 0.0])
    return out


class ClassifyTest(unittest.TestCase):
    def test_gestures_any_rotation_scale_aspect(self):
        for move in MOVES:
            for rot in (0, 30, 90, 160, 210, 300):
                for scale in (0.12, 0.3):
                    for aspect in (4 / 3, 16 / 9):
                        lm = hand(move, rot, scale, aspect=aspect)
                        self.assertEqual(classify(lm, aspect), move, "%s rot=%s scale=%s aspect=%.2f" % (move, rot, scale, aspect))

    def test_unclear_shapes(self):
        self.assertIsNone(classify(hand("point")))
        self.assertIsNone(classify([]))
        self.assertIsNone(classify(None))


class JudgeVoteTest(unittest.TestCase):
    def test_judge_matrix(self):
        self.assertEqual(judge("rock", "scissors"), "win")
        self.assertEqual(judge("scissors", "paper"), "win")
        self.assertEqual(judge("paper", "rock"), "win")
        self.assertEqual(judge("rock", "paper"), "lose")
        for m in MOVES:
            self.assertEqual(judge(m, m), "draw")

    def test_vote(self):
        self.assertEqual(vote(["rock", "rock", None, "rock", "paper"]), "rock")
        self.assertIsNone(vote(["rock", "rock"]), "帧太少")
        self.assertIsNone(vote(["rock", "paper", "rock", "paper"]), "分歧太大")


class TrackerTest(unittest.TestCase):
    def test_window_around_reveal(self):
        from vision.rps_bridge import RoundTracker
        t = [100.0]
        tr = RoundTracker(clock=lambda: t[0])
        tr.on_event({"type": "rps", "data": {"phase": "start", "round": 3, "reveal_in": 3.5}})
        self.assertIsNone(tr.feed("paper"))
        self.assertFalse(tr.active(), "揭晓前 0.6 秒之前不看")
        for dt in (2.95, 3.2, 3.5, 3.8, 4.2):
            t[0] = 100.0 + dt
            self.assertTrue(tr.active())
            self.assertIsNone(tr.feed("rock" if dt != 3.2 else None))
        t[0] = 105.1
        self.assertEqual(tr.feed(None), (3, "rock"))
        self.assertFalse(tr.active())


class BusRoundTest(unittest.TestCase):
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
        self.assertEqual(r["result"], judge("rock", robot))
        self.assertIn("您出石头" if r["result"] != "draw" else "都出了石头", said(self.bus)[-1])

    def test_wrong_round_ignored(self):
        self.bus.voice("石头剪刀布")
        self.assertFalse(self.bus.rps_gesture(99, "rock")["ok"])
        self.assertIsNotNone(self.bus.rps)

    def test_unclear_gesture(self):
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

    def test_suspended_while_safety_chain_holds_arm(self):
        self.bus.fall("卧室")
        self.clock.advance(5.2, self.bus)
        self.assertEqual(self.bus.fsm.state, "SOS")
        before = list(self.arm.sent)
        self.bus.voice("石头剪刀布")
        self.assertIsNone(self.bus.rps, "保命链占着臂，猜拳要挂起")
        self.clock.advance(6, self.bus)
        self.assertEqual(self.arm.sent, before, "手#1 不能在举牌/按 SOS 时去出拳")

    def test_fall_cancels_round(self):
        self.bus.voice("石头剪刀布")
        self.bus.fall("客厅")
        self.assertIsNone(self.bus.rps)
        self.clock.advance(10, self.bus)
        self.assertFalse(any("超时" in e["text"] for e in events(self.bus, "rps")))


if __name__ == "__main__":
    unittest.main()
