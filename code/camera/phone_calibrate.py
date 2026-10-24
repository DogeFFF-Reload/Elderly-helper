#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Timed NORMAL-ACTIVITY phone camera collection. No alarm or actuator calls.

Run once, follow spoken prompts, then share the generated numeric-data ZIP.
Stage labels are scheduled instructions, NOT verified observations or falls.
"""
import argparse
import base64
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import uuid
import zipfile

if __package__:
    from .phone_source import PhoneFrameSource
    from .phone_fall_watch import Image, energy_profile, to_small_gray
else:
    from phone_source import PhoneFrameSource
    from phone_fall_watch import Image, energy_profile, to_small_gray

STAGES = (
    ("stand", "站立不动", "站在画面内，保持自然站立"),
    ("walk", "正常走动", "在画面内正常来回走动"),
    ("sit_stand", "缓慢坐下再站起", "用稳固的椅子，慢慢坐下再站起，按自己的节奏重复"),
)
FIELDS = (
    "round", "stage", "received_utc", "session_s", "stage_s", "source_sequence",
    "frame_age_s", "delta_s", "baseline", "energy", "centroid_y",
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "phone_calibration"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CollectionError(Exception):
    """User-facing, credential-free error."""


class WindowsSpeaker:
    """Offline Chinese speech; text is stdin data, never executable shell text."""
    def __call__(self, text):
        if os.name != "nt":
            raise CollectionError("当前系统不支持自动中文语音；请找同伴看屏幕提示，使用 --no-voice。")
        script = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1
    if ($null -eq $voice) { throw 'No Chinese voice installed' }
    $speaker.SelectVoice($voice.VoiceInfo.Name)
    $speaker.Speak([Console]::In.ReadToEnd())
} finally { $speaker.Dispose() }
"""
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            result = subprocess.run(
                [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CollectionError("中文语音启动失败。可请同伴看屏幕提示，并加 --no-voice 重试。") from None
        if result.returncode:
            raise CollectionError("未能播放中文语音。请检查 Windows 中文语音；或请同伴看提示，加 --no-voice 重试。")


class LatestFrame:
    """Continuously drain MJPEG so preparation/speech cannot queue old pictures."""
    def __init__(self, source, interval):
        self.source = source
        self.interval = interval
        self.condition = threading.Condition()
        self.stopping = threading.Event()
        self.latest = None
        self.error = None
        self.thread = threading.Thread(target=self._read, daemon=True, name="phone-frames")

    def start(self):
        self.thread.start()

    def _read(self):
        frames = self.source.frames()
        sequence = 0
        try:
            for jpeg in frames:
                if self.stopping.is_set():
                    break
                sequence += 1
                with self.condition:
                    self.latest = (sequence, jpeg, time.monotonic(), utc_now())
                    self.condition.notify_all()
                if not self.source.mjpeg and self.stopping.wait(self.interval):
                    break
        except Exception as exc:
            # Never serialize a URL, credential, urllib exception, or traceback.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                message = "摄像头登录失败，请核对账号和密码。"
            elif isinstance(exc, ValueError):
                message = "摄像头没有提供可读取的视频流，请核对 App 视频地址和分辨率。"
            else:
                message = "摄像头连接失败或视频流断开。请保持 App 在前台，检查网络后重新采集。"
            with self.condition:
                self.error = message
                self.condition.notify_all()
        finally:
            frames.close()

    def check(self, max_age):
        with self.condition:
            if self.error:
                raise CollectionError(self.error)
            if self.latest and time.monotonic() - self.latest[2] > max_age:
                raise CollectionError("视频流超过允许时间没有新画面，本次采集不完整。")

    def get(self, after=0, not_before=0.0, timeout=3.0):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self.error:
                    raise CollectionError(self.error)
                if self.latest and self.latest[0] > after and self.latest[2] >= not_before:
                    return self.latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CollectionError("未能及时收到新的摄像头画面，本次采集不完整。")
                self.condition.wait(min(remaining, 0.1))

    def close(self):
        self.stopping.set()
        # The HTTP reader has a five-second socket timeout. Do not try closing
        # a generator while its worker is executing it.
        self.thread.join(timeout=5.5)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def phase_statistics(rows, duration, interval):
    energies = [float(row["energy"]) for row in rows if row["energy"] != ""]
    times = [float(row["stage_s"]) for row in rows]
    gaps = [b - a for a, b in zip([0.0] + times, times + [duration])]
    expected = math.ceil(duration / interval)
    minimum = max(2, math.floor(expected * 0.8))
    gap_limit = max(1.5, 3 * interval)
    good = len(rows) >= minimum and max(gaps) <= gap_limit
    return {
        "samples": len(rows), "comparisons": len(energies), "expected_samples": expected,
        "minimum_samples": minimum, "max_gap_s": max(gaps), "gap_limit_s": gap_limit,
        "sampling_ok": good, "energy_min": min(energies) if energies else None,
        "energy_max": max(energies) if energies else None,
        "energy_mean": statistics.mean(energies) if energies else None,
        "energy_median": statistics.median(energies) if energies else None,
        "energy_p95": percentile(energies, 0.95),
    }


def wait_until(deadline, reader, frame_timeout):
    while True:
        reader.check(frame_timeout)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def write_results(directory, summary):
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = [
        "CareMate 手机画面日常动作采集结果", "",
        "状态：" + summary["status"], "说明：" + summary["message"],
        "本包仅包含数值和计划动作标签；不含照片、视频、账号、密码或摄像头网址。",
        "动作标签由定时计划给出，程序没有核实你是否完成该动作。",
        "这些全部是日常动作样本，不能证明跌倒识别有效，也不据此自动设定报警阈值。",
        "请自行确认：手机固定、全身入镜、动作与提示一致，镜头/位置/光照未在途中改变。",
        "若动作做错、没有听清提示或手机被碰动，请重新采集。", "",
        "每段第一条为基准画面，energy/centroid_y/delta_s 留空；其后每条比较均保存到 samples.csv。",
        "energy：相邻采样灰度图平均绝对差 / 12，理论范围 0–21.25。",
        "centroid_y：画面变化的纵向位置，0 为顶部、1 为底部；不是人体中心或跌倒概率。",
        "received_utc 是电脑收到画面的时间，不是手机拍摄时间；无法据此测出端到端延迟。", "",
    ]
    for phase in summary["phases"]:
        stats = phase.get("statistics", {})
        lines.append("第%d轮 %s：%s；画面 %d；能量比较 %d；最大能量 %s" % (
            phase["round"], phase["name"], phase["status"], stats.get("samples", 0),
            stats.get("comparisons", 0), stats.get("energy_max")))
    lines.extend(["", "请把旁边的整个 ZIP 发回分析；若状态不是 complete，请同时说明中断原因。"])
    (directory / "结果说明.txt").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    archive = directory.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in ("samples.csv", "events.jsonl", "summary.json", "结果说明.txt"):
            bundle.write(directory / name, arcname=directory.name + "/" + name)
    return archive


def collect(source, *, output=DEFAULT_OUTPUT, rounds=3, duration=20.0, prep=10.0,
            interval=0.5, frame_timeout=3.0, speaker=None):
    """Save each processed sample; return (ZIP path, summary), even after Ctrl+C."""
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    directory = Path(output).resolve() / session_id
    directory.mkdir(parents=True)
    summary = {
        "schema_version": 1, "session_id": session_id, "started_utc": utc_now(),
        "status": "running", "message": "正在采集", "labels": "scheduled_normal_activity_only",
        "settings": {"rounds": rounds, "duration_s": duration, "prep_s": prep,
                     "sample_interval_s": interval, "frame_timeout_s": frame_timeout,
                     "source_type": "mjpeg" if source.mjpeg else "snapshot", "voice": speaker is not None},
        "metric": {"name": "mean_absolute_grayscale_difference_divided_by_12", "range": [0, 21.25],
                   "size": [48, 32], "centroid": "motion_not_person", "p95": "linear_interpolation"},
        "phases": [{"round": r, "stage": key, "name": name, "status": "not_started"}
                   for r in range(1, rounds + 1) for key, name, _ in STAGES],
    }
    reader = LatestFrame(source, interval)
    started = time.monotonic()
    current = None
    current_rows = []
    reader_started = False
    with (directory / "samples.csv").open("w", encoding="utf-8-sig", newline="") as sample_file, \
            (directory / "events.jsonl").open("w", encoding="utf-8") as event_file:
        writer = csv.DictWriter(sample_file, fieldnames=FIELDS)
        writer.writeheader()
        sample_file.flush()

        def event(kind, **details):
            record = {"utc": utc_now(), "session_s": time.monotonic() - started, "event": kind, **details}
            event_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            event_file.flush()

        def say(text):
            print(text, flush=True)
            event("prompt", text=text)
            if speaker:
                speaker(text)
            if reader_started:
                reader.check(frame_timeout)

        try:
            say("语音提示已开启。请确认能听清。手机固定，全身入镜，椅子放稳。本次只做日常动作。"
                if speaker else "[无语音] 请同伴按屏幕提示报时。手机固定，全身入镜，椅子放稳。")
            reader.start()
            reader_started = True
            first = reader.get(timeout=frame_timeout + 5)
            to_small_gray(first[1])
            event("camera_connected")
            for phase in summary["phases"]:
                current = phase
                current_rows = []
                current["status"] = "preparing"
                instruction = next(item[2] for item in STAGES if item[0] == phase["stage"])
                say("第%d轮，%s。%s。现在有%s秒准备时间。" % (
                    phase["round"], phase["name"], instruction, "%g" % prep))
                wait_until(time.monotonic() + prep, reader, frame_timeout)
                print("本段采集 %g 秒。" % duration, flush=True)
                say("开始。")
                phase_start = time.monotonic()
                phase_end = phase_start + duration
                current["started_utc"] = utc_now()
                current["status"] = "recording"
                event("phase_start", round=phase["round"], stage=phase["stage"])
                previous = None
                previous_time = None
                last_sequence = 0
                due = phase_start
                while due < phase_end:
                    wait_until(due, reader, frame_timeout)
                    remaining = phase_end - time.monotonic()
                    if remaining <= 0:
                        break
                    sequence, jpeg, received, received_utc = reader.get(
                        after=last_sequence, not_before=due, timeout=min(frame_timeout, remaining))
                    if received >= phase_end:
                        break
                    gray = to_small_gray(jpeg)
                    energy, centroid = "", ""
                    if previous is not None:
                        energy, centroid, _ = energy_profile(previous, gray)
                    row = {"round": phase["round"], "stage": phase["stage"], "received_utc": received_utc,
                           "session_s": received - started, "stage_s": received - phase_start,
                           "source_sequence": sequence, "frame_age_s": time.monotonic() - received,
                           "delta_s": "" if previous_time is None else received - previous_time,
                           "baseline": int(previous is None), "energy": energy, "centroid_y": centroid}
                    writer.writerow(row)
                    sample_file.flush()
                    current_rows.append(row)
                    previous, previous_time, last_sequence = gray, received, sequence
                    # Skip missed slots without fabricating extra samples of the same frame.
                    due = phase_start + (math.floor((time.monotonic() - phase_start) / interval) + 1) * interval
                wait_until(phase_end, reader, frame_timeout)
                current["statistics"] = phase_statistics(current_rows, duration, interval)
                current["status"] = "complete" if current["statistics"]["sampling_ok"] else "insufficient"
                current["ended_utc"] = utc_now()
                event("phase_end", round=phase["round"], stage=phase["stage"], status=current["status"])
                say("这一段结束，可以休息。")
                current = None
            if all(p["status"] == "complete" for p in summary["phases"]):
                summary.update(status="complete", message="所有计划阶段采集完毕；请自行确认动作、固定机位和全身入镜。")
            else:
                summary.update(status="insufficient", message="部分阶段样本不足或间隔过大；请检查网络后重新采集。")
        except KeyboardInterrupt:
            summary.update(status="interrupted", message="用户按 Ctrl+C 中断，已保存部分数据；不能算完整测试。")
        except CollectionError as exc:
            summary.update(status="error", message=str(exc))
        except Exception:
            summary.update(status="error", message="画面解码或采集失败；已保存部分数据，请检查连接后重试。")
        finally:
            if current is not None and current["status"] in ("preparing", "recording"):
                current["status"] = summary["status"]
                current["statistics"] = phase_statistics(current_rows, duration, interval)
            if reader_started:
                reader.close()
            summary["ended_utc"] = utc_now()
            event("session_end", status=summary["status"], message=summary["message"])
    archive = write_results(directory, summary)
    print("\n状态：%s；%s\n数据包：%s" % (summary["status"], summary["message"], archive), flush=True)
    if summary["status"] == "complete" and speaker:
        try:
            speaker("三项动作的全部轮次采集结束。数据包已保存，请回电脑查看结果。")
        except CollectionError:
            print("结束语音未播放；数据包已保存。", flush=True)
    return archive, summary


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    source_args = parser.add_mutually_exclusive_group(required=True)
    source_args.add_argument("--stream-url", help="完整 HTTP MJPEG 地址，iPhone 不附加 /shot.jpg")
    source_args.add_argument("--phone", help="安卓 IP Webcam 基址，自动附加 /shot.jpg")
    parser.add_argument("--username", default=os.environ.get("PHONE_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("PHONE_PASSWORD"))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=float, default=20, help="每段采样秒数，默认20")
    parser.add_argument("--prep", type=float, default=10, help="每段准备秒数，默认10")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--frame-timeout", type=float, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-voice", action="store_true", help="关闭语音；请同伴看屏幕提示报时")
    parser.add_argument("--open-output", action="store_true", help="完成后在 Windows 打开结果文件夹")
    args = parser.parse_args(argv)
    if Image is None:
        parser.error("缺少 Pillow，请运行 python -m pip install pillow")
    values = (args.duration, args.prep, args.interval, args.frame_timeout)
    if not all(math.isfinite(value) for value in values):
        parser.error("时间参数必须为有限数值")
    if args.rounds < 1 or args.prep < 0 or args.interval <= 0 or args.frame_timeout <= 0:
        parser.error("轮数、采样间隔和超时必须为正数，准备时间不得为负数")
    if args.duration < 3 * args.interval:
        parser.error("每段时间至少为采样间隔的三倍")
    url = args.stream_url or args.phone.rstrip("/") + "/shot.jpg"
    try:
        source = PhoneFrameSource(url, mjpeg=bool(args.stream_url), username=args.username, password=args.password)
    except ValueError as exc:
        parser.error(str(exc))
    print("仅采集手机画面的数值；不发送报警，不连接主脑或机械手。Ctrl+C 可停止并保存部分数据。")
    archive, summary = collect(source, output=args.output, rounds=args.rounds, duration=args.duration,
                               prep=args.prep, interval=args.interval, frame_timeout=args.frame_timeout,
                               speaker=None if args.no_voice else WindowsSpeaker())
    if args.open_output and os.name == "nt":
        try:
            os.startfile(str(archive.parent))
        except OSError:
            print("未能自动打开文件夹，请复制上方数据包路径。")
    return {"complete": 0, "insufficient": 2, "interrupted": 130, "error": 1}[summary["status"]]


if __name__ == "__main__":
    sys.exit(main())
