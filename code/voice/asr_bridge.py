"""ASR 桥：麦克风 → 识别 → 文字发到 /mock/voice（与操作台手打同一入口、同一格式 {"text": ...}）。

后端（--backend）：
  vosk   离线中文识别，默认。pip install vosk sounddevice
         模型 vosk-model-small-cn-0.22（约 42MB）解压到 code/voice/models/ 或用 --model 指定
  wav    用 vosk 识别 16kHz 单声道 wav（复现问题 / 回归用）
  stdin  键盘输入一行 = 老人说一句（没麦克风时调试，或现场兜底）

防自激（EchoGuard）：喇叭里 AI 自己的台词会被麦克风听到。桥订阅总线的 say 事件，
  估算播报时长，期间的识别结果丢弃；之后一小段时间内与台词高度相似的结果也丢弃。
  "对""确认"这类短回答不做相似度过滤，避免误杀。

运行：
  python -m voice.asr_bridge --backend stdin
  python -m voice.asr_bridge --list-devices
  python -m voice.asr_bridge --device 1
"""
import argparse
import collections
import difflib
import json
import os
import queue
import re
import sys
import time
from pathlib import Path

from config import utf8_console
from voice.bus_client import EventFollower, post_json
from voice.text_normalize import is_filler, join_cjk, normalize

DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "vosk-model-small-cn-0.22"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"

# 小模型常见同音错字 → 固定短语（现场按彩排结果往里加）
ASR_FIXES = {
    "雪压": "血压", "血鸭": "血压", "学压": "血压",
    "我没是": "我没事", "我没世": "我没事",
    "手指草": "手指操", "手纸操": "手指操",
    "腰二零": "120", "要二零": "120",
    "确任": "确认", "去消": "取消",
}

_CORE = re.compile(r"[\s，。？！,.?!~～、：:“”\"]")


def apply_fixes(text):
    for wrong, right in ASR_FIXES.items():
        text = text.replace(wrong, right)
    return text


def _core(text):
    return _CORE.sub("", text)


class EchoGuard:
    def __init__(self, chars_per_sec=4.0, lead=0.3, tail=0.8, memory=15.0, clock=time.monotonic):
        self.cps = chars_per_sec
        self.lead = lead
        self.tail = tail
        self.memory = memory
        self.clock = clock
        self.busy_until = 0.0
        self.lines = collections.deque(maxlen=20)

    def on_say(self, text, now=None):
        now = self.clock() if now is None else now
        start = max(now + self.lead, self.busy_until)
        self.busy_until = start + len(_core(text)) / self.cps + self.tail
        self.lines.append((_core(normalize(text)), self.busy_until))

    def should_drop(self, text, now=None):
        now = self.clock() if now is None else now
        if now < self.busy_until:
            return True, "AI 正在播报"
        core = _core(text)
        if len(core) < 4:
            return False, ""
        for line, end in self.lines:
            if now - end > self.memory:
                continue
            if core in line or difflib.SequenceMatcher(None, core, line).ratio() > 0.6:
                return True, "疑似 AI 台词回声"
        return False, ""


class Bridge:
    def __init__(self, server, guard=None, dry_run=False, log=print):
        self.url = server.rstrip("/") + "/mock/voice"
        self.guard = guard
        self.dry_run = dry_run
        self.log = log

    def handle(self, raw):
        text = normalize(apply_fixes(join_cjk(raw)))
        if is_filler(text):
            return None
        if self.guard:
            drop, why = self.guard.should_drop(text)
            if drop:
                self.log("  · 丢弃“%s”（%s）" % (text, why))
                return None
        if self.dry_run:
            self.log("→ [dry-run] %s" % text)
            return {"ok": True, "dry_run": True}
        try:
            r = post_json(self.url, {"text": text})
            self.log("→ %s   [%s]" % (text, r.get("intent", r.get("reason", ""))))
            return r
        except Exception as e:  # 总线没起来时别崩，继续听
            self.log("× 发送失败：%s（%s）" % (text, e))
            return None


# ---------- 后端 ----------

def run_stdin(bridge):
    print("键盘模式：输入一句话回车 = 老人说一句；Ctrl+C 退出")
    for line in sys.stdin:
        if line.strip():
            bridge.handle(line)


def _load_vosk(model_path):
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError:
        sys.exit("缺少 vosk：pip install vosk sounddevice")
    if not Path(model_path).is_dir():
        sys.exit("找不到模型目录：%s\n下载 %s 解压到 code/voice/models/，或用 --model 指定"
                 % (model_path, MODEL_URL))
    SetLogLevel(-1)
    return Model(str(model_path)), KaldiRecognizer


def run_vosk(bridge, model_path, device=None, samplerate=None):
    model, KaldiRecognizer = _load_vosk(model_path)
    try:
        import sounddevice as sd
    except ImportError:
        sys.exit("缺少 sounddevice：pip install sounddevice")
    if samplerate is None:
        samplerate = int(sd.query_devices(device, "input")["default_samplerate"])
    rec = KaldiRecognizer(model, samplerate)
    q = queue.Queue()

    def callback(indata, frames, t, status):
        q.put(bytes(indata))

    with sd.RawInputStream(samplerate=samplerate, blocksize=samplerate // 4, device=device,
                           dtype="int16", channels=1, callback=callback):
        print("正在听（%d Hz，设备 %s）… Ctrl+C 退出" % (samplerate, device if device is not None else "默认"))
        while True:
            if rec.AcceptWaveform(q.get()):
                text = json.loads(rec.Result()).get("text", "")
                if text.strip():
                    bridge.handle(text)


def run_wav(bridge, model_path, wav_path):
    import wave
    model, KaldiRecognizer = _load_vosk(model_path)
    with wave.open(wav_path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            sys.exit("wav 需为 16-bit 单声道")
        rec = KaldiRecognizer(model, wf.getframerate())
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                bridge.handle(json.loads(rec.Result()).get("text", ""))
        bridge.handle(json.loads(rec.FinalResult()).get("text", ""))


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="ASR → /mock/voice")
    ap.add_argument("--backend", choices=["vosk", "wav", "stdin"], default="vosk")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default=os.environ.get("VOSK_MODEL", str(DEFAULT_MODEL)))
    ap.add_argument("--device", type=int, default=None, help="麦克风设备号，见 --list-devices")
    ap.add_argument("--samplerate", type=int, default=None)
    ap.add_argument("--file", help="--backend wav 时的音频文件")
    ap.add_argument("--no-echo-guard", action="store_true", help="关闭防自激（耳机/麦克风离喇叭很远时）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    guard = None
    if not args.no_echo_guard and args.backend == "vosk":
        guard = EchoGuard()
        EventFollower(args.server, lambda ev: guard.on_say(ev["text"]), types=["say"]).start()
    bridge = Bridge(args.server, guard=guard, dry_run=args.dry_run)

    try:
        if args.backend == "stdin":
            run_stdin(bridge)
        elif args.backend == "wav":
            if not args.file:
                sys.exit("--backend wav 需要 --file")
            run_wav(bridge, args.model, args.file)
        else:
            run_vosk(bridge, args.model, args.device, args.samplerate)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
