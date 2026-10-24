"""Visual input integrated into the complete project demo, isolated from hardware.

Saved poses -> local master HTTP -> health/medicine/cancel/rescue/family/exercise.
The rescue input is explicitly synthetic. No camera or audio is opened here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
import zipfile

import config
import demo_driver
from gesture.fall_rules import FallDetector
from hand_controller.poses import label as pose_label
from vision import synth
from vision.fall_bridge import run
from vision.input_quality import InputQuality
from vision.kpio import KeypointWriter, read_keypoints
from vision.pc_observe import write_json
from vision.rehearsal import MockMaster, MockPoster, STATE_NAMES


WINDOW = "CareMate complete visual mock demo - F fullscreen | Q close"


class DemoTarget:
    def __init__(self, master, *, viewer=None, speed=2, status_path=None):
        self.master, self.viewer, self.speed, self.status_path = master, viewer, speed, status_path
        self.kp = None
        self.scene = "准备完整模拟演示"
        self.source = "本机模拟主程序，所有执行动作仅记录"
        self.logs = []
        self.current = self.state()
        self.closed = False
        self.last_save = 0.0

    def post(self, path, body):
        result = self.master.request(path, body)
        self.current = self.state()
        self.present()
        return result

    def state(self):
        return self.master.request("/api/state")

    def events(self, since):
        return self.master.request("/api/events?since=%d&client=visual_demo" % since)["events"]

    def last_id(self):
        return self.master.request("/api/events?since=999999999&client=visual_demo")["last_id"]

    def now(self):
        return self.master.clock()

    def note(self, text):
        self.logs.append(text)
        self.logs = self.logs[-120:]

    def present(self):
        if self.viewer and not self.viewer.show(self):
            self.closed = True
            raise KeyboardInterrupt
        if self.status_path and time.monotonic() - self.last_save >= .5:
            write_json(self.status_path, {"status": "running", "window_open": self.viewer is not None,
                       "scene": self.scene, "input_source": self.source, "guard_state": self.current["guard"]["state"],
                       "actions": len(self.master.arm.sent), "camera_opened": False, "real_actuator": False,
                       "monitor_url": "http://127.0.0.1:%d/ui/monitor" % self.master.port})
            self.last_save = time.monotonic()

    def wait(self, seconds):
        remaining = seconds
        while remaining > 1e-8:
            step = min(.1, remaining)
            self.master.advance(step)
            remaining -= step
            if self.viewer:
                self.current = self.state()
                self.present()
                time.sleep(step / self.speed)


class VisualSink(MockPoster):
    def __init__(self, target):
        super().__init__(target.master)
        self.target = target
        self.last_push = float("-inf")

    def skeleton(self, kp, fallen):
        super().skeleton(kp, fallen)
        if self.target.now() - self.last_push >= .2:
            self.flush()
            self.last_push = self.target.now()


def play_poses(target, frames, *, aspect=4 / 3, until_alarm=False):
    sink, quality = VisualSink(target), InputQuality(log=lambda *_: None)
    previous = 0.0

    def source():
        nonlocal previous
        for t, kp in frames:
            if until_alarm and sink.receipts:
                return
            if t < previous:
                raise ValueError("骨架时间发生倒退")
            target.kp = kp
            target.wait(t - previous)
            previous = t
            yield t, kp, None, aspect

    events = run(source(), FallDetector(), sink, "演示卧室", log=target.note, quality=quality)
    sink.flush()
    target.current = target.state()
    target.present()
    return {"events": events, "receipts": sink.receipts, "input_quality": quality.snapshot()}


class VisualDriver(demo_driver.Driver):
    """The existing rescue scene obtains its event from pose rules."""
    def __init__(self, target):
        super().__init__(target, out=target.note)
        self.visual_inputs = []

    def post(self, path, body=None):
        if path == "/mock/fall":
            self.t.scene = "合成倒地 → 视觉规则触发 → 完整求助流程"
            self.t.source = "合成骨架演示；不是用户真实跌倒"
            result = play_poses(self.t, synth.build("fall"), until_alarm=True)
            self.visual_inputs.append(result)
            if not result["events"]:
                self.failures.append("合成倒地未产生视觉事件")
            return {"ok": bool(result["events"]), "state": self.t.current["guard"]["state"]}
        return super().post(path, body)


def load_input(path):
    meta, frames = read_keypoints(path)
    stages_file = path.with_name("stages.jsonl")
    if stages_file.is_file():
        stages = [json.loads(line) for line in stages_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        start = next((r["t"] for r in stages if r.get("stage") == "prepare_stand"), None)
        end = next((r["t"] for r in stages if r.get("stage") == "finished"), None)
        if start is not None and end is not None:
            frames = [(t, kp) for t, kp in frames if start - .00051 <= t < end - .00051]
    if not frames:
        raise ValueError("没有可回放的骨架")
    origin = frames[0][0]
    frames = [(t - origin, kp) for t, kp in frames]
    return meta, frames


def latest_recording(root):
    paths = list(root.glob("*/keypoints.jsonl")) if root.exists() else []
    return max(paths, key=lambda p:p.stat().st_mtime) if paths else None


def confirmation_matches(path):
    confirmation = path.with_name("operator_confirmation.json")
    try:
        data = json.loads(confirmation.read_text(encoding="utf-8"))
        return (data.get("normal_activities_completed") is True and data.get("camera_fixed") is True
                and data.get("keypoints_sha256") == hashlib.sha256(path.read_bytes()).hexdigest())
    except (ValueError, OSError):
        return False


def run_full_demo(output, *, keypoints=None, normal_confirmed=False, viewer=None, speed=2):
    report = {"mode": "complete_visual_mock_demo", "checks": {}, "visual_inputs": [],
              "camera_opened": False, "real_actuator": False, "audio_played": False,
              "normal_confirmed": normal_confirmed, "synthetic_rescue": True}
    with MockMaster() as master:
        target = DemoTarget(master, viewer=viewer, speed=speed, status_path=output / "live_status.json")
        driver = VisualDriver(target)
        try:
            target.scene = "早上量血压 → 复述确认 → 中午递药（模拟）"
            report["checks"]["health_and_medicine"] = driver.run(["health", "medicine"])

            target.scene = "保存的骨架回放" if keypoints else "合成正常坐下：检查视觉输入"
            target.source = ("用户确认正常活动完成、电脑固定；按原始时间判定" if normal_confirmed else
                             "未标注的已保存骨架；不从文件名推断动作") if keypoints else "合成骨架，不是实拍"
            if keypoints:
                meta, frames = load_input(keypoints)
                provenance = str(meta.get("source", ""))
                if provenance.startswith(("pc_camera:", "camera:", "phone:")):
                    target.scene = "真实摄像头骨架回放" + ("：正常活动已确认" if normal_confirmed else "：动作未确认")
                elif provenance.startswith("synthetic:"):
                    target.scene = "保存的合成骨架回放"
                report["recording"] = {"file": keypoints.name,
                                       "sha256": hashlib.sha256(keypoints.read_bytes()).hexdigest(),
                                       "source": "saved_keypoints", "played_frames": len(frames),
                                       "selection": "scheduled activity window when stages.jsonl is available; otherwise entire recording"}
                shutil.copyfile(keypoints, output / "recorded_keypoints.jsonl")
                confirmation = keypoints.with_name("operator_confirmation.json")
                if confirmation.is_file():
                    shutil.copyfile(confirmation, output / "operator_confirmation.json")
                replay_writer = KeypointWriter(output / "visual_replay.jsonl", "recorded:activity_window")
                try:
                    for t, kp in frames:
                        replay_writer.write(t, kp, meta.get("aspect", 4 / 3))
                finally:
                    replay_writer.close()
            else:
                meta, frames = {"aspect": 4 / 3}, synth.build("sit")
            normal = play_poses(target, frames, aspect=meta.get("aspect", 4 / 3))
            report["visual_inputs"].append({"kind": "recorded" if keypoints else "synthetic_normal", **normal})
            report["checks"]["visual_input_usable"] = normal["input_quality"]["usable_pose_frames"] > 0
            report["checks"]["normal_keeps_idle"] = not normal["events"] and target.state()["guard"]["state"] == "IDLE"
            if not report["checks"]["normal_keeps_idle"]:
                raise RuntimeError("录制中出现疑似事件，停止完整演示并保留证据")

            target.scene = "合成倒地 → 老人回应没事 → 取消升级"
            target.source = "合成骨架，用于检查取消分支"
            before = len(master.arm.sent)
            cancel = play_poses(target, synth.build("fall"), until_alarm=True)
            report["visual_inputs"].append({"kind": "synthetic_cancel", **cancel})
            target.post("/mock/button", {"key": "ok"})
            target.wait(master.bus.cfg["ask_wait_day"] + .1)
            report["checks"]["cancel_before_sos"] = bool(cancel["events"]) and target.state()["guard"]["state"] == "IDLE" and not any(
                a["pose"] == "press_sos" for a in master.arm.sent[before:])

            report["checks"]["visual_rescue"] = driver.run(["night_fall"], reset=False)
            report["visual_inputs"] += [{"kind": "synthetic_rescue", **r} for r in driver.visual_inputs]
            target.scene = "家属已确认 → 老人回应没事 → 回到待机"
            target.post("/mock/button", {"key": "ok"})
            report["checks"]["cancel_after_rescue"] = target.state()["guard"]["state"] == "IDLE" and any(
                n["kind"] == "cancel" for n in target.state()["notifications"])

            target.scene = "展台手指操：主程序带练，动作仅模拟记录"
            target.source = "主程序脚本与模拟执行端，无真实动作"
            report["checks"]["exercise"] = driver.run(["exercise"], reset=False)
            report["checks"]["single_sos"] = sum(a["pose"] == "press_sos" for a in master.arm.sent) == 1
            report["checks"]["all_visual_receipts"] = all(len(r["events"]) == len(r["receipts"]) for r in report["visual_inputs"])
            report["checks"]["no_driver_failures"] = not driver.failures
            report["ok"] = all(report["checks"].values())
        except KeyboardInterrupt:
            report.update(ok=False, interrupted=True)
        except (ValueError, RuntimeError, TypeError, OSError) as exc:
            report.update(ok=False, error=type(exc).__name__)
        report.update(mock_actions=list(master.arm.sent), events=list(master.bus.events.items),
                      driver_log=target.logs, final_state=target.state()["guard"]["state"])
        write_json(output / "report.json", report)
        write_report(output, report)
        target.scene = "完整模拟演示通过" if report["ok"] else "演示停止，请查看结果报告"
        target.source = "各类输入已分别标注；未连接真实执行端"
        target.current = target.state()
        if viewer and not target.closed:
            try:
                # Keep the native view and three project web screens available.
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    target.present()
                    time.sleep(.1)
            except KeyboardInterrupt:
                pass
        write_json(output / "live_status.json", {"status": "complete" if report["ok"] else "failed", "window_open": False,
                                                 "camera_opened": False, "real_actuator": False})
    return report


def write_report(output, report):
    import html
    input_note = ("本次使用已保存的骨架；" + ("操作者已确认正常活动与固定机位。" if report["normal_confirmed"] else "动作仍未人工标注。")) if "recording" in report else "本次使用合成骨架，没有真人录制输入。"
    names = {"health_and_medicine": "血压确认和模拟递药", "visual_input_usable": "视觉骨架可用",
             "normal_keeps_idle": "录制回放保持待机", "cancel_before_sos": "回应没事后取消升级",
             "visual_rescue": "视觉触发完整夜间求助", "cancel_after_rescue": "救援后取消并告知模拟家属",
             "exercise": "手指操完整带练", "single_sos": "仅一次模拟 SOS",
             "all_visual_receipts": "视觉事件均由主程序接收", "no_driver_failures": "既有演示检查均通过"}
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (names.get(k,k), "通过" if v else "未通过") for k,v in report["checks"].items())
    (output / "report.html").write_text("<!doctype html><meta charset='utf-8'><title>视觉完整模拟演示</title>"
        "<style>body{font:17px/1.8 'Microsoft YaHei',sans-serif;max-width:1000px;margin:40px auto;padding:20px;background:#f2f5fa;color:#203547}table{width:100%;border-collapse:collapse;background:white}td{padding:12px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;background:white;padding:20px}</style>"
        "<h1>视觉接入完整模拟演示</h1><p><b>整体结果：" + ("完整流程通过" if report["ok"] else "未通过或已中断") + "</b></p>"
        "<p>骨架输入 → 主程序真实 HTTP → 血压确认 / 模拟递药 / 视觉观察 / 取消 / 求助 / 家属确认 / 手指操。</p>"
        "<p>" + input_note + "</p>"
        "<p><b>救援倒地输入为合成骨架。未开启摄像头、音频或真实执行端。</b></p>"
        "<p>录制回放不是实时摄像头上报；软件流程通过不能替代真人跌倒效果验收。</p><table>" + rows + "</table><h2>演示逐项记录</h2><pre>" +
        html.escape("\n".join(report["driver_log"])) + "</pre>", encoding="utf-8")
    archive = output.parent / (output.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("report.html", "report.json", "recorded_keypoints.jsonl", "visual_replay.jsonl", "operator_confirmation.json"):
            if (output / name).exists():
                z.write(output / name, name)


class Viewer:
    def __init__(self):
        import cv2
        from PIL import ImageFont
        self.cv2, self.fullscreen = cv2, True
        self.fonts = {s: ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", s) for s in (23, 28, 38)}
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    def show(self, target):
        import numpy as np
        from PIL import Image, ImageDraw
        from vision.preview import BONES
        image = Image.new("RGB", (1440,900), "#0e1929")
        draw = ImageDraw.Draw(image)
        draw.text((28,18), "CareMate · 视觉接入完整模拟演示", font=self.fonts[38], fill="white")
        draw.text((28,83), target.scene, font=self.fonts[28], fill="#ffe39a")
        draw.text((28,135), target.source, font=self.fonts[23], fill="#b5c5d9")
        state = target.current
        guard = state["guard"]["state"]
        draw.rounded_rectangle((22,195,735,785), radius=20, fill="#182a41")
        points = {k:(int(50+p[0]*650),int(210+p[1]*550)) for k,p in (target.kp or {}).items()
                  if p is not None and (len(p)<3 or p[2]>=.4)}
        for a,b in BONES:
            if a in points and b in points:
                draw.line([points[a],points[b]], fill="#ffe39a" if guard!="IDLE" else "#96e4c0", width=7)
        for p in points.values():
            draw.ellipse((p[0]-5,p[1]-5,p[0]+5,p[1]+5),fill="white")
        if not points:
            draw.text((155,430), "等待视觉输入",font=self.fonts[38],fill="#b5c5d9")
        draw.text((780,205), "主程序："+STATE_NAMES.get(guard,guard),font=self.fonts[38],fill="#96e4c0")
        say = (state.get("last_say") or {}).get("text", "")
        for i in range(0,min(len(say),96),24):
            draw.text((780,280+(i//24)*37),say[i:i+24],font=self.fonts[23],fill="white")
        draw.text((780,455), "执行端：全程模拟",font=self.fonts[28],fill="#ffe39a")
        for i,action in enumerate(target.master.arm.sent[-4:]):
            text = "手%d · %s" % (action["hand"],pose_label(action["pose"]))
            draw.text((780,510+i*43),text[:25],font=self.fonts[23],fill="#b5c5d9")
        tail = target.logs[-2:]
        for i,line in enumerate(tail):
            draw.text((28,800+i*36),line.strip()[:57],font=self.fonts[23],fill="#b5c5d9")
        draw.text((28,868), "Q：关闭并保存  |  F：切换全屏  |  回放速度 %.1f 倍；规则按原始时间判定" % target.speed,font=self.fonts[23],fill="#b5c5d9")
        self.cv2.imshow(WINDOW, self.cv2.cvtColor(np.asarray(image),self.cv2.COLOR_RGB2BGR))
        key = self.cv2.waitKey(1)&0xff
        if key in (ord('q'),ord('Q')):
            return False
        if key in (ord('f'),ord('F'),27):
            self.fullscreen = not self.fullscreen
            self.cv2.setWindowProperty(WINDOW,self.cv2.WND_PROP_FULLSCREEN,
                                      self.cv2.WINDOW_FULLSCREEN if self.fullscreen else self.cv2.WINDOW_NORMAL)
        return self.cv2.getWindowProperty(WINDOW,self.cv2.WND_PROP_VISIBLE)>=1

    def close(self):
        self.cv2.destroyAllWindows()


def main(argv=None):
    config.utf8_console()
    parser=argparse.ArgumentParser(description="视觉接入完整模拟演示：不打开摄像头或真实执行端")
    parser.add_argument("--keypoints",type=Path)
    parser.add_argument("--latest-recording",action="store_true",help="优先使用最近一份电脑骨架；无录制时使用标明的合成坐姿")
    parser.add_argument("--normal-confirmed",action="store_true",help="操作者已确认本录制为正常活动")
    parser.add_argument("--headless",action="store_true",help="不打开窗口，快速检查并保存报告")
    parser.add_argument("--speed",type=float,default=2)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args(argv)
    if args.latest_recording and not args.keypoints:
        args.keypoints=latest_recording(config.CODE_DIR/"data"/"pc_pose")
    if args.keypoints and confirmation_matches(args.keypoints):
        args.normal_confirmed=True
    if args.normal_confirmed and not args.keypoints:
        parser.error("--normal-confirmed 必须指定录制文件")
    if not 0 < args.speed <= 10:
        parser.error("回放速度必须大于0且不超过10")
    output=args.output or config.CODE_DIR/"data"/"integrated_demo"/(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:6])
    if output.exists() or (output.parent/(output.name+".zip")).exists():
        parser.error("请使用新的结果目录")
    output.mkdir(parents=True)
    viewer=None
    try:
        if not args.headless:
            viewer=Viewer()
        report=run_full_demo(output,keypoints=args.keypoints,normal_confirmed=args.normal_confirmed,
                             viewer=viewer,speed=args.speed)
        print("完整模拟演示："+("通过" if report["ok"] else "未通过"))
        print("报告：%s"%(output/"report.html").resolve())
        return 0 if report["ok"] else 1
    finally:
        if viewer:
            viewer.close()


if __name__=="__main__":
    raise SystemExit(main())
