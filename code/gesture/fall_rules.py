"""跌倒判定规则（视觉模块）。

关键点用归一化坐标 [x, y] 或 [x, y, 可见度]，原点左上、y 向下。名称见 KEYPOINTS。

单帧只能看出"身体是不是横着"，分不清摔倒、弯腰捡东西、上床躺下。所以判定看时间：

  快速跌倒：fall_window 秒内髋部下降 ≥ min_hip_drop 个躯干长，且躯干变横
            （站着、坐着摔下来都算；弯腰时髋部不降，不算）
            → 之后 confirm_hold 秒内没站起来（看不见人也算没站起来）→ 报警
  缓慢滑倒：躯干横着、髋部低（比站立时低 min_hip_drop 个躯干长）、不在床区，
            持续 slow_hold 秒 → 报警（老人常常是慢慢滑坐到地上的）
  床区内躺着是正常的，不报。

报警后要连续站立 recover_hold 秒才重新布防，避免一次摔倒报很多次。
所有阈值都在 DEFAULT_RULES，可用 JSON 覆盖（fall_bridge / eval_falls 的 --rules、--set）。
"""
import collections
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

DEFAULT_RULES = {
    "upright_angle": 35.0,     # 躯干与竖直夹角 ≤ 这个 = 直立
    "lying_angle": 55.0,       # ≥ 这个 = 横着
    "fall_window": 1.0,        # 秒：髋部在这么短时间内大幅下降才算"快速"（慢慢滑坐不算）
    "min_hip_drop": 0.5,       # 髋部下降多少个躯干长（弯腰时髋部几乎不降，靠这条区分）
    "confirm_hold": 2.0,       # 秒：快速跌倒后这么久没站起来 → 报警
    "cancel_hold": 0.5,        # 秒：候选期间连续直立这么久 → 取消（单帧抖动不算）
    "slow_hold": 12.0,         # 秒：横躺在低处（非床区）这么久 → 报警
    "recover_hold": 2.0,       # 秒：报警后连续直立这么久 → 重新布防
    "floor_y": 0.70,           # 没见过站姿时，髋部 y 超过它算"低处"
    "min_visibility": 0.4,     # 关键点可见度低于它当没看见
    "hip_smooth": 0.15,        # 秒：髋部高度平滑时间常数，压住单帧抖动
    "bed_zones": [],           # [[x1, y1, x2, y2], ...] 归一化；髋部中点在区内 = 在床上
}

_TORSO = ("neck", "l_shoulder", "r_shoulder", "l_hip", "r_hip")


def _vis(p):
    return p[2] if len(p) > 2 else 1.0


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def torso_angle(kp, aspect=1.0):
    neck = kp["neck"]
    hip = _mid(kp["l_hip"], kp["r_hip"])
    dx, dy = (hip[0] - neck[0]) * aspect, hip[1] - neck[1]
    return math.degrees(math.atan2(abs(dx), abs(dy) + 1e-9))


def is_fallen(kp, angle_threshold=60.0, hip_floor=0.70):
    """单帧粗判（mock 和大屏用）；真正报警用 FallDetector。"""
    hip_y = _mid(kp["l_hip"], kp["r_hip"])[1]
    return torso_angle(kp) > angle_threshold and hip_y > hip_floor


def features(kp, aspect=1.0, min_visibility=0.4):
    """单帧特征；躯干关键点看不全返回 None。aspect = 画面宽/高，用来还原真实角度。"""
    if not kp:
        return None
    pts = {}
    for name, p in kp.items():
        if p is not None and _vis(p) >= min_visibility:
            pts[name] = p
    if "neck" not in pts and "l_shoulder" in pts and "r_shoulder" in pts:
        pts["neck"] = _mid(pts["l_shoulder"], pts["r_shoulder"])
    if any(k not in pts for k in ("neck", "l_hip", "r_hip")):
        return None
    hip = _mid(pts["l_hip"], pts["r_hip"])
    neck = pts["neck"]
    length = math.hypot((hip[0] - neck[0]) * aspect, hip[1] - neck[1])
    if length < 1e-3:
        return None
    return {
        "angle": torso_angle(pts, aspect),
        "hip_x": hip[0],
        "hip_y": hip[1],
        "torso_len": length,
        "head_y": pts["head"][1] if "head" in pts else None,
    }


def in_zone(x, y, zones):
    return any(z[0] <= x <= z[2] and z[1] <= y <= z[3] for z in zones)


