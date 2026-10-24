"""臂的物理安全（v2.5 §5.2）：关节限位、速度限幅、工作空间禁区、按压限力。

全是纯函数：arm_service 收到动作时先整条预检一遍（不过就回 4xx，主脑操作台出红字），
执行时每一段再按臂的实际位置查一遍。动作点位和禁区在 arm_skills.json。
"""
import json
import math
import os

from hand_controller.arm_kinematics import BODY_POINTS, body_points

SKILLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_skills.json")

# 关节限位（rad）：和 X5 上 Follower.yaml 的 joint_limits 一字不差（说明书范围收紧约 15%，J1 收紧 20%）。
# 度数：J1 ±144，J2 0~170，J3 0~195，J4/J5 ±76，J6 ±153。SDK 自己也会拒绝越界的 moveJ，
# 这里先拦是为了把原因原样回给主脑。**不许放宽**：J1 转过头拉坏过底座线缆。
JOINT_LOWER = (-2.5133, 0.0, 0.0, -1.3265, -1.3265, -2.6704)
JOINT_UPPER = (2.5133, 2.9671, 3.4034, 1.3265, 1.3265, 2.6704)

PATH_STEP = 0.05   # 沿关节空间插值检查禁区的步长（rad）：moveJ 每个关节匀速，路径就是关节空间直线


class SafetyError(Exception):
    """预检 / 执行前检查没过；消息原样回给主脑。"""


