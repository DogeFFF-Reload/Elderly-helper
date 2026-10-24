"""用药提醒闭环：开关、手动/定时提醒、递药打卡、漏服通知、保命链隔离、配置与意图。"""
import unittest

from helpers import events, make_bus, said

import config
import master_service
from agent import intent


MED_KEYS = ("med_remind", "med_ask_taken", "med_taken", "med_not_yet", "med_missed", "med_none")


def med_snapshot(bus):
    return bus.snapshot()["med"]


def med_events(bus, level=None):
    return [e for e in events(bus, "med") if level is None or e["level"] == level]


def med_notifications(bus):
    return [n for n in bus.notifications if "med" in n["kind"]]


def notifications(bus, kind):
    return [n for n in bus.notifications if n["kind"] == kind]


def med_line(bus, key):
    """按总线执行台词时的同一组参数，渲染 lines.json 里的 med_* 台词。"""
    c = bus.cfg["contacts"][0]
    return bus.llm.scripted(key, med=bus.cfg["med_name"], contact=c["name"], pron=c["pron"])


def med_lines(bus):
    return {med_line(bus, key) for key in MED_KEYS}


class MedReminderDefaultOffTest(unittest.TestCase):
    def test_default_off_refuses_remind_and_stays_silent(self):
        bus, clock, arm = make_bus()
        r = master_service.dispatch_post(bus, "/api/med/remind", {})
        self.assertFalse(r["ok"])
        self.assertIn("没开", r["error"])
        self.assertFalse(med_snapshot(bus)["enabled"])

        # 跨过默认的 20:00（T0 是 10:00），关闭状态不应有任何用药事件或台词
        clock.advance(11 * 3600, bus, step=600)
        self.assertEqual(events(bus, "med"), [])
        self.assertEqual(med_notifications(bus), [])
        self.assertFalse(set(said(bus)) & med_lines(bus))

    def test_default_off_taken_keeps_original_intent(self):
        bus, clock, arm = make_bus()
        r = bus.voice("吃了")
        self.assertNotEqual(r["intent"], "med_taken")
        self.assertEqual(events(bus, "med"), [])
        self.assertEqual(med_notifications(bus), [])


class MedReminderSwitchTest(unittest.TestCase):
    def test_enable_logs_once_and_zero_disables(self):
        bus, clock, arm = make_bus()
        self.assertFalse(med_snapshot(bus)["enabled"])

        r = master_service.dispatch_post(bus, "/api/med/enable", {"on": True})
        self.assertEqual(r, {"ok": True, "enabled": True})
        self.assertTrue(med_snapshot(bus)["enabled"])
        self.assertEqual(len(med_events(bus)), 1)

        # 重复打开不重复记事件
        master_service.dispatch_post(bus, "/api/med/enable", {"on": True})
        self.assertEqual(len(med_events(bus)), 1)

        r = master_service.dispatch_post(bus, "/api/med/enable", {"on": "0"})
        self.assertEqual(r, {"ok": True, "enabled": False})
        self.assertFalse(med_snapshot(bus)["enabled"])


