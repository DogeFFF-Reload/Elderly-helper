"""跌倒判定规则（视觉同学维护；这里是可运行的最小版）。

关键点用归一化坐标 (x, y)，y 向下增大。名称见 SKELETON_EDGES。
规则：躯干（颈→髋中点）与竖直方向夹角 > 60°，且髋部低于画面 70% → 倒地。
"""
import math

KEYPOINTS = ["head", "neck", "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_wrist", "r_wrist",
             "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle"]

STANDING = {
    "head": (0.50, 0.16), "neck": (0.50, 0.25),
    "l_shoulder": (0.44, 0.27), "r_shoulder": (0.56, 0.27),
    "l_elbow": (0.41, 0.40), "r_elbow": (0.59, 0.40),
    "l_wrist": (0.40, 0.52), "r_wrist": (0.60, 0.52),
    "l_hip": (0.46, 0.54), "r_hip": (0.54, 0.54),
    "l_knee": (0.46, 0.70), "r_knee": (0.54, 0.70),
    "l_ankle": (0.46, 0.86), "r_ankle": (0.54, 0.86),
}

FALLEN = {
    "head": (0.22, 0.80), "neck": (0.30, 0.81),
    "l_shoulder": (0.32, 0.76), "r_shoulder": (0.32, 0.86),
    "l_elbow": (0.40, 0.72), "r_elbow": (0.27, 0.90),
    "l_wrist": (0.47, 0.76), "r_wrist": (0.20, 0.88),
    "l_hip": (0.52, 0.79), "r_hip": (0.52, 0.86),
    "l_knee": (0.64, 0.78), "r_knee": (0.65, 0.87),
    "l_ankle": (0.78, 0.80), "r_ankle": (0.77, 0.88),
}


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def torso_angle(kp):
    neck = kp["neck"]
    hip = _mid(kp["l_hip"], kp["r_hip"])
    dx, dy = hip[0] - neck[0], hip[1] - neck[1]
    return math.degrees(math.atan2(abs(dx), abs(dy) + 1e-9))


def is_fallen(kp, angle_threshold=60.0, hip_floor=0.70):
    hip_y = _mid(kp["l_hip"], kp["r_hip"])[1]
    return torso_angle(kp) > angle_threshold and hip_y > hip_floor
