"""Authenticated phone MJPEG -> pose observation. No bus or actuator requests."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import uuid

from camera.phone_calibrate import CollectionError, FrameWaitTimeout, LatestFrame, wait_until
from camera.phone_source import PhoneFrameSource
from gesture.fall_rules import FallDetector, features
from vision.input_quality import InputQuality
from vision.kpio import KeypointWriter, load_rules


def jpeg_width(jpeg):
    """Width from the JPEG SOF header without decoding; None if not found."""
    i = 2
    while i + 9 < len(jpeg):
        if jpeg[i] != 0xFF:
            return None
        marker = jpeg[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(jpeg[i + 7:i + 9], "big")
        i += 2 + int.from_bytes(jpeg[i + 2:i + 4], "big")
    return None


def decode_frame(jpeg):
    import cv2
    import numpy as np
    # Frames wider than 1280 px are decoded at half size: decode and pose both get
    # ~30% faster, the pose model downsamples to 256 px anyway, the monitor to 960 px.
    width = jpeg_width(jpeg)
    flags = cv2.IMREAD_REDUCED_COLOR_2 if width and width > 1280 else cv2.IMREAD_COLOR
    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), flags)
    if frame is None:
        raise CollectionError("手机返回的画面无法解码，请检查视频地址。")
    return frame


def phone_source(camera, estimator, *, duration=60.0, fps=10.0, max_frames=0, frame_timeout=3.0,
                 continuous=False, startup_timeout=12.0):
    """Use fresh frames and actual receipt times; inference never queues MJPEG."""
    if not all(math.isfinite(n) and n > 0 for n in (duration, fps, frame_timeout, startup_timeout)) or max_frames < 0:
        raise ValueError("时长、帧率和超时必须为正数，帧数不得为负数")
    if type(continuous) is not bool:
        raise ValueError("continuous须为布尔值")
    interval = 1.0 / fps
    reader = LatestFrame(camera, interval)
    reader.start()
    started = time.monotonic()
    deadline, due = (math.inf if continuous else started + duration), started
    sequence, count = 0, 0
    try:
        while True:
            wait_until(min(due, deadline), reader, frame_timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                # Login and homepage discovery may take longer than an ordinary
                # gap. This never relaxes the live-frame freshness threshold.
                budget = startup_timeout if sequence == 0 else frame_timeout
                sequence, jpeg, received, _utc = reader.get(
                    after=sequence, not_before=due, timeout=min(budget, remaining))
            except FrameWaitTimeout:
                if time.monotonic() < deadline:
                    raise
                reader.check(frame_timeout)
                return
            if received >= deadline:
                return
            frame = decode_frame(jpeg)
            height, width = frame.shape[:2]
            kp = estimator(frame)
            yield received - started, kp, frame, width / height
            count += 1
            if max_frames and count >= max_frames:
                return
            due = max(received + interval, time.monotonic())
    finally:
        reader.close()


class ObservationPoster:
    """Implements the bridge sink locally. There is no network client here."""
    def __init__(self):
        self.falls = 0

    def skeleton(self, _kp, _fallen):
        pass

    def fall(self, _room):
        self.falls += 1


def observe_phone(args, runner, *, estimator_factory=None, log=print):
    """Run the repository's pose/rule pipeline and save replayable coordinates."""
    camera = PhoneFrameSource(args.phone_stream, mjpeg=True,
                              username=args.phone_username, password=args.phone_password)
    if args.record_keypoints:
        path = Path(args.record_keypoints).resolve()
    else:
        session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
        path = Path(__file__).resolve().parents[1] / "data" / "phone_pose" / session / "keypoints.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {"schema_version": 1, "mode": "phone_pose_observation", "status": "running",
               "started_utc": datetime.now(timezone.utc).isoformat(), "frames": 0,
               "person_frames": 0, "usable_pose_frames": 0, "local_suspected_events": None,
               "duration_limit_s": args.duration, "fps_limit": args.fps_cap or 10,
               "frame_limit": args.frames, "ground_truth": "unlabeled_observation",
               "limits": "Detection counts are availability, not accuracy; no fall ground truth."}
    estimator = preview = source = recorder = None
    poster = ObservationPoster()
    quality = InputQuality(log=log)
    finished = False
    rules = load_rules(args.rules, args.set, args.bed)
    try:
        if estimator_factory is None:
            from vision.pose_backends import PoseEstimator
            estimator_factory = PoseEstimator
        estimator = estimator_factory(complexity=args.complexity)
        recorder = KeypointWriter(path, "phone:mjpeg")  # Never put the URL/credentials in metadata.
        if args.preview:
            from vision.preview import Preview
            preview = Preview(rules)
        detector = FallDetector(rules)
        source = phone_source(camera, estimator, duration=args.duration, fps=args.fps_cap or 10, max_frames=args.frames)

        def tracked_source():
            nonlocal finished
            try:
                for t, kp, frame, aspect in source:
                    summary["frames"] += 1
                    summary["person_frames"] += int(kp is not None)
                    summary["usable_pose_frames"] += int(features(kp, aspect, detector.rules["min_visibility"]) is not None)
                    summary["last_frame_s"] = t
                    yield t, kp, frame, aspect
                finished = True
            finally:
                source.close()

        log("[手机姿态观察] 画面只在内存中处理；保存骨架坐标，本地判断，不向主脑发送。")
        found = runner(tracked_source(), detector, poster, args.room, recorder, preview, log=log, quality=quality)
        summary["local_suspected_events"] = found
        summary["status"] = "complete" if finished else "stopped"
        if not summary["usable_pose_frames"]:
            summary["status"] = "no_usable_pose"
            summary["message"] = "未得到可用躯干骨架；请查看画面、人物位置和可见度。"
    except KeyboardInterrupt:
        summary.update(status="interrupted", message="用户中断，保留已录骨架。")
    except CollectionError as exc:
        summary.update(status="error", message=str(exc))
    except ImportError:
        summary.update(status="error", message="缺少姿态依赖，请安装 code/requirements-vision.txt。")
    except SystemExit as exc:
        summary.update(status="error", message=str(exc))
    except Exception:
        summary.update(status="error", message="姿态处理失败；保留已录骨架，请检查模型和输入画面。")
    finally:
        if source:
            source.close()
        if recorder:
            recorder.close()
        if preview:
            preview.close()
        if estimator:
            estimator.close()
        summary["local_suspected_count"] = poster.falls
        summary["input_quality"] = quality.snapshot()
        summary["ended_utc"] = datetime.now(timezone.utc).isoformat()
        path.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("[观察结果] 状态=%s，处理%d帧，检测到人%d帧，可用躯干骨架%d帧。" % (
        summary["status"], summary["frames"], summary["person_frames"], summary["usable_pose_frames"]))
    log("骨架文件：%s\n结果摘要：%s" % (path if path.is_file() else "未生成", path.with_suffix(".summary.json")))
    if summary.get("message"):
        log(summary["message"])
    return 0 if summary["status"] in ("complete", "stopped") else 130 if summary["status"] == "interrupted" else 1
