"""猜拳的视觉一侧：手势分类（转手、远近、画面比例都不影响）+ 多帧投票 + 识别窗口。
主脑一侧（一局流程、判输赢、资源锁）由软件模块的 test_rps_hook 负责。"""
import math
import unittest

import helpers  # noqa: F401  （把 code/ 加进 sys.path）

from gesture.rps_engine import MOVES, classify, vote

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


class VoteTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
