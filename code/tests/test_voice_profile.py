"""回声代聊：授权 → 旁听 → 代聊 → 收回；风格档案只有文字特征。"""
import json
import os
import tempfile
import unittest

from helpers import Clock, make_bus, said

import agent.voice_profile as vp
from agent.voice_profile import StyleProfile, extract_style

DAUGHTER_MSGS = ["妈，记得按时吃药哈", "妈，晚上早点睡哈", "妈，记得按时吃药哈，周末回来看您"]


class ExtractTest(unittest.TestCase):
    def test_style_features(self):
        st = extract_style(DAUGHTER_MSGS)
        self.assertEqual(st["address"], "妈")
        self.assertEqual(st["particle"], "哈")
        self.assertIn("记得按时吃药", st["catchphrases"])

    def test_text_features_only(self):
        self.assertEqual(set(extract_style(DAUGHTER_MSGS)), {"address", "catchphrases", "particle"})

    def test_renamed_to_style_profile(self):
        src = open(vp.__file__, encoding="utf-8").read()
        self.assertNotIn("指纹", src)
        self.assertNotIn("fingerprint", src.lower())


class GateTest(unittest.TestCase):
    def test_no_learning_or_speaking_without_authorization(self):
        p = StyleProfile(None, Clock())
        self.assertFalse(p.observe("daughter", "妈，记得按时吃药哈"))
        self.assertIsNone(p.speak("daughter"))
        self.assertEqual(p.snapshot(), {})

    def test_speak_reuses_only_her_words(self):
        p = StyleProfile(None, Clock())
        p.authorize("daughter", DAUGHTER_MSGS)
        line = p.speak("daughter", "她")
        self.assertTrue(line.startswith("让我替她说两句："))
        body = line.split("：", 1)[1].lstrip("妈，").rstrip("哈")
        self.assertTrue(any(body in m for m in DAUGHTER_MSGS), body)

    def test_son_uses_he(self):
        p = StyleProfile(None, Clock())
        p.authorize("son", ["爸，少喝点酒"])
        self.assertTrue(p.speak("son", "他").startswith("让我替他说两句"))


class EchoCycleTest(unittest.TestCase):
    def test_authorize_listen_echo_revoke(self):
        with tempfile.TemporaryDirectory() as d:
            bus, clock, arm = make_bus(data_dir=d)
            path = os.path.join(d, "voice_profile.json")

            bus.family_message("daughter", DAUGHTER_MSGS[0])
            bus.voice("我想女儿了")
            self.assertIn("还没开启回声代聊", said(bus)[-1])

            # 授权：亲按后才学，授权前的留言一并纳入（家属已同意）
            bus.echo_authorize("daughter")
            self.assertEqual(bus.style.snapshot()["daughter"]["sample_count"], 1)
            # 旁听
            self.assertTrue(bus.family_message("daughter", DAUGHTER_MSGS[1])["learned"])
            bus.family_message("daughter", DAUGHTER_MSGS[2])
            # 代聊
            bus.voice("我想女儿了")
            line = said(bus)[-1]
            self.assertTrue(line.startswith("让我替她说两句"))
            self.assertIn(DAUGHTER_MSGS[2], line, "附上她最近一条真实留言")

            # 收回：立刻清空，文件里也没了
            bus.echo_revoke("daughter")
            self.assertNotIn("daughter", bus.style.snapshot())
            self.assertNotIn("daughter", json.loads(open(path, encoding="utf-8").read()))
            bus.voice("我想女儿了")
            self.assertIn("还没开启回声代聊", said(bus)[-1])

    def test_file_has_no_audio_or_biometrics(self):
        with tempfile.TemporaryDirectory() as d:
            bus, clock, arm = make_bus(data_dir=d)
            bus.echo_authorize("daughter")
            for m in DAUGHTER_MSGS:
                bus.family_message("daughter", m)
            raw = open(os.path.join(d, "voice_profile.json"), encoding="utf-8").read().lower()
            for word in ("audio", "wav", "voiceprint", "embedding", "声纹"):
                self.assertNotIn(word, raw)


if __name__ == "__main__":
    unittest.main()
