"""TTS 播放器：订阅总线的 say 事件 → 从本机喇叭念出来。

喇叭在哪【待确认】：跑在接喇叭的那台机器上即可（X5-B 接显示器/音箱，或笔记本）。
若最终喇叭在床头盒（ESP32），用 --backend cmd 把音频转发过去，接口等硬件同学定。

后端（--backend）：
  auto    Windows → sapi；有 espeak-ng → espeak；否则 print
  sapi    Windows 自带 System.Speech，零安装；装了中文语音（如 Huihui）自动选中文
  espeak  Linux / X5：espeak-ng -v cmn（离线，音质一般）
  cmd     自定义命令，台词从 stdin 送入。例（piper 离线中文，音质好）：
          --cmd "piper -m zh_CN-huayan-medium.onnx --output-raw | aplay -r 22050 -f S16_LE -t raw -"
  print   只打印（mock）

运行：python -m voice.tts_player [--backend auto] [--server http://127.0.0.1:8000]
"""
import argparse
import base64
import os
import queue
import shutil
import subprocess
import sys
import threading

from config import utf8_console
from voice.bus_client import EventFollower

MAX_BACKLOG = 3  # 积压超过这么多句就丢最旧的，保证紧急台词不被手指操口令堵住

SAPI_SCRIPT = r"""
[Console]::InputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1
if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }
$s.Rate = __RATE__
[Console]::Out.WriteLine("READY|" + $s.Voice.Name + "|" + $s.Voice.Culture.Name)
while ($true) {
  $l = [Console]::In.ReadLine()
  if ($l -eq $null) { break }
  if ($l.Trim()) { $s.Speak($l) }
  [Console]::Out.WriteLine("DONE")
}
"""


class SapiSpeaker:
    """常驻一个 PowerShell 进程，逐行念，避免每句话都冷启动。"""

    def __init__(self, rate=0):
        script = SAPI_SCRIPT.replace("__RATE__", str(int(rate)))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ready = self.proc.stdout.readline().decode("utf-8", "replace").strip()
        if not ready.startswith("READY|"):
            raise RuntimeError("System.Speech 启动失败：%r" % ready)
        _, self.voice, self.culture = ready.split("|", 2)
        if not self.culture.lower().startswith("zh"):
            print("! 没找到中文语音（当前 %s），中文会念不出来。"
                  "设置 → 时间和语言 → 语音 → 添加语音：中文（简体）" % self.voice, file=sys.stderr)

    def describe(self):
        return "sapi（%s，%s）" % (self.voice, self.culture)

    def speak(self, text):
        self.proc.stdin.write((text.replace("\n", " ") + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        self.proc.stdout.readline()  # 等 DONE：念完一句再念下一句


class CmdSpeaker:
    def __init__(self, cmd):
        self.cmd = cmd

    def describe(self):
        return "cmd（%s）" % self.cmd

    def speak(self, text):
        subprocess.run(self.cmd, shell=True, input=text.encode("utf-8"), check=False)


class EspeakSpeaker(CmdSpeaker):
    def __init__(self):
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        super().__init__(None)
        self.exe = exe

    def describe(self):
        return "espeak（%s）" % self.exe

    def speak(self, text):
        subprocess.run([self.exe, "-v", "cmn", text], check=False)


class PrintSpeaker:
    def describe(self):
        return "print（只打印）"

    def speak(self, text):
        print("🔊 " + text, flush=True)


def make_speaker(backend, cmd=None, rate=0):
    if backend == "auto":
        if os.name == "nt":
            backend = "sapi"
        elif shutil.which("espeak-ng") or shutil.which("espeak"):
            backend = "espeak"
        else:
            backend = "print"
    if backend == "sapi":
        return SapiSpeaker(rate)
    if backend == "espeak":
        return EspeakSpeaker()
    if backend == "cmd":
        if not cmd:
            sys.exit("--backend cmd 需要 --cmd")
        return CmdSpeaker(cmd)
    return PrintSpeaker()


class Player:
    def __init__(self, speaker, max_backlog=MAX_BACKLOG, log=print):
        self.speaker = speaker
        self.max_backlog = max_backlog
        self.log = log
        self.q = queue.Queue()

    def on_event(self, ev):
        if ev["type"] == "reset":
            self._drain()
            return
        if ev["type"] != "say":
            return
        while self.q.qsize() >= self.max_backlog:
            try:
                self.log("  · 积压，跳过：" + self.q.get_nowait())
            except queue.Empty:
                break
        self.q.put(ev["text"])

    def _drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def run(self):
        while True:
            text = self.q.get()
            print("🔊 " + text, flush=True)
            if not isinstance(self.speaker, PrintSpeaker):
                self.speaker.speak(text)


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="总线 say 事件 → 喇叭")
    ap.add_argument("--backend", choices=["auto", "sapi", "espeak", "cmd", "print"], default="auto")
    ap.add_argument("--cmd", help="--backend cmd 时的命令，台词从 stdin 送入")
    ap.add_argument("--rate", type=int, default=0, help="sapi 语速 -10..10，老人建议 -1")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--say", help="不连总线，直接念一句（测喇叭用）")
    args = ap.parse_args()

    speaker = make_speaker(args.backend, args.cmd, args.rate)
    print("TTS 后端：" + speaker.describe(), flush=True)
    if args.say:
        speaker.speak(args.say)
        return

    player = Player(speaker)
    follower = EventFollower(args.server, player.on_event, types=["say", "reset"]).start()
    threading.Thread(target=player.run, daemon=True).start()
    print("订阅 %s 的 say 事件… Ctrl+C 退出" % args.server, flush=True)
    try:
        while True:
            threading.Event().wait(5)
            if not follower.connected:
                print("  · 等待总线 %s …" % args.server, flush=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
