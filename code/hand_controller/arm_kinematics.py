"""Panthera-HT 正运动学（纯 Python，零依赖）：笔记本 mock 和 X5 真机用同一套算禁区。

关节链抄自 SDK 的 Panthera-HT_description_V1.1.0_follower.urdf（6 个转动关节，rpy 全是 0）。
单位：关节角 rad，位置 m，坐标系 = 臂底座 base_link（z 朝上，底座底面 z=0）。
和 SDK 自己的 pinocchio 正解对一遍：X5 上 python -m hand_controller.arm_backend --check-fk
"""
import math

# (关节名, 相对父连杆的平移 xyz, 转轴)；joint_i 转完就是 link_i 的原点
CHAIN = (
    ("joint1", (0.0, 0.0, 0.0584), (0, 0, 1)),
    ("joint2", (0.0181985117133034, 0.0, 0.0535), (0, 1, 0)),
    ("joint3", (-0.26, 0.0, 0.0), (0, -1, 0)),
    ("joint4", (0.23, 0.0, 0.06), (0, -1, 0)),
    ("joint5", (0.07, -0.000450862561756094, 0.034989137438241), (0, 0, -1)),
    ("joint6", (0.02495, 0.0, -0.0375), (1, 0, 0)),
)
GRIPPER_TCP = 0.16605080437572   # link6 → gripper_center_link，沿 link6 的 x 轴

# 禁区检查看这几个点：肘、腕、法兰、末端（夹爪中心 / 手心）
BODY_POINTS = ("elbow", "wrist", "flange", "tool")


def _rot(axis, q):
    """绕单位轴（只会是 ±x/±y/±z）转 q 的 3×3 旋转矩阵。"""
    x, y, z = axis
    c, s = math.cos(q), math.sin(q)
    if x:
        s *= x
        return ((1, 0, 0), (0, c, -s), (0, s, c))
    if y:
        s *= y
        return ((c, 0, s), (0, 1, 0), (-s, 0, c))
    s *= z
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _apply(r, v):
    return tuple(sum(r[i][k] * v[k] for k in range(3)) for i in range(3))


def _add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def frames(q):
    """每个关节转完后的 (位置, 旋转)，共 6 个（link1…link6）。"""
    if len(q) != len(CHAIN):
        raise ValueError("关节角要 %d 个，给了 %d 个" % (len(CHAIN), len(q)))
    pos, rot = (0.0, 0.0, 0.0), ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    out = []
    for (_, xyz, axis), qi in zip(CHAIN, q):
        pos = _add(pos, _apply(rot, xyz))
        rot = _mul(rot, _rot(axis, qi))
        out.append((pos, rot))
    return out


def body_points(q, tool_length=GRIPPER_TCP):
    """禁区检查用的几个点 {名字: (x, y, z)}。tool_length = 法兰到末端的距离（夹爪 / 手#1 各不相同）。"""
    f = frames(q)
    flange, rot6 = f[5]
    return {
        "elbow": f[2][0],
        "wrist": f[4][0],
        "flange": flange,
        "tool": _add(flange, _apply(rot6, (tool_length, 0.0, 0.0))),
    }


def tool_position(q, tool_length=GRIPPER_TCP):
    return body_points(q, tool_length)["tool"]
