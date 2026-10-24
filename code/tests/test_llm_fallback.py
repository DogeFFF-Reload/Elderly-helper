"""llm 通道：超时 / 出错自动回落 scripted、冷却、内容检查、不占总线锁、紧急台词插队。"""
import threading
import time
import unittest
import urllib.error
from unittest import mock

from helpers import events, make_bus, said

import config
from agent.llm_client import LLMClient

SCRIPTED_DELIVER = "好的，药盒递过来了，您慢慢拿，拿稳了我再收回来。"


class FakeLLM:
    """假接口：reply 可以是字符串、异常，或 sleep 秒数（模拟卡住）。"""

    def __init__(self, reply="好嘞，药盒给您递过来啦，您慢慢拿。", sleep=0.0):
        self.reply = reply
        self.sleep = sleep
        self.calls = []

    def __call__(self, messages, timeout):
        self.calls.append(messages)
        if self.sleep:
            time.sleep(self.sleep)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Now:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def client(transport, mode="llm", timeout=0.3, cooldown=60.0, now=time.monotonic):
    return LLMClient(mode, timeout=timeout, cooldown=cooldown, keep_words=["女儿", "儿子"],
                     transport=transport, now=now)


class ClientTest(unittest.TestCase):
    def test_scripted_mode_never_calls(self):
        fake = FakeLLM()
        text, info = client(fake, mode="scripted").render("deliver_start", {})
        self.assertEqual(text, SCRIPTED_DELIVER)
        self.assertEqual(fake.calls, [])
        self.assertFalse(client(None, mode="llm").enabled, "llm 模式没配接口 = 没开")

    def test_llm_polishes_whitelisted_line(self):
        fake = FakeLLM()
        text, info = client(fake).render("deliver_start", {})
        self.assertEqual((text, info["source"]), ("好嘞，药盒给您递过来啦，您慢慢拿。", "llm"))

    def test_safety_lines_never_go_to_llm(self):
        fake = FakeLLM()
        c = client(fake)
        for key, params in (("sos_sent", {"contact": "女儿"}), ("dial_confirm", {"secs": 10}),
                            ("health_readback", {"sys": 138, "dia": 85}), ("ask_ok", {"room": "卧室"})):
            text, info = c.render(key, params)
            self.assertEqual(info["source"], "scripted", key)
        self.assertEqual(fake.calls, [])

    def test_timeout_falls_back_fast_then_cools_down(self):
        fake = FakeLLM(sleep=2.0)
        c = client(fake, timeout=0.3)
        t0 = time.monotonic()
        text, info = c.render("deliver_start", {})
        self.assertLess(time.monotonic() - t0, 0.8, "整句计时：0.3 秒必须回来")
        self.assertEqual(text, SCRIPTED_DELIVER)
        self.assertEqual((info["reason"], info["tripped"]), ("timeout", True))
        t0 = time.monotonic()
        text, info = c.render("cancel_ok", {})
        self.assertLess(time.monotonic() - t0, 0.1, "冷却期内不再干等")
        self.assertEqual((info["reason"], info["tripped"]), ("cooldown", False))
        self.assertEqual(len(fake.calls), 1)

    def test_cooldown_expires(self):
        now = Now()
        fake = FakeLLM(reply=urllib.error.URLError("network down"))
        c = client(fake, cooldown=60, now=now)
        self.assertEqual(c.render("deliver_start", {})[1]["reason"], "error")
        now.t += 59
        c.render("deliver_start", {})
        self.assertEqual(len(fake.calls), 1)
        now.t += 2
        fake.reply = "好嘞，药盒给您递过来啦，您慢慢拿。"
        self.assertEqual(c.render("deliver_start", {})[1]["source"], "llm")
        self.assertIn("URLError", c.status()["last_error"])

    def test_content_check(self):
        c = client(FakeLLM())
        cases = {
            "好嘞，药盒来了，我顺便帮您叫120。": "120",
            "药盒递给您了，已经告诉儿子了。": "称呼",
            "OK，药盒给您。": "英文",
            "药盒递过来了，记得吃 2 片。": "数字",
            "药盒递过来了，记得一次两片。": "中文数量",
            "药" * 80: "太长",
        }
        for reply, why in cases.items():
            c.transport = FakeLLM(reply)
            text, info = c.render("deliver_start", {})
            self.assertEqual(text, SCRIPTED_DELIVER, why)
            self.assertTrue(info["reason"].startswith("rejected"), why)
        c.transport = FakeLLM("好，已经给您女儿发了消息，让她给您回电话。")
        text, info = c.render("call_family", {"contact": "女儿", "pron": "她"})
        self.assertEqual(info["source"], "scripted", "通知渠道说明不能被润色成已外部送达")
        self.assertIn("家属演示页面", text)

    def test_notification_channel_disclosures_never_polished(self):
        fake = FakeLLM(reply="已经告诉女儿了。")
        c = client(fake)
        for key in ("call_family", "message_sent", "safe_report"):
            text, info = c.render(key, {"contact": "女儿", "pron": "她"})
            self.assertEqual(info["source"], "scripted")
            self.assertIn("家属演示页面", text)
        self.assertEqual(fake.calls, [])
        c.transport = FakeLLM("记下了，今天喝了 2 杯，一共 5 杯水。")
        self.assertEqual(c.render("water_saved", {"cups": 2, "today": 5})[1]["source"], "llm", "数字一致可以放行")

    def test_aliases_count_as_names(self):
        cfg = config.load({"llm_mode": "llm"}, use_local=False)
        c = LLMClient.from_config(cfg, transport=FakeLLM("药盒递过来了，我跟小军也说了。"))
        text, info = c.render("deliver_start", {})
        self.assertEqual(text, SCRIPTED_DELIVER)
        self.assertIn("小军", info["reason"])

    def test_abort_when_queue_busy(self):
        fake = FakeLLM(sleep=1.0)
        c = client(fake, timeout=2.0)
        t0 = time.monotonic()
        text, info = c.render("deliver_start", {}, abort=lambda: time.monotonic() - t0 > 0.1)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual((text, info["reason"], info["tripped"]), (SCRIPTED_DELIVER, "queue_busy", False))


