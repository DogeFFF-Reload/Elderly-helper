"""健康口述：解析 + 复述确认（§2.3）+ 只存数字 + 日报不诊断。"""
import json
import os
import tempfile
import unittest

from helpers import T0, Clock, make_bus, said, events

from agent.health_tracker import HealthTracker, classify_bp, parse_reading


class ParseTest(unittest.TestCase):
    def test_blood_pressure_formats(self):
        cases = {
            "我刚量了血压 138比85": (138, 85, None),
            "血压138/85": (138, 85, None),
            "高压138，低压85": (138, 85, None),
            "低压85 高压138": (138, 85, None),
            "收缩压一百三十八舒张压八十五": (138, 85, None),
            "我 刚 量 了 血压 一 百 三十 八 比 八十 五": (138, 85, None),  # Vosk 式输出
            "血压138 85": (138, 85, None),
            "高压一百二十六低压七十八 心率七十二": (126, 78, 72),
            "１３８／８５": (138, 85, None),
            "不对，是138比88": (138, 88, None),
        }
        for text, (s, d, p) in cases.items():
            r = parse_reading(text)
            self.assertIsNotNone(r, text)
            self.assertEqual(r["kind"], "bp", text)
            self.assertEqual((r["sys"], r["dia"], r["pulse"]), (s, d, p), text)

    def test_implausible_values_ask_again(self):
        for text in ("血压85比138", "血压300比85", "高压138低压20"):
            self.assertEqual(parse_reading(text)["kind"], "bp_invalid", text)

    def test_not_blood_pressure(self):
        for text in ("今天下棋10比0赢了", "把药递给我", "我没事", "第3比2"):
            self.assertIsNone(parse_reading(text), text)

    def test_water(self):
        self.assertEqual(parse_reading("我喝了一杯水")["ml"], 200)
        self.assertEqual(parse_reading("喝了两杯水")["ml"], 400)
        self.assertEqual(parse_reading("我喝了300毫升水")["ml"], 300)
        r = parse_reading("我喝水了")
        self.assertEqual((r["ml"], r["estimated"]), (200, True))
        self.assertIsNone(parse_reading("我喝了药"))

    def test_home_measurement_rule(self):
        self.assertEqual(classify_bp(135, 80), "偏高")
        self.assertEqual(classify_bp(134, 85), "偏高")
        self.assertEqual(classify_bp(134, 84), "正常")
        self.assertEqual(classify_bp(120, 78), "正常")


class ReadbackTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t = HealthTracker(None, self.clock, confirm_window=20)

    def test_readback_before_record(self):
        acts = self.t.propose("我刚量了血压 138比85")
        say = [a for a in acts if a["do"] == "say"][0]
        self.assertEqual(say["key"], "health_readback")
        self.assertEqual((say["params"]["sys"], say["params"]["dia"]), (138, 85))
        self.assertEqual(self.t.records, [], "复述前不能落账")
        acts = self.t.confirm()
        self.assertEqual(len(self.t.records), 1)
        self.assertEqual(self.t.records[0]["flag"], "偏高")
        self.assertTrue(any(a["do"] == "notify" and a["kind"] == "health" for a in acts))

    def test_reject_does_not_record(self):
        self.t.propose("血压138比85")
        self.t.reject()
        self.assertEqual(self.t.records, [])
        self.assertIsNone(self.t.pending)

    def test_timeout_discards(self):
        self.t.propose("血压138比85")
        self.clock.advance(19.9)
        self.assertEqual(self.t.tick(), [])
        self.clock.advance(0.2)
        acts = self.t.tick()
        self.assertIn("未记录", acts[0]["text"])
        self.assertEqual(self.t.records, [])

    def test_confirmation_at_deadline_without_tick_discards(self):
        self.t.propose("血压138比85")
        self.clock.advance(20)
        acts = self.t.confirm()
        self.assertEqual(self.t.records, [])
        self.assertIsNone(self.t.pending)
        self.assertTrue(any("未记录" in a.get("text", "") for a in acts))

    def test_normal_reading_has_no_family_alert(self):
        self.t.propose("血压120比78")
        acts = self.t.confirm()
        self.assertFalse(any(a["do"] == "notify" for a in acts))

    def test_persist_numbers_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "health.json")
            t = HealthTracker(path, self.clock)
            t.propose("我刚量了血压 138比85 心率72")
            t.confirm()
            t.propose("我喝了一杯水")
            raw = open(path, encoding="utf-8").read()
            self.assertNotIn("我刚量了", raw)
            for rec in json.loads(raw)["records"]:
                self.assertLessEqual(set(rec), {"ts", "kind", "sys", "dia", "pulse", "flag", "ml"})
            self.assertEqual(len(HealthTracker(path, self.clock).records), 2, "重启后能读回")

    def test_daily_report_interprets_not_diagnoses(self):
        self.t.propose("血压138比85"); self.t.confirm()
        self.t.propose("血压126比78"); self.t.confirm()
        self.t.propose("喝了两杯水")
        r = self.t.daily_report()
        self.assertEqual(len(r["bp"]), 2)
        self.assertEqual(r["high_count"], 1)
        self.assertEqual(r["water_cups"], "2")
        self.assertIn("咨询医生", r["summary"])
        self.assertNotIn("高血压", r["summary"])
        self.assertIn("不作诊断", r["note"])

    def test_report_only_counts_today(self):
        self.t.records.append({"ts": T0 - 86400, "kind": "bp", "sys": 150, "dia": 95, "pulse": None, "flag": "偏高"})
        self.assertEqual(self.t.daily_report()["bp"], [])


class BusReadbackTest(unittest.TestCase):
    def test_morning_story(self):
        bus, clock, arm = make_bus()
        bus.voice("我刚量了血压 138比85")
        self.assertEqual(said(bus)[-1], "我听到的是高压 138、低压 85，对吗？")
        self.assertEqual(bus.health.records, [])
        self.assertEqual(bus.voice("对")["intent"], "health_confirm")
        self.assertEqual(len(bus.health.records), 1)
        self.assertTrue(any(n["kind"] == "health" and n["contact"] == "daughter" for n in bus.notifications))
        self.assertIn("家属演示页面已记录给女儿", said(bus)[-1])

    def test_correction_replaces_pending(self):
        bus, clock, arm = make_bus()
        bus.voice("血压138比85")
        bus.voice("不对，是138比88")
        self.assertIn("低压 88", said(bus)[-1])
        bus.voice("对")
        self.assertEqual((bus.health.records[0]["sys"], bus.health.records[0]["dia"]), (138, 88))

    def test_plain_no_then_retry(self):
        bus, clock, arm = make_bus()
        bus.voice("血压138比85")
        self.assertEqual(bus.voice("不对")["intent"], "health_reject")
        self.assertEqual(bus.health.records, [])
        self.assertIn("再说一遍", said(bus)[-1])

    def test_yes_without_pending_records_nothing(self):
        bus, clock, arm = make_bus()
        self.assertEqual(bus.voice("对")["intent"], "chitchat")
        self.assertEqual(bus.health.records, [])

    def test_dial_confirm_wins_over_health(self):
        bus, clock, arm = make_bus()
        bus.voice("血压138比85")
        bus.voice("帮我打120")
        bus.voice("确认")
        self.assertTrue(any("已拨打" in e["text"] for e in events(bus, "dial")))
        self.assertIsNotNone(bus.health.pending, "“确认”被 120 用掉，血压仍待确认")


if __name__ == "__main__":
    unittest.main()
