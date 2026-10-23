"""递药全链 + 白名单外物品不递 + SOS 按压动作序列。"""
import unittest

from helpers import events, make_bus


class HandoverTest(unittest.TestCase):
    def test_deliver_medicine(self):
        for text in ("把药递给我", "帮我拿一下药", "把药盒拿给我"):
            bus, clock, arm = make_bus()
            self.assertEqual(bus.voice(text)["intent"], "deliver", text)
            self.assertEqual(arm.sent, [("deliver_medicine", 1)], text)

    def test_unknown_item_not_delivered(self):
        bus, clock, arm = make_bus()
        bus.voice("把遥控器递给我")
        self.assertEqual(arm.sent, [])
        self.assertIn("遥控器", events(bus, "blocked")[0]["text"])

    def test_sos_press_sequence(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(5.2, bus)
        bus.family_ack("daughter")
        self.assertEqual(arm.sent, [("press_sos", 1), ("gentle_hold", 2),
                                    ("sign_family_received", 1), ("sign_coming", 2)])
        labels = [e["text"] for e in events(bus, "arm")]
        self.assertEqual(labels[0], "臂+手#1 按下 SOS 键")


if __name__ == "__main__":
    unittest.main()
