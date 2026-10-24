"""T5AI 语音键 → 云端大模型：听写 → 理解 → 执行「臂手协同 · 预设动作」。

POST /voice_cmd 的 16 kHz / 16 bit / 单声道 PCM：
  1. 听写：ASR_MODEL（默认 qwen3.5-omni-plus；qwen3.8-max 不收音频）转成文字
  2. 「停 / 别动 / 停下」这类词：本地直接急停，不等大模型
  3. 理解：INTENT_MODEL（默认 qwen3.8-max）只在预设白名单里挑一个，或判定 none；拿不准就 none
  4. 执行：和页面上的预设按钮同一条路（ArmWorker.teach('preset')），照样整条安全预检
网络 / 大模型出错抛 LLMError，voice_cmd 退回 vosk 离线关键词。

环境变量（X5 上在 /root/caremate/unit.env，600 权限）：LLM_BASE_URL、LLM_API_KEY（OpenAI 兼容接口，阿里云百炼）；
  可选 VOICE_ASR_MODEL、VOICE_INTENT_MODEL、VOICE_PRESET_SCALE（默认 1.5）、VOICE_LLM_TIMEOUT（秒，默认 12）。
"""
import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
import wave

from hand_controller.arm_presets import PRESETS

ASR_MODEL = os.environ.get("VOICE_ASR_MODEL", "qwen3.5-omni-plus")
INTENT_MODEL = os.environ.get("VOICE_INTENT_MODEL", "qwen3.8-max")
TIMEOUT = float(os.environ.get("VOICE_LLM_TIMEOUT", "12"))
STOP_WORDS = ("停下", "停止", "停住", "别动", "不要动", "暂停", "急停")   # 不放单字「停」：「停在那」是保持姿态
SCALE_RANGE = (0.5, 2.0)
VOICE_DEFAULT_SCALE = 1.5   # 语音做预设默认倍速（现场要求，10-25）；VOICE_PRESET_SCALE 可改

# 业务动作：离线识别时代语音就能触发的那几个，走和 T5AI 按键同一条 dispatch（绑定了脚本就跑脚本）
BUSINESS = {
    "press_sos": "按呼叫铃求救（老人说救命、喊人、不舒服、摔倒了）",
    "deliver_medicine": "递药盒（把药递给老人）",
    "return_medicine": "还药盒（把药盒收回放好）",
    "home": "归位 / 回零位 / 回去待着（回到零位待机点：手张开、全部关节回 0°）",
}

# 只动手的手势（不动臂）：走和页面手势按钮同一条 dispatch
GESTURES = {
    "hand_exercise": "十三拍手指操（带老人一起做手指操，十三拍，一根根手指依次弯曲伸直；说「手指操 / 做操 / 活动活动手指」）",
}

LAST = {}     # 最近一次：听到什么、理解成什么、执行结果、各段耗时（/status 和页面看）


class LLMError(Exception):
    """网络、鉴权、模型返回格式不对：调用方退回离线识别。"""


def configured():
    return bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"))


def _post(body, stream=False):
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode("utf-8"),
                                 {"Authorization": "Bearer " + os.environ["LLM_API_KEY"],
                                  "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise LLMError("%s 返回 HTTP %s：%s" % (body["model"], e.code, e.read().decode("utf-8", "replace")[:160]))
    except (urllib.error.URLError, OSError) as e:
        raise LLMError("连不上大模型：%s" % e)
    if not stream:
        try:
            return json.loads(raw)["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError("%s 回包看不懂：%s" % (body["model"], e))
    text = ""
    for line in raw.splitlines():     # omni 模型在兼容模式下只支持流式
        if line.startswith("data:") and "[DONE]" not in line:
            try:
                for c in json.loads(line[5:]).get("choices", []):
                    text += (c.get("delta") or {}).get("content") or ""
            except ValueError:
                continue
    return text


def _wav_b64(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def transcribe(pcm):
    body = {"model": ASR_MODEL, "stream": True, "modalities": ["text"], "messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + _wav_b64(pcm), "format": "wav"}},
        {"type": "text", "text": "把这段语音逐字转写成简体中文，只输出转写的文字；没有人说话就输出空。"}]}]}
    return _post(body, stream=True).strip().strip("。.!！")


