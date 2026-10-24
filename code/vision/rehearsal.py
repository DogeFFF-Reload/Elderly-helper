"""Offline pose replay -> real master HTTP -> recording-only simulated actuator.

python -m vision.rehearsal
python -m vision.rehearsal --keypoints data/phone_pose/.../keypoints.jsonl

No camera, SDK, audio player or external server is accepted or started.
Synthetic skeletons test software behavior; they do not measure pose accuracy.
"""
import argparse
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import html
from http.client import HTTPConnection
import json
import math
from pathlib import Path
import re
import shutil
import threading
import uuid
import zipfile

import config
import master_service
from gesture.fall_rules import FallDetector, STANDING
from vision import synth
from vision.fall_bridge import run
from vision.input_quality import InputQuality
from vision.kpio import KeypointWriter, read_keypoints


CASE_NAMES = {"fall": "快速倒地", "stumble": "绊一下后恢复", "sit": "正常坐下",
              "chair_fall": "从坐姿倒地", "bend": "弯腰", "bed": "床区躺下",
              "slow_floor": "缓慢滑倒", "fall_occluded": "倒地后被遮挡", "refall": "起身后再次倒地",
              "no_person": "画面无人", "low_visibility": "身体关键点不清楚", "input_gap": "中途缺少画面"}
STATE_NAMES = {"IDLE": "待机", "ASKING": "询问是否没事", "SOS": "模拟求助", "HELP_COMING": "模拟家属已确认"}


class RecordingArm:
    """Injected into Bus: no ArmClient, socket, serial port or SDK exists here."""
    def __init__(self):
        self.sent = []

    def send(self, pose, hand):
        self.sent.append({"pose": pose, "hand": hand})

    def status(self):
        return {"online": True, "mock": True, "url": "in-process recording only"}


class ReplayClock:
    def __init__(self):
        self.t = 1800000000.0

    def __call__(self):
        return self.t


class MockMaster(AbstractContextManager):
    """A private loopback master, independent of local config and proxies."""
    def __enter__(self):
        self.clock, self.arm = ReplayClock(), RecordingArm()
        self.bus = master_service.Bus(config.load({"llm_mode": "scripted", "night_override": "off"},
                                                  use_local=False), clock=self.clock, arm=self.arm)
        self.server = master_service.serve(self.bus, "127.0.0.1", 0)
        self.server.stop_ticker.set()  # Replay advances the injected clock explicitly.
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        return self

    def advance(self, seconds):
        with self.bus.lock:
            self.clock.t += seconds
            self.bus.tick()

    def request(self, path, body):
        # An explicit HTTPConnection never follows redirects or system proxies.
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                         headers={"Content-Type": "application/json; charset=utf-8"})
            response = conn.getresponse()
            result = json.loads(response.read())
            if response.status != 200 or not result.get("ok"):
                raise RuntimeError("本机模拟主程序未接受请求")
            return result
        finally:
            conn.close()

    def __exit__(self, *_exc):
        self.server.stop_ticker.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class MockPoster:
    def __init__(self, master, min_visibility=.4):
        self.master = master
        self.min_visibility = min_visibility
        self.latest = None
        self.receipts = []

    def skeleton(self, kp, fallen):
        if kp:
            self.latest = {"keypoints": {k: list(p[:2]) for k, p in kp.items()
                                         if p is not None and (len(p) < 3 or p[2] >= self.min_visibility)},
                           "fallen": bool(fallen)}

    def flush(self):
        if self.latest and self.latest["keypoints"]:
            self.master.request("/api/skeleton", self.latest)
            self.latest = None

    def fall(self, room):
        self.flush()
        self.receipts.append(self.master.request("/mock/fall", {"room": room}))


