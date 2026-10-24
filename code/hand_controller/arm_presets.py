"""臂手协同预设动作：不改变左右位置（底座 J1、腕部左右摆 J5 锁在出发时的角度），只在臂当前朝向的竖直平面里
动肩 J2、肘 J3、腕俯仰 J4、手腕旋转 J6，配合灵巧手的手势。做完回到出发姿态，可以接在脚本任意一步后面。

步骤：
  {"arm": (J2, J3, J4, J6), "speed": °/s}   J6 = None 保持出发时的角度
  {"hand": 手部动作}                          arm_service.HAND_PRESETS（带力控），等之前的手势做完再做
  {"gesture": 手势, "async": True}            poses 里的手势；async = 手做手势的同时臂接着走（协同）
  {"wait": 秒}
  {"back": True}                              回到出发姿态
手掌朝向：J6 = 0 时手朝前、手掌竖直、拇指朝上（握手的姿势）；±90° 让手掌朝前 / 朝上，正负号现场看一次再定。
角度按 arm_kinematics 设计（手腕俯仰 ≈ J3 + J4 − J2）：在 J1 = −120°~120° 都不碰桌面、不进禁区、不超限位。
"""

PRESETS = {
    "wave": {
        "label": "挥手打招呼",
        "desc": "举手到头顶高度，手掌朝前，前后摆腕三次，手指同时挥动",
        "steps": [
            {"hand": "release"},
            {"arm": (55, 110, 35, 90), "speed": 20},
            {"gesture": "wave", "async": True},
            {"arm": (55, 110, 20, 90), "speed": 30}, {"arm": (55, 110, 50, 90), "speed": 30},
            {"arm": (55, 110, 20, 90), "speed": 30}, {"arm": (55, 110, 50, 90), "speed": 30},
            {"arm": (55, 110, 35, 90), "speed": 30},
            {"wait": 0.5},
            {"back": True},
        ],
    },
    "handshake": {
        "label": "握手",
        "desc": "向前伸手（手掌竖直、拇指朝上），等 3 秒让人握住，轻握后上下晃三下再松开",
        "steps": [
            {"hand": "release"},
            {"arm": (70, 105, -35, 0), "speed": 20},
            {"wait": 3},
            {"hand": "grasp"},
            {"wait": 0.5},
            {"arm": (70, 105, -27, 0), "speed": 25}, {"arm": (70, 105, -43, 0), "speed": 25},
            {"arm": (70, 105, -27, 0), "speed": 25}, {"arm": (70, 105, -43, 0), "speed": 25},
            {"arm": (70, 105, -35, 0), "speed": 25},
            {"wait": 0.5},
            {"hand": "release"},
            {"wait": 1},
            {"back": True},
        ],
    },
    "thumbs_up": {
        "label": "点赞",
        "desc": "抬手到胸前斜上方，竖大拇指，手腕点两下",
        "steps": [
            {"arm": (45, 100, -10, None), "speed": 20},
            {"gesture": "thumbs_up"},
            {"arm": (45, 100, -25, None), "speed": 25}, {"arm": (45, 100, -10, None), "speed": 25},
            {"arm": (45, 100, -25, None), "speed": 25}, {"arm": (45, 100, -10, None), "speed": 25},
            {"wait": 1},
            {"hand": "relax"},
            {"back": True},
        ],
    },
    "offer": {
        "label": "掌心向上请接",
        "desc": "向前伸出、手掌朝上张开，停 4 秒（请老人把东西放上来 / 示意接过）",
        "steps": [
            {"hand": "release"},
            {"arm": (65, 90, -25, 90), "speed": 20},
            {"wait": 4},
            {"back": True},
        ],
    },
    "nod": {
        "label": "点头致意",
        "desc": "手到胸前，手腕向下点两次，像点头",
        "steps": [
            {"hand": "relax"},
            {"arm": (30, 60, -30, None), "speed": 20},
            {"arm": (30, 60, -55, None), "speed": 20}, {"arm": (30, 60, -30, None), "speed": 20},
            {"arm": (30, 60, -55, None), "speed": 20}, {"arm": (30, 60, -30, None), "speed": 20},
            {"back": True},
        ],
    },
    "cheer": {
        "label": "握拳加油",
        "desc": "握拳举起，上下挥三下",
        "steps": [
            {"gesture": "rps_rock"},
            {"arm": (55, 110, 35, None), "speed": 20},
            {"arm": (55, 90, 55, None), "speed": 30}, {"arm": (55, 110, 35, None), "speed": 30},
            {"arm": (55, 90, 55, None), "speed": 30}, {"arm": (55, 110, 35, None), "speed": 30},
            {"arm": (55, 90, 55, None), "speed": 30}, {"arm": (55, 110, 35, None), "speed": 30},
            {"hand": "release"},
            {"back": True},
        ],
    },
    "beckon": {
        "label": "招手请过来",
        "desc": "向前伸出、掌心向上，手指一握一张两次，手腕轻抬，示意「请过来 / 跟我来」",
        "steps": [
            {"hand": "release"},
            {"arm": (65, 90, -25, 90), "speed": 20},
            {"gesture": "clench_open", "async": True},
            {"arm": (65, 90, -10, 90), "speed": 20}, {"arm": (65, 90, -25, 90), "speed": 20},
            {"gesture": "clench_open", "async": True},
            {"arm": (65, 90, -10, 90), "speed": 20}, {"arm": (65, 90, -25, 90), "speed": 20},
            {"hand": "release"},
            {"back": True},
        ],
    },
    "point": {
        "label": "指向前方",
        "desc": "向正前方平伸，伸出食指指着停 2 秒（「请看这边」）",
        "steps": [
            {"arm": (60, 95, -35, 0), "speed": 20},
            {"gesture": "fingers_1"},
            {"wait": 2},
            {"hand": "relax"},
            {"back": True},
        ],
    },
    "raise_hand": {
        "label": "举手示意",
        "desc": "手张开举过头顶停 2 秒（「我在这里 / 请注意」）",
        "steps": [
            {"hand": "release"},
            {"arm": (40, 120, 10, 90), "speed": 20},
            {"wait": 2},
            {"back": True},
        ],
    },
    "ok": {
        "label": "OK 手势",
        "desc": "抬手到胸前斜上方比 OK，手腕点一下",
        "steps": [
            {"arm": (45, 100, -10, 90), "speed": 20},
            {"gesture": "ok"},
            {"arm": (45, 100, -25, 90), "speed": 20}, {"arm": (45, 100, -10, 90), "speed": 20},
            {"wait": 1},
            {"hand": "relax"},
            {"back": True},
        ],
    },
    "pat": {
        "label": "轻拍安抚",
        "desc": "手掌朝下放松，手腕向下轻拍三下（先把臂移到老人手的上方再用）",
        "steps": [
            {"hand": "relax"},
            {"arm": (60, 80, -20, -90), "speed": 20},
            {"arm": (60, 80, -35, -90), "speed": 15}, {"arm": (60, 80, -20, -90), "speed": 15},
            {"arm": (60, 80, -35, -90), "speed": 15}, {"arm": (60, 80, -20, -90), "speed": 15},
            {"arm": (60, 80, -35, -90), "speed": 15}, {"arm": (60, 80, -20, -90), "speed": 15},
            {"back": True},
        ],
    },
    "count": {
        "label": "举手数一到五",
        "desc": "举手到胸前斜上方，依次伸出一到五根手指",
        "steps": [
            {"arm": (45, 100, -10, 90), "speed": 20},
            {"gesture": "count_five"},
            {"wait": 1},
            {"hand": "relax"},
            {"back": True},
        ],
    },
}