def _system_prompt(scripts=None):
    menu = "\n".join("- %s：%s。%s" % (k, p["label"], p["desc"]) for k, p in PRESETS.items())
    biz = "\n".join("- %s：%s" % kv for kv in BUSINESS.items())
    gest = "\n".join("- %s：%s" % kv for kv in GESTURES.items())
    scr = "\n".join("- %s：%s" % kv for kv in (scripts or {}).items())
    return (
        "你是养老陪护机器人（一条机械臂 + 灵巧手）的语音指令解析器。用户的话来自语音转写，可能有同音错字，按发音理解。\n"
        "只能从下面的预设动作里选一个执行：\n" + menu + "\n\n"
        "或者下面的业务动作之一（action 为 business，键名放在 business）：\n" + biz + "\n\n"
        "或者下面的手部手势之一（只动手不动臂；action 为 gesture，键名放在 gesture）：\n" + gest + "\n\n"
        + ("或者现场编排好的脚本之一（action 为 script，脚本名原样放在 script；一个脚本是一整套连贯动作，"
           "用户要的事情和某个脚本的说法 / 步骤对得上就优先选脚本）：\n" + scr + "\n\n" if scr else "") +
        "规则：\n"
        "1. 只有用户明确想让机器人做其中某个动作时才选它；闲聊、听不清、和这些动作对不上、拿不准，一律 action 为 none。\n"
        "2. 用户要停下 / 别动 → action 为 stop。老人呼救、说救命或不舒服 → business 的 press_sos。\n"
        "3. speed：默认 null（按 1.5 倍速做）；用户说「慢一点 / 慢慢」给 0.8，「快一点」给 2，「正常速度」给 1。\n"
        "4. back：默认 false（做完停在动作最后的位置）；只有用户明确说「做完回来 / 收回去 / 回到原位」才给 true。\n"
        "只输出 JSON，不要别的字："
        '{"action": "preset" | "business" | "gesture" | "script" | "stop" | "none", "preset": "预设键名或 null", '
        '"gesture": "手势键名或 null", '
        '"business": "业务键名或 null", "script": "脚本名或 null", "speed": 数字或 null, '
        '"back": true 或 false, "reason": "一句话说明"}\n'
        "例：「跟大家打个招呼」→ {\"action\": \"preset\", \"preset\": \"wave\", \"speed\": null, \"back\": false, "
        "\"reason\": \"要打招呼\"}；「今天天气真好」→ {\"action\": \"none\", \"preset\": null, \"speed\": null, "
        "\"back\": false, \"reason\": \"闲聊\"}"
    )


def understand(text, scripts=None):
    scripts = scripts or {}
    body = {"model": INTENT_MODEL, "enable_thinking": False, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _system_prompt(scripts)}, {"role": "user", "content": text}]}
    raw = _post(body)
    try:
        intent = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except ValueError:
        raise LLMError("理解模型没给 JSON：%s" % raw[:80])
    action, preset = intent.get("action"), intent.get("preset")
    business, script = intent.get("business"), intent.get("script")
    if action in scripts or (action == "script" and script in scripts) or \
            (action not in ("preset", "business", "stop", "none") and script in scripts):
        speed = intent.get("speed")
        try:
            speed = min(max(float(speed), SCALE_RANGE[0]), SCALE_RANGE[1]) if speed is not None else None
        except (TypeError, ValueError):
            speed = None
        return {"action": "script", "script": action if action in scripts else script, "preset": None,
                "speed": speed, "back": True, "reason": str(intent.get("reason", ""))[:80]}
    if action == "script":                      # 说的脚本名不在列表里：不做
        action = "none"
    gesture = intent.get("gesture")
    if action in GESTURES or (action == "gesture" and gesture in GESTURES) or \
            (action not in ("preset", "business", "stop", "none") and gesture in GESTURES):
        return {"action": "gesture", "gesture": action if action in GESTURES else gesture, "preset": None,
                "speed": None, "back": True, "reason": str(intent.get("reason", ""))[:80]}
    if action == "gesture":                     # 白名单外的手势：不做
        action = "none"
    if action in BUSINESS or (action == "business" and business in BUSINESS) or \
            (action not in ("preset", "stop", "none") and business in BUSINESS):
        return {"action": "business", "business": action if action in BUSINESS else business, "preset": None,
                "speed": None, "back": True, "reason": str(intent.get("reason", ""))[:80]}
    if action in PRESETS:                       # 模型有时把键名写进 action：{"action": "wave"}
        action, preset = "preset", preset if preset in PRESETS else action
    elif action not in ("preset", "stop", "none") and preset in PRESETS:
        action = "preset"
    if action == "preset" and preset not in PRESETS:   # 白名单外的一律不做
        action = "none"
    if action not in ("preset", "stop", "none"):
        action = "none"
    intent["preset"] = preset
    speed = intent.get("speed")
    try:
        speed = min(max(float(speed), SCALE_RANGE[0]), SCALE_RANGE[1]) if speed is not None else None
    except (TypeError, ValueError):
        speed = None
    return {"action": action, "preset": intent.get("preset") if action == "preset" else None,
            "speed": speed, "back": intent.get("back") is True,     # 语音做预设：默认做完停在原处，说「回来」才回
            "reason": str(intent.get("reason", ""))[:80]}


