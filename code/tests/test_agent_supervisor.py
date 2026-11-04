"""agent 白名单 + 越权拦截：保命动作只归状态机。"""
import unittest

from helpers import events, make_bus

import config
from agent.supervisor import Supervisor


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.s = Supervisor(config.load(use_local=False)["contacts"])

    def test_fsm_only_skills_blocked(self):
        for skill in ("press_sos", "dial_120", "gentle_hold", "notify_emergency"):
            ok, reason = self.s.check(skill)
            self.assertFalse(ok, skill)
            self.assertIn("状态机", reason)

    def test_unknown_skill_blocked(self):
        self.assertFalse(self.s.check("open_door")[0])

    def test_item_and_contact_whitelists(self):
        self.assertTrue(self.s.check("deliver_medicine", item="药")[0])
        self.assertFalse(self.s.check("deliver_medicine", item="剪刀")[0])
        self.assertTrue(self.s.check("call_family", contact="daughter")[0])
        self.assertFalse(self.s.check("call_family", contact="张三")[0])

    def test_arm_lock(self):
        self.assertFalse(self.s.check("deliver_medicine", item="药", busy={"arm"})[0])
        self.assertFalse(self.s.check("finger_exercise", busy={"arm"})[0])
        self.assertTrue(self.s.check("call_family", contact="son", busy={"arm"})[0])

    def test_bus_suspends_arm_skills_during_rescue(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(6, bus)
        self.assertEqual(bus.fsm.state, "SOS")
        bus.voice("把药递给我")
        self.assertNotIn("deliver_medicine", arm.poses(), "手#1 正在按 SOS，不能被递药挪走")
        self.assertIn("已挂起", events(bus, "blocked")[-1]["text"])
        bus.voice("叫我儿子")
        self.assertTrue(any(n["contact"] == "son" and n["kind"] == "call" for n in bus.notifications),
                        "不占臂的请求照常放行")
        bus.family_ack("daughter")
        bus.voice("做个手指操吧")
        self.assertIsNone(bus.exercise, "举着“家人已收到”的牌时不做手指操")
        self.assertFalse(bus.agent_propose("deliver_medicine")["ok"])
        bus.button("ok")
        bus.voice("把药递给我")
        self.assertEqual(arm.poses()[-1], "deliver_medicine", "保命链结束后恢复")

    def test_every_request_goes_through_supervisor(self):
        bus, clock, arm = make_bus()
        for text in ("把药递给我", "叫我女儿", "报平安", "微波炉怎么热饭", "做个手指操吧"):
            bus.voice(text)
        self.assertEqual(bus.supervisor.blocked, [])
        bus.voice("把剪刀递给我")
        self.assertEqual(bus.supervisor.blocked[-1]["skill"], "deliver_medicine")

    def test_bus_blocks_agent_sos(self):
        bus, clock, arm = make_bus()
        r = bus.agent_propose("press_sos")
        self.assertFalse(r["ok"])
        self.assertEqual(bus.fsm.state, "IDLE")
        self.assertEqual(arm.sent, [])
        self.assertTrue(events(bus, "blocked"))


if __name__ == "__main__":
    unittest.main()
