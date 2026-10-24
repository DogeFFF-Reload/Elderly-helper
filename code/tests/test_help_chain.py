"""主动求助：120 双重确认、呼叫家属、联系人白名单。"""
import unittest

from helpers import events, make_bus, said


def dialed(bus):
    return any("已拨打" in e["text"] for e in events(bus, "dial"))


class Dial120Test(unittest.TestCase):
    def test_confirm_within_window(self):
        bus, clock, arm = make_bus()
        bus.voice("帮我打120")
        clock.advance(9, bus)
        bus.voice("确认")
        self.assertTrue(dialed(bus))
        self.assertTrue(any(n["kind"] == "120" for n in bus.notifications))

    def test_timeout_does_not_dial(self):
        bus, clock, arm = make_bus()
        bus.voice("帮我打120")
        clock.advance(10.4, bus)
        bus.voice("确认")
        self.assertFalse(dialed(bus))
        self.assertTrue(any("未拨" in e["text"] for e in events(bus, "dial")))

    def test_cancel_does_not_dial(self):
        bus, clock, arm = make_bus()
        bus.voice("帮我打 幺 二 零")
        bus.voice("取消")
        self.assertFalse(dialed(bus))
        self.assertIn("先不打", said(bus)[-1])

    def test_confirm_without_request(self):
        bus, clock, arm = make_bus()
        bus.voice("确认")
        self.assertFalse(dialed(bus))

    def test_confirm_at_or_after_deadline_without_tick(self):
        for delta in (10.0, 10.1):
            with self.subTest(delta=delta):
                bus, clock, arm = make_bus()
                bus.voice("帮我打120")
                clock.advance(delta)  # reproduce input racing the periodic tick
                bus.voice("确认")
                self.assertFalse(dialed(bus))
                self.assertFalse(bus.fsm.snapshot()["dial_pending"])
                self.assertFalse(any(n["kind"] == "120" for n in bus.notifications))

    def test_negative_confirmation_cancels(self):
        for text in ("确认不要打", "确定不打了", "确认取消"):
            with self.subTest(text=text):
                bus, clock, arm = make_bus()
                bus.voice("帮我打120")
                self.assertEqual(bus.voice(text)["intent"], "dial_cancel")
                self.assertFalse(dialed(bus))

    def test_clearing_incident_disarms_old_dial_confirmation(self):
        for stage in ("ASKING", "SOS", "HELP_COMING"):
            for via in ("voice", "button"):
                with self.subTest(stage=stage, via=via):
                    bus, clock, arm = make_bus()
                    bus.fall("客厅")
                    if stage != "ASKING":
                        clock.advance(5.2, bus)
                    if stage == "HELP_COMING":
                        bus.family_ack("daughter")
                    bus.voice("帮我打120")
                    if via == "voice":
                        bus.voice("我没事")
                    else:
                        bus.button("ok")
                    self.assertEqual(bus.fsm.state, "IDLE")
                    self.assertIsNone(bus.fsm.dial_deadline)
                    bus.voice("确认")
                    self.assertFalse(dialed(bus))


class AlarmReplyTest(unittest.TestCase):
    def test_distress_or_negated_assurance_does_not_clear_alarm(self):
        for stage in ("ASKING", "SOS"):
            for text in ("我不是没事，我很疼", "我没事但起不来", "我哪里没事了", "还好但动不了"):
                with self.subTest(stage=stage, text=text):
                    bus, clock, arm = make_bus()
                    bus.fall("客厅")
                    if stage == "SOS":
                        clock.advance(5.2, bus)
                    bus.voice(text)
                    self.assertEqual(bus.fsm.state, "SOS")
                    self.assertFalse(events(bus, "ok"))

    def test_unambiguous_assurance_still_clears(self):
        bus, clock, arm = make_bus()
        bus.fall("客厅")
        bus.voice("我没事")
        self.assertEqual(bus.fsm.state, "IDLE")

    def test_notification_is_explicit_local_demo_record(self):
        bus, clock, arm = make_bus()
        bus.fall("客厅")
        clock.advance(5.2, bus)
        self.assertEqual(bus.notifications[-1]["delivery"], "local_demo_record")
        self.assertIn("家属演示页面", said(bus)[-1])
        self.assertTrue(any("未外部发送" in e["text"] for e in events(bus, "notify")))


class CallFamilyTest(unittest.TestCase):
    def test_call_whitelisted_contacts(self):
        bus, clock, arm = make_bus()
        bus.voice("叫我女儿")
        bus.voice("帮我叫一下儿子")
        calls = [(n["contact"], n["kind"]) for n in bus.notifications]
        self.assertEqual(calls, [("daughter", "call"), ("son", "call")])

    def test_stranger_blocked(self):
        bus, clock, arm = make_bus()
        r = bus.voice("给张三打电话")
        self.assertEqual(r["intent"], "call_blocked")
        self.assertEqual(bus.notifications, [])
        self.assertIn("张三", events(bus, "blocked")[0]["text"])

    def test_message_and_safe_report(self):
        bus, clock, arm = make_bus()
        bus.voice("给女儿留言：我今天挺好的")
        self.assertEqual(bus.notifications[-1]["text"], "妈妈留言：我今天挺好的")
        bus.voice("报平安")
        self.assertEqual(sorted(n["contact"] for n in bus.notifications if n["kind"] == "safe"), ["daughter", "son"])


if __name__ == "__main__":
    unittest.main()
