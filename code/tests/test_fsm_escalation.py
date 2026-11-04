"""保命状态机全路径：跌倒 → 询问 → 升级 → SOS → 家属确认 / 取消。"""
import unittest

from helpers import events, make_bus, said


def fallen(bus, room="卧室"):
    bus.fall(room)
    return bus.fsm.state


class EscalationTest(unittest.TestCase):
    def test_no_response_escalates_after_wait(self):
        bus, clock, arm = make_bus()
        self.assertEqual(fallen(bus), "ASKING")
        self.assertIn("您还好吗", said(bus)[-1])
        clock.advance(4.8, bus)
        self.assertEqual(bus.fsm.state, "ASKING")
        clock.advance(0.4, bus)
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertEqual(arm.sent[:2], [("press_sos", 1), ("gentle_hold", 2)])
        self.assertTrue(any(n["kind"] == "fall" and n["contact"] == "daughter" for n in bus.notifications))

    def test_green_key_cancels(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        bus.button("ok")
        self.assertEqual(bus.fsm.state, "IDLE")
        clock.advance(10, bus)
        self.assertNotIn("press_sos", arm.poses())

    def test_saying_ok_cancels(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        self.assertEqual(bus.voice("我没事")["intent"], "elder_ok")
        self.assertEqual(bus.fsm.state, "IDLE")
        self.assertEqual(bus.skeleton["pose"], "standing")

    def test_distress_escalates_immediately(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        bus.voice("疼，起不来")
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertIn("老人回应", events(bus, "sos")[0]["text"])

    def test_help_120_while_asking(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        bus.voice("快帮我打120")
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertIsNotNone(bus.fsm.dial_deadline)

    def test_signs_only_after_family_ack(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        clock.advance(6, bus)
        self.assertNotIn("sign_family_received", arm.poses(), "家属没确认前不能举“家人已收到”")
        bus.family_ack("daughter")
        self.assertEqual(bus.fsm.state, "HELP_COMING")
        self.assertEqual(arm.sent[-2:], [("sign_family_received", 1), ("sign_coming", 2)])
        self.assertIn("马上就到", said(bus)[-1])

    def test_unacked_escalates_to_next_contact(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        clock.advance(5.2, bus)
        clock.advance(30.2, bus)
        self.assertEqual(bus.fsm.snapshot()["notified"], ["daughter", "son"])
        self.assertTrue(any(n["contact"] == "son" and n["kind"] == "fall" for n in bus.notifications))
        clock.advance(30.2, bus)
        self.assertIn("帮我打120", said(bus)[-1])
        bus.family_ack("son")
        self.assertEqual(bus.fsm.acked_by, "son")

    def test_ok_after_sos_tells_family(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        clock.advance(6, bus)
        bus.voice("我没事")
        self.assertEqual(bus.fsm.state, "IDLE")
        self.assertTrue(any(n["kind"] == "cancel" for n in bus.notifications))
        self.assertIn(("release", 2), arm.sent)

    def test_repeat_fall_does_not_double_escalate(self):
        bus, clock, arm = make_bus()
        fallen(bus)
        clock.advance(6, bus)
        bus.fall("卧室")
        clock.advance(6, bus)
        self.assertEqual(arm.poses().count("press_sos"), 1)


if __name__ == "__main__":
    unittest.main()
