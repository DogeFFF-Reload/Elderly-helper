"""摄像头桥（视觉模块）：画面 → 骨架 → 跌倒规则 → 总线。

输入（选一个）：
  --camera [N]        实拍，默认 0 号摄像头
  --video PATH        预录视频当输入（Plan B：现场光照/遮挡不行时）
  --phone-stream URL  手机 MJPEG 输入；只做本地姿态观察，默认60秒，保存骨架
  --keypoints PATH    回放录下来的骨架（不需要摄像头和模型，最稳的兜底）
  --synthetic NAME    内置合成场景：fall / stumble / sit / bend / bed / slow_floor ...
  --mock              直接发一次倒地（最早的 mock 方式）

发给总线的格式与 mock 完全一致，主脑不用改：
  POST /api/skeleton {"keypoints": {...}, "fallen": bool}   大屏画实时骨架（约 5 次/秒）
  POST /mock/fall?room=卧室                                 规则判定跌倒时发一次
画面只在本机内存里处理，不落盘、不外发；--record-keypoints 只存坐标。

调参：--preview 开一个本机窗口看骨架和判定过程；--set lying_angle=50 改单个阈值；
     --bed x1,y1,x2,y2 标出床（床上躺着不报）；录下来的骨架用 vision.eval_falls 批量评估。
"""
import argparse
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request

import config
from gesture.fall_rules import FALLEN, FallDetector, is_fallen
from vision import synth
from vision.input_quality import InputQuality
from vision.kpio import KeypointWriter, load_rules, read_keypoints

SKELETON_INTERVAL = 0.2
MIN_POST_VISIBILITY = 0.3


def _post(url, payload=None, timeout=2.0):
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class BusPoster:
    """后台线程发送，摄像头循环永不因网络卡住；骨架只发最新一帧，跌倒事件排队必达。"""

    def __init__(self, server, dry_run=False, log=print, post=_post):
        self.server = server.rstrip("/")
        self.dry_run = dry_run
        self.log = log
        self.post = post
        self.events = queue.Queue()
        self.latest = None
        self.last_sent = 0.0
        self.cond = threading.Condition()
        self.busy = False
        self.online = None
        threading.Thread(target=self._worker, daemon=True).start()

    def skeleton(self, kp, fallen):
        now = time.monotonic()
        if kp is None or now - self.last_sent < SKELETON_INTERVAL:
            return
        self.last_sent = now
        pts = {k: [round(p[0], 4), round(p[1], 4)] for k, p in kp.items()
               if p is not None and (len(p) < 3 or p[2] >= MIN_POST_VISIBILITY)}
        with self.cond:
            self.latest = {"keypoints": pts, "fallen": bool(fallen)}
            self.cond.notify()

    def fall(self, room):
        with self.cond:
            self.events.put(room)
            self.cond.notify()

    def drain(self, timeout=3.0):
        end = time.monotonic() + timeout
        while (not self.events.empty() or self.latest or self.busy) and time.monotonic() < end:
            time.sleep(0.05)

    def _worker(self):
        while True:
            with self.cond:
                while self.events.empty() and self.latest is None:
                    self.cond.wait()
                room = None if self.events.empty() else self.events.get()
                skel, self.latest = (self.latest, None) if room is None else (None, self.latest)
                self.busy = True
            try:
                if room is not None:
                    url = "%s/mock/fall?room=%s" % (self.server, urllib.parse.quote(room))
                    if self.dry_run:
                        self.log("→ [dry-run] POST " + url)
                    else:
                        r = self.post(url)
                        self.log("→ 已上报跌倒（%s）总线状态：%s" % (room, r.get("state")))
                elif skel is not None and not self.dry_run:
                    self.post(self.server + "/api/skeleton", skel)
                if self.online is False:
                    self.log("· 总线已恢复连接")
                self.online = True
            except Exception as e:  # 总线没起来时不能让视觉挂掉
                if self.online is not False:
                    self.log("× 连不上总线 %s（%s），会继续重试" % (self.server, e))
                self.online = False
                if room is not None:
                    self.events.put(room)
                    time.sleep(1.0)
            finally:
                self.busy = False


# ---------- 输入源：都产出 (t, kp, frame, aspect) ----------

def _pace(t0, t):
    delay = t0 + t - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def keypoint_source(frames, aspect, realtime=True):
    t0 = time.monotonic()
    for t, kp in frames:
        if realtime:
            _pace(t0, t)
        yield t, kp, None, aspect


def opencv_source(capture_arg, estimator, realtime, fps_cap=0.0, log=print):
    import cv2
    cap = cv2.VideoCapture(capture_arg)
    if not cap.isOpened():
        raise SystemExit("打不开 %s（摄像头编号不对、被占用，或视频路径错误）" % capture_arg)
    is_file = isinstance(capture_arg, str)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if is_file and fps <= 1:
        fps = 25.0
    t0, n, last_proc = time.monotonic(), 0, -1.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if is_file:
                    return
                log("× 摄像头读帧失败，1 秒后重试")
                time.sleep(1.0)
                continue
            t = n / fps if is_file else time.monotonic() - t0
            n += 1
            if is_file and realtime:
                _pace(t0, t)
            if fps_cap and t - last_proc < 1.0 / fps_cap:
                continue
            last_proc = t
            h, w = frame.shape[:2]
            yield t, estimator(frame), frame, w / h
    finally:
        cap.release()


# ---------- 主循环 ----------

