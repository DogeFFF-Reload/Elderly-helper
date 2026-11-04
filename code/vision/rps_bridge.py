"""猜拳视觉桥：总线喊开始 → 揭晓那一刻看老人的手 → 回传手势，输赢由总线判。

  python -m vision.rps_bridge --camera 0 [--preview]
  python -m vision.rps_bridge --stdin        # 没摄像头：每局键盘输入 石头/剪刀/布（或 r/s/p）

流程：老人说"石头剪刀布" → 总线发 rps 事件（phase=start，reveal_in 秒后手#1 揭晓）
     → 本桥在揭晓前后约 2 秒内逐帧识别手势、多帧投票
     → POST /api/rps/gesture {"round": n, "gesture": "rock"|"scissors"|"paper"|null}
只在这个窗口里跑手部模型，平时不占 CPU；画面不落盘。
"""
import argparse
import sys
import threading
import time

from config import utf8_console
from gesture.rps_engine import NAMES, classify, vote
from voice.bus_client import EventFollower, post_json

LEAD = 0.6       # 揭晓前多久开始看（含事件轮询延迟）
WINDOW = 2.1     # 一共看多久
KEYS = {"r": "rock", "s": "scissors", "p": "paper", "石头": "rock", "剪刀": "scissors", "布": "paper"}


class RoundTracker:
    """把总线的 rps 开始事件换算成本机时钟下的识别窗口。"""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.current = None

    def on_event(self, ev):
        data = ev.get("data") or {}
        if data.get("phase") != "start":
            return
        start = self.clock() + float(data.get("reveal_in", 3.5)) - LEAD
        with self.lock:
            self.current = {"round": data.get("round"), "start": start, "end": start + WINDOW, "labels": []}

    def feed(self, label):
        """识别窗口内喂一帧的判断结果；窗口结束时返回 (round, 投票结果)，否则 None。"""
        now = self.clock()
        with self.lock:
            r = self.current
            if r is None or now < r["start"]:
                return None
            if now <= r["end"]:
                r["labels"].append(label)
                return None
            self.current = None
        return r["round"], vote(r["labels"])

    def active(self):
        with self.lock:
            return self.current is not None and self.clock() >= self.current["start"]


def report(server, round_id, gesture, log=print):
    try:
        r = post_json(server + "/api/rps/gesture", {"round": round_id, "gesture": gesture})
        log("→ 第 %s 局：老人出 %s → %s" % (round_id, NAMES.get(gesture, "（没看清）"), r.get("result") or r.get("reason")))
    except Exception as e:
        log("× 回传失败：%s" % e)


def run_camera(server, index, preview):
    import cv2
    from vision.pose_backends import HandEstimator
    tracker = RoundTracker()
    EventFollower(server, tracker.on_event, types=["rps"]).start()
    hands = HandEstimator()
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        sys.exit("打不开摄像头 %s" % index)
    print("等总线喊“石头剪刀布”…（Ctrl+C 退出）", flush=True)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.2)
                continue
            label = None
            if tracker.active() or preview:
                h, w = frame.shape[:2]
                lm = hands(frame)
                label = classify(lm, w / h) if lm else None
            done = tracker.feed(label)
            if done:
                report(server, *done)
            if preview:
                text = "%s %s" % ("LOOKING" if tracker.active() else "idle", label or "-")
                cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (36, 191, 251), 2)
                cv2.imshow("CareMate RPS (q to quit)", frame)
                if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    break
            elif not tracker.active():
                time.sleep(0.03)
    finally:
        cap.release()
        hands.close()
        if preview:
            cv2.destroyAllWindows()


def run_stdin(server):
    tracker = RoundTracker()

    def on_event(ev):
        tracker.on_event(ev)
        if (ev.get("data") or {}).get("phase") == "start":
            print("\n第 %s 局开始：老人出了什么？输入 石头/剪刀/布 或 r/s/p，回车" % ev["data"].get("round"), flush=True)

    EventFollower(server, on_event, types=["rps"]).start()
    print("键盘模式：等总线喊“石头剪刀布”…（Ctrl+C 退出）", flush=True)
    for line in sys.stdin:
        gesture = KEYS.get(line.strip().lower())
        with tracker.lock:
            current, tracker.current = tracker.current, None
        if current is None:
            print("现在没有进行中的一局", flush=True)
            continue
        report(server, current["round"], gesture)


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description="猜拳视觉桥")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--camera", nargs="?", const=0, type=int, metavar="N")
    src.add_argument("--stdin", action="store_true")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--preview", action="store_true", help="本机窗口显示识别结果（不保存）")
    args = ap.parse_args()
    try:
        if args.stdin:
            run_stdin(args.server.rstrip("/"))
        else:
            run_camera(args.server.rstrip("/"), args.camera, args.preview)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