class MedReminderManualTest(unittest.TestCase):
    def test_manual_remind_opens_one_dose_and_repeat_only_speaks(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        r = master_service.dispatch_post(bus, "/api/med/remind", {})
        self.assertTrue(r["ok"])
        self.assertEqual(said(bus)[-1], med_line(bus, "med_remind"))
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")

        doses = len(med_snapshot(bus)["doses"])
        spoken = said(bus).count(med_line(bus, "med_remind"))
        r2 = master_service.dispatch_post(bus, "/api/med/remind", {})
        self.assertTrue(r2["ok"])
        self.assertEqual(len(med_snapshot(bus)["doses"]), doses)
        self.assertEqual(said(bus).count(med_line(bus, "med_remind")), spoken + 1)

    def test_deliver_then_taken_closes_loop(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        master_service.dispatch_post(bus, "/api/med/remind", {})

        r = bus.voice("把药递给我")
        self.assertEqual(r["intent"], "deliver")
        self.assertIn("deliver_medicine", arm.poses())
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "delivered")
        self.assertIn(med_line(bus, "med_ask_taken"), said(bus))

        r = bus.voice("吃了")
        self.assertEqual(r["intent"], "med_taken")
        snap = med_snapshot(bus)
        self.assertIsNone(snap["pending"])
        last = snap["doses"][-1]
        self.assertEqual(last["state"], "taken")
        self.assertTrue(last["taken_ts"])
        meds = notifications(bus, "med")
        self.assertEqual(len(meds), 1)
        self.assertEqual(meds[0]["contact"], bus.cfg["contacts"][0]["id"])
        self.assertIn(med_line(bus, "med_taken"), said(bus))

    def test_taken_without_delivery_closes_loop(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        master_service.dispatch_post(bus, "/api/med/remind", {})
        r = bus.voice("吃过了")
        self.assertEqual(r["intent"], "med_taken")
        snap = med_snapshot(bus)
        self.assertIsNone(snap["pending"])
        self.assertEqual(snap["doses"][-1]["state"], "taken")
        self.assertEqual(len(notifications(bus, "med")), 1)

    def test_not_yet_keeps_pending_until_taken(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        master_service.dispatch_post(bus, "/api/med/remind", {})
        r = bus.voice("还没吃")
        self.assertEqual(r["intent"], "med_not_yet")
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")
        self.assertIn(med_line(bus, "med_not_yet"), said(bus))
        self.assertEqual(med_notifications(bus), [])

        r = bus.voice("吃了")
        self.assertEqual(r["intent"], "med_taken")
        self.assertIsNone(med_snapshot(bus)["pending"])
        self.assertEqual(med_snapshot(bus)["doses"][-1]["state"], "taken")


class MedReminderMissedTest(unittest.TestCase):
    def test_missed_after_warns_and_notifies_once(self):
        bus, clock, arm = make_bus(med_reminder="on", med_missed_after=30)
        master_service.dispatch_post(bus, "/api/med/remind", {})
        clock.advance(31, bus)

        snap = med_snapshot(bus)
        self.assertIsNone(snap["pending"])
        self.assertEqual(snap["doses"][-1]["state"], "missed")
        warns = med_events(bus, "warn")
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["type"], "med")
        self.assertEqual(len(notifications(bus, "med_missed")), 1)
        self.assertIn(med_line(bus, "med_missed"), said(bus))

        clock.advance(60, bus)
        self.assertEqual(len(notifications(bus, "med_missed")), 1)


class MedReminderNoPendingTest(unittest.TestCase):
    def test_taken_with_no_pending_is_not_med_taken(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        r = bus.voice("吃了")
        self.assertNotEqual(r["intent"], "med_taken")
        self.assertEqual(events(bus, "med"), [])
        self.assertEqual(med_notifications(bus), [])


class MedReminderScheduleTest(unittest.TestCase):
    def test_schedule_reminds_once_at_due_time(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["10:00"], med_missed_after=3600)
        clock.advance(1, bus)
        doses = med_snapshot(bus)["doses"]
        self.assertEqual(len(doses), 1)
        self.assertEqual((doses[0]["source"], doses[0]["slot"]), ("schedule", "10:00"))
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")

        clock.advance(120, bus)
        self.assertEqual(len(med_snapshot(bus)["doses"]), 1)
        self.assertEqual(said(bus).count(med_line(bus, "med_remind")), 1)

    def test_schedule_outside_grace_is_skipped(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["09:00"], med_missed_after=3600)
        clock.advance(60, bus)
        self.assertEqual(med_snapshot(bus)["doses"], [])
        self.assertNotIn(med_line(bus, "med_remind"), said(bus))

    def test_schedule_waits_until_due(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["10:05"], med_missed_after=3600)
        clock.advance(299, bus)
        self.assertEqual(med_snapshot(bus)["doses"], [])

        clock.advance(1, bus)
        doses = med_snapshot(bus)["doses"]
        self.assertEqual(len(doses), 1)
        self.assertEqual((doses[0]["source"], doses[0]["slot"]), ("schedule", "10:05"))


class MedReminderSafetyChainTest(unittest.TestCase):
    def test_fall_chain_blocks_reminder_and_missed_until_released(self):
        bus, clock, arm = make_bus(med_reminder="on", med_missed_after=30, ask_wait_day=600)
        master_service.dispatch_post(bus, "/api/med/remind", {})

        self.assertEqual(bus.fall("卧室")["state"], "ASKING")
        self.assertNotEqual(bus.fsm.state, "IDLE")

        # 保命链进行中超过 missed_after 也不判漏服
        clock.advance(31, bus)
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")
        self.assertEqual(notifications(bus, "med_missed"), [])

        r = master_service.dispatch_post(bus, "/api/med/remind", {})
        self.assertFalse(r["ok"])
        self.assertIn("保命链", r["error"])

        # 询问期间优先走 asking 上下文，不被当成服药打卡
        r = bus.voice("吃了")
        self.assertIn(r["intent"], ("elder_distress", "elder_ok"))
        self.assertEqual(med_snapshot(bus)["doses"][-1]["state"], "reminded")
        self.assertEqual(notifications(bus, "med"), [])
        self.assertEqual(notifications(bus, "med_missed"), [])
        self.assertEqual(len(med_events(bus)), 1)   # 只有最初那次手动提醒

        bus.button("ok")
        self.assertEqual(bus.fsm.state, "IDLE")
        clock.advance(31, bus)
        self.assertEqual(med_snapshot(bus)["doses"][-1]["state"], "missed")
        self.assertEqual(len(notifications(bus, "med_missed")), 1)


class MedReminderEstopTest(unittest.TestCase):
    def test_estop_reminds_but_holds_delivery(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=3600)
        bus.set_estop(True)
        r = master_service.dispatch_post(bus, "/api/med/remind", {})
        self.assertTrue(r["ok"])
        self.assertIn(med_line(bus, "med_remind"), said(bus))
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")

        r = bus.voice("把药递给我")
        self.assertEqual(r["intent"], "deliver")
        self.assertNotIn("deliver_medicine", arm.poses())
        self.assertEqual(med_snapshot(bus)["pending"]["state"], "reminded")


class MedReminderResetTest(unittest.TestCase):
    def test_reset_clears_doses_keeps_switch_and_fired_slots(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["10:00"], med_missed_after=3600)
        clock.advance(1, bus)
        self.assertEqual(len(med_snapshot(bus)["doses"]), 1)

        bus.reset()
        snap = med_snapshot(bus)
        self.assertEqual(snap["doses"], [])
        self.assertIsNone(snap["pending"])
        self.assertTrue(snap["enabled"])

        clock.advance(60, bus)
        self.assertEqual(med_snapshot(bus)["doses"], [])


class MedReminderSnapshotTest(unittest.TestCase):
    def test_snapshot_shape_and_state_text(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"], med_missed_after=60)
        master_service.dispatch_post(bus, "/api/med/remind", {})
        snap = med_snapshot(bus)
        self.assertTrue({"enabled", "name", "times", "missed_after", "pending", "doses"} <= set(snap))
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["name"], bus.cfg["med_name"])
        self.assertEqual(snap["times"], ["20:00"])
        self.assertEqual(snap["missed_after"], 60)
        self.assertEqual(snap["pending"]["state_text"], "已提醒")
        self.assertTrue(snap["doses"])
        for dose in snap["doses"]:
            self.assertIn("state_text", dose)


class MedConfigTest(unittest.TestCase):
    def test_invalid_med_config_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.load({"med_times": ["25:00"]}, use_local=False)
        with self.assertRaises(config.ConfigError):
            config.load({"med_reminder": "maybe"}, use_local=False)
        with self.assertRaises(config.ConfigError):
            config.load({"med_missed_after": 9}, use_local=False)

    def test_missed_after_tunable_and_limits(self):
        bus, clock, arm = make_bus(med_reminder="on", med_times=["20:00"])
        r = bus.set_params({"med_missed_after": 60})
        self.assertTrue(r["ok"])
        self.assertEqual(med_snapshot(bus)["missed_after"], 60)

        for bad in (9, 7201):
            r = bus.set_params({"med_missed_after": bad})
            self.assertFalse(r["ok"])
            self.assertTrue(any("med_missed_after" in e for e in r["errors"]))
        self.assertEqual(med_snapshot(bus)["missed_after"], 60)


class MedIntentTest(unittest.TestCase):
    def test_med_intents_need_pending(self):
        self.assertEqual(intent.classify("吃了", {"med_pending": True}), ("med_taken", {}))
        self.assertEqual(intent.classify("我还没吃", {"med_pending": True}), ("med_not_yet", {}))
        name, _ = intent.classify("吃了", {})
        self.assertNotEqual(name, "med_taken")


if __name__ == "__main__":
    unittest.main()
