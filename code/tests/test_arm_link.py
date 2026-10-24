"""执行端链路：动作成败回执、保命动作没做成要告警、断链保护（过期动作不补做）、在线探测。"""
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from unittest import mock

from helpers import FakeArm, events, make_bus

from hand_controller import arm_service
from master_service import ArmClient


def start_arm():
    """真的 arm_service Handler（mock 执行），验证的是和硬件同学约定的接口本身。"""
    patcher = mock.patch.object(arm_service, "_execute", lambda pose, hand: None)
    patcher.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), arm_service.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
        patcher.stop()
    return "http://127.0.0.1:%d" % server.server_address[1], stop


def closed_url():
    """一个当前没人监听的端口。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), arm_service.Handler)
    port = server.server_address[1]
    server.server_close()
    return "http://127.0.0.1:%d" % port


class Recorder:
    def __init__(self, arm):
        self.results, self.links = [], []
        arm.on_result = lambda *a: self.results.append(a)
        arm.on_link = self.links.append


class ArmClientTest(unittest.TestCase):
    def setUp(self):
        self.url, stop = start_arm()
        self.addCleanup(stop)
        arm_service.HISTORY.clear()

    def test_ok_and_rejected(self):
        arm = ArmClient(self.url)
        rec = Recorder(arm)
        arm.send("press_sos", 1)
        arm.send("fly_away", 1)   # 动作表里没有
        arm.drain()
        self.assertEqual(rec.results[0], ("press_sos", 1, True, "", ""))
        pose, hand, ok, why, detail = rec.results[1]
        self.assertEqual((ok, why), (False, "rejected"))
        self.assertIn("unknown pose", detail)
        self.assertTrue(arm.online, "被拒绝说明执行端在线")
        self.assertEqual(rec.links, [True])
        st = arm.status()
        self.assertEqual((st["done"], st["failed"], st["online"]), (1, 1, True))
        self.assertEqual([h["pose"] for h in arm_service.HISTORY], ["press_sos"])

    def test_offline_reports_once_per_change(self):
        arm = ArmClient(closed_url(), timeout=0.3)
        rec = Recorder(arm)
        arm.send("press_sos", 1)
        arm.send("gentle_hold", 2)
        arm.drain()
        self.assertEqual([r[3] for r in rec.results], ["offline", "offline"])
        self.assertEqual(rec.links, [False], "掉线只报一次，不是每个动作报一次")
        self.assertFalse(arm.status()["online"])

    def test_stale_command_dropped_not_replayed(self):
        # §5.2 断链保护：排队太久的动作（链路刚恢复时的积压）直接丢，臂不补做
        arm = ArmClient(self.url, stale=5.0)
        rec = Recorder(arm)
        arm.q.put((arm.clock() - 10, "deliver_medicine", 1))
        arm.send("home", 1)
        arm.drain()
        self.assertEqual(rec.results[0][:4], ("deliver_medicine", 1, False, "stale"))
        self.assertEqual([h["pose"] for h in arm_service.HISTORY], ["home"])
        self.assertEqual(arm.status()["dropped"], 1)

    def test_probe_sees_arm_before_any_action(self):
        arm = ArmClient(self.url)
        rec = Recorder(arm)
        self.assertIsNone(arm.online)
        self.assertTrue(arm.probe())
        self.assertEqual(rec.links, [True])
        self.assertFalse(ArmClient(closed_url(), timeout=0.3).probe())

    def test_callback_crash_does_not_kill_sender(self):
        arm = ArmClient(self.url)
        arm.on_result = lambda *a: 1 / 0
        arm.send("home", 1)
        arm.send("press_sos", 1)
        arm.drain()
        self.assertEqual(arm.status()["done"], 2)

    def test_kit_aliases_stay_on_our_single_service(self):
        """kit 的验收命令可以打到仓库服务，但内部仍记录规范动作名。"""
        for path in ("/press_sos", "/pose/comfort_rhythm?hand=2", "/handover_med",
                     "/pose/rock", "/pose/thumbs_up?hand=1"):
            req = Request(self.url + path, data=b"", method="POST")
            with urlopen(req, timeout=1) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual([h["pose"] for h in arm_service.HISTORY],
                         ["press_sos", "gentle_hold", "deliver_medicine", "rps_rock",
                          "sign_family_received"])
        self.assertEqual([h["requested"] for h in arm_service.HISTORY],
                         ["press_sos", "comfort_rhythm", "handover_med", "rock", "thumbs_up"])

    def test_unknown_alias_is_rejected_without_execution(self):
        # kit-compatible path: documented as HTTP 200 with ok=false
        with urlopen(Request(self.url + "/pose/no_such_pose", data=b"", method="POST"), timeout=1) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('"ok": false', response.read().decode("utf-8"))
        # master-facing query path: HTTP error so ArmClient counts rejection
        req = Request(self.url + "/pose?name=no_such_pose", data=b"", method="POST")
        with self.assertRaises(Exception) as ctx:
            urlopen(req, timeout=1)
        self.assertIn("400", str(ctx.exception))
        self.assertEqual(arm_service.HISTORY, [])

    def test_action_history_is_bounded(self):
        old_history = list(arm_service.HISTORY)
        try:
            arm_service.HISTORY.clear()
            for index in range(arm_service.HISTORY_LIMIT + 25):
                arm_service._record_history({"n": index})
            self.assertEqual(len(arm_service.HISTORY), arm_service.HISTORY_LIMIT)
            self.assertEqual(arm_service.HISTORY[0]["n"], 25)
        finally:
            arm_service.HISTORY[:] = old_history


class DeadArm(FakeArm):
    """执行端全挂：每个动作都回执失败（同步回调，和真 ArmClient 的线程回调走同一个入口）。"""
    online = False

    def send(self, pose, hand):
        super().send(pose, hand)
        self.on_result(pose, hand, False, "offline", "connection refused")


def system_events(bus, level=None):
    return [e for e in events(bus, "system") if level is None or e["level"] == level]


class BusArmFeedbackTest(unittest.TestCase):
    def test_sos_press_failure_alerts_operator_but_family_still_notified(self):
        bus, clock, arm = make_bus(arm=DeadArm())
        bus.fall("卧室")
        clock.advance(6, bus)
        self.assertEqual(bus.fsm.state, "SOS", "执行端挂了，保命链照样升级")
        self.assertTrue([n for n in bus.notifications if n["kind"] == "fall"], "家属通知走软件通道，不靠臂")
        danger = [e["text"] for e in system_events(bus, "danger")]
        self.assertTrue(any("按下 SOS 键" in t and "执行端连不上" in t for t in danger), danger)
        self.assertTrue(any("轻握" in t for t in danger), "手#2 轻握也是保命动作")
        self.assertFalse([e for e in events(bus, "arm_fail")], "type=system：大屏不显示")

    def test_non_safety_failures_logged_once_until_recovery(self):
        bus, clock, arm = make_bus()
        for _ in range(3):
            bus._arm_result("finger_fist", 1, False, "offline", "")
        self.assertEqual(len(system_events(bus, "warn")), 1, "手指操 13 拍不能刷屏")
        bus._arm_result("home", 1, True, "", "")
        bus._arm_result("finger_open", 1, False, "rejected", "unknown pose: finger_open")
        warns = system_events(bus, "warn")
        self.assertEqual(len(warns), 2)
        self.assertIn("执行端拒绝", warns[-1]["text"])

    def test_link_change_logged(self):
        bus, clock, arm = make_bus()
        bus._arm_link(False)
        bus._arm_link(True)
        texts = [e["text"] for e in system_events(bus)]
        self.assertTrue(any("执行端连不上" in t for t in texts))
        self.assertTrue(any("执行端已连上" in t for t in texts))

    def test_arm_params_tunable_at_runtime(self):
        bus, clock, arm = make_bus()
        self.assertTrue(bus.set_params({"arm_timeout": 3, "arm_stale": 8})["ok"])
        self.assertEqual((arm.timeout, arm.stale), (3.0, 8.0))
        self.assertFalse(bus.set_params({"arm_timeout": 0})["ok"])

    def test_snapshot_keeps_console_shape(self):
        bus, clock, arm = make_bus()
        self.assertIs(bus.snapshot()["arm"]["online"], True)

    def test_hands_track_last_pose_for_screen(self):
        # 大屏执行端动画的数据源：每只手最近一次动作 + 做没做成（手指操每拍是 quiet，不进时间线）
        bus, clock, arm = make_bus()
        hands = bus.snapshot()["hands"]
        self.assertEqual((hands["1"]["pose"], hands["2"]["pose"]), ("home", "release"))
        bus.voice("做个手指操吧")
        clock.advance(8, bus)
        h1 = bus.snapshot()["hands"]["1"]
        self.assertTrue(h1["pose"].startswith("finger_"), h1)
        self.assertEqual(h1["status"], "sent")
        bus._arm_result(h1["pose"], 1, True, "", "")
        self.assertEqual(bus.snapshot()["hands"]["1"]["status"], "ok")
        bus.reset()
        self.assertTrue(bus.snapshot()["hands"]["1"]["pose"].startswith("finger_"), "重置不让臂动，显示真机停在哪")

    def test_hands_show_failure_status(self):
        bus, clock, arm = make_bus(arm=DeadArm())
        bus.fall("卧室")
        clock.advance(6, bus)
        h = bus.snapshot()["hands"]
        self.assertEqual((h["1"]["pose"], h["1"]["status"]), ("press_sos", "offline"))
        self.assertEqual((h["2"]["pose"], h["2"]["status"]), ("gentle_hold", "offline"))

    def test_status_reports_mock_executor(self):
        # 执行端是 mock 时，大屏 / 操作台 / 自检都不能说“真机”：主脑从 /status 读 mock 字段
        url, stop = start_arm()
        self.addCleanup(stop)
        arm = ArmClient(url)
        self.assertTrue(arm.probe())
        self.assertIs(arm.status()["mock"], True)
        from tools import preflight
        nodes = {"bus": {"state": "IDLE"}, "arm": arm.status(), "box": {"online": False}, "vision": {"online": False}, "clients": {},
                 "heard": None, "llm": {"mode": "scripted"}, "params_changed": {}}
        rows = [r for r in preflight.check(nodes) if r[1] == "执行端"]
        self.assertEqual(rows[0][0], "warn")
        self.assertIn("模拟", rows[0][2])

    def test_end_to_end_with_real_arm_service(self):
        url, stop = start_arm()
        self.addCleanup(stop)
        arm_service.HISTORY.clear()
        bus, clock, _ = make_bus(arm=ArmClient(url), arm_url=url)
        bus.fall("卧室")
        clock.advance(6, bus)
        bus.family_ack("daughter")
        bus.arm.drain()
        self.assertEqual([h["pose"] for h in arm_service.HISTORY],
                         ["press_sos", "gentle_hold", "sign_family_received", "sign_coming"])
        self.assertFalse(system_events(bus, "danger"))
        self.assertEqual(bus.nodes()["arm"]["done"], 4)


if __name__ == "__main__":
    unittest.main()
