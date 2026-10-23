"""agent 白名单 + 越权拦截：保命动作只归状态机。"""
import unittest

from helpers import events, make_bus

import config
from agent.supervisor import Supervisor


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.s = Supervisor(config.load()["contacts"])

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

    def test_bus_blocks_agent_sos(self):
        bus, clock, arm = make_bus()
        r = bus.agent_propose("press_sos")
        self.assertFalse(r["ok"])
        self.assertEqual(bus.fsm.state, "IDLE")
        self.assertEqual(arm.sent, [])
        self.assertTrue(events(bus, "blocked"))


if __name__ == "__main__":
    unittest.main()
