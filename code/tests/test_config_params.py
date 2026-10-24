"""演示参数配置化：启动校验、环境变量、config.local.json、运行中调参、时间覆盖开关。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import events, make_bus

import config


class LoadTest(unittest.TestCase):
    def test_defaults_valid(self):
        self.assertEqual(config.validate(config.load(use_local=False)), [])

    def test_bad_values_rejected_at_startup(self):
        for bad in ({"ask_wait_day": 0}, {"night_start_hour": 24}, {"llm_mode": "gpt"},
                    {"night_override": "maybe"},
                    {"contacts": [{"id": "a", "name": "女儿", "pron": "她"}, {"id": "a", "name": "儿子", "pron": "他"}]},
                    {"contacts": []},
                    {"contacts": [{"name": "女儿", "pron": "她"}]}):
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                config.load(bad, use_local=False)

    def test_expected_utc_offset_default_and_range(self):
        self.assertEqual(config.load(use_local=False)["expected_utc_offset"], 8)
        with self.assertRaises(config.ConfigError, msg="超出 -12..14"):
            config.load({"expected_utc_offset": 20}, use_local=False)
        cfg = config.load(use_local=False)
        self.assertNotIn("expected_utc_offset", config.TUNABLE, "时区预期值只用于自检，运行中不可调")
        self.assertFalse(config.apply_params(cfg, {"expected_utc_offset": 9})[0])

    def test_env_override_and_bad_env(self):
        no_local = mock.patch.object(config, "LOCAL_PATH", Path(tempfile.gettempdir()) / "caremate-none.json")
        with no_local, mock.patch.dict(os.environ, {"CAREMATE_NIGHT_START_HOUR": "22", "CAREMATE_ASK_WAIT_DAY": "30"}):
            cfg = config.load()
            self.assertEqual((cfg["night_start_hour"], cfg["ask_wait_day"]), (22, 30.0))
        with no_local, mock.patch.dict(os.environ, {"CAREMATE_NIGHT_START_HOUR": "晚上"}):
            with self.assertRaises(config.ConfigError) as cm:
                config.load()
            self.assertIn("night_start_hour", str(cm.exception))

    def test_local_json_typo_warns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.local.json"
            p.write_text(json.dumps({"ask_wait_dya": 8, "ask_wait_night": 6, "_备注": "x"}), encoding="utf-8")
            warnings = []
            with mock.patch.object(config, "LOCAL_PATH", p):
                cfg = config.load(warnings=warnings)
            self.assertEqual(cfg["ask_wait_night"], 6.0)
            self.assertEqual(len(warnings), 1)
            self.assertIn("ask_wait_dya", warnings[0])

    def test_llm_mode_without_endpoint_warns(self):
        warnings = []
        config.load({"llm_mode": "llm"}, warnings=warnings, use_local=False)
        self.assertTrue(any("scripted" in w for w in warnings))


class ApplyParamsTest(unittest.TestCase):
    def test_all_or_nothing(self):
        cfg = config.load(use_local=False)
        changed, errors = config.apply_params(cfg, {"ask_wait_day": "8", "ask_wait_night": -1})
        self.assertEqual(changed, {})
        self.assertEqual(len(errors), 1)
        self.assertEqual(cfg["ask_wait_day"], 5.0, "有一项不合法就整批不生效")
        changed, errors = config.apply_params(cfg, {"ask_wait_day": "8", "night_start_hour": 21})
        self.assertEqual((changed, errors), ({"ask_wait_day": 8.0, "night_start_hour": 21}, []))

    def test_secrets_and_contacts_not_tunable(self):
        cfg = config.load(use_local=False)
        for key in ("llm_api_key", "contacts", "arm_url"):
            self.assertFalse(config.apply_params(cfg, {key: "x"})[0], key)


class BusParamsTest(unittest.TestCase):
    def test_new_wait_applies_to_next_fall(self):
        bus, clock, arm = make_bus()
        r = bus.set_params({"ask_wait_day": 8})
        self.assertTrue(r["ok"])
        self.assertIn("ask_wait_day 5→8", events(bus, "system")[-1]["text"])
        bus.fall("卧室")
        clock.advance(7.8, bus)
        self.assertEqual(bus.fsm.state, "ASKING")
        clock.advance(0.4, bus)
        self.assertEqual(bus.fsm.state, "SOS")

    def test_health_window_follows(self):
        bus, clock, arm = make_bus()
        bus.set_params({"health_confirm_window": 40})
        bus.voice("我刚量了血压 138比85")
        clock.advance(30, bus)
        self.assertIsNotNone(bus.health.pending)

    def test_invalid_rejected(self):
        bus, clock, arm = make_bus()
        r = bus.set_params({"dial_confirm_window": 1000})
        self.assertFalse(r["ok"])
        self.assertEqual(bus.cfg["dial_confirm_window"], 10.0)

    def test_night_override_from_config_survives_reset(self):
        bus, clock, arm = make_bus(night_override="on")
        self.assertTrue(bus.is_night(), "测试时钟在上午，但配置强制夜间")
        bus.set_night("auto")
        self.assertFalse(bus.is_night())
        bus.reset()
        self.assertTrue(bus.is_night(), "重置回到配置里的开关，而不是写死 auto")
        bus.fall("卧室")
        self.assertEqual(bus.fsm.wait, 3.0)


if __name__ == "__main__":
    unittest.main()
