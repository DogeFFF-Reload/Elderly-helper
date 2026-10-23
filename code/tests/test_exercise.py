"""手指操 13 拍 + 中断。"""
import unittest

from helpers import events, make_bus, said


class ExerciseTest(unittest.TestCase):
    def test_full_13_beats(self):
        bus, clock, arm = make_bus()
        bus.voice("做个手指操吧")
        clock.advance(3 + 13 * 2 + 1, bus)
        beats = [t for t in said(bus) if t.startswith("第")]
        self.assertEqual(len(beats), 13)
        self.assertTrue(beats[-1].startswith("第13拍"))
        self.assertEqual(sum(1 for p in arm.poses() if p.startswith("finger_")), 13)
        self.assertIn("做完啦", said(bus)[-1])
        self.assertFalse(bus.snapshot()["exercise"]["active"])

    def test_stop_midway(self):
        bus, clock, arm = make_bus()
        bus.voice("做个手指操吧")
        clock.advance(5.2, bus)
        bus.voice("停")
        self.assertIn("做到第 2 拍", events(bus, "exercise")[-1]["text"])
        clock.advance(10, bus)
        self.assertEqual(len([t for t in said(bus) if t.startswith("第")]), 2)

    def test_fall_interrupts(self):
        bus, clock, arm = make_bus()
        bus.voice("做个手指操吧")
        clock.advance(4, bus)
        bus.fall("客厅")
        self.assertIsNone(bus.exercise)
        self.assertEqual(bus.fsm.state, "ASKING")


if __name__ == "__main__":
    unittest.main()
