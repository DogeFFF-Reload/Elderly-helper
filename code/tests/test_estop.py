"""软件急停：开关后臂和双手一律不动，保命链和家属通知照常走软件通道。"""
import json
import unittest
import urllib.request

from helpers import events, make_bus, said

import master_service
from hand_controller import arm_service
from master_service import ArmClient
from test_arm_link import Recorder, closed_url, start_arm
from test_preflight import nodes
from tools import preflight


def system_texts(bus, level=None):
    return [e["text"] for e in events(bus, "system") if level is None or e["level"] == level]


class EstopBusTest(unittest.TestCase):
    def test_fall_while_halted_still_escalates_and_notifies(self):
        bus, clock, arm = make_bus()
        bus.set_estop(True)
        arm.sent.clear()
        bus.fall("卧室")
        clock.advance(10, bus)
        self.assertEqual(bus.fsm.state, "SOS")
        self.assertEqual(bus.snapshot()["guard"]["state"], "SOS")
        self.assertTrue(bus.notifications)
        self.assertEqual(arm.sent, [])
        danger = system_texts(bus, "danger")
        self.assertTrue(any("急停中，保命动作未执行" in t for t in danger), danger)

    def test_deliver_while_halted_says_estop_hold(self):
        bus, clock, arm = make_bus()
        bus.set_estop(True)
        bus.voice("把药递给我")
        self.assertNotIn("deliver_medicine", arm.poses())
        self.assertTrue(any("机械臂先停一停" in t for t in said(bus)), said(bus))

    def test_estop_interrupts_exercise_and_stops_arm(self):
        bus, clock, arm = make_bus()
        bus.voice("做个手指操吧")
        clock.advance(4, bus)
        self.assertIsNotNone(bus.exercise)
        sent = len(arm.sent)
        bus.set_estop(True)
        self.assertIsNone(bus.exercise)
        clock.advance(10, bus)
        self.assertEqual(len(arm.sent), sent)

    def test_reset_keeps_estop_and_resume_allows_deliver(self):
        bus, clock, arm = make_bus()
        bus.set_estop(True)
        bus.reset()
        self.assertTrue(bus.estop)
        bus.set_estop(False)
        arm.sent.clear()
        bus.voice("把药递给我")
        self.assertIn("deliver_medicine", arm.poses())

    def test_repeated_estop_logs_one_event(self):
        bus, clock, arm = make_bus()
        bus.set_estop(True)
        bus.set_estop(True)
        self.assertEqual(len(events(bus, "estop")), 1)


class ArmClientHaltTest(unittest.TestCase):
    def test_halt_drops_actions_and_status_follows_resume(self):
        arm = ArmClient(closed_url(), timeout=0.3)
        rec = Recorder(arm)
        arm.halt()
        self.assertTrue(arm.status()["halted"])
        arm.send("press_sos", 1)
        arm.drain()
        self.assertEqual(rec.results, [("press_sos", 1, False, "estop", "急停")])
        arm.resume()
        self.assertFalse(arm.status()["halted"])

    def test_arm_service_stop_endpoint(self):
        url, stop = start_arm()
        self.addCleanup(stop)
        arm_service.HISTORY.clear()
        req = urllib.request.Request(url + "/stop", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertTrue(data["stopped"])
        self.assertEqual([h["pose"] for h in arm_service.HISTORY], ["stop"])


class EstopRouteTest(unittest.TestCase):
    def test_dispatch_post_on_values(self):
        bus, clock, arm = make_bus()
        self.assertEqual(master_service.dispatch_post(bus, "/api/estop", {"on": True}),
                         {"ok": True, "estop": True})
        self.assertTrue(bus.estop)
        self.assertEqual(master_service.dispatch_post(bus, "/api/estop", {"on": False}),
                         {"ok": True, "estop": False})
        self.assertFalse(bus.estop)
        self.assertEqual(master_service.dispatch_post(bus, "/api/estop", {"on": "0"}),
                         {"ok": True, "estop": False})
        self.assertEqual(master_service.dispatch_post(bus, "/api/estop", {}),
                         {"ok": True, "estop": True})

    def test_preflight_flags_estop(self):
        n = nodes()
        n["estop"] = True
        rows = preflight.check(n)
        stop = [r for r in rows if r[1] == "急停"]
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0][0], preflight.FAIL)
        n["estop"] = False
        self.assertFalse([r for r in preflight.check(n) if r[1] == "急停"])


if __name__ == "__main__":
    unittest.main()
