"""猜拳的视觉一侧：手部关键点 → 石头/剪刀/布，多帧投票。纯标准库。

输赢不在这里判：主脑先定好机器人出什么，收到回传的手势后由 master_service.rps_judge 判（确定性内核）。

输入是 MediaPipe Hands 的 21 个点（[x, y] 或 [x, y, z]，归一化坐标）：
0 手腕；食指 5/6/8（根、中节、指尖）；中指 9/10/12；无名指 13/14/16；小指 17/18/20。
判断"手指伸直"：指尖到手腕的距离明显大于中节到手腕的距离 —— 与手的朝向、远近无关。
"""
import collections
import math

MOVES = ("rock", "scissors", "paper")
NAMES = {"rock": "石头", "scissors": "剪刀", "paper": "布"}

WRIST = 0
FINGERS = {"index": (6, 8), "middle": (10, 12), "ring": (14, 16), "pinky": (18, 20)}  # (中节, 指尖)
EXTEND_RATIO = 1.15


def _dist(a, b, aspect):
    return math.hypot((a[0] - b[0]) * aspect, a[1] - b[1])


def extended_fingers(lm, aspect=1.0):
    """返回 {"index": bool, "middle": ..., "ring": ..., "pinky": ...}。aspect = 画面宽/高。"""
    w = lm[WRIST]
    return {name: _dist(lm[tip], w, aspect) > EXTEND_RATIO * _dist(lm[pip], w, aspect)
            for name, (pip, tip) in FINGERS.items()}


def classify(lm, aspect=1.0):
    """'rock' / 'scissors' / 'paper'；看不出来返回 None（交给多帧投票）。"""
    if not lm or len(lm) < 21:
        return None
    e = extended_fingers(lm, aspect)
    n = sum(e.values())
    if n == 0:
        return "rock"
    if n == 4:
        return "paper"
    if e["index"] and e["middle"] and not e["ring"] and not e["pinky"]:
        return "scissors"
    return None


def vote(labels, min_count=3, min_share=0.6):
    """多帧投票：认出来的帧里占比 ≥ min_share 且至少 min_count 帧，才算数。"""
    seen = [x for x in labels if x]
    if len(seen) < min_count:
        return None
    best, count = collections.Counter(seen).most_common(1)[0]
    return best if count / len(seen) >= min_share else None