def handle(pcm, run_preset, stop_all, list_scripts=None, run_script=None, dispatch=None, dry=False):
    """一段录音 → (HTTP 码, 回包)。run_preset(键, 倍速, 是否回出发姿态) / stop_all() 由 arm_service 给；
    list_scripts() → {脚本名: 说法和步骤摘要}，run_script(名, 倍速)；dispatch = 按键那条路（业务动作用）。"""
    t0 = time.monotonic()
    text = transcribe(pcm)
    t1 = time.monotonic()
    rec = {"ts": time.time(), "heard": text, "asr_model": ASR_MODEL, "asr_s": round(t1 - t0, 2)}
    LAST.clear()
    LAST.update(rec)
    if not text:
        LAST.update(action="none", reason="没听到话")
        return 200, {"ok": False, "heard": "", "error": "没听到说话"}
    if any(w in text for w in STOP_WORDS):        # 停：本地马上停，不等大模型
        LAST.update(action="stop", reason="停止词，本地直接停")
        if not dry:
            stop_all()
        return 200, {"ok": True, "heard": text, "action": "stop", "dry": dry}
    intent = understand(text, list_scripts() if list_scripts else None)
    LAST.update(intent, intent_model=INTENT_MODEL, intent_s=round(time.monotonic() - t1, 2))
    body = dict(intent, heard=text, dry=dry)
    if intent["action"] == "stop":
        if not dry:
            stop_all()
        return 200, dict(body, ok=True)
    if intent["action"] == "none":
        return 200, dict(body, ok=False, error="没听出要做哪个预设动作")
    if intent["action"] == "script":           # 现场编排的整套动作：和页面「运行」同一条路（整条安全预检）
        if dry or run_script is None:
            return 200, dict(body, ok=dry, error=None if dry else "这里不能跑脚本")
        code, result = run_script(intent["script"], intent["speed"])
        LAST.update(code=code, error=result.get("error"))
        return code, dict(body, ok=code == 200, error=result.get("error"))
    if intent["action"] == "gesture":          # 只动手：和页面手势按钮同一条 dispatch
        if dry or dispatch is None:
            return 200, dict(body, ok=dry, error=None if dry else "这里不能做手势")
        code, result = dispatch(intent["gesture"], "1", "/voice_cmd", "voice:llm")
        LAST.update(code=code, error=result.get("error"))
        return code, dict(body, ok=code == 200, error=result.get("error"))
    if intent["action"] == "business":
        if dry or dispatch is None:
            return 200, dict(body, ok=dry, error=None if dry else "这里不能做业务动作")
        code, result = dispatch(intent["business"], "1", "/voice_cmd", "voice:llm")
        LAST.update(code=code, error=result.get("error"))
        return code, dict(body, ok=code == 200, error=result.get("error"))
    scale = intent["speed"] or float(os.environ.get("VOICE_PRESET_SCALE", str(VOICE_DEFAULT_SCALE)))
    LAST["scale"] = scale
    if dry:
        return 200, dict(body, ok=True, scale=scale)
    code, result = run_preset(intent["preset"], scale, intent["back"])
    LAST.update(code=code, error=result.get("error"))
    return code, dict(body, ok=code == 200, scale=scale, error=result.get("error"))


def status():
    return {"llm": configured(), "asr_model": ASR_MODEL, "intent_model": INTENT_MODEL, "last": dict(LAST)}