def run(source, detector, poster, room, recorder=None, preview=None, log=print, quality=None):
    """跑完一个输入源；返回触发的报警列表。"""
    alarms, seen, frames, t_report = [], 0, 0, 0.0
    quality = quality if quality is not None else InputQuality(log=log)
    try:
        for t, kp, frame, aspect in source:
            quality.before_frame(t, aspect, detector)
            detector.aspect = aspect
            ev = detector.update(t, kp)
            quality.after_frame(t, kp, aspect, detector)
            frames += 1
            seen += kp is not None
            poster.skeleton(kp, detector.state != FallDetector.WATCH)
            if recorder:
                recorder.write(t, kp, aspect)
            if ev:
                alarms.append(ev)
                log("⚠ %.1fs 判定跌倒（%s）：%s" % (t, ev["kind"], ev["reason"]))
                poster.fall(room)
            if preview and not preview.show(frame, kp, detector):
                break
            if t - t_report >= 5.0:
                log("· %.0fs  检测到人的帧占比 %d%%  状态 %s" % (t, 100 * seen // max(frames, 1), detector.state))
                t_report, seen, frames = t, 0, 0
    except Exception:
        quality.set_state("input_error", quality.last_t)
        raise
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    return alarms


def main():
    config.utf8_console()
    ap = argparse.ArgumentParser(description="视觉桥：骨架 → 跌倒规则 → 总线")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--camera", nargs="?", const=0, type=int, metavar="N", help="摄像头编号，默认 0")
    src.add_argument("--video", metavar="PATH")
    src.add_argument("--phone-stream", metavar="URL", help="手机完整 MJPEG 地址；本入口只观察、不向总线上报")
    src.add_argument("--keypoints", metavar="PATH")
    src.add_argument("--synthetic", choices=sorted(synth.SCENARIOS))
    src.add_argument("--mock", action="store_true")
    ap.add_argument("--room", default=config.load()["default_room"])
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--rules", help="规则 JSON 文件")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="覆盖单个阈值，可重复")
    ap.add_argument("--bed", action="append", metavar="x1,y1,x2,y2", help="床区（归一化坐标），可重复")
    ap.add_argument("--preview", action="store_true", help="本机窗口显示骨架和判定过程（调参用，不保存）")
    ap.add_argument("--record-keypoints", metavar="PATH", help="把骨架坐标录成 .jsonl（不含图像）")
    ap.add_argument("--loop", action="store_true", help="视频/骨架/合成场景循环播放")
    ap.add_argument("--fast", action="store_true", help="视频/骨架不按真实时间播放（离线跑）")
    ap.add_argument("--fps-cap", type=float, default=0.0, help="最多每秒处理几帧（X5 上省 CPU，如 10）")
    ap.add_argument("--complexity", type=int, choices=[0, 1, 2], default=0, help="姿态模型大小：0 快 / 2 准")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    ap.add_argument("--phone-username", default=os.environ.get("PHONE_USERNAME"))
    ap.add_argument("--phone-password", default=os.environ.get("PHONE_PASSWORD"))
    ap.add_argument("--duration", type=float, default=60, help="手机姿态观察时长，默认60秒")
    ap.add_argument("--frames", type=int, default=0, help="手机处理帧数上限，0=仅按时长结束")
    args = ap.parse_args()

    if args.phone_stream:
        if args.loop:
            ap.error("手机姿态观察不使用 --loop")
        if (not math.isfinite(args.duration) or args.duration <= 0 or not math.isfinite(args.fps_cap)
                or args.fps_cap < 0 or args.frames < 0):
            ap.error("手机观察时长须为正数，帧率和帧数上限须非负")
        from vision.phone_input import observe_phone
        try:
            return observe_phone(args, run)
        except ValueError as exc:
            ap.error(str(exc))

    poster = BusPoster(args.server, dry_run=args.dry_run)

    if args.mock:
        kp = {k: list(v) for k, v in FALLEN.items()}
        poster.skeleton(kp, is_fallen(FALLEN))
        poster.fall(args.room)
        poster.drain()
        return

    rules = load_rules(args.rules, args.set, args.bed)
    estimator = None
    if args.camera is not None or args.video:
        from vision.pose_backends import PoseEstimator
        estimator = PoseEstimator(complexity=args.complexity)

    def make_source():
        if args.camera is not None:
            return opencv_source(args.camera, estimator, True, args.fps_cap)
        if args.video:
            return opencv_source(args.video, estimator, not args.fast, args.fps_cap)
        if args.keypoints:
            meta, frames = read_keypoints(args.keypoints)
            return keypoint_source(frames, meta.get("aspect", 4 / 3), not args.fast)
        sc = synth.SCENARIOS[args.synthetic]
        rules.update({k: v for k, v in sc.get("rules", {}).items() if k not in rules})
        return keypoint_source(synth.build(args.synthetic), 4 / 3, not args.fast)

    preview = None
    if args.preview:
        from vision.preview import Preview
        preview = Preview(rules)
    recorder = None
    if args.record_keypoints:
        source_name = "camera:%s" % args.camera if args.camera is not None else (args.video or args.keypoints or args.synthetic)
        recorder = KeypointWriter(args.record_keypoints, source_name)

    print("视觉桥启动：%s → %s（房间：%s）" % (
        "camera %s" % args.camera if args.camera is not None else args.video or args.keypoints or "synthetic:" + args.synthetic,
        "dry-run" if args.dry_run else args.server, args.room), flush=True)
    try:
        while True:
            detector = FallDetector(rules)
            run(make_source(), detector, poster, args.room, recorder, preview)
            if not args.loop or args.camera is not None:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if recorder:
            recorder.close()
            print("骨架已保存：%s" % args.record_keypoints)
        if preview:
            preview.close()
        if estimator:
            estimator.close()
        poster.drain()


if __name__ == "__main__":
    sys.exit(main())
