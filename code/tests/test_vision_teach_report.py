"""跌倒判定规则 + 夜间窗口 + 教学（不现编）+ 日报。"""
import json
import unittest

from helpers import make_bus, said

import config
from agent.device_helper import GUIDES_PATH, DeviceHelper
from gesture.fall_rules import FALLEN, STANDING, is_fallen


class FallRulesTest(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(is_fallen(FALLEN))
        self.assertFalse(is_fallen(STANDING))


class NightWindowTest(unittest.TestCase):
    def test_window_crosses_midnight(self):
        for h, expect in ((18, False), (19, True), (23, True), (0, True), (5, True), (6, False), (12, False)):
            self.assertEqual(config.is_night(h, 19, 6), expect, h)
        self.assertTrue(config.is_night(23, 22, 6))
        self.assertFalse(config.is_night(21, 22, 6))
        self.assertFalse(config.is_night(3, 0, 0))

    def test_override_and_shorter_wait(self):
        bus, clock, arm = make_bus()
        self.assertFalse(bus.is_night(), "测试时钟在上午")
        bus.set_night("on")
        self.assertTrue(bus.is_night())
        bus.fall("卧室")
        self.assertEqual(bus.fsm.wait, 3.0)
        bus.reset()
        self.assertEqual(bus.night_mode, "auto")


class TeachTest(unittest.TestCase):
    def setUp(self):
        self.h = DeviceHelper()
        self.guides = json.loads(GUIDES_PATH.read_text(encoding="utf-8"))

    def _steps(self, task_id):
        for d in self.guides["devices"]:
            for t in d["tasks"]:
                if t["id"] == task_id:
                    return t["steps"]

    def test_known_task_uses_library_steps(self):
        r = self.h.match("电视怎么换台")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["steps"], self._steps("tv_channel"))
        self.assertEqual(self.h.match("怎么打视频给女儿")["task_id"], "vc_call")
        self.assertEqual(self.h.match("空调怎么调成制热")["task_id"], "ac_heat")

    def test_unknown_task_is_not_invented(self):
        for text in ("电饭煲内胆怎么拆洗", "空调滤网怎么洗", "电视坏了怎么开"):
            r = self.h.match(text)
            self.assertEqual(r["status"], "unknown_task", text)
            self.assertNotIn("steps", r)

    def test_unknown_device(self):
        self.assertEqual(self.h.match("洗衣机怎么用")["status"], "unknown_device")

    def test_bus_teach_card(self):
        bus, clock, arm = make_bus()
        bus.voice("电视怎么换台")
        self.assertEqual(bus.teach_card["steps"], self._steps("tv_channel"))
        bus.voice("电饭煲内胆怎么拆洗")
        self.assertEqual(bus.teach_card["status"], "unknown")
        self.assertIn("不敢乱教", said(bus)[-1])
        clock.advance(46, bus)
        self.assertIsNone(bus.teach_card, "教学卡 45 秒后自动收起")


class ReportTest(unittest.TestCase):
    def test_family_sees_report(self):
        bus, clock, arm = make_bus()
        bus.voice("我刚量了血压 138比85"); bus.voice("对")
        bus.voice("我喝了一杯水")
        r = bus.snapshot()["health"]["today"]
        self.assertEqual(r["bp"][0]["flag"], "偏高")
        self.assertEqual(r["water_ml"], 200)


if __name__ == "__main__":
    unittest.main()
