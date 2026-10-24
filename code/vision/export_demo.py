"""Render an honest, silent mock demonstration MP4 without capture or a GUI."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import uuid
import zipfile

import config
from vision.integrated_demo import DemoRenderer, confirmation_matches, latest_recording, run_full_demo


class VideoSink:
    realtime = False
    keep_open = False
    native_window = False

    def __init__(self, output, *, encoder, fps=6, speed=2, font=None):
        from PIL import ImageDraw
        self.draw = ImageDraw
        self.output, self.fps, self.speed = output, fps, speed
        self.renderer = DemoRenderer(font_path=font, footer="软件流程备份 · 无声 · 正常骨架为回放，倒地为合成，执行动作和通知均模拟")
        self.origin = None
        self.frames = 0
        self.last_frame = None
        self.scenes = []
        self.highlights = []
        self.last_highlight = None
        self.offset = 0.0
        self.error_log = (output/"encoder.log").open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen([
                encoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "1440x900",
                "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output/"software_demo.mp4")
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.error_log)
        except OSError:
            self.error_log.close()
            raise

    def write_frame(self, frame):
        self.process.stdin.write(frame)
        self.frames += 1

    def show(self, target):
        if target.frame is not None:
            raise RuntimeError("视频导出只接受骨架回放，不接受摄像头画面")
        now = target.now()
        if self.origin is None:
            self.origin = now
        index = max(0, int(math.floor((now-self.origin)/self.speed*self.fps + 1e-6)))
        index += int(round(self.offset*self.fps))
        scene = (target.scene, target.source)
        changed = not self.scenes or scene != (self.scenes[-1]["scene"], self.scenes[-1]["source"])
        state = getattr(target, "current", {})
        highlight = (scene, state.get("guard", {}).get("state"),
                     (state.get("last_say") or {}).get("text"))
        highlight_changed = highlight != self.last_highlight
        if not highlight_changed and index < self.frames:
            return True
        image = self.renderer.render(target)
        overlay = self.draw.Draw(image)
        # This is present in every exported frame, including screenshots.
        overlay.rounded_rectangle((1040, 16, 1415, 66), radius=10, fill="#7c3f12")
        overlay.text((1057, 22), "备份回放 · 全程模拟", font=self.renderer.fonts[28], fill="white")
        frame = image.tobytes()
        while self.frames < index:
            self.write_frame(self.last_frame or frame)
        if changed:
            self.scenes.append({"scene": target.scene, "source": target.source,
                                "video_start_s": round(self.frames/self.fps, 3)})
            image.save(self.output/("scene_%02d.png" % len(self.scenes)))
        if highlight_changed:
            self.highlights.append({"video_start_s": round(self.frames/self.fps, 3),
                                    "guard_state": highlight[1], "caption": highlight[2]})
            for _ in range(self.fps):
                self.write_frame(frame)
            self.offset += 1.0
        else:
            self.write_frame(frame)
        self.last_frame = frame
        self.last_highlight = highlight
        return True

    def close(self):
        error = None
        try:
            if self.last_frame:
                for _ in range(self.fps*2):
                    self.write_frame(self.last_frame)
        except OSError as exc:
            error = exc
        finally:
            try:
                self.process.stdin.close()
            except OSError as exc:
                error = exc
            try:
                result = self.process.wait(timeout=45)
            except subprocess.TimeoutExpired as exc:
                self.process.kill()
                result = self.process.wait(timeout=5)
                error = exc
            finally:
                self.error_log.close()
        if result or error:
            raise RuntimeError("视频编码失败，查看 encoder.log；未生成可交付的备份")


def main(argv=None):
    config.utf8_console()
    parser = argparse.ArgumentParser(description="导出无声的软件模拟演示备份；无窗口、不读取摄像头或真实设备")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--keypoints", type=Path)
    inputs.add_argument("--latest-recording", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--speed", type=float, default=2)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    args = parser.parse_args(argv)
    if not 1 <= args.fps <= 15 or not math.isfinite(args.speed) or not .5 <= args.speed <= 4:
        parser.error("帧率范围1–15；播放速度范围0.5–4")
    if not args.ffmpeg or not Path(args.ffmpeg).is_file():
        parser.error("未找到 FFmpeg，请安装后通过 --ffmpeg 指定本机可执行文件")
    if args.latest_recording:
        args.keypoints = latest_recording(config.CODE_DIR/"data/pc_pose")
        if args.keypoints is None:
            parser.error("没有保存的电脑骨架；不会转而打开摄像头")
    if args.keypoints and not args.keypoints.is_file():
        parser.error("找不到骨架文件")
    output = args.output or config.CODE_DIR/"data/demo_backup"/(
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:6])
    if output.exists() or output.with_suffix(".zip").exists():
        parser.error("输出已存在，请使用新目录")
    output.mkdir(parents=True)
    sink = None
    failed = False
    try:
        sink = VideoSink(output, encoder=args.ffmpeg, fps=args.fps, speed=args.speed, font=args.font)
        report = run_full_demo(output, keypoints=args.keypoints,
                               normal_confirmed=bool(args.keypoints and confirmation_matches(args.keypoints)),
                               viewer=sink, speed=args.speed)
    except (OSError, ValueError, RuntimeError, BrokenPipeError):
        failed = True
    finally:
        if sink:
            try:
                sink.close()
            except (OSError, RuntimeError):
                failed = True
    if failed:
        print("导出失败；保留部分结果和编码日志，不能作为完整演示交付。")
        return 1
    manifest = {"mode": "silent_software_mock_backup", "ok": report["ok"], "frames": sink.frames,
                "fps": args.fps, "duration_s": round(sink.frames/args.fps, 3), "scene_index": sink.scenes,
                "highlights": sink.highlights,
                "video_sha256": hashlib.sha256((output/"software_demo.mp4").read_bytes()).hexdigest(),
                "camera_opened": False, "real_actuator": False, "audio": False,
                "limits": "Software-rendered backup, not a camera recording or a completed team rehearsal. Normal input is saved keypoints or explicitly synthetic; falls, actions and notifications are simulated."}
    (output/"video_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    with zipfile.ZipFile(output.with_suffix(".zip"), "a", zipfile.ZIP_DEFLATED) as package:
        for name in ("software_demo.mp4", "video_manifest.json"):
            package.write(output/name, name)
    print(("软件演示备份" if report["ok"] else "未通过，仅保存部分备份供排查")+"：%s\n清单：%s" % (
        (output/"software_demo.mp4").resolve(), (output/"video_manifest.json").resolve()))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
