"""动作表（执行端真源，由硬件同学维护；这里是 mock 版占位）。

label 会显示在大屏时间线上。真硬件到场后，每个 pose 对应 RH56F2 六指位置 + 臂关节角。
"""
POSES = {
    "home": {"label": "回到待机位", "hand": 1},
    "deliver_medicine": {"label": "臂+手#1 递出药盒", "hand": 1},
    "press_sos": {"label": "臂+手#1 按下 SOS 键", "hand": 1},
    "sign_family_received": {"label": "手#1 举牌“家人已收到”", "hand": 1},
    "gentle_hold": {"label": "手#2 轻握安抚（限力）", "hand": 2},
    "sign_coming": {"label": "手#2 举牌“别怕，女儿马上到”", "hand": 2},
    "release": {"label": "手#2 松开", "hand": 2},
    "rps_rock": {"label": "手#1 出：石头", "hand": 1},
    "rps_scissors": {"label": "手#1 出：剪刀", "hand": 1},
    "rps_paper": {"label": "手#1 出：布", "hand": 1},
}

# RH56F2 kit vocabulary.  These names are accepted at the HTTP boundary and
# are kept here as a single source of truth so the physical backend and the
# mock expose the same action inventory.
for _name, _label, _hand in (
    ("rock", "出：石头", 1), ("paper", "出：布", 1),
    ("scissors", "出：剪刀", 1), ("ok", "手势：OK", 1),
    ("thumbs_up", "手势：点赞", 1), ("fingers_1", "手指：一", 1),
    ("fingers_2", "手指：二", 1), ("fingers_3", "手指：三", 1),
    ("fingers_4", "手指：四", 1), ("fingers_5", "手指：五", 1),
    ("hold_card", "捏住提示牌", 1), ("grip_pillow", "按键握持", 1),
    ("nod_like", "点头点赞", 1), ("clench_open", "握拳再张开", 1),
    ("count_five", "数一到五", 1), ("comfort_rhythm", "手#2 轻握节拍", 2),
    ("wave", "挥手", 1),
):
    POSES.setdefault(_name, {"label": _label, "hand": _hand})

# 手指操 13 拍：(口令, 手形)
FINGER_MOVES = [
    ("双手握拳", "fist"),
    ("五指张开", "open"),
    ("拇指碰食指", "pinch_1"),
    ("拇指碰中指", "pinch_2"),
    ("拇指碰无名指", "pinch_3"),
    ("拇指碰小指", "pinch_4"),
    ("伸出一根手指", "count_1"),
    ("伸出两根手指", "count_2"),
    ("伸出三根手指", "count_3"),
    ("伸出四根手指", "count_4"),
    ("伸出五根手指", "count_5"),
    ("再握紧拳头", "fist"),
    ("慢慢张开，甩一甩", "open"),
]

for _say, _shape in FINGER_MOVES:
    POSES.setdefault("finger_" + _shape, {"label": "手#1 手指操：" + _say, "hand": 1})


def label(pose):
    p = POSES.get(pose)
    return p["label"] if p else pose


# ---------- 动作 → 五指开合度（arm_service 交给 rh56f2_driver） ----------
# 开合度 0 = 全握，100 = 全开。数值取自 caremate_hand2_kit 的词表，
# 标 :示教: 的是这里新补的，上真机后对着实物再调
TH, IX, MI, RG, PK, TR = "thumb", "index", "middle", "ring", "pinky", "thumb_rot"


def _j(th, ix, mi, rg, pk, tr):
    return {TH: th, IX: ix, MI: mi, RG: rg, PK: pk, TR: tr}


ROCK = _j(10, 10, 10, 10, 10, 50)
PAPER = _j(90, 95, 95, 95, 95, 50)
HOLD_CARD = _j(40, 40, 10, 10, 10, 70)        # 捏住举牌的杆

JOINTS = {
    "home": _j(70, 75, 75, 75, 75, 50),            # :示教: 放松
    "release": PAPER,
    "deliver_medicine": _j(35, 35, 35, 35, 35, 60),  # :示教: 握住药盒
    "press_sos": _j(30, 30, 30, 30, 30, 55),       # 半握拳用指节按键（kit 的 grip_pillow），不拿单根手指硬顶
    "sign_family_received": HOLD_CARD,
    "sign_coming": HOLD_CARD,
    "rps_rock": ROCK,
    "rps_scissors": _j(15, 90, 90, 10, 10, 50),
    "rps_paper": PAPER,
    "finger_fist": ROCK,
    "finger_open": PAPER,
    "finger_count_5": PAPER,
}
for _n in range(1, 5):                                  # 伸出 n 根：食指起依次伸直，拇指收着
    JOINTS["finger_count_%d" % _n] = _j(10, *[90 if i < _n else 10 for i in range(4)], 50)
for _k, _f in enumerate((IX, MI, RG, PK), 1):           # :示教: 拇指转过去碰第 k 指
    JOINTS["finger_pinch_%d" % _k] = dict(PAPER, **{TH: 35, _f: 45, TR: 85})

# 轻握安抚：一握一松的节拍，循环到新动作 / 急停打断，最长 max_seconds。
# 握紧那一拍也只到 GENTLE_MIN_OPEN，不握到底（§5.2 握力上限）；force 写进 FORCE_SET，单位 g，手指受力到了就停
GENTLE_MIN_OPEN = 50
RHYTHMS = {
    "gentle_hold": {"steps": [(_j(55, 55, 55, 55, 55, 55), 1.2), (_j(80, 85, 85, 85, 85, 55), 1.2)],
                    "max_seconds": 60, "force": 300},
}
RHYTHMS["comfort_rhythm"] = RHYTHMS["gentle_hold"]      # kit 的名字，同一个节拍

# ---------- kit 词表（数值取自 caremate_hand2_kit 的 POSE_VOCAB / SEQUENCES） ----------
THUMBS_UP = _j(95, 10, 10, 10, 10, 60)
JOINTS.update({
    "rock": ROCK, "paper": PAPER, "scissors": JOINTS["rps_scissors"],
    "ok": _j(45, 45, 90, 90, 90, 50), "thumbs_up": THUMBS_UP,
    "hold_card": HOLD_CARD, "grip_pillow": JOINTS["press_sos"],
})
for _n in range(1, 6):
    JOINTS["fingers_%d" % _n] = JOINTS["finger_count_%d" % _n]

# 按顺序走一遍的动作：[(开合度, 停多少秒), …]
SEQUENCES = {
    "wave": [(PAPER, 0.4), (ROCK, 0.3), (PAPER, 0.4), (ROCK, 0.3), (PAPER, 0.3)],
    "nod_like": [(THUMBS_UP, 0.5), (PAPER, 0.2), (THUMBS_UP, 0.6)],
    "clench_open": [(ROCK, 0.7), (PAPER, 0.7)],
    "count_five": [(JOINTS["fingers_%d" % n], 0.7 if n < 5 else 0.8) for n in range(1, 6)],
}
