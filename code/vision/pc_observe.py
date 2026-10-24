"""Full-screen local PC camera observation, Chinese cues, no bus/actuator client."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

import config
from gesture.fall_rules import FallDetector
from vision.input_quality import InputQuality, LABELS
from vision.kpio import KeypointWriter


STAGES = (
    ("prepare_stand", "准备：退后，让肩部和髋部都入镜", 10),
    ("stand", "站立不动，保持自然姿势", 15),
    ("prepare_walk", "准备：只在画面范围内走动", 5),
    ("walk", "正常走动，不要碰电脑或摄像头", 15),
    ("prepare_sit", "准备：找稳固的椅子；没有椅子可跳过", 10),
    ("sit_stand", "缓慢坐下，再站起；按自己的节奏", 15),
)
WINDOW = "CareMate PC camera - Space start | F full screen | Q exit"


def stage_at(elapsed):
    if elapsed is None:
        return "framing", "先调整取景：尽量全身入镜；准备好后按空格", None
    for name, text, seconds in STAGES:
        if elapsed < seconds:
            return name, text, max(1, int(seconds - elapsed + .999))
        elapsed -= seconds
    return "finished", "本轮结束，可以回到聊天；按 Q 关闭画面", 0


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def draw_frame(frame, kp, detector, info):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from vision.preview import BONES, COLORS

    image = frame.copy()
    h, w = image.shape[:2]
    points = {k: (int(p[0] * w), int(p[1] * h)) for k, p in (kp or {}).items()
              if p is not None and (len(p) < 3 or p[2] >= detector.rules["min_visibility"])}
    color = COLORS.get(detector.state, (200, 200, 200))
    for a, b in BONES:
        if a in points and b in points:
            cv2.line(image, points[a], points[b], color, 3, cv2.LINE_AA)
    for point in points.values():
        cv2.circle(image, point, 4, (255, 255, 255), -1, cv2.LINE_AA)

    canvas = Image.new("RGB", (1440, 900), "#0e1929")
    scale = min(1040 / w, 640 / h)
    picture = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).resize((int(w * scale), int(h * scale)))
    canvas.paste(picture, (20 + (1040 - picture.width) // 2, 210 + (640 - picture.height) // 2))
    draw = ImageDraw.Draw(canvas)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.exists():
        raise RuntimeError("Chinese display font unavailable")
    # Pillow caches the font data through FreeType; retain objects across frames.
    if not hasattr(draw_frame, "fonts"):
        draw_frame.fonts = {s: ImageFont.truetype(str(font_path), s) for s in (24, 28, 38, 64)}
    fonts = draw_frame.fonts
    draw.text((26, 18), "CareMate · 电脑摄像头本地观察", font=fonts[38], fill="white")
    draw.text((26, 86), info["instruction"], font=fonts[38], fill="#ffe39a")
    draw.text((26, 153), "空格：开始一轮  |  F：切换全屏  |  Q：结束并保存", font=fonts[24], fill="#b5c5d9")
    x = 1090
    draw.text((x, 220), "当前状态", font=fonts[28], fill="#b5c5d9")
    status = info["input_quality"]["state"]
    short = {"observing": "正在观察", "no_person": "没有检测到人", "pose_unavailable": "肩/髋看不清楚",
             "candidate": "疑似倒地确认中", "suspected_fall": "本地疑似事件"}.get(status, "等待画面")
    draw.text((x, 272), short, font=fonts[28], fill="#96e4c0" if status == "observing" else "#ffe39a")
    if info["countdown"] is not None:
        draw.text((x, 350), str(info["countdown"]), font=fonts[64], fill="white")
        draw.text((x + 100, 384), "秒", font=fonts[28], fill="#b5c5d9")
    else:
        draw.text((x, 370), "准备好后按空格", font=fonts[28], fill="white")
    draw.text((x, 485), "观察模式", font=fonts[28], fill="white")
    draw.text((x, 535), "只存坐标，不存影像", font=fonts[24], fill="#b5c5d9")
    draw.text((x, 579), "不触发硬件或报警", font=fonts[24], fill="#b5c5d9")
    draw.text((x, 670), "无需做跌倒动作", font=fonts[28], fill="#ffe39a")
    draw.text((26, 861), "动作名称来自计划提示，程序没有核实你是否完成；本次观察不代表跌倒识别准确率。", font=fonts[24], fill="#b5c5d9")
    return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)


def observe(args):
    import cv2
    from vision.pose_backends import PoseEstimator

    folder = args.output
    folder.mkdir(parents=True, exist_ok=False)
    status_file = folder / "live_status.json"
    quality = InputQuality(log=lambda *_: None)
    detector = FallDetector()
    writer = KeypointWriter(folder / "keypoints.jsonl", "pc_camera:%d" % args.camera)
    cap = estimator = None
    started = time.monotonic()
    test_started = None
    last_saved = last_frame = 0.0
    stage = None
    phase_counts = {}
    events = []
    fullscreen = True
    info = {"status": "starting", "window_open": False, "frames": 0, "data_kind": "unlabeled_real_pc_camera",
            "images_saved": False, "bus_posts": 0, "phase_labels": "scheduled prompts, not verified actions"}
    write_json(status_file, info)
    try:
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError("摄像头无法打开，可能被其他程序占用")
        estimator = PoseEstimator(complexity=0)
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        info["window_open"] = True
        while time.monotonic() - started < args.duration:
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError("摄像头画面读取中断")
            now = time.monotonic()
            if now - last_frame < .1:
                continue
            last_frame = now
            t = now - started
            h, w = frame.shape[:2]
            kp = estimator(frame)
            quality.before_frame(t, w / h, detector)
            detector.aspect = w / h
            event = detector.update(t, kp)
            quality.after_frame(t, kp, w / h, detector)
            if event:
                events.append(event)  # Local record only. There is no poster here.
            current, instruction, countdown = stage_at(None if test_started is None else now - test_started)
            if current != stage:
                stage = current
                with (folder / "stages.jsonl").open("a", encoding="utf-8") as target:
                    target.write(json.dumps({"t": t, "stage": stage, "instruction": instruction}, ensure_ascii=False) + "\n")
            stats = phase_counts.setdefault(current, {"frames": 0, "usable_pose_frames": 0})
            stats["frames"] += 1
            stats["usable_pose_frames"] += int(quality.state not in ("no_person", "pose_unavailable"))
            writer.write(t, kp, w / h)
            info.update(status="observing", frames=quality.frames, elapsed_s=round(t, 2),
                        resolution=[w, h], stage=current, instruction=instruction, countdown=countdown,
                        input_quality=quality.snapshot(), phase_counts=phase_counts,
                        suspected_events=events, fullscreen=fullscreen,
                        prompted_round_complete=current == "finished")
            cv2.imshow(WINDOW, draw_frame(frame, kp, detector, info))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q")):
                break
            if key == 27 and fullscreen:
                fullscreen = False
                cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_NORMAL)
            if key in (ord("f"), ord("F")):
                fullscreen = not fullscreen
                cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN,
                                      cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)
            if key == ord(" ") and test_started is None:
                test_started = time.monotonic()
            if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                break
            if now - last_saved >= .5:
                write_json(status_file, info)
                writer.f.flush()
                last_saved = now
        info["status"] = "stopped"
    except KeyboardInterrupt:
        info["status"] = "interrupted"
    except Exception as exc:
        info.update(status="error", message=str(exc))
    finally:
        if cap is not None:
            cap.release()
        if estimator is not None:
            estimator.close()
        writer.close()
        cv2.destroyAllWindows()
        info.update(window_open=False, input_quality=quality.snapshot(), ended_utc=datetime.now(timezone.utc).isoformat())
        write_json(status_file, info)
        write_json(folder / "summary.json", info)
    return 1 if info["status"] == "error" else 0


def main(argv=None):
    config.utf8_console()
    parser = argparse.ArgumentParser(description="电脑摄像头全屏本地观察：中文提示，不连接主程序或执行端")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--duration", type=float, default=600, help="窗口最长保留秒数，默认10分钟")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.duration <= 0:
        parser.error("时长必须为正数")
    args.output = args.output or config.CODE_DIR / "data" / "pc_pose" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])
    if args.output.exists():
        parser.error("请使用新的保存目录，避免覆盖记录")
    print("本地观察：保存骨架和计划提示，不保存图像，不向主程序或硬件发送。", flush=True)
    print("保存位置：%s" % args.output.resolve(), flush=True)
    return observe(args)


if __name__ == "__main__":
    raise SystemExit(main())
