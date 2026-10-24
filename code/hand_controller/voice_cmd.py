"""T5AI 大屏的语音键：POST /voice_cmd，正文是 16 kHz / 16 bit / 单声道裸 PCM（屏上录 4 秒，净得约 3 秒）。

vosk 离线识别 → 按关键词（长词优先）找动作 → 和按键走同一条路（arm_service.dispatch，手#1）。
词表取自 T5AI 交接包里 kit 版 arm_service 的 VOICE_CMDS / VOICE_ARM，换成仓库的动作名。
会动臂的只有四个：递药、还药、按铃、归位；其余是手#1 的手势。

模型：VOSK_MODEL（默认 /root/caremate/vosk-cn，vosk-model-small-cn-0.22），第一次用到才加载；
加载不了只让 /voice_cmd 回 503，别的接口照常。依赖：pip install vosk（X5 上装在系统 python3）。
"""
import json
import os
import random
import threading
import time

MODEL_DIR = os.environ.get("VOSK_MODEL", "/root/caremate/vosk-cn")
RATE = 16000
MIN_BYTES = 8000          # 0.25 秒；再短就是误触
MAX_BYTES = 1 << 20       # 约 30 秒；屏上只录 4 秒
MAX_UNK = 0.5             # 说的话一半以上不在词表里 = 闲聊，不当指令（见 recognize）

RPS = "rps_random"        # 猜拳：随机出一手
KEYWORDS = {
    # 会动臂
    "递药": "deliver_medicine", "送药": "deliver_medicine", "发药": "deliver_medicine",
    "拿药": "deliver_medicine", "药盒": "deliver_medicine",
    "还药": "return_medicine", "交回": "return_medicine", "放回": "return_medicine",
    "按铃": "press_sos", "按铃铛": "press_sos", "呼叫": "press_sos", "救命": "press_sos", "紧急": "press_sos",
    "归位": "home", "复位": "home", "回去": "home",
    # 只动手#1
    "挥手": "wave", "你好": "wave", "招手": "wave", "打招呼": "wave",
    "点赞": "thumbs_up", "大拇指": "thumbs_up", "棒": "thumbs_up", "厉害": "thumbs_up", "夸": "thumbs_up",
    "握拳": "clench_open", "张开": "clench_open", "握住": "clench_open", "抓": "clench_open", "拳": "clench_open",
    "石头": RPS, "剪刀": RPS, "布": RPS, "猜拳": RPS, "玩": RPS,
    "手指操": "count_five", "手指": "count_five", "做操": "count_five", "操": "count_five",
    "锻炼": "count_five", "动动": "count_five",
}
_BY_LENGTH = sorted(KEYWORDS, key=len, reverse=True)


class VoiceError(Exception):
    pass


_MODEL = {"model": None, "grammar": None, "error": None}
_MU = threading.Lock()
LAST = {}                 # 最近一次识别，/status 看


def segment(word, known):
    """按模型词表把关键词切成词（最长优先），切不开返回 None。
    小模型词表里没有「递药」「还药」「按铃」这类词，整词放进 grammar 会被 vosk 丢掉，要写成「递 药」。"""
    out, i = [], 0
    while i < len(word):
        for j in range(len(word), i, -1):
            if known(word[i:j]):
                out.append(word[i:j])
                i = j
                break
        else:
            return None
    return " ".join(out)


def grammar(known):
    """识别只在这些短语里挑；切不开的关键词（如「按铃铛」的铛）不放，靠更短的「按铃」兜住。"""
    phrases = sorted({p for p in (segment(kw, known) for kw in KEYWORDS) if p})
    return phrases + ["[unk]"]


