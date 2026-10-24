"""Input availability, separate from the detector's suspected-fall state."""
import math

from gesture.fall_rules import FallDetector, features


LABELS = {
    "waiting": "等待画面",
    "observing": "正在观察",
    "no_person": "未检测到人",
    "pose_unavailable": "身体关键点不完整或可见度不足",
    "input_gap": "画面间隔过长，已重新开始动作计时",
    "candidate": "疑似倒地，正在确认",
    "suspected_fall": "疑似跌倒已触发",
    "input_error": "输入中断或画面无效，观察已停止",
}


class InputQuality:
    def __init__(self, max_gap=3.0, log=print):
        self.max_gap = max_gap
        self.log = log
        self.last_t = None
        self.frames = self.person_frames = self.usable_frames = self.gaps = 0
        self.max_observed_gap = 0.0
        self.state = "waiting"
        self.transitions = []

    def set_state(self, state, t=None):
        if state != self.state:
            self.state = state
            # Bound the report size when visibility flickers for a long time.
            self.transitions.append({"t": t, "state": state, "text": LABELS[state]})
            self.transitions = self.transitions[-100:]
            self.log("[视觉状态] " + LABELS[state])

    def before_frame(self, t, aspect, detector):
        if not math.isfinite(t) or t < 0 or not math.isfinite(aspect) or aspect <= 0:
            raise ValueError("骨架时间和画面宽高比必须是有效正数（时间可为0）")
        if self.last_t is not None:
            gap = t - self.last_t
            if gap <= 0:
                raise ValueError("骨架时间必须严格递增")
            self.max_observed_gap = max(self.max_observed_gap, gap)
            if gap > self.max_gap:
                self.gaps += 1
                # Missing video time is not evidence that a person stayed down.
                # Preserve an already emitted fall so a gap cannot re-arm it.
                fallen = detector.state == FallDetector.FALLEN
                detector.reset()
                if fallen:
                    detector.state = FallDetector.FALLEN
                self.set_state("input_gap", t)
        self.last_t = t

    def after_frame(self, t, kp, aspect, detector):
        self.frames += 1
        self.person_frames += int(bool(kp))
        usable = features(kp, aspect, detector.rules["min_visibility"]) is not None
        self.usable_frames += int(usable)
        state = "no_person" if not kp else "pose_unavailable" if not usable else {
            FallDetector.WATCH: "observing", FallDetector.CANDIDATE: "candidate",
            FallDetector.FALLEN: "suspected_fall",
        }[detector.state]
        self.set_state(state, t)
        detector.input_status = state

    def snapshot(self):
        return {"state": self.state, "text": LABELS[self.state], "frames": self.frames,
                "person_frames": self.person_frames, "usable_pose_frames": self.usable_frames,
                "input_gaps": self.gaps, "max_gap_s": round(self.max_observed_gap, 4),
                "gap_reset_after_s": self.max_gap, "transitions": list(self.transitions)}