def run_case(name, frames, *, aspect=4 / 3, rules=None, expected=None, recording=None, log=print):
    """Expected is explicit; a recording's filename never supplies a label."""
    quality = InputQuality(log=log)
    detector = FallDetector(rules)
    writer = KeypointWriter(recording, "synthetic:" + name if expected is not None else "unlabeled:replay") if recording else None
    alarms, error = [], None
    with MockMaster() as master:
        poster = MockPoster(master, detector.rules["min_visibility"])
        previous = 0.0

        def source():
            nonlocal previous
            for t, kp in frames:
                # Validation in run() precedes any detector update. Advance only
                # valid forward intervals; malformed recordings fail explicitly.
                if math.isfinite(t) and t >= previous:
                    master.advance(t - previous)
                previous = t
                yield t, kp, None, aspect

        try:
            alarms = run(source(), detector, poster, "离线演示房间", writer, log=log, quality=quality)
            poster.flush()
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            error = type(exc).__name__  # No input paths, URLs or credentials in reports.
        finally:
            if writer:
                writer.close()

        state_after_input = master.bus.fsm.state
        states = ["IDLE"]
        for state in [r["state"] for r in poster.receipts] + [state_after_input]:
            if state != states[-1]:
                states.append(state)
        if not error and poster.receipts:
            if master.bus.fsm.state == "ASKING":
                master.advance(master.bus.cfg["ask_wait_day"] + .01)
            if master.bus.fsm.state != states[-1]:
                states.append(master.bus.fsm.state)
            master.request("/api/family/ack", {"contact": "daughter"})
            states.append(master.bus.fsm.state)

        poses = [a["pose"] for a in master.arm.sent]
        checks = {"input_complete": error is None, "has_frames": quality.frames > 0,
                  "one_receipt_per_event": len(poster.receipts) == len(alarms),
                  "mock_actuator": master.arm.status()["mock"]}
        if poster.receipts and not error:
            checks.update(master_received=all(r["state"] in ("ASKING", "SOS", "HELP_COMING") for r in poster.receipts),
                          escalated="SOS" in states,
                          family_ack=states[-1] == "HELP_COMING",
                          single_sos=poses.count("press_sos") == 1,
                          comfort="gentle_hold" in poses,
                          family_signs=all(p in poses for p in ("sign_family_received", "sign_coming")),
                          notification=any(n["kind"] == "fall" for n in master.bus.notifications))
        elif not error:
            checks.update(idle=state_after_input == "IDLE", no_actions=not poses,
                          no_notifications=not master.bus.notifications)
        if expected is not None:
            checks["expected_events"] = [a["kind"] for a in alarms] == expected
        else:
            checks["usable_pose_available"] = quality.usable_frames > 0
        result = {"name": name, "display_name": CASE_NAMES.get(name, name),
                  "source_kind": "synthetic" if expected is not None else "unlabeled_recording",
                  "expected_events": expected, "events": alarms, "receipts": poster.receipts,
                  "states": states, "mock_actions": master.arm.sent, "checks": checks,
                  "input_quality": quality.snapshot(), "input_error": error,
                  "timeline": list(master.bus.events.items), "ok": all(checks.values())}
    return result


def builtin_cases():
    for name, scenario in synth.SCENARIOS.items():
        yield name, synth.build(name), scenario.get("rules"), scenario["expect"]
    yield "no_person", [(i / 10, None) for i in range(40)], None, []
    partial = {k: [x, y, .1] for k, (x, y) in STANDING.items()}
    yield "low_visibility", [(i / 10, partial) for i in range(40)], None, []
    # A clock gap during a candidate must not itself confirm a fall.
    frames = [(t, kp) for t, kp in synth.build("fall") if t < 3.0]
    frames += [(t + 7, kp) for t, kp in synth.build("sit") if t < 1.0]
    yield "input_gap", frames, None, []


