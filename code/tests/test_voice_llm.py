"""T5AI 语音键 → 云端大模型 → 预设动作（全程 mock 网络，不打真接口）。"""
import json
import os
import unittest
from unittest.mock import patch

from hand_controller import voice_cmd, voice_llm

PCM = b"\x01\x00" * 16000          # 1 秒
ENV = {"LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "sk-test"}


def fake_post(transcript, intent):
    """第一次调用（流式）= 听写，第二次 = 理解。记下请求体。"""
    calls = []

    def post(body, stream=False):
        calls.append((body, stream))
        if stream:
            return transcript
        return intent if isinstance(intent, str) else json.dumps(intent, ensure_ascii=False)
    return post, calls


class VoiceLLMTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runs, self.stops = [], []
        self.llm = (lambda n, s, b: (self.runs.append((n, s, b)) or (200, {"ok": True})),
                    lambda: self.stops.append(1))

    def handle(self, transcript, intent, dry=False):
        post, calls = fake_post(transcript, intent)
        with patch.object(voice_llm, "_post", post):
            code, body = voice_cmd.handle(PCM, dispatch=None, llm=self.llm, dry=dry)
        return code, body, calls

    def test_preset_from_speech(self):
        code, body, calls = self.handle("跟大家挥挥手吧", {"action": "preset", "preset": "wave", "speed": None, "back": True})
        self.assertEqual((code, body["ok"], body["preset"], body["engine"]), (200, True, "wave", "llm"))
        self.assertEqual(self.runs, [("wave", 1.5, True)])
        asr, intent = calls
        self.assertTrue(asr[1])
        self.assertEqual(asr[0]["model"], voice_llm.ASR_MODEL)
        self.assertEqual(intent[0]["model"], "qwen3.8-max")
        self.assertIn("举手示意", intent[0]["messages"][0]["content"])     # 白名单在提示词里
        self.assertEqual(voice_llm.LAST["heard"], "跟大家挥挥手吧")

    def test_preset_key_written_into_action(self):
        self.handle("跟大家打个招呼", {"action": "wave", "preset": "wave", "speed": None, "back": True})
        self.handle("握个手吧", {"action": "handshake", "preset": None})
        self.handle("来个飞吻", {"action": "kiss", "preset": "kiss"})          # 白名单外：不做
        self.assertEqual(self.runs, [("wave", 1.5, True), ("handshake", 1.5, False)])

    def test_voice_default_speed_is_1_5x(self):
        self.handle("点个赞", {"action": "preset", "preset": "thumbs_up"})
        self.handle("慢慢点个赞", {"action": "preset", "preset": "thumbs_up", "speed": 0.8})
        self.handle("正常速度点个赞", {"action": "preset", "preset": "thumbs_up", "speed": 1})
        self.assertEqual([r[1] for r in self.runs], [1.5, 0.8, 1.0])
        with patch.dict(os.environ, {"VOICE_PRESET_SCALE": "1.2"}):
            self.handle("点个赞", {"action": "preset", "preset": "thumbs_up"})
        self.assertEqual(self.runs[-1][1], 1.2)
        self.assertIn("按 1.5 倍速做", voice_llm._system_prompt())

    def test_voice_presets_stay_by_default(self):
        # 语音做预设：没说「回来」就停在动作最后的位置；模型漏写 / 写错 back 也按不回
        for intent in ({"action": "preset", "preset": "point"},
                       {"action": "preset", "preset": "point", "back": None},
                       {"action": "preset", "preset": "point", "back": "true"}):
            self.handle("指一下前面", intent)
        self.handle("指一下前面，指完收回来", {"action": "preset", "preset": "point", "back": True})
        self.assertEqual(self.runs, [("point", 1.5, False)] * 3 + [("point", 1.5, True)])
        prompt = voice_llm._system_prompt()
        self.assertIn("back：默认 false", prompt)

    def test_business_actions_go_through_dispatch(self):
        sent = []
        disp = lambda *a: (sent.append(a) or (200, {"ok": True}))
        post, _ = fake_post("救命啊", {"action": "business", "business": "press_sos"})
        with patch.object(voice_llm, "_post", post):
            code, body = voice_cmd.handle(PCM, disp, llm=self.llm)
        self.assertEqual(sent, [("press_sos", "1", "/voice_cmd", "voice:llm")])
        self.assertEqual(body["business"], "press_sos")
        post, _ = fake_post("回去待着吧", {"action": "home"})
        with patch.object(voice_llm, "_post", post):
            voice_cmd.handle(PCM, disp, llm=self.llm)
        self.assertEqual(sent[-1][0], "home")
        post, _ = fake_post("把门打开", {"action": "business", "business": "open_door"})   # 白名单外
        with patch.object(voice_llm, "_post", post):
            code, body = voice_cmd.handle(PCM, disp, llm=self.llm)
        self.assertEqual(len(sent), 2)
        self.assertFalse(body["ok"])
        self.assertEqual(self.runs, [])

    def test_scripts_by_voice(self):
        scripts = {"递物演示": "说法：递水、拿东西给老人；步骤：去「水杯」、握紧3、去「老人」"}
        ran = []
        llm = self.llm + ((lambda: scripts), (lambda name, speed: (ran.append((name, speed)) or (200, {"ok": True}))))
        for intent in ({"action": "script", "script": "递物演示"}, {"action": "递物演示"},
                       {"action": "script", "script": "递物演示", "speed": 0.8}):
            post, calls = fake_post("帮我递瓶水", intent)
            with patch.object(voice_llm, "_post", post):
                code, body = voice_cmd.handle(PCM, None, llm=llm)
            self.assertEqual((code, body["action"], body["script"]), (200, "script", "递物演示"))
        self.assertEqual(ran, [("递物演示", None), ("递物演示", None), ("递物演示", 0.8)])
        self.assertIn("递物演示：说法：递水", calls[1][0]["messages"][0]["content"])   # 脚本和说法进了提示词
        post, _ = fake_post("帮我开门", {"action": "script", "script": "开门脚本"})      # 不存在的脚本：不跑
        with patch.object(voice_llm, "_post", post):
            code, body = voice_cmd.handle(PCM, None, llm=llm)
        self.assertFalse(body["ok"])
        self.assertEqual(len(ran), 3)
        self.assertEqual(self.runs, [])

    def test_finger_exercise_by_voice(self):
        sent = []
        disp = lambda *a: (sent.append(a) or (200, {"ok": True}))
        for intent in ({"action": "gesture", "gesture": "hand_exercise"}, {"action": "hand_exercise"}):
            post, calls = fake_post("带我做一下手指操", intent)
            with patch.object(voice_llm, "_post", post):
                code, body = voice_cmd.handle(PCM, disp, llm=self.llm)
            self.assertEqual((code, body["action"], body["gesture"]), (200, "gesture", "hand_exercise"))
        self.assertEqual(sent, [("hand_exercise", "1", "/voice_cmd", "voice:llm")] * 2)
        self.assertIn("十三拍手指操", calls[1][0]["messages"][0]["content"])
        post, _ = fake_post("跳个舞", {"action": "gesture", "gesture": "dance"})      # 白名单外
        with patch.object(voice_llm, "_post", post):
            code, body = voice_cmd.handle(PCM, disp, llm=self.llm)
        self.assertFalse(body["ok"])
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.runs, [])
        # 离线兜底：「手指操 / 做操」也是十三拍
        self.assertEqual(voice_cmd.KEYWORDS["手指操"], "hand_exercise")
        self.assertEqual(voice_cmd.KEYWORDS["做操"], "hand_exercise")
        with patch.dict(os.environ, {"LLM_API_KEY": ""}), patch.object(voice_cmd, "recognize", return_value=("做 手指操", 0.0)):
            code, body = voice_cmd.handle(PCM, disp, llm=self.llm)
        self.assertEqual(sent[-1][0], "hand_exercise")

    def test_speed_and_hold(self):
        self.handle("慢慢举个手，举着别放下", {"action": "preset", "preset": "raise_hand", "speed": 0.6, "back": False})
        self.assertEqual(self.runs, [("raise_hand", 0.6, False)])
        self.runs.clear()
        self.handle("飞快地点个赞", {"action": "preset", "preset": "thumbs_up", "speed": 9, "back": True})
        self.assertEqual(self.runs, [("thumbs_up", 2.0, True)])        # 倍速夹在 0.5~2

    def test_stop_word_skips_the_llm(self):
        code, body, calls = self.handle("快停下", {"action": "preset", "preset": "wave"})
        self.assertEqual((body["action"], self.stops, self.runs), ("stop", [1], []))
        self.assertEqual(len(calls), 1)                                  # 只听写，不等理解模型
        self.handle("挥手然后停在那", {"action": "preset", "preset": "wave", "back": False})
        self.assertEqual(self.runs, [("wave", 1.5, False)])               # 「停在那」不是急停

    def test_only_whitelisted_presets_run(self):
        for intent in ({"action": "preset", "preset": "press_sos"}, {"action": "rm -rf"}, {"action": "none"}, "我觉得是挥手"):
            code, body, _ = self.handle("嗯……", intent)
            self.assertFalse(body["ok"])
        self.assertEqual(self.runs, [])

    def test_dry_run_and_silence(self):
        code, body, _ = self.handle("握个手", {"action": "preset", "preset": "handshake"}, dry=True)
        self.assertEqual((body["ok"], body["dry"], self.runs), (True, True, []))
        code, body, calls = self.handle("", {"action": "preset", "preset": "wave"})
        self.assertFalse(body["ok"])
        self.assertEqual(len(calls), 1)

    def test_network_failure_falls_back_to_offline(self):
        def boom(body, stream=False):
            raise voice_llm.LLMError("连不上大模型")
        sent = []
        with patch.object(voice_llm, "_post", boom), \
                patch.object(voice_cmd, "recognize", return_value=("挥手", 0.0)):
            code, body = voice_cmd.handle(PCM, lambda *a: (sent.append(a) or (200, {"ok": True})), llm=self.llm)
        self.assertEqual(sent[0][0], "wave")
        self.assertNotIn("engine", body)
        self.assertEqual(self.runs, [])

    def test_not_configured_uses_offline(self):
        with patch.dict(os.environ, {"LLM_API_KEY": ""}), patch.object(voice_cmd, "recognize", return_value=("点赞", 0.0)):
            code, body = voice_cmd.handle(PCM, lambda *a: (200, {"ok": True}), llm=self.llm)
        self.assertEqual(body["keyword"], "点赞")


if __name__ == "__main__":
    unittest.main()