def _model():
    """加载 vosk 模型；失败记原因，下次再试（装好模型不用重启服务）。"""
    with _MU:
        if _MODEL["model"] is None:
            try:
                from vosk import Model, SetLogLevel
                SetLogLevel(-1)
                if not os.path.isdir(MODEL_DIR):
                    raise FileNotFoundError("没有模型目录 %s（VOSK_MODEL）" % MODEL_DIR)
                model = Model(MODEL_DIR)
                find = getattr(model, "vosk_model_find_word", None) or model.find_word
                _MODEL["grammar"] = json.dumps(grammar(lambda w: find(w) != -1), ensure_ascii=False)
                _MODEL["model"], _MODEL["error"] = model, None
            except Exception as e:  # noqa: BLE001 识别不了只影响语音键
                _MODEL["error"] = "%s：%s" % (type(e).__name__, e)
        return _MODEL["model"]


def preload():
    """服务起来时后台先加载（X5 上要几秒），第一次按语音键就不用等。"""
    ok = _model() is not None
    print("[语音] vosk 模型%s" % ("就绪：" + MODEL_DIR if ok else "没加载：" + _MODEL["error"]), flush=True)


def recognize(pcm):
    """裸 PCM → (识别出的文字（去掉空格和 [unk]）, [unk] 占说话时长的比例)。
    只认词表里的词，口语别的字记成 [unk]；但闲聊也会被硬凑成关键词（「今天天气不错」→「紧急」），
    所以要看 [unk] 占多少：X5 实测指令 ≤ 0.41，闲聊 ≥ 0.68。"""
    model = _model()
    if model is None:
        raise VoiceError(_MODEL["error"])
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(model, RATE, _MODEL["grammar"])
    rec.SetWords(True)
    rec.AcceptWaveform(pcm)
    words = json.loads(rec.FinalResult()).get("result", [])
    total = sum(w["end"] - w["start"] for w in words)
    unk = sum(w["end"] - w["start"] for w in words if w["word"] == "[unk]")
    text = "".join(w["word"] for w in words if w["word"] != "[unk]")
    return text, (unk / total if total else 0.0)


def strip_wav(data):
    """带 WAV 头的也收（测试时直接发 .wav）：跳到 data 块。"""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    i = 12
    while i + 8 <= len(data):
        size = int.from_bytes(data[i + 4:i + 8], "little")
        if data[i:i + 4] == b"data":
            return data[i + 8:i + 8 + size]
        i += 8 + size + (size & 1)
    return b""


def match(text):
    """文字里含哪个关键词（长词优先）；没有返回 None。"""
    for kw in _BY_LENGTH:
        if kw in text:
            return kw
    return None


def handle(body, dispatch):
    """/voice_cmd 的正文 → (HTTP 码, 回包)。dispatch(动作名, hand_text, alias_of, requested) 和按键同一条路。"""
    pcm = strip_wav(body)
    if len(pcm) < MIN_BYTES:
        return 400, {"ok": False, "error": "录音太短（%d 字节）" % len(pcm)}
    started = time.monotonic()
    try:
        heard, unk = recognize(pcm)
    except VoiceError as e:
        return 503, {"ok": False, "error": "语音识别用不了：%s" % e}
    LAST.clear()
    LAST.update(ts=time.time(), heard=heard, unk=round(unk, 2), seconds=round(time.monotonic() - started, 2))
    kw = match(heard)
    if kw is None or unk > MAX_UNK:
        print("[语音] 听到「%s」（%d%% 听不懂），不算指令" % (heard, unk * 100), flush=True)
        why = "没听清" if not heard else "没听懂：" + heard if kw is None else "大半没听懂，不算「%s」" % kw
        return 200, {"ok": False, "heard": heard, "unk": round(unk, 2), "error": why}
    name = KEYWORDS[kw]
    if name == RPS:
        name = random.choice(("rps_rock", "rps_scissors", "rps_paper"))
    LAST.update(keyword=kw, pose=name)
    print("[语音] 听到「%s」→ %s" % (heard, name), flush=True)
    code, reply = dispatch(name, "1", "/voice_cmd", "voice:" + kw)
    reply.update(heard=heard, keyword=kw)
    LAST["code"] = code
    return code, reply


def status():
    return {"model": MODEL_DIR, "loaded": _MODEL["model"] is not None, "error": _MODEL["error"],
            "last": dict(LAST)}