def _strip(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


def load_config(effector, path=None):
    """读 arm_skills.json（_ 开头的键是说明）。effector = gripper | hand1，决定末端长度。"""
    with open(path or SKILLS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = _strip(raw)
    for key in ("speed", "points", "skills"):
        cfg[key] = _strip(raw[key])
    cfg["tool_length"] = raw["tool_length"][effector]
    cfg["effector"] = effector
    return cfg


def skill_names(path=None):
    """有臂步骤的动作名（不用管末端是什么）。"""
    with open(path or SKILLS_FILE, encoding="utf-8") as f:
        return set(_strip(json.load(f)["skills"]))


def deg(q):
    return [round(math.degrees(x), 1) for x in q]


def check_limits(q, name=""):
    for i, (x, lo, hi) in enumerate(zip(q, JOINT_LOWER, JOINT_UPPER)):
        if not lo <= x <= hi:
            raise SafetyError("%sJ%d = %.0f° 超出限位 %.0f°~%.0f°" % (
                name and "点位 %s 的 " % name, i + 1, math.degrees(x), math.degrees(lo), math.degrees(hi)))


def _inside(p, zone):
    return all(lo <= c <= hi for c, lo, hi in zip(p, zone["min"], zone["max"]))


def check_pose(q, cfg, name=""):
    """一个姿态：肘、腕、法兰、末端都不能进禁区、不能低于桌面（floor_z 为 null = 不查桌面）。"""
    pts = body_points(q, cfg["tool_length"])
    floor = cfg.get("floor_z")
    for part in BODY_POINTS:
        p = pts[part]
        if floor is not None and p[2] < floor:
            raise SafetyError("%s%s 低于桌面（z = %.2f m < %.2f m）" % (
                name and "到 %s 时" % name, part, p[2], cfg["floor_z"]))
        for zone in cfg["keep_out"]:
            if _inside(p, zone):
                raise SafetyError("%s%s 进了禁区「%s」（%.2f, %.2f, %.2f）" % (
                    name and "到 %s 时" % name, part, zone["name"], p[0], p[1], p[2]))


def check_segment(q_from, q_to, cfg, name=""):
    """一段 moveJ：终点限位 + 沿路每隔 PATH_STEP 查一次禁区（起点不查：臂已经在那儿了）。"""
    check_limits(q_to, name)
    span = max(abs(b - a) for a, b in zip(q_from, q_to))
    n = max(1, int(math.ceil(span / PATH_STEP)))
    for k in range(1, n + 1):
        t = k / n
        check_pose([a + (b - a) * t for a, b in zip(q_from, q_to)], cfg, name)


ESCAPE_SLACK = 0.002   # 脱困段：身体点允许比起点再低这么多（m），只算读数噪声


def check_escape(q_from, q_to, cfg, name=""):
    """脱困段：起点已经低于桌面 / 在禁区里（末端长度估大了、或示教时就贴着桌面），check_segment 一步都过不去。
    只放行"越走越好"的一段：沿路每个身体点要么合规，要么不比它在起点时更低、也不进起点时不在的禁区；
    终点必须完全合规。"""
    check_limits(q_to, name)
    check_pose(q_to, cfg, name)
    start = body_points(q_from, cfg["tool_length"])
    span = max(abs(b - a) for a, b in zip(q_from, q_to))
    n = max(1, int(math.ceil(span / PATH_STEP)))
    for k in range(1, n + 1):
        pts = body_points([a + (b - a) * k / n for a, b in zip(q_from, q_to)], cfg["tool_length"])
        for part in BODY_POINTS:
            p, p0 = pts[part], start[part]
            if cfg.get("floor_z") is not None and p[2] < cfg["floor_z"] and p[2] < p0[2] - ESCAPE_SLACK:
                raise SafetyError("%s%s 低于桌面且还在往下（z = %.2f m）" % (name and "去 %s 时" % name, part, p[2]))
            for zone in cfg["keep_out"]:
                if _inside(p, zone) and not _inside(p0, zone):
                    raise SafetyError("%s%s 会进禁区「%s」" % (name and "去 %s 时" % name, part, zone["name"]))


# 分组归位的顺序：底座、肩、肘、腕（J4~J6 一起）
RETURN_GROUPS = (("底座", (0,)), ("肩", (1,)), ("肘", (2,)), ("腕", (3, 4, 5)))


def plan_route(q, goal, cfg, lifts=()):
    """从 q 安全走到 goal 的分段路线（不含起点）：[(说明, 关节角), …]；找不到就 SafetyError。

    依次试：① 直走；② 先走 lifts 里的某个抬起姿态再直走；③ 各关节分组按不同顺序逐组归位（也可先抬起）。
    每段都按 check_segment 查；只有第一段在起点本身不合规时才按 check_escape（只许越走越高）放行。"""
    from itertools import permutations

    def seg_ok(a, b, first):
        try:
            check_segment(a, b, cfg)
            return True
        except SafetyError:
            if not first:
                return False
        try:
            check_escape(a, b, cfg)
            return True
        except SafetyError:
            return False

    starts = [([], list(q))]
    for text, lifted in lifts:
        if seg_ok(q, lifted, True):
            starts.append(([(text, list(lifted))], list(lifted)))
    try:
        check_pose(q, cfg)
    except SafetyError:   # 起点就贴着 / 低于桌面：先抬起离开桌面，别直接贴着桌面横扫
        starts = starts[1:] + starts[:1]
    for prefix, s in starts:
        if seg_ok(s, goal, not prefix):
            return prefix + [("回 rest", list(goal))]
    for prefix, s in starts:
        for order in permutations(RETURN_GROUPS):
            route, cur, ok = list(prefix), list(s), True
            for text, joints in order:
                nxt = list(cur)
                for j in joints:
                    nxt[j] = goal[j]
                if nxt == cur:
                    continue
                if not seg_ok(cur, nxt, not route):
                    ok = False
                    break
                route.append(("%s归位" % text, nxt))
                cur = nxt
            if ok and route:
                return route
    raise SafetyError("找不到安全的回 rest 路线（直走、先抬起、分组归位都会碰桌面或禁区）")


def duration(q_from, q_to, cfg, slow=False):
    """moveJ 的时长：最快的关节也不超过限速；靠近老人（slow）用更低的限速。"""
    sp = cfg["speed"]
    vmax = sp["near_person_vel"] if slow else sp["max_joint_vel"]
    span = max(abs(b - a) for a, b in zip(q_from, q_to))
    return max(sp["min_seconds"], span / vmax)


def skill_moves(skill, cfg):
    """技能里所有 move 步要去的点位名，按顺序。"""
    return [step[1] for step in cfg["skills"][skill] if step[0] == "move"]


def check_skill(skill, q_start, cfg, require_taught=False):
    """整条技能从 q_start 出发预检一遍。返回终点关节角（排在它后面的技能从这里接着查）。"""
    if skill not in cfg["skills"]:
        raise SafetyError("动作 %s 没有臂的步骤" % skill)
    q = list(q_start)
    previous = None
    for point in skill_moves(skill, cfg):
        p = cfg["points"].get(point)
        if p is None:
            raise SafetyError("点位 %s 不在 arm_skills.json 里" % point)
        if require_taught and not p.get("taught"):
            raise SafetyError("点位 %s 还没示教（X5 上先停 arm_service，再 python -m hand_controller.arm_teach）" % point)
        if point == 'pick_med' and cfg.get('fixed_grasp_approach'):
            if previous != 'pre_grasp':
                raise SafetyError('夹取前必须先到固定朝向的 pre_grasp 接近点')
            pre = cfg['points']['pre_grasp']['q']
            if any(abs(pre[i]-p['q'][i]) > math.radians(1) for i in (0,4,5)):
                raise SafetyError('下降夹取时底座和夹爪旋转轴必须保持不变')
            if abs((pre[1]-pre[2]-pre[3])-(p['q'][1]-p['q'][2]-p['q'][3])) > math.radians(2):
                raise SafetyError('下降夹取时夹爪朝向会变化，请重新生成接近点')
            if body_points(pre,cfg['tool_length'])['tool'][2] - body_points(p['q'],cfg['tool_length'])['tool'][2] < .039:
                raise SafetyError('接近点需要至少比夹取点高 4 cm')
        check_segment(q, p["q"], cfg, point)
        q = list(p["q"])
        previous = point
    return q
