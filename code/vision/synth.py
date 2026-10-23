"""合成骨架序列：没有摄像头也能验证规则、跑通全链路（fall_bridge --synthetic fall）。

每个场景 = 关键帧之间平滑插值 + 少量抖动，附带期望结果，测试和 eval_falls 都用它。
"""
import math
import random

from gesture.fall_rules import FALLEN, STANDING

SITTING = {
    "head": (0.50, 0.27), "neck": (0.50, 0.36),
    "l_shoulder": (0.44, 0.38), "r_shoulder": (0.56, 0.38),
    "l_elbow": (0.41, 0.50), "r_elbow": (0.59, 0.50),
    "l_wrist": (0.42, 0.60), "r_wrist": (0.58, 0.60),
    "l_hip": (0.46, 0.64), "r_hip": (0.54, 0.64),
    "l_knee": (0.45, 0.72), "r_knee": (0.55, 0.72),
    "l_ankle": (0.45, 0.88), "r_ankle": (0.55, 0.88),
}

# 侧面弯腰捡东西：躯干横了，但髋部没降
BENDING = {
    "head": (0.24, 0.60), "neck": (0.30, 0.56),
    "l_shoulder": (0.31, 0.53), "r_shoulder": (0.31, 0.59),
    "l_elbow": (0.31, 0.66), "r_elbow": (0.32, 0.70),
    "l_wrist": (0.31, 0.78), "r_wrist": (0.32, 0.80),
    "l_hip": (0.50, 0.52), "r_hip": (0.50, 0.56),
    "l_knee": (0.50, 0.70), "r_knee": (0.51, 0.71),
    "l_ankle": (0.50, 0.86), "r_ankle": (0.51, 0.87),
}

BED_ZONE = [0.15, 0.45, 0.85, 0.68]
IN_BED = {k: (x, y - 0.24) for k, (x, y) in FALLEN.items()}

# (动作, 目标姿态, 秒)；"gone" = 画面里没人（被床/柜子挡住）
SCENARIOS = {
    "fall": {"expect": ["rapid"], "steps": [
        ("hold", STANDING, 2.0), ("move", FALLEN, 0.6), ("hold", FALLEN, 6.0)]},
    "stumble": {"expect": [], "steps": [
        ("hold", STANDING, 2.0), ("move", FALLEN, 0.6), ("hold", FALLEN, 0.3),
        ("move", STANDING, 0.6), ("hold", STANDING, 4.0)]},
    "sit": {"expect": [], "steps": [
        ("hold", STANDING, 2.0), ("move", SITTING, 1.0), ("hold", SITTING, 15.0)]},
    "chair_fall": {"expect": ["rapid"], "steps": [
        ("hold", SITTING, 3.0), ("move", FALLEN, 0.7), ("hold", FALLEN, 5.0)]},
    "bend": {"expect": [], "steps": [
        ("hold", STANDING, 2.0), ("move", BENDING, 0.8), ("hold", BENDING, 15.0),
        ("move", STANDING, 0.8), ("hold", STANDING, 2.0)]},
    "bed": {"expect": [], "rules": {"bed_zones": [BED_ZONE]}, "steps": [
        ("hold", STANDING, 2.0), ("move", IN_BED, 3.0), ("hold", IN_BED, 25.0)]},
    "slow_floor": {"expect": ["slow"], "steps": [
        ("hold", STANDING, 2.0), ("move", FALLEN, 4.0), ("hold", FALLEN, 16.0)]},
    "fall_occluded": {"expect": ["rapid"], "steps": [
        ("hold", STANDING, 2.0), ("move", FALLEN, 0.6), ("hold", FALLEN, 0.4), ("gone", None, 5.0)]},
    "refall": {"expect": ["rapid", "rapid"], "steps": [
        ("hold", STANDING, 2.0), ("move", FALLEN, 0.6), ("hold", FALLEN, 4.0),
        ("move", STANDING, 1.5), ("hold", STANDING, 4.0),
        ("move", FALLEN, 0.6), ("hold", FALLEN, 4.0)]},
}


def _smooth(u):
    return u * u * (3 - 2 * u)


def _lerp(a, b, u):
    return {k: (a[k][0] + (b[k][0] - a[k][0]) * u, a[k][1] + (b[k][1] - a[k][1]) * u) for k in a}


def build(name, fps=15.0, noise=0.004, seed=0):
    """返回 [(t, kp 或 None), ...]，kp 的每个点是 [x, y, 可见度]。"""
    rng = random.Random(seed)
    steps = SCENARIOS[name]["steps"]
    frames, t, dt = [], 0.0, 1.0 / fps
    current = steps[0][1]
    for action, pose, secs in steps:
        n = max(1, int(round(secs * fps)))
        start = current
        for i in range(n):
            if action == "gone":
                frames.append((round(t, 4), None))
            else:
                u = _smooth((i + 1) / n) if action == "move" else 1.0
                kp = _lerp(start, pose, u) if action == "move" else pose
                frames.append((round(t, 4), {k: [round(x + rng.gauss(0, noise), 4),
                                                  round(y + rng.gauss(0, noise), 4), 0.99]
                                             for k, (x, y) in kp.items()}))
            t += dt
        if pose is not None:
            current = pose
    return frames


def duration(name):
    return math.fsum(s for _, _, s in SCENARIOS[name]["steps"])
