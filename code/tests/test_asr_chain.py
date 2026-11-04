"""语音链路：ASR 文本整理、防自激、桥走 /mock/voice、TTS 播放队列。"""
import threading
import unittest

from helpers import make_bus

import master_service
from voice.asr_bridge import Bridge, EchoGuard
from voice.bus_client import EventFollower, post_json
from voice.text_normalize import chinese_to_int, is_filler, normalize
from voice.tts_player import Player


class NormalizeTest(unittest.TestCase):
    def test_numbers(self):
        for run, n in (("一百三十八", 138), ("八十五", 85), ("十八", 18), ("一百零五", 105), ("一百五", 150),
                       ("两千", 2000), ("一二零", 120), ("幺二零", 120), ("三十", 30)):
            self.assertEqual(chinese_to_int(run), n, run)

    def test_sentences(self):
        cases = {
            "我 刚 量 了 血压 一 百 三十 八 比 八十 五": "我刚量了血压138比85",
            "帮 我 打 幺 二 零": "帮我打120",
            "等一下": "等一下",
            "喝了两杯水": "喝了两杯水",
            "十分感谢": "十分感谢",
            "血压138 85": "血压138 85",
            "我刚量了血压138比85": "我刚量了血压138比85",
        }
        for raw, expect in cases.items():
            self.assertEqual(normalize(raw), expect, raw)
            self.assertEqual(normalize(normalize(raw)), expect, "幂等：" + raw)

    def test_filler(self):
        self.assertTrue(is_filler("嗯"))
        self.assertTrue(is_filler("  。"))
        self.assertFalse(is_filler("对"), "“对”是有效回答，不能当噪音")


class EchoGuardTest(unittest.TestCase):
    def test_drop_while_speaking_and_echoes(self):
        g = EchoGuard(clock=lambda: 0.0)
        line = "我听到的是高压 138、低压 85，对吗？"
        g.on_say(line, now=0.0)
        self.assertTrue(g.should_drop("我没事", now=1.0)[0], "播报中")
        after = g.busy_until + 0.1
        self.assertTrue(g.should_drop("我听到的是高压138低压85对吗", now=after)[0], "自己的台词回声")
        self.assertFalse(g.should_drop("对", now=after)[0], "短回答不做相似度过滤")
        self.assertFalse(g.should_drop("把药递给我", now=after)[0])
        self.assertFalse(g.should_drop("我听到的是高压138低压85对吗", now=after + 60)[0], "过了记忆窗口")

    def test_lines_queue_back_to_back(self):
        g = EchoGuard()
        g.on_say("第一句话说完", now=0.0)
        first = g.busy_until
        g.on_say("第二句", now=0.0)
        self.assertGreater(g.busy_until, first)


class BridgeTest(unittest.TestCase):
    def test_fixes_and_normalize_dry_run(self):
        sent = []
        b = Bridge("http://unused", dry_run=True, log=sent.append)
        b.handle("雪 压 一 百 三十 八 比 八十 五")
        b.handle("帮 我 打 腰 二 零")
        b.handle("嗯")
        self.assertEqual(sent, ["→ [dry-run] 血压138比85", "→ [dry-run] 帮我打120"])

    def test_guard_drops_before_post(self):
        logs = []
        g = EchoGuard(clock=lambda: 0.0)
        g.on_say("您还好吗？没事就按绿键", now=0.0)
        self.assertIsNone(Bridge("http://unused", guard=g, dry_run=True, log=logs.append).handle("您还好吗"))
        self.assertIn("丢弃", logs[0])


class LiveBusTest(unittest.TestCase):
    """真起一个总线（随机端口），确认 ASR 桥和 TTS 订阅走的是同一套 HTTP 接口。"""

    @classmethod
    def setUpClass(cls):
        cls.bus, cls.clock, cls.arm = make_bus()
        cls.server = master_service.serve(cls.bus, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_ticker.set()
        cls.server.shutdown()
        cls.server.server_close()

    def test_bridge_posts_same_format_as_mock_voice(self):
        follower = EventFollower(self.base, lambda ev: None, types=["say"])
        follower.poll_once()  # 对齐到当前，不重放历史
        r = Bridge(self.base, log=lambda *_: None).handle("我 刚 量 了 血压 一 百 三十 八 比 八十 五")
        self.assertEqual(r["intent"], "health_record")
        self.assertEqual(self.bus.last_heard["text"], "我刚量了血压138比85")
        says = follower.poll_once()
        self.assertEqual([e["text"] for e in says], ["我听到的是高压 138、低压 85，对吗？"])
        # 手打入口完全等价
        self.assertEqual(post_json(self.base + "/mock/voice", {"text": "对"})["intent"], "health_confirm")


class FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


class PlayerTest(unittest.TestCase):
    def test_backlog_and_reset(self):
        p = Player(FakeSpeaker(), max_backlog=3, log=lambda *_: None)
        for i in range(5):
            p.on_event({"type": "say", "text": "第%d拍" % i})
        self.assertEqual(list(p.q.queue), ["第2拍", "第3拍", "第4拍"], "积压时丢最旧的")
        p.on_event({"type": "heard", "text": "不该念"})
        self.assertEqual(p.q.qsize(), 3)
        p.on_event({"type": "reset", "text": ""})
        self.assertEqual(p.q.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
