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