class FallDetector:
    WATCH, CANDIDATE, FALLEN = "WATCH", "CANDIDATE", "FALLEN"

    def __init__(self, rules=None, aspect=4 / 3):
        self.rules = dict(DEFAULT_RULES)
        if rules:
            self.rules.update(rules)
        self.aspect = aspect
        self.reset()

    def reset(self):
        self.state = self.WATCH
        self.history = collections.deque()
        self.standing_hip = None    # 站立时髋部高度（指数平滑），判断"低处"用
        self.standing_len = None
        self.hip_s = None           # 平滑后的髋部高度
        self.hip_t = None
        self.lying_since = None
        self.candidate_since = None
        self.upright_since = None
        self.last = None            # 最近一帧的特征 + 判断，调参叠加层用

    def _low(self, f):
        r = self.rules
        if self.standing_hip is not None:
            return f["hip_y"] - self.standing_hip >= r["min_hip_drop"] * self.standing_len
        return f["hip_y"] >= r["floor_y"]

    def update(self, t, kp):
        """喂一帧（kp 为 None = 没检测到人）。触发报警时返回事件 dict，否则 None。"""
        r = self.rules
        f = features(kp, self.aspect, r["min_visibility"])
        upright = lying = in_bed = low = False
        if f is not None:
            if self.hip_s is None:
                self.hip_s = f["hip_y"]
            else:
                a = 1 - math.exp(-max(t - self.hip_t, 1e-3) / r["hip_smooth"])
                self.hip_s += a * (f["hip_y"] - self.hip_s)
            self.hip_t = t
            f["hip_y"] = self.hip_s
            upright = f["angle"] <= r["upright_angle"]
            lying = f["angle"] >= r["lying_angle"]
            in_bed = in_zone(f["hip_x"], f["hip_y"], r["bed_zones"])
            if upright:
                # 站立髋高取"最近见过的最高位置"，只缓慢下移：
                # 摔倒后坐在地上（躯干也是直的）不会立刻把基准拉到地面
                if self.standing_hip is None or f["hip_y"] < self.standing_hip:
                    self.standing_hip = f["hip_y"]
                else:
                    self.standing_hip += 0.005 * (f["hip_y"] - self.standing_hip)
                self.standing_len = f["torso_len"] if self.standing_len is None else \
                    0.9 * self.standing_len + 0.1 * f["torso_len"]
            low = self._low(f)
            self.history.append((t, f))
        while self.history and t - self.history[0][0] > r["fall_window"]:
            self.history.popleft()
        self.upright_since = (self.upright_since if self.upright_since is not None else t) if upright else None
        self.last = {"t": t, "f": f, "upright": upright, "lying": lying, "in_bed": in_bed, "low": low,
                     "state": self.state}

        if self.state == self.WATCH:
            if lying and not in_bed:
                if self._rapid_drop(f):
                    self.state, self.candidate_since = self.CANDIDATE, t
                elif low:
                    self.lying_since = self.lying_since if self.lying_since is not None else t
                    if t - self.lying_since >= r["slow_hold"]:
                        return self._alarm(t, "slow", "横躺在低处 %.0f 秒没起来" % (t - self.lying_since))
                else:
                    self.lying_since = None
            else:
                self.lying_since = None
        elif self.state == self.CANDIDATE:
            if self.upright_since is not None and t - self.upright_since >= r["cancel_hold"]:
                self.state, self.candidate_since = self.WATCH, None
            elif t - self.candidate_since >= r["confirm_hold"]:
                return self._alarm(t, "rapid", "快速倒地后 %.1f 秒没站起来" % (t - self.candidate_since))
        elif self.state == self.FALLEN:
            if self.upright_since is not None and t - self.upright_since >= r["recover_hold"]:
                self.state = self.WATCH
                self.lying_since = None
        self.last["state"] = self.state
        return None

    def _rapid_drop(self, f):
        """窗口内髋部最大下降量是否 ≥ min_hip_drop 个躯干长（躯干长优先用站立时的，倒地后会透视缩短）。"""
        if not self.history:
            return False
        ref = self.standing_len or max(h["torso_len"] for _, h in self.history)
        # 用窗口内偏高的 20% 分位而不是最高点，单个抖动帧不会把"下降量"放大
        hips = sorted(h["hip_y"] for _, h in self.history)
        drop = f["hip_y"] - hips[int(0.2 * (len(hips) - 1))]
        return drop >= self.rules["min_hip_drop"] * ref

    def _alarm(self, t, kind, reason):
        self.state = self.FALLEN
        self.candidate_since = self.lying_since = None
        self.last["state"] = self.state
        return {"type": "fall", "kind": kind, "t": t, "reason": reason}