class BusTest(unittest.TestCase):
    def test_slow_llm_does_not_block_safety_kernel(self):
        fake = FakeLLM(sleep=2.0)
        bus, clock, arm = make_bus(llm_transport=fake, llm_timeout=0.5)
        t0 = time.monotonic()
        bus.voice("把药递给我")
        bus.fall("卧室")
        self.assertLess(time.monotonic() - t0, 0.2, "LLM 卡住时，跌倒处理不能等它")
        self.assertEqual(bus.fsm.state, "ASKING")
        self.assertEqual(arm.poses(), ["deliver_medicine"])
        bus.speech.drain()
        self.assertLess(time.monotonic() - t0, 0.5, "后面有紧急台词排队 → 不等 LLM")
        lines = said(bus)
        self.assertEqual(lines[0], SCRIPTED_DELIVER)
        self.assertIn("您还好吗", lines[1])

    def test_timeout_logged_once_and_spoken_scripted(self):
        fake = FakeLLM(sleep=2.0)
        bus, clock, arm = make_bus(llm_transport=fake, llm_timeout=0.5)
        bus.voice("把药递给我")
        bus.speech.drain()
        bus.voice("做个手指操吧")
        bus.speech.drain()
        self.assertEqual(said(bus)[0], SCRIPTED_DELIVER)
        notes = [e for e in events(bus, "system") if "LLM" in e["text"]]
        self.assertEqual(len(notes), 1, "只在刚进冷却时记一次")
        self.assertIn("超时", notes[0]["text"])
        self.assertEqual(bus.snapshot()["llm"]["fallback"], 2)

    def test_llm_line_spoken_and_chitchat_gets_context(self):
        fake = FakeLLM(reply="我在呢，您想做点什么？")
        bus, clock, arm = make_bus(llm_transport=fake)
        bus.voice("今天天气真好")
        bus.speech.drain()
        self.assertEqual(said(bus)[-1], "我在呢，您想做点什么？")
        self.assertIn("今天天气真好", fake.calls[0][-1]["content"])

    def test_reset_drops_pending_lines(self):
        fake = FakeLLM(sleep=0.3)
        bus, clock, arm = make_bus(llm_transport=fake, llm_timeout=1.0)
        bus.voice("把药递给我")
        bus.reset()
        bus.speech.drain()
        self.assertEqual(said(bus), [], "重置前没播完的台词作废")

    def test_worker_survives_unexpected_error(self):
        bus, clock, arm = make_bus(llm_transport=FakeLLM())
        bus.llm.render = mock.Mock(side_effect=RuntimeError("bug"))
        bus.voice("把药递给我")
        bus.speech.drain()
        self.assertEqual(said(bus)[-1], SCRIPTED_DELIVER, "出意外也退回 scripted")
        bus.fall("卧室")
        bus.speech.drain()
        self.assertIn("您还好吗", said(bus)[-1], "台词线程还活着，紧急台词照常播")

    def test_bus_lock_free_while_llm_thinks(self):
        fake = FakeLLM(sleep=0.5)
        bus, clock, arm = make_bus(llm_transport=fake, llm_timeout=1.0)
        bus.voice("把药递给我")
        got = threading.Event()
        threading.Thread(target=lambda: (bus.tick(), got.set()), daemon=True).start()
        self.assertTrue(got.wait(0.2), "LLM 生成期间状态机 tick 照常")
        bus.speech.drain()


if __name__ == "__main__":
    unittest.main()