def write_report(output, results):
    report = {"schema_version": 1, "mode": "offline_pose_mock_master", "ok": all(r["ok"] for r in results),
              "scope": "Synthetic or explicitly unlabeled keypoint replay; real master HTTP on 127.0.0.1; recording-only actuator. No camera, model inference, audio or real hardware.",
              "limits": "Software checks are not real-scene detection accuracy. The accepted third phone round contains energy values and cannot be replayed as keypoints.",
              "cases": results}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    rows = []
    for r in results:
        label = "合成输入" if r["source_kind"] == "synthetic" else "未标注骨架，仅检查链路"
        rows.append("<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(r["display_name"]), label, len(r["events"]),
            html.escape(" → ".join(STATE_NAMES.get(s, s) for s in r["states"])),
            "软件检查通过" if r["ok"] else "检查失败"))
    details = "".join("<details><summary>%s · 输入状态与主程序时间线</summary><pre>%s</pre></details>" % (
        html.escape(r["display_name"]), html.escape(json.dumps({k: r[k] for k in ("checks", "input_quality", "timeline", "mock_actions")},
                                                    ensure_ascii=False, indent=2))) for r in results)
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CareMate 视觉离线联调结果</title><style>body{font:16px/1.75 'Microsoft YaHei',sans-serif;background:#f2f5fa;color:#203547;margin:0}main{max-width:1100px;margin:32px auto;padding:24px}header{background:#153247;color:white;padding:24px;border-radius:14px}section,details{background:white;margin-top:20px;padding:22px;border-radius:12px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px;text-align:left;border-bottom:1px solid #dde5ee}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.table{overflow:auto}summary{cursor:pointer}h1{font-size:26px}</style>
<main><header><h1>CareMate 视觉离线联调结果</h1><p>骨架回放 → 本机主程序 HTTP → 询问 → 模拟求助 → 模拟家属确认</p></header>
<section><b>本报告验证软件流程，不代表真人跌倒识别准确率。</b><p>没有连接手机、摄像头、机械臂、机械手或语音播放器。动作和通知均为本机模拟记录。原先第三轮保存的画面变化数值不能还原成骨架，本演示未将它冒充真人姿态数据。</p><p>未标注录制只报告发生了什么，不根据文件名推断是否应该报警。看不见人或骨架不完整表示无法判断。</p></section>
<section class="table"><table><thead><tr><th>输入</th><th>数据性质</th><th>疑似事件数</th><th>主程序状态</th><th>结果</th></tr></thead><tbody>ROWS</tbody></table></section>DETAILS</main></html>"""
    page = page.replace("<tbody>ROWS</tbody>", "<tbody>" + "".join(rows) + "</tbody>")
    (output / "report.html").write_text(page.replace("DETAILS</main>", details + "</main>"), encoding="utf-8")
    (output / "说明.txt").write_text(
        "CareMate 视觉离线联调包\n\n先打开 report.html 查看中文结果。report.json 保存详细检查证据。\n"
        "keypoints/ 中的文件为明确标注的合成骨架，可以回放；不是用户的真人录像或真人骨架。\n"
        "动作和家属通知均在本机模拟，没有连接摄像头、机械臂、机械手或播放声音。\n"
        "软件检查通过不等于真人跌倒识别准确。此前第三轮只保存了画面变化数值，不能还原骨架。\n"
        "如需重跑，在仓库 code/ 目录运行：python -m vision.rehearsal\n"
        "以后回放观察入口保存的坐标：python -m vision.rehearsal --keypoints 路径/keypoints.jsonl\n"
        "回放文件默认未标注，只检查链路；不会根据文件名推断是否应该报警。\n", encoding="utf-8")
    handoff = config.CODE_DIR.parent / "docs" / "26_field_handoff_vision_phone_debug.md"
    if handoff.is_file():
        shutil.copyfile(handoff, output / "现场交接.md")
    archive = output.parent / (output.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(output))
    return report, archive


def main(argv=None):
    config.utf8_console()
    parser = argparse.ArgumentParser(description="无设备视觉联调：独立本机主程序、模拟执行端、无声音")
    parser.add_argument("--keypoints", action="append", type=Path, help="回放已有骨架；默认无标签，不从文件名猜标签")
    parser.add_argument("--scenario", choices=list(synth.SCENARIOS), help="只检查一个内置合成动作")
    parser.add_argument("--output", type=Path, help="结果目录（建议使用新目录）")
    args = parser.parse_args(argv)
    if args.keypoints and args.scenario:
        parser.error("--keypoints 与 --scenario 只能选择一种")
    output = args.output or config.CODE_DIR / "data" / "vision_rehearsal" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("结果目录已有文件，请使用新目录，避免覆盖记录")
    if (output.parent / (output.name + ".zip")).exists():
        parser.error("同名结果压缩包已存在，请使用新目录")
    output.mkdir(parents=True, exist_ok=True)
    print("离线联调开始：只运行本机模拟主程序；不访问摄像头，不播放声音。")
    results = []
    if args.keypoints:
        for i, path in enumerate(args.keypoints, 1):
            try:
                meta, frames = read_keypoints(path)
            except (OSError, ValueError, KeyError, TypeError):
                parser.error("无法读取骨架文件，请使用 fall_bridge 保存的 JSONL 文件")
            name = "%02d_%s" % (i, re.sub(r"[^\w-]", "_", path.stem))
            print("[回放] %s（未标注）" % path.name)
            results.append(run_case(name, frames, aspect=meta.get("aspect", 4 / 3)))
    else:
        for name, frames, rules, expected in builtin_cases():
            if args.scenario and name != args.scenario:
                continue
            print("[场景] " + CASE_NAMES[name] + "（合成输入）")
            results.append(run_case(name, frames, rules=rules, expected=expected,
                                    recording=output / "keypoints" / (name + ".jsonl")))
    report, archive = write_report(output, results)
    print("软件检查：%d/%d 通过。" % (sum(r["ok"] for r in results), len(results)))
    print("结果报告：%s\n交接压缩包：%s" % ((output / "report.html").resolve(), archive.resolve()))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
