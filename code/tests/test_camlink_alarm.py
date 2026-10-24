"""视频看护的报警通道：全用假板子 / 假发送器，不碰真实网络、不建真实警报。"""
import json
import tempfile
import unittest
from pathlib import Path

from vision.camlink.alarm import AlarmChannel, find_board, scan_boards
from vision.camlink.server import CamLink


class FakeClient:
    def __init__(self, base, *, mode, src):
        self.base, self.mode, self.src = base, mode, src
        self.preflights = self.delivered = 0

    def preflight(self):
        self.preflights += 1
        return {"status": "dry_accepted", "version": "5.7.1"}

    def deliver(self, event_id):
        self.delivered += 1
        return {"event_id": event_id, "status": "bedside_accepted", "alarm_id": "a1", "attempts": 1}


class FakePoster:
    def __init__(self, client, *, log, on_change):
        self.client, self.on_change, self.falls = client, on_change, []

    def fall(self, room):
        eid = "vision-%d" % len(self.falls)
        self.falls.append(room)
        self.on_change({"event_id": eid, "status": "queued", "room": room})
        return eid

    def close(self, timeout=None):
        return True


def channel(mode="live", finder=lambda _f: "http://10.0.0.5:8013"):
    return AlarmChannel(mode, "auto", room="卧室", log=lambda _m: None, finder=finder,
                        client_factory=FakeClient, poster_factory=FakePoster)


class AlarmChannelTest(unittest.TestCase):
    def test_off_never_contacts_a_board(self):
        ch = channel("off", finder=lambda _f: self.fail("No board lookup in off mode"))
        self.assertTrue(ch.connect_once())
        self.assertIsNone(ch.fall())
        self.assertEqual(ch.snapshot()["mode"], "off")

    def test_live_finds_board_checks_dry_then_sends_falls(self):
        ch = channel()
        self.assertTrue(ch.connect_once())
        st = ch.snapshot()
        self.assertEqual((st["base"], st["ready"], st["version"]), ("http://10.0.0.5:8013", True, "5.7.1"))
        self.assertEqual(ch.client.preflights, 1)
        eid = ch.fall()
        self.assertEqual(ch.poster.falls, ["卧室"])
        self.assertEqual(ch.record(eid)["status"], "queued")

    def test_missing_board_is_reported_and_fall_recorded_as_failed(self):
        ch = channel(finder=lambda _f: None)
        self.assertFalse(ch.connect_once())
        self.assertFalse(ch.snapshot()["ready"])
        self.assertIn("没找到床头板", ch.snapshot()["error"])
        eid = ch.fall()
        self.assertEqual(ch.record(eid)["error_type"], "no_board")

    def test_test_alarm_needs_live_and_ready_board(self):
        ch = channel()
        with self.assertRaises(RuntimeError):
            ch.send_test()
        ch.connect_once()
        result = ch.send_test()
        self.assertEqual(result["status"], "bedside_accepted")
        self.assertTrue(ch.record(result["event_id"])["test"])
        dry = channel("dry")
        dry.connect_once()
        with self.assertRaises(RuntimeError):
            dry.send_test()


class FindBoardTest(unittest.TestCase):
    def test_saved_address_first_then_scan_and_remember(self):
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / "board.json"
            saved.write_text(json.dumps({"base": "http://10.0.0.9:8013"}), encoding="utf-8")
            self.assertEqual(find_board("auto", saved, check=lambda b: b.endswith(".9:8013"),
                                        scan=lambda: self.fail("No scan when saved works")), "http://10.0.0.9:8013")
            found = find_board("auto", saved, check=lambda b: False, scan=lambda: "http://10.0.0.7:8013")
            self.assertEqual(found, "http://10.0.0.7:8013")
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["base"], "http://10.0.0.7:8013")

    def test_scan_checks_only_open_hosts_on_local_subnet(self):
        opened = []
        base = scan_boards(["192.168.7.20"], check=lambda b: b == "http://192.168.7.31:8013",
                           opener=lambda h: opened.append(h) or h in ("192.168.7.30", "192.168.7.31"))
        self.assertEqual(base, "http://192.168.7.31:8013")
        self.assertNotIn("192.168.7.20", opened)          # 不扫自己
        self.assertEqual(len(opened), 253)


class ServerForwardsRuleEventsTest(unittest.TestCase):
    def test_rule_event_goes_to_alarm_and_status_shows_delivery(self):
        ch = channel()
        ch.connect_once()
        link = CamLink(token="k", alarm=ch)

        class EventOnce:
            aspect = 16 / 9
            rules = link.detector.rules
            state = link.detector.state

            def update(self, _t, _kp):
                return {"kind": "fall"}

            def reset(self):
                pass
        link.detector = EventOnce()
        link.pose({"kp": None})
        status = link.status()
        self.assertEqual(status["mode"], "live")
        self.assertEqual(status["events"][-1]["delivery"], "queued")
        self.assertEqual(ch.poster.falls, ["卧室"])


if __name__ == "__main__":
    unittest.main()
