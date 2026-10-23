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
