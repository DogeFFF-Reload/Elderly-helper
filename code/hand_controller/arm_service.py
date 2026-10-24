"""臂 + 双手执行服务（:8013）。和主脑的约定不变（README §10.8、§10.11）：

POST /pose?name=press_sos&hand=1   收到就回 200，动作在臂 / 这只手自己的线程里做；做不了回 4xx/503，error 写原因
POST /stop                         软件急停：臂停在原地保持、双手停住，清掉排队、打断节拍；之后的 /pose 照常接收
GET  /status                       在线 + 臂和每只手的状态 + 最近动作

臂（Panthera-HT，arm_backend）：arm_skills.json 里有臂步骤的动作（home、deliver_medicine、press_sos、
  sign_family_received）走臂的线程：先整条预检（关节限位、禁区、桌面、点位示教过没有），不过回 422/503；
  一次做一个，保命动作打断正在做的普通动作、排着的普通动作作废；同一个动作正在做或排着回 409。
  末端是 Panthera 夹爪还是手#1 由 ARM_EFFECTOR 定（不设：装了夹爪就用夹爪，否则手#1）。
手：rh56f2_driver（手册 §2.2 的 EB 90 协议），动作 → 五指开合度查 poses.JOINTS（一次到位）/ SEQUENCES（走一遍）/ RHYTHMS（循环节拍）。

caremate_hand2_kit 的写法也认（只在 HTTP 边界换成仓库的规范名，HISTORY 里始终是规范名，requested 记 kit 原名）：
  POST /pose/<名字>?hand=  只做手的动作，映射后会动臂的回 400；未知名字按 kit 的约定回 200 {"ok": false}
  POST /press_sos  /handover_med  /return_med  /reset  /thumbs  /card_up  /wave
  kit 名字 comfort_rhythm、rock、fingers_3、hold_card…… 换成仓库的动作名，见 KIT_NAMES / KIT_ROUTES。

T5AI 大屏（firmware/bedside_t5ai）：四个键走上面的 /handover_med /return_med /press_sos /reset，
  语音键 POST /voice_cmd（16 kHz 裸 PCM）→ voice_cmd 离线识别 → 同一条路；在线灯看 GET /status 回不回 200。

环境变量（手的写法和 caremate_hand2_kit docs/20 一致）：
  ARM_BACKEND   mock（默认，臂只打印）| panthera；真臂见 arm_backend 的说明
  ARM_EFFECTOR  gripper | hand1（不设自动选）
  ARM_SKILLS_FILE  点位 / 动作文件，不设用 arm_skills.json；上电自检用 hand_controller/arm_selftest.json（小角度）
  VOSK_MODEL    语音键的 vosk 中文模型目录，默认 /root/caremate/vosk-cn
  MOCK_SERIAL   不设 = mock，只打印不碰串口；=0 走真串口（rh56_register_map.VERIFIED 之前只读，不发动作）
  HAND1_PORT / HAND1_ID   手#1 串口（COM7、/dev/ttyUSB0…）和手的 ID，默认 1
  HAND2_PORT / HAND2_ID   手#2，默认 ID 2（手出厂都是 1：各接各的串口时设 HAND2_ID=1）

运行：python -m hand_controller.arm_service [--host 0.0.0.0] [--port 8013]
     python -m hand_controller.arm_service --probe     只读：读两只手的实际角度、臂的关节角就退出（上电后先跑这个）
"""
import argparse
import json
import os
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from config import utf8_console
from hand_controller import arm_safety as S
from hand_controller import rh56_register_map as RM
from hand_controller import voice_cmd
from hand_controller.grasp import wait_put
from hand_controller.arm_backend import TOLERANCE, ArmError, make_backend
from hand_controller.arm_kinematics import tool_position
from hand_controller.arm_presets import PRESETS
from hand_controller.poses import JOINTS, POSES, RHYTHMS, SAFETY, SEQUENCES, label
from hand_controller.rh56f2_driver import RH56F2, HandError

# Standalone exhibition hardware: one Panthera with its own gripper and one RH56.
HAND_COUNT = int(os.environ.get("HAND_COUNT", "2"))
HAND_LAYOUT = os.environ.get("HAND_LAYOUT", "mounted")
TEACH_MODE = False
TEACH_POINTS = ("above_tray", "pick_med", "handover", "above_sos", "sos_press", "show_sign", "pre_grasp", "orient_clear")
TEACH_MAX_DEG = 30        # 单次点动上限（°）
TEACH_MAX_CM = 10         # 单次垂直移动上限（cm）
TEACH_MIN_SECONDS = 0.6   # 自定义转速时一段最短这么久：再小的点动也不猛冲
TEACH_BASE_LIMIT = 120    # 示教 / 回放底座限于 ±120°
TEACH_MAX_VEL = 0.5236    # 臂没报 SDK 速度限幅时，示教、跟随、脚本回放的转速上限（rad/s，30°/s）；业务动作仍按 speed.max_joint_vel
SERVO_PERIOD = 0.05       # 跟随模式：每 50 ms 看一次有没有新目标
SERVO_IDLE = 0.8          # 跟随模式：这么久没有新目标、且已走到 → 结束这次跟随
SERVO_MIN_SECONDS = 0.15  # 跟随模式一帧最短时长（小幅拖动不猛冲）
# 2026-10-25 现场：跟随拖动时带着手的臂冲过了头，J2 实际到 177°，碰到电机层 175° 锁定，全部阻尼、臂掉下来。三层防护：
TEACH_LIMIT_MARGIN = 5    # 示教 / 跟随 / 3D 拖动的目标离软件限位至少留这么多度（J2/J3 收拢那一侧的 0° 不留）
NEAR_LIMIT_DEG = 15       # 目标离软件限位不到这么多度：这一段限速 NEAR_LIMIT_VEL
NEAR_LIMIT_VEL = 0.2618   # 15°/s
WATCHDOG_DEG = 1          # 跟随时实际角度往外走到离软件限位不到这么多度 → 立刻停住保持（远在电机层锁定之前）
# 10-25 又锁了两次（J2 停在 169~170° 后被重力拖到 176°），在不缩小范围的前提下再加两层：
FULL_TORQUE_DEG = 25      # 关节目标或当前角离软件限位不到这么多度：用满力矩（平时示教 / 跟随只用 60%），顶得住过了平衡点的重力
PREDICT_S = 0.4           # 按速度预判：当前角 + 速度 × 这么多秒 会到软件限位 → 提前停（冲得越快停得越早，慢慢拖不受影响）
TEACH_TORQUE = 0.6


def teach_limits():
    """示教用的关节范围（rad）：软件限位往里收 TEACH_LIMIT_MARGIN，J1 再限 ±TEACH_BASE_LIMIT。"""
    import math
    m = math.radians(TEACH_LIMIT_MARGIN)
    lo = [lim + m if lim != 0 else lim for lim in S.JOINT_LOWER]
    hi = [lim - m for lim in S.JOINT_UPPER]
    b = math.radians(TEACH_BASE_LIMIT)
    lo[0], hi[0] = max(lo[0], -b), min(hi[0], b)
    return lo, hi


def check_teach_target(cur, q, joints):
    """示教目标里 joints 这几个关节（下标）要在 teach_limits 内；已经在余量外的关节只许往里走。"""
    import math
    lo, hi = teach_limits()
    for i in joints:
        x, c = q[i], cur[i]
        if lo[i] <= x <= hi[i]:
            continue
        inward = (x > hi[i] and x < c) or (x < lo[i] and x > c)
        if not inward:
            raise S.SafetyError("J%d 目标 %.1f° 太靠近限位：示教留 %d° 余量，范围 %.0f°~%.0f°" % (
                i + 1, math.degrees(x), TEACH_LIMIT_MARGIN, math.degrees(lo[i]), math.degrees(hi[i])))


# 保持手朝向（朝向锁）：手朝前、拇指竖直向上、手掌所在平面垂直桌面 = J5 = J6 = 0、手腕俯仰水平。
# 手腕俯仰 = J3 + J4 − J2（arm_kinematics 算出来的），所以锁住时 J4 = J2 − J3：肩、肘怎么动，J4 都跟着补。
# J4 是 J2、J3 的线性组合：两个锁住的姿态之间按关节直线走，路上每一点也都是锁住的，不用插中间点。
def orient_locked(q):
    q = list(q)
    q[3] = q[1] - q[2]
    q[4] = 0.0
    q[5] = 0.0
    return q


def teach_torque(q_from, q_to):
    """示教 / 跟随这一段的力矩比例：靠近软件限位（目标或当前离限位 < FULL_TORQUE_DEG）用满力矩，否则 60%。"""
    import math
    zone = math.radians(FULL_TORQUE_DEG)
    for a, b, lo, hi in zip(q_from, q_to, S.JOINT_LOWER, S.JOINT_UPPER):
        for x in (a, b):
            if hi - x < zone or (lo != 0 and x - lo < zone):
                return 1.0
    return TEACH_TORQUE


def near_limit_vel(q_from, q_to, vel):
    """这一段里有关节的目标离软件限位不到 NEAR_LIMIT_DEG：限速到 NEAR_LIMIT_VEL（冲过头的惯性小）。"""
    import math
    for a, b, lo, hi in zip(q_from, q_to, S.JOINT_LOWER, S.JOINT_UPPER):
        if abs(b - a) < 1e-4:
            continue
        room = min(b - lo if lo != 0 else math.inf, hi - b)
        if room < math.radians(NEAR_LIMIT_DEG):
            return min(vel, NEAR_LIMIT_VEL)
    return vel
# 录制回放：示教里走成功的这些动作按顺序记下（关节目标 + 转速 + 夹爪），回放时连成一条动作
RECORDABLE = ("teach_jog", "teach_vertical", "teach_recall", "teach_start", "teach_grip", "teach_hand", "teach_pose",
              "teach_servo", "teach_replay")
REPLAY_BLEND = 0.85       # 回放中间点：走完规划时长的这么多就接下一段，不在每个点停顿
REPLAY_GRIP_WAIT = 2.5    # 回放里夹爪开合后等这么久再走
REPLAY_SETTLE = 0.5       # 回放里后面接手部动作 / 等待的那一步：走完规划时长再多等这么久，停稳了再抓放
HAND_WAIT = 1.5           # 手部动作后等手指到位
SCRIPT_MAX_STEPS = 200


def _hand_shape(fingers, thumb_rot):
    shape = dict.fromkeys(RM.JOINT_ORDER, fingers)
    shape["thumb_rot"] = thumb_rot
    return shape


# 末端是手#1 时示教 / 脚本里的手部动作：键 → (按钮上的字, 开合度 0 握紧~100 张开, 力控阈值 g：手指受力到了就停)
HAND_PRESETS = {
    "release": ("张开", JOINTS["release"], 300),
    "relax": ("放松", JOINTS["home"], 300),
    "grasp": ("握取", _hand_shape(15, 60), 300),
    "grasp_firm": ("握紧", _hand_shape(8, 60), 500),
    "pinch": ("捏取", JOINTS["finger_pinch_1"], 300),
    # 一串 (开合度, 停多少秒)：四指先弯、拇指最后包上来压住四指，像人握拳
    "fist_wrap": ("握紧2", [(dict(_hand_shape(90, 50), thumb=95), 0.35),
                            (dict(_hand_shape(55, 50), thumb=95), 0.35),
                            (dict(_hand_shape(20, 50), thumb=90), 0.35),
                            (dict(_hand_shape(3, 50), thumb=90), 0.3),
                            (dict(_hand_shape(3, 75), thumb=20), 0.3)], 500),
    # 照现场视频（从上往下抓药盒 / 眼镜盒）：手掌朝下张开 → 拇指先转到对掌位、几乎伸直地落到物体近侧 →
    # 四指从上面包过去、往远侧弯下来，物体夹在拇指和四指之间。力控 350 g：碰到物体就停，不会捏成拳
    "wrap_grasp": ("握紧3", [(dict(_hand_shape(95, 50), thumb=95), 0.2),
                             (dict(_hand_shape(92, 85), thumb=70), 0.4),
                             (dict(_hand_shape(65, 85), thumb=60), 0.35),
                             (dict(_hand_shape(40, 85), thumb=50), 0.35),
                             (dict(_hand_shape(15, 85), thumb=40), 0.4)], 350),
}
# 能绑定录制的业务动作：绑定后 T5AI 大屏按键、语音、主脑、页面业务按钮触发它时都改为回放录制。
# 按 SOS（保命）和归位不开放绑定
BINDABLE = {"deliver_medicine": "递药盒", "return_medicine": "还药"}   # 动作 → T5AI 按键上的字


def verify_gripper(state, threshold=.12):
    import math
    value = state.get('gripper_position')
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ArmError('夹爪位置反馈无效，停止取物')
    if not threshold < value < 1.5:
        raise ArmError('夹爪未检测到有效夹持（位置 %.3f rad），停在夹取位置；请重新对准药盒' % value)
    return {'holding': True, 'method': 'position_stall_heuristic', 'position': value}

HISTORY = []   # 收下的指令（按到达顺序），/status 和回归测试看它
HISTORY_LIMIT = 200
SKILLS_FILE = os.environ.get("ARM_SKILLS_FILE") or None   # 不设 = arm_skills.json；自检用 arm_selftest.json
ARM_SKILLS = S.skill_names(SKILLS_FILE)   # hand=1 时这些动作归臂


def _record_history(entry):
    """收下的指令记一笔，最多留 HISTORY_LIMIT 条。"""
    HISTORY.append(entry)
    del HISTORY[:-HISTORY_LIMIT]


class HandWorker:
    """一只手一个线程：动作按到达顺序做；节拍类动作循环到被新动作 / 急停打断，或到时长上限。"""

    def __init__(self, hand, driver):
        self.hand, self.driver = hand, driver
        self.q = queue.Queue()
        self.wake = threading.Event()   # 来了新动作或急停：正在做的节拍立刻收手
        self.epoch = 0                  # 每次急停 +1：之前排进来还没做的动作全部作废
        self.running = None             # (epoch, pose) 正在做的
        self.current = None             # 最近一次开始做的动作名
        self.done = 0
        self.last_error = None
        self.fail_seq = 0               # 后台做动作失败的次数：主脑探测 /status 时按它补报"没做成"
        self.fail_pose = None
        self.custom = None              # 单指控制台给的开合度 + 力控（pose 名 "_custom"）
        self._mu = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def ready(self):
        """能不能接动作：mock 总能；真机要串口打得开、寄存器表核对过。"""
        if not self.driver.connect():
            return False, self.driver.error
        if not self.driver.mock and not RM.VERIFIED:
            return False, "寄存器表还没按手册核对（rh56_register_map.VERIFIED = False），真机只读"
        return True, ""

    def submit(self, pose):
        with self._mu:
            self.q.put((self.epoch, pose))
            self.wake.set()

    def stop(self):
        with self._mu:
            self.epoch += 1
            self.wake.set()
        if not self.driver.mock and RM.VERIFIED and self.driver.connect():
            try:   # 真手：角度设置值写 -1，手指停在当前位置（手册：-1 = 该指不动作）
                self.driver.hold()
            except HandError as e:
                print("[手#%d] 急停时没写进 hold：%s（拔 24V 是最后一道）" % (self.hand, e), flush=True)

    def interrupted(self):
        r = self.running
        return self.wake.is_set() or (r is not None and r[0] != self.epoch)

    def pause(self, seconds):
        """节拍里的停顿；被打断就提前返回 True。"""
        return self.wake.wait(seconds) or self.interrupted()

    def drain(self):
        self.q.join()

    def _run(self):
        while True:
            epoch, pose = self.q.get()
            try:
                with self._mu:
                    if epoch != self.epoch:
                        continue            # 急停之前排的，作废
                    if self.q.empty():
                        self.wake.clear()
                    self.running = (epoch, pose)
                self.current = pose
                _execute(pose, self.hand)
                self.done += 1
            except Exception as e:  # noqa: BLE001 一个动作出错不能弄死这只手的线程
                self.last_error = "%s：%s" % (pose, e)
                self.fail_pose = pose
                self.fail_seq += 1
                print("[手#%d] %s 没做成：%s" % (self.hand, label(pose), e), flush=True)
            finally:
                self.running = None
                self.q.task_done()

    def status(self):
        return dict(self.driver.status(), current=self.current, busy=self.running is not None,
                    queued=self.q.qsize(), done=self.done, last_error=self.last_error,
                    fail_seq=self.fail_seq, fail_pose=self.fail_pose)


_HANDS = {}
_HANDS_MU = threading.Lock()


def hands():
    """两只手的 worker，第一次用到时按环境变量建（import 时不碰串口）。"""
    with _HANDS_MU:
        if not _HANDS:
            for h, default_id in ((1, RM.SLAVE_ID_HAND1), (2, RM.SLAVE_ID_HAND2))[:HAND_COUNT]:
                driver = RH56F2(os.environ.get("HAND%d_PORT" % h, ""), int(os.environ.get("HAND%d_ID" % h, default_id)))
                _HANDS[h] = HandWorker(h, driver)
        return _HANDS


def _execute(pose, hand):
    """真正动手，在这只手的线程里跑（臂的动作归 ArmWorker）。查 poses.JOINTS（一次到位）、
    SEQUENCES（按顺序走一遍）或 RHYTHMS（循环节拍）。"""
    w = hands()[hand]
    if pose == "_custom":          # 单指控制台：六个关节各自的开合度
        shape, force = w.custom
        w.driver.set_force(force)
        w.driver.move(shape)
        return
    if pose in RHYTHMS:
        r = RHYTHMS[pose]
        if r.get("force") is not None:
            try:
                w.driver.set_force(r["force"])
            except HandError as e:   # 力度没写进去：照样做，开合度本身已经限着
                print("[手#%d] 力度上限没写进去：%s" % (hand, e), flush=True)
        end = time.monotonic() + r["max_seconds"]
        print("[手#%d] %s：节拍开始（最长 %d 秒）" % (hand, label(pose), r["max_seconds"]), flush=True)
        while time.monotonic() < end and not w.interrupted():
            for joints, hold in r["steps"]:
                w.driver.move(joints)
                if w.pause(hold):
                    return
        return
    if pose in SEQUENCES:
        for joints, hold in SEQUENCES[pose]:
            w.driver.move(joints)
            if w.pause(hold):
                return
        print("[手#%d%s] %s" % (hand, " mock" if w.driver.mock else "", label(pose)), flush=True)
        return
    w.driver.move(JOINTS[pose])
    print("[手#%d%s] %s" % (hand, " mock" if w.driver.mock else "", label(pose)), flush=True)


class ArmWorker:
    """臂一个线程，一次做一个技能（arm_skills.json 的步骤）。

    排队规则：同名动作正在做或排着 → 409；保命动作（poses.SAFETY）来了，正在做的普通动作当场停住、
    排着的普通动作作废；最多排 MAX_PENDING 个。每个动作收下前从"它开始时臂会在的位置"整条预检，
    执行时每一段再按臂的实际位置查一遍（被打断过的臂不在点位上）。
    """

    MAX_PENDING = 2

    def __init__(self, backend, effector=None, skills_path=None):
        self.backend = backend
        self._effector = effector or os.environ.get("ARM_EFFECTOR") or None
        self.skills_path = skills_path
        self.cfg = None
        self.cv = threading.Condition()
        self.pending = []        # [(epoch, 动作名)]
        self.running = None
        self.step = None         # 正在做的那一步，给 /status 看
        self.abort = threading.Event()
        self.epoch = 0
        self.at = None           # 最近一次走到的点位名；被打断、刚开机 = None（下一段按靠近老人限速）
        self.current = None
        self.done = 0
        self.aborted = 0
        self.last_error = None
        self.fail_seq = 0        # 后台做动作出错的次数（被打断、急停不算）：主脑探测 /status 时按它补报"没做成"
        self.fail_pose = None
        self.settle = None       # 最近一次走到点位时每个关节差多少度（看重力下垂）
        self.grasp = None
        self.recording = None    # 录制中：[步骤]；None = 没在录
        self._jog_axis = None    # 正在走的点动 (关节, 方向)：录制时把同轴同向的连续点动并成一段
        self._pose_go = None     # 正在去的姿态库姿态 (名字, 转速)
        self.servo_target = None  # 跟随模式：最新目标（rad）；None = 不在跟随
        self.servo_vel = None
        self.servo_updated = 0.0
        self._servo_done = None   # 上一次跟随结束时的 (目标, 转速)，录制用
        self._replay_preset = None  # 正在做的 teach_replay 是哪个预设（按钮直接做的）；脚本回放为 None，不录
        self._rec_last_end = None   # 录制：上一个动作做完的时刻，算两个动作之间停了多久
        self.orient_lock = False    # 保持手朝向（拇指朝上、手掌竖直）：开着时 J4 跟着 J2/J3 补，J5、J6 固定在 0
        self.recordings_path = os.environ.get("ARM_RECORDINGS_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(skills_path or S.SKILLS_FILE)), "arm_recordings.json")
        self.bindings_path = os.path.join(os.path.dirname(self.recordings_path), "arm_bindings.json")
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def effector(self):
        return self.cfg["effector"] if self.cfg else self._effector

    def _setup(self):
        if self.cfg is None:
            eff = self._effector or ("gripper" if self.backend.has_gripper else "hand1")
            if eff not in ("gripper", "hand1"):
                raise ValueError("ARM_EFFECTOR 只能是 gripper 或 hand1，现在是 %r" % eff)
            self.cfg = S.load_config(eff, self.skills_path)

    def ready(self):
        """(HTTP 码, 原因)。连得上、没锁定、末端（手#1）能动，才收臂的动作。"""
        ok, why = self.backend.ready()
        if not ok:
            return 503, "臂：%s" % why
        self._setup()
        if self.effector == "hand1":
            ok, why = hands()[1].ready()
            if not ok:
                return 503, "臂末端的手#1：%s" % why
        return 200, ""

    def request(self, name):
        """收一个臂的动作：(HTTP 码, 原因)。"""
        code, why = self.ready()
        if code != 200:
            return code, why
        with self.cv:
            if name == self.running or name in [n for _, n in self.pending]:
                return 409, "臂已经在做「%s」" % label(name)
            if name in SAFETY:
                self.pending = [(e, n) for e, n in self.pending if n in SAFETY]
                if self.running is not None and self.running not in SAFETY:
                    self.abort.set()
            if len(self.pending) >= self.MAX_PENDING:
                return 409, "臂前面还排着 %d 个动作" % len(self.pending)
            try:
                S.check_skill(name, self._start_for_next(), self.cfg, require_taught=not self.backend.mock)
            except S.SafetyError as e:
                return 422, "「%s」没过安全预检：%s" % (label(name), e)
            self.pending.append((self.epoch, name))
            self.cv.notify()
        return 200, ""

    def _end_of(self, name, default):
        moves = S.skill_moves(name, self.cfg)
        return list(self.cfg["points"][moves[-1]]["q"]) if moves else default

    def _start_for_next(self):
        """新动作开始时臂大概在哪：排着的最后一个的终点 / 正在做的终点 / 现在的位置（空闲或正被打断）。"""
        if self.pending:
            return self._end_of(self.pending[-1][1], self.backend.joints())
        if self.running is not None and not self.abort.is_set():
            return self._end_of(self.running, self.backend.joints())
        return self.backend.joints()

    def stop(self):
        """软件急停：作废排队，正在做的当场停住保持。"""
        with self.cv:
            self.epoch += 1
            self.pending = []
            if self.running is not None:
                self.abort.set()

    def busy(self):
        with self.cv:
            return self.running is not None or bool(self.pending)

    def wait_idle(self, timeout=5.0):
        end = time.monotonic() + timeout
        while self.busy():
            if time.monotonic() > end:
                return False
            time.sleep(0.01)
        return True

    def _run(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                epoch, name = self.pending.pop(0)
                if epoch != self.epoch:
                    continue
                self.running, self.current = name, name
                self.abort.clear()
            ok = False
            t0 = time.monotonic()
            try:
                ok = self._run_skill(name)
                if ok:
                    self.done += 1
                    if self.recording is not None and name in RECORDABLE:
                        before = len(self.recording)
                        self._record(name)
                        if len(self.recording) > before and self._rec_last_end is not None:
                            # 这个动作开始前停了多久：页面可以把它变成「等待」步骤
                            self.recording[before]["gap"] = round(max(0.0, t0 - self._rec_last_end), 1)
                        self._rec_last_end = time.monotonic()
            except Exception as e:  # noqa: BLE001 一个动作出错不能弄死臂的线程
                self.last_error = "%s：%s" % (name, e)
                self.fail_pose = name
                self.fail_seq += 1
                print("[臂] %s 没做成：%s" % (label(name), e), flush=True)
            finally:
                if not ok:
                    self.aborted += 1
                    self.at = None
                    try:
                        self.backend.hold()
                    except Exception as e:  # noqa: BLE001
                        print("[臂] 停住保持也没成功：%s（拍物理急停）" % e, flush=True)
                with self.cv:
                    self.running, self.step = None, None

    def _run_skill(self, name):
        """按步骤做；被打断返回 False。"""
        cfg = self.cfg
        if name == "teach_servo":
            return self._servo_loop()
        for kind, arg in cfg["skills"][name]:
            if self.abort.is_set():
                return False
            if kind == "move":
                p = cfg["points"][arg]
                q0 = self.backend.joints()
                S.check_segment(q0, p["q"], cfg, arg)
                slow = self.at is None or p.get("near_person") or cfg["points"][self.at].get("near_person")
                if p.get("vel"):    # 示教 / 回放自定义转速，仍不超过 max_joint_vel
                    span = max(abs(b - a) for a, b in zip(q0, p["q"]))
                    secs = max(TEACH_MIN_SECONDS, span / near_limit_vel(q0, p["q"], min(p["vel"], self.max_vel)))
                else:
                    secs = S.duration(q0, p["q"], cfg, slow)
                self.step = "→ %s（%.1f 秒%s）" % (p.get("label", arg), secs, "，慢速" if slow else "")
                print("[臂%s] %s %s" % (" mock" if self.backend.mock else "", label(name), self.step), flush=True)
                t0 = time.monotonic()
                torque = p.get("torque_scale", 1.0)
                if p.get("vel"):     # 示教 / 回放的点：靠近限位用满力矩
                    torque = max(torque, teach_torque(q0, p["q"]))
                if not self.backend.move(p["q"], secs, self.abort, torque):
                    return False
                if (p.get("blend") or p.get("settle")) and not self.backend.mock:
                    # 小于到位容差的一小段 move 会立刻返回：中间点至少走完大部分规划时长再接下一段；
                    # 后面要抓放 / 停留的点走完全程再多等一会儿，停稳了再动手
                    left = t0 + (secs * REPLAY_BLEND if p.get("blend") else secs + REPLAY_SETTLE) - time.monotonic()
                    if left > 0 and self.backend.wait(left, self.abort):
                        return False
                self.at = arg
                err = self.backend.last_errors
                if err:
                    self.settle = {"point": arg, "err_deg": S.deg(err)}
                    if max(err) > TOLERANCE:
                        print("[臂] 到 %s 停稳了，但离目标差 %s°（位置模式没有重力前馈，带载关节会下垂）"
                              % (arg, S.deg(err)), flush=True)
            elif kind == "grip":
                self.step = "末端 %s" % arg
                self._grip(name, arg)
            elif kind in ("gesture", "gesture_async"):
                w = hands()[1]
                w.drain()
                self.step = "手势：%s" % label(arg)
                w.submit(arg)
                if kind == "gesture":
                    w.drain()
            elif kind == "fingers":
                w = hands()[1]
                w.drain()
                self.step = "手型：" + " ".join("%s%d" % (FINGER_NAMES[j][0], v) for j, v in arg[0].items())
                w.driver.set_force(arg[1])
                w.driver.move(arg[0])
            elif kind == "hand":
                hands()[1].drain() if self.effector == "hand1" else None   # 先等同时进行的手势做完
                self.step = "手：%s" % HAND_PRESETS[arg][0]
                print("[臂%s] %s %s" % (" mock" if self.backend.mock else "", label(name), self.step), flush=True)
                self._hand(arg)
            elif kind == "wait":
                self.step = "等 %s 秒" % arg
                if self.backend.wait(float(arg), self.abort):
                    return False
            elif kind == "wait_put":
                driver = hands()[1].driver if self.effector == "hand1" else None
                if driver is not None and not driver.mock and RM.PROFILE == "f2_motion":
                    def notice(text):
                        self.step = text
                        print('[还药] ' + text, flush=True)
                    self.grasp = None
                    self.grasp = wait_put(driver, self.abort, float(arg), notice)
                    if self.grasp is None:
                        return False
                else:
                    if self.backend.wait(float(arg), self.abort):
                        return False
                    self._grip(name, "close")
            elif kind == "verify_grip":
                if not self.backend.mock and self.effector == 'gripper':
                    self.grasp = verify_gripper(self.backend.status(), float(arg))
            else:
                raise ArmError("arm_skills.json 里不认识的步骤 %r" % kind)
        return True

    def _grip(self, name, how):
        if self.effector == "gripper":
            if how != "relax":
                self.backend.grip(how == "close")
            return
        pose = {"open": "release", "relax": "home"}.get(how, name)
        w = hands()[1]
        w.submit(pose if pose in JOINTS else "release")
        w.drain()

    def _servo_loop(self):
        """跟随模式：拖动条每变一次就把最新目标发下去（moveJ 一帧，所有变了的关节同时走），不等走到；
        SERVO_IDLE 秒没有新目标且已走完 → 结束。每一帧都按臂的实际位置重查整段。"""
        import math
        last, until, prev, prev_t = None, 0.0, None, None
        guard = math.radians(WATCHDOG_DEG)
        while not self.abort.is_set():
            now = time.monotonic()
            act = self.backend.joints()
            if prev is not None:
                dt = max(now - prev_t, 1e-3)
                for i, (x, x0, lo, hi) in enumerate(zip(act, prev, S.JOINT_LOWER, S.JOINT_UPPER)):
                    v = (x - x0) / dt
                    ahead = x + v * PREDICT_S           # 按现在的速度，0.4 秒后会到哪
                    # 已经到了 / 越过发下去的目标还在往外冲 = 不听指令了（重力在拖）
                    over = last is not None and ((v > 0 and x >= last[i] - guard) or (v < 0 and x <= last[i] + guard))
                    # 看门狗：往外走、离软件限位不到 1°；或冲过头、照这速度 0.4 秒内会过软件限位 → 停住（hold 用满力矩）
                    if (x > hi - guard and x > x0) or (lo != 0 and x < lo + guard and x < x0) or \
                            (over and ((v > 0 and ahead > hi) or (lo != 0 and v < 0 and ahead < lo))):
                        raise ArmError("J%d 实际到了 %.1f°（%.0f°/s 往限位冲），已提前停住保持（防止碰到电机层锁定）"
                                       % (i + 1, math.degrees(x), math.degrees(abs(v))))
            prev, prev_t = act, now
            with self.cv:
                tgt, vel, upd = self.servo_target, self.servo_vel, self.servo_updated
                if tgt == last and now - upd > SERVO_IDLE and now > until + 0.3:
                    self._servo_done = (list(tgt), vel)
                    self.servo_target = None
                    break
            if tgt != last:
                q0 = self.backend.joints()
                S.check_segment(q0, tgt, self.cfg, "跟随目标")
                span = max(abs(b - a) for a, b in zip(q0, tgt))
                secs = max(SERVO_MIN_SECONDS, span / near_limit_vel(q0, tgt, min(vel, self.max_vel)))
                self.step = "跟随 %s°" % "/".join("%g" % d for d in S.deg(tgt))
                self.backend.command(tgt, secs, teach_torque(q0, tgt))
                last, until = list(tgt), now + secs
                prev, prev_t = self.backend.joints(), time.monotonic()   # 速度从发完这一帧起算（模拟臂是瞬移的）
            self.abort.wait(SERVO_PERIOD)
        with self.cv:
            self.servo_target = None
        self.at = None
        return not self.abort.is_set()

    def _hand(self, preset):
        """末端做一个手部动作：手#1 按力控阈值走到开合度；独立夹爪时张开 / 握取类映射成开 / 合。"""
        _, shape, force = HAND_PRESETS[preset]
        if self.effector == "gripper":
            if preset != "relax":
                self.backend.grip(preset != "release")
            return
        driver = hands()[1].driver
        driver.set_force(force)
        for joints, hold in (shape if isinstance(shape, list) else [(shape, 0)]):
            driver.move(joints)
            if hold and self.abort.wait(hold):
                return

    def holds_hand1(self):
        """手#1 装在臂上、臂正忙：手#1 不接别的动作。"""
        return self.effector == "hand1" and self.busy()

    @property
    def max_vel(self):
        """示教、跟随、脚本的转速上限 = SDK 配置的关节速度限幅（Follower.yaml velocity_limits，最小的那个）。"""
        v = getattr(self.backend, "vel_limit", None)
        return float(v) if v else TEACH_MAX_VEL

    def _teach_vel(self, speed):
        """页面给的 °/s → rad/s：不给用靠近老人的限速，最快不超过 max_vel（SDK 的速度限幅）。"""
        import math
        sp = self.cfg["speed"]
        if not speed:
            return sp["near_person_vel"]
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("转速必须是正数")
        return min(max(math.radians(speed), math.radians(1)), self.max_vel)

    def _teach_point(self, key, q, vel):
        self.cfg['points'][key] = {'q': q, 'taught': True, 'near_person': True, 'torque_scale': .6, 'vel': vel}

    def _record(self, name):
        """一个示教动作走成功了：把它的关节目标 / 夹爪记进录制。同轴同向、同转速的连续点动并成一段。"""
        rec = self.recording
        if name == "teach_replay":   # 按钮做的预设动作记成一步；脚本回放不记
            if self._replay_preset:
                key, back = self._replay_preset
                rec.append({"preset": key} if back else {"preset": key, "back": False})
            return
        if name == "teach_servo":    # 跟随拖动：只记松手后停下的位置
            q, vel = self._servo_done
            rec.append({"move": [round(float(x), 4) for x in q], "vel": round(vel, 4), "via": "drag"})
            return
        if name == "teach_pose":     # 去姿态库里的姿态：记姿态名，以后重新示教这个姿态，脚本跟着变
            pose, speed = self._pose_go
            rec.append(dict({"pose": pose}, **({"speed": speed} if speed else {})))
            return
        for kind, arg in self.cfg["skills"][name]:
            if kind == "grip":
                if arg in ("open", "close"):
                    rec.append({"grip": arg})
                elif self.effector == "hand1":       # 「放松」：夹爪不动，灵巧手回放松手型
                    rec.append({"hand": "relax"})
            elif kind == "hand":
                rec.append({"hand": arg})
            elif kind == "move":
                p = self.cfg["points"][arg]
                step = {"move": [round(float(x), 4) for x in p["q"]],
                        "vel": round(p.get("vel") or self.cfg["speed"]["near_person_vel"], 4), "via": name[6:]}
                if name == "teach_jog" and self._jog_axis:
                    step["joint"], step["dir"] = self._jog_axis
                    prev = rec[-1] if rec else None
                    if prev and prev.get("joint") == step["joint"] and prev.get("dir") == step["dir"] \
                            and prev.get("vel") == step["vel"]:
                        prev["move"] = step["move"]
                        continue
                rec.append(step)

    def _load_recordings(self):
        try:
            with open(self.recordings_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_recordings(self, data):
        tmp = self.recordings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.recordings_path)

    def recordings(self):
        return self._load_recordings()

    def bindings(self):
        """业务动作 → 录制名（arm_bindings.json）。"""
        try:
            with open(self.bindings_path, encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if k in BINDABLE and v}
        except FileNotFoundError:
            return {}

    def bind(self, action, name):
        """把业务动作绑到一条录制；name 为空 = 解绑，恢复原来的业务动作。返回 (HTTP 码, 回包)。"""
        if action not in BINDABLE:
            return 400, {"ok": False, "error": "只能绑定：" + "、".join(BINDABLE.values())}
        name = (name or "").strip()
        if name and name not in self._load_recordings():
            return 404, {"ok": False, "error": "没有这条录制"}
        data = self.bindings()
        if name:
            data[action] = name
        else:
            data.pop(action, None)
        tmp = self.bindings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.bindings_path)
        return 200, {"ok": True, "bindings": data}

    @property
    def poses_path(self):
        return os.path.join(os.path.dirname(self.recordings_path), "arm_poses.json")

    def poses(self):
        """姿态库：名字 → {"q": 关节角 rad, "at": 示教时间}（arm_poses.json）。脚本按名字引用，重新示教一处，所有脚本跟着变。"""
        try:
            with open(self.poses_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save_poses(self, data):
        tmp = self.poses_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.poses_path)

    def library(self):
        return {"poses": {n: dict(p, q_deg=S.deg(p["q"])) for n, p in self.poses().items()},
                "scripts": self._load_recordings(), "bindings": self.bindings(), "bindable": BINDABLE,
                "hand_presets": {k: v[0] for k, v in HAND_PRESETS.items()}, "effector": self.effector,
                "presets": {k: {"label": v["label"], "desc": v["desc"]} for k, v in PRESETS.items()}}

    @staticmethod
    def _check_name(name, what):
        name = (name or "").strip()
        if not 0 < len(name) <= 32 or any(ord(c) < 32 for c in name):
            raise ValueError("%s名称要 1~32 个字" % what)
        return name

    def _check_q(self, q, what):
        import math
        if len(q) != 6 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in q):
            raise S.SafetyError("%s关节角无效" % what)
        if abs(q[0]) > math.radians(TEACH_BASE_LIMIT):
            raise S.SafetyError("%s底座超过 ±%d°" % (what, TEACH_BASE_LIMIT))
        self._setup()
        S.check_limits(q, what)
        S.check_pose(q, self.cfg, what)

    def save_pose(self, name, q, derived=None):
        """存 / 覆盖一个姿态。锁住的姿态不能改（ValueError）。由它派生的「上方 N cm」姿态（没锁的）跟着重算。
        返回 (名字, 跟着更新的派生姿态列表)。"""
        from hand_controller.approach import translate_fixed
        name = self._check_name(name, "姿态")
        q = [round(float(x), 4) for x in q]
        self._check_q(q, "姿态「%s」" % name)
        data = self.poses()
        if data.get(name, {}).get("locked"):
            raise ValueError("姿态「%s」已锁定，先解锁再改" % name)
        data[name] = dict({"q": q, "at": time.strftime("%Y-%m-%d %H:%M")}, **({"derived": derived} if derived else {}))
        moved = []
        for child, p in data.items():
            d = p.get("derived")
            if child == name or not d or d.get("from") != name or p.get("locked"):
                continue
            try:
                cq = [round(float(x), 4) for x in translate_fixed(q, dz=d["dz"])]
                self._check_q(cq, "姿态「%s」" % child)
            except S.SafetyError as e:
                raise ValueError("「%s」跟着移动后不安全：%s" % (child, e))
            p.update(q=cq, at=data[name]["at"])
            moved.append(child)
        self._save_poses(data)
        return name, moved

    def set_pose(self, name, q_deg):
        """3D 视图里拖动姿态标记：直接给六个关节角（°）改这个姿态，不动真臂。"""
        import math
        if name not in self.poses():
            return 404, {"ok": False, "error": "没有这个姿态"}
        try:
            name, moved = self.save_pose(name, [math.radians(float(v)) for v in q_deg],
                                         self.poses()[name].get("derived"))
        except (S.SafetyError, ValueError, TypeError) as e:
            return 422, {"ok": False, "error": str(e)}
        return 200, {"ok": True, "pose_moved": name, "followers": moved}

    ROUTE_MODES = ("direct", "lift", "via")

    def set_route(self, name, mode, cm=6.0, via=()):
        """去这个姿态走哪条路：direct 直走；lift 先在原地垂直抬起 cm、平移到目标正上方再垂直落下；
        via 依次经过 via 里的姿态再到目标。前往按钮、脚本里「移动到姿态」都按它走。"""
        data = self.poses()
        if name not in data:
            return 404, {"ok": False, "error": "没有这个姿态"}
        if mode not in self.ROUTE_MODES:
            return 400, {"ok": False, "error": "路径只能是 direct / lift / via"}
        route = {"mode": mode}
        if mode == "lift":
            if not 1 <= cm <= TEACH_MAX_CM:
                return 400, {"ok": False, "error": "抬起高度要 1~%d cm" % TEACH_MAX_CM}
            route["cm"] = round(cm, 1)
        if mode == "via":
            via = [v for v in via if v]
            bad = [v for v in via if v not in data or v == name]
            if not via or bad:
                return 400, {"ok": False, "error": "途经姿态要从姿态库里选（不能是它自己）：%s" % "、".join(bad)}
            route["via"] = via
        if mode == "direct":
            data[name].pop("route", None)
        else:
            data[name]["route"] = route
        self._save_poses(data)
        return 200, {"ok": True, "pose_route": name, "route": route}

    def route_to(self, name, cur, poses=None):
        """从关节角 cur 去姿态 name 的路线：[(说明, 关节角)…]，最后一个是姿态本身。"""
        from hand_controller.approach import translate_fixed
        poses = poses if poses is not None else self.poses()
        p = poses[name]
        r = p.get("route") or {"mode": "direct"}
        goal = [float(x) for x in p["q"]]
        if r["mode"] == "lift":
            dz = r.get("cm", 6) / 100
            way = []
            try:
                way.append(("原地抬起 %g cm" % r.get("cm", 6), translate_fixed(cur, dz=dz)))
            except S.SafetyError:
                pass     # 当前姿态抬不起来（贴着限位）：直接去目标正上方
            try:
                way.append(("到「%s」正上方" % name, translate_fixed(goal, dz=dz)))
            except S.SafetyError as e:
                raise S.SafetyError("「%s」正上方 %g cm 解不出来：%s" % (name, r.get("cm", 6), e))
            return way + [("落到「%s」" % name, goal)]
        if r["mode"] == "via":
            way = []
            for v in r.get("via", []):
                if v not in poses:
                    raise S.SafetyError("「%s」的途经姿态「%s」已经不在姿态库里" % (name, v))
                way.append(("经过「%s」" % v, [float(x) for x in poses[v]["q"]]))
            return way + [("到「%s」" % name, goal)]
        return [("到「%s」" % name, goal)]

    def lock_pose(self, name, on):
        data = self.poses()
        if name not in data:
            return 404, {"ok": False, "error": "没有这个姿态"}
        data[name]["locked"] = bool(on)
        self._save_poses(data)
        return 200, {"ok": True, "pose_locked": name, "locked": bool(on)}

    def delete_pose(self, name):
        data = self.poses()
        if data.get(name, {}).get("locked"):
            return 423, {"ok": False, "error": "姿态「%s」已锁定，先解锁再删" % name}
        if data.pop(name, None) is None:
            return 404, {"ok": False, "error": "没有这个姿态"}
        self._save_poses(data)
        used = [n for n, r in self._load_recordings().items() if any(st.get("pose") == name for st in r["steps"])]
        return 200, {"ok": True, "pose_deleted": name, "used_by": used}

    @staticmethod
    def _clean_steps(steps):
        """脚本保存前整理：只认这几种步骤，数字都在范围内。"""
        import math
        if not isinstance(steps, list) or not steps:
            raise ValueError("脚本是空的")
        if len(steps) > SCRIPT_MAX_STEPS:
            raise ValueError("脚本最多 %d 步" % SCRIPT_MAX_STEPS)
        out = []
        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                raise ValueError("第 %d 步格式不对" % i)
            if "pose" in st:
                clean = {"pose": str(st["pose"])}
                if st.get("speed") not in (None, "", 0):
                    v = float(st["speed"])
                    if not (math.isfinite(v) and 0 < v <= 60):
                        raise ValueError("第 %d 步转速不对" % i)
                    clean["speed"] = round(v, 1)
            elif "move" in st:
                q = [float(x) for x in st["move"]]
                if len(q) != 6 or not all(math.isfinite(x) for x in q):
                    raise ValueError("第 %d 步关节角无效" % i)
                clean = {k: st[k] for k in ("via", "joint", "dir") if k in st}
                clean["move"] = q
                if st.get("vel"):
                    clean["vel"] = float(st["vel"])
            elif "preset" in st:
                if st["preset"] not in PRESETS:
                    raise ValueError("第 %d 步预设动作不认识：%s" % (i, st["preset"]))
                clean = {"preset": st["preset"]}
                if st.get("back") is False:     # 做完停在最后的姿态，不回出发姿态
                    clean["back"] = False
            elif "hand" in st:
                if st["hand"] not in HAND_PRESETS:
                    raise ValueError("第 %d 步手部动作不认识：%s" % (i, st["hand"]))
                clean = {"hand": st["hand"]}
            elif "grip" in st:
                if st["grip"] not in ("open", "close"):
                    raise ValueError("第 %d 步夹爪动作不认识" % i)
                clean = {"grip": st["grip"]}
            elif "wait" in st:
                w = float(st["wait"])
                if not (math.isfinite(w) and 0 <= w <= 120):
                    raise ValueError("第 %d 步等待要 0~120 秒" % i)
                clean = {"wait": round(w, 1)}
            elif "orient" in st:
                clean = {"orient": bool(st["orient"])}
            elif "fingers" in st:
                shape, force = clean_fingers(st["fingers"], st.get("force", 300))
                if set(shape) != set(FINGER_NAMES):
                    raise ValueError("第 %d 步手型要给全六个关节" % i)
                clean = {"fingers": shape, "force": force}
            else:
                raise ValueError("第 %d 步不认识" % i)
            out.append(clean)
        return out

    def save_script(self, name, steps, voice=None):
        """编排好的脚本存进录制文件（和实时录制同一个列表，T5AI 按键也能绑）。返回 (名字, 还没示教的姿态)。
        voice = 语音说法（「递水、拿东西给老人」），给大模型挑脚本用；None = 保留原来的。"""
        name = self._check_name(name, "脚本")
        steps = self._clean_steps(steps)
        data = self._load_recordings()
        old_voice = (data.get(name) or {}).get("voice")
        data[name] = {"created": time.strftime("%Y-%m-%d %H:%M"), "steps": steps}
        voice = old_voice if voice is None else str(voice).strip()[:80]
        if voice:
            data[name]["voice"] = voice
        self._save_recordings(data)
        poses = self.poses()
        return name, sorted({st["pose"] for st in steps if "pose" in st and st["pose"] not in poses})

    def _build_replay(self, steps, scale, start=0, q_start=None, first_cap=True):
        """脚本 / 录制 → 内存里的 teach_replay 技能（点位 _rec_N）。每个关节目标都重新查一遍。
        后面紧跟移动的点连贯通过（blend）；后面是手部动作、等待或结尾的点走完停稳（settle）。
        预设动作在这里按"走到这一步时臂在哪"展开：底座 J1、腕部 J5 不动，做完回到展开前的姿态。"""
        import math
        sp = self.cfg["speed"]
        if not 0.2 <= scale <= 3:
            raise S.SafetyError("倍速只能在 0.2~3 之间")
        if not 0 <= start < len(steps):
            raise S.SafetyError("起始步超出范围")
        for key in [k for k in self.cfg["points"] if k.startswith("_rec_")]:
            del self.cfg["points"][key]
        poses = self.poses()
        skill, first = [], [first_cap]
        lock = [False]      # 脚本里的「保持手朝向」：从「开」那一步起，后面的姿态 / 录制点都按锁住的手腕走
        cur = list(q_start if q_start is not None else
                   (getattr(self.backend, 'last_command', None) or self.backend.joints()))

        def add_move(q, vel, where, i, sub=None):
            if lock[0]:
                q = orient_locked(q)
            self._check_q(q, where)
            vel *= scale
            if first[0]:   # 从现在的位置去第一个点：位置未知，按靠近老人的限速
                vel, first[0] = min(vel, sp["near_person_vel"]), False
            vel = min(max(vel, math.radians(1)), self.max_vel)
            key = "_rec_%d" % i if sub is None else "_rec_%d_%s" % (i, sub)   # 页面按 _rec_<步号> 高亮正在做的步
            self._teach_point(key, list(q), vel)
            skill.append(["move", key])
            cur[:] = q

        def need_hand(what):
            if self.effector != "hand1":
                raise S.SafetyError("%s要用灵巧手，当前末端不是灵巧手" % what)

        for i, st in enumerate(steps[start:], start + 1):
            where = "第 %d 步" % i
            if "pose" in st:
                if st["pose"] not in poses:
                    raise S.SafetyError("第 %d 步的姿态「%s」还没示教" % (i, st["pose"]))
                vel = math.radians(float(st["speed"])) if st.get("speed") else sp["near_person_vel"]
                way = self.route_to(st["pose"], list(cur), poses)
                for n, (text, q) in enumerate(way):    # 最后一段用 _rec_<步号>，页面照样能高亮
                    add_move(q, vel, "%s %s" % (where, text), i, None if n == len(way) - 1 else "r%d" % n)
            elif "move" in st:
                add_move([float(x) for x in st["move"]], float(st.get("vel") or sp["near_person_vel"]), where, i)
            elif "preset" in st:
                preset = PRESETS.get(st["preset"])
                if preset is None:
                    raise S.SafetyError("第 %d 步的预设动作不认识：%s" % (i, st["preset"]))
                base = list(cur)
                where = "第 %d 步「%s」" % (i, preset["label"])
                was_locked, lock[0] = lock[0], False    # 预设动作有自己的手腕动作，不受朝向锁管
                for n, sub in enumerate(preset["steps"]):
                    if "arm" in sub:
                        j2, j3, j4, j6 = sub["arm"]
                        q = [base[0], math.radians(j2), math.radians(j3), math.radians(j4), base[4],
                             base[5] if j6 is None else math.radians(j6)]
                        add_move(q, math.radians(sub["speed"]), where, i, n)
                    elif sub.get("back"):
                        if st.get("back") is not False:
                            add_move(base, math.radians(20), where, i, n)
                    elif "hand" in sub:
                        need_hand(where)
                        skill += [["hand", sub["hand"]], ["wait", HAND_WAIT]]
                    elif "gesture" in sub:
                        need_hand(where)
                        skill.append(["gesture_async" if sub.get("async") else "gesture", sub["gesture"]])
                    elif "wait" in sub:     # 倍速连停顿一起缩放
                        skill.append(["wait", round(float(sub["wait"]) / scale, 2)])
                lock[0] = was_locked
            elif "hand" in st:
                if st["hand"] not in HAND_PRESETS:
                    raise S.SafetyError("第 %d 步手部动作不认识" % i)
                skill += [["hand", st["hand"]], ["wait", HAND_WAIT]]
            elif "fingers" in st:
                need_hand(where)
                shape, force = clean_fingers(st["fingers"], st.get("force", 300))
                skill += [["fingers", (shape, force)], ["wait", HAND_WAIT]]
            elif "orient" in st:
                lock[0] = bool(st["orient"])
                if lock[0] and max(abs(a - b) for a, b in zip(orient_locked(cur), cur)) > math.radians(0.5):
                    add_move(list(cur), sp["near_person_vel"], where + " 摆正手的朝向", i)
            elif st.get("grip") in ("open", "close"):
                if self.effector == "gripper":
                    skill += [["grip", st["grip"]], ["wait", REPLAY_GRIP_WAIT]]
                else:   # 夹爪时期录的：换成手的张开 / 握取
                    skill += [["hand", "release" if st["grip"] == "open" else "grasp"], ["wait", HAND_WAIT]]
            elif "wait" in st:
                skill.append(["wait", float(st["wait"])])
            else:
                raise S.SafetyError("第 %d 步不认识：%r" % (i, st))
        if not skill:
            raise S.SafetyError("脚本是空的")
        for k, (kind, arg) in enumerate(skill):
            if kind == "move":   # 同时做的手势不打断连贯（看它后面那一步）
                nxt = next((s[0] for s in skill[k + 1:] if s[0] != "gesture_async"), None)
                self.cfg["points"][arg]["blend" if nxt == "move" else "settle"] = True
        return skill + [["wait", 1.0]]

    def teach(self, kind, joint=None, delta=None, point=None, close=False, speed=None, name="", scale=1.0,
              target=None, start=0, targets=None, back=True):
        """Bounded position teaching, serialized with normal arm requests."""
        if not TEACH_MODE and kind not in ("replay", "rec_delete", "preset", "start"):
            return 423, {"ok": False, "error": "请先进入点动示教模式"}
        code, why = self.ready()
        if code != 200:
            return code, {"ok": False, "error": why}
        with self.cv:
            following = kind == "servo" and self.running == "teach_servo" and self.servo_target is not None                 and not self.pending and not self.abort.is_set()
            if self.busy() and not following:
                return 409, {"ok": False, "error": "等待当前动作结束后再点动或记录"}
            try:
                vel = self._teach_vel(speed)
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}
            if kind == "servo":
                import math
                if not targets:
                    return 400, {"ok": False, "error": "没有给目标关节"}
                q = list(self.servo_target if following else
                         (getattr(self.backend, 'last_command', None) or self.backend.joints()))
                for j, d in targets.items():
                    if j not in range(1, 7) or not math.isfinite(d):
                        return 400, {"ok": False, "error": "关节号 1~6、角度要是数字"}
                    q[j - 1] = math.radians(d)
                if self.orient_lock:
                    q = orient_locked(q)    # 手腕由朝向锁算，页面发来的 J4~J6 不算数
                if abs(q[0]) > math.radians(TEACH_BASE_LIMIT):
                    return 422, {"ok": False, "error": "底座不能超过 ±%d°" % TEACH_BASE_LIMIT}
                try:
                    check_teach_target(self.backend.joints(), q,
                                       sorted({j - 1 for j in targets} | ({3, 4, 5} if self.orient_lock else set())))
                    S.check_segment(self.backend.joints(), q, self.cfg, "跟随目标")
                except S.SafetyError as e:
                    return 422, {"ok": False, "error": str(e)}
                self.servo_target, self.servo_vel, self.servo_updated = q, vel, time.monotonic()
                if following:
                    return 200, {"ok": True, "pose": "teach_servo", "servo": "update"}
                self.cfg['skills']['teach_servo'] = []
                code, why = self.request('teach_servo')
                if code != 200:
                    self.servo_target = None
                return code, {"ok": code == 200, "pose": "teach_servo", "servo": "start", "error": why}
            if kind == "rec_start":
                q = list(getattr(self.backend, 'last_command', None) or self.backend.joints())
                self.recording = [{"move": [round(float(x), 4) for x in q],
                                   "vel": round(self.cfg["speed"]["near_person_vel"], 4), "via": "start"}]
                if self.orient_lock:
                    self.recording.append({"orient": True})
                self._rec_last_end = time.monotonic()
                return 200, {"ok": True, "recording": 0}
            if kind == "orient":     # 保持手朝向：开 = 先把手摆正（J4 = J2 − J3、J5 = J6 = 0）再锁住
                import math
                if not close:
                    self.orient_lock = False
                    if self.recording is not None:
                        self.recording.append({"orient": False})
                    return 200, {"ok": True, "orient_lock": False}
                q0 = list(getattr(self.backend, 'last_command', None) or self.backend.joints())
                q = orient_locked(q0)
                try:
                    check_teach_target(self.backend.joints(), q, [3, 4, 5])
                    S.check_segment(self.backend.joints(), q, self.cfg, "摆正手的朝向")
                except S.SafetyError as e:
                    return 422, {"ok": False, "error": "现在的姿态没法保持手朝向：%s" % e}
                self.orient_lock = True
                if self.recording is not None:
                    self.recording.append({"orient": True})
                if max(abs(a - b) for a, b in zip(q, q0)) < math.radians(0.5):
                    return 200, {"ok": True, "orient_lock": True}
                self._teach_point('_teach_target', q, vel)
                self.cfg['skills']['teach_orient'] = [["move", "_teach_target"], ["wait", 1.0]]
                code, why = self.request('teach_orient')
                return code, {"ok": code == 200, "orient_lock": True, "pose": "teach_orient", "error": why}
            if kind == "rec_take":    # 停止录制，把录到的步骤交给页面（加进正在编辑的脚本，不另存）
                if self.recording is None:
                    return 409, {"ok": False, "error": "没有在录制"}
                taken, self.recording = self.recording, None
                return 200, {"ok": True, "taken": taken}
            if kind == "rec_discard":
                self.recording = None
                return 200, {"ok": True, "recording": None}
            if kind == "rec_save":
                name = (name or "").strip()
                if not 0 < len(name) <= 32 or any(ord(c) < 32 for c in name):
                    return 400, {"ok": False, "error": "录制名称要 1~32 个字"}
                if self.recording is None:
                    return 409, {"ok": False, "error": "没有在录制"}
                if len(self.recording) < 2:
                    return 422, {"ok": False, "error": "还没录到任何动作"}
                data = self._load_recordings()
                data[name] = {"created": time.strftime("%Y-%m-%d %H:%M"), "steps": self.recording}
                try:
                    self._save_recordings(data)
                except OSError as e:
                    return 500, {"ok": False, "error": "保存失败：%s" % e}
                n = len(self.recording)
                self.recording = None
                return 200, {"ok": True, "saved": name, "steps": n}
            if kind == "rec_delete":
                data = self._load_recordings()
                if data.pop(name, None) is None:
                    return 404, {"ok": False, "error": "没有这条录制"}
                self._save_recordings(data)
                unbound = [a for a, n in self.bindings().items() if n == name]
                for a in unbound:      # 删掉的录制不能还挂在按键上
                    self.bind(a, "")
                return 200, {"ok": True, "deleted": name, "unbound": unbound}
            if kind == "replay":
                self._replay_preset = None
                rec = self._load_recordings().get(name)
                if rec is None:
                    return 404, {"ok": False, "error": "没有这条录制"}
                try:
                    self.cfg['skills']['teach_replay'] = self._build_replay(rec["steps"], scale, start)
                except (S.SafetyError, ValueError, TypeError, KeyError) as e:
                    return 422, {"ok": False, "error": "回放没过检查：%s" % e}
                code, why = self.request('teach_replay')
                return code, {"ok": code == 200, "pose": 'teach_replay', "replay": name, "from_step": start + 1,
                              "error": why}
            if kind == "preset":
                if name not in PRESETS:
                    return 404, {"ok": False, "error": "没有这个预设动作"}
                self._replay_preset = (name, back)
                try:
                    # 按钮直接做预设：人看着臂按下去的，第一段不按"位置未知"限速，跟倍速走（仍不超过 SDK 速度限幅）
                    step = {"preset": name} if back else {"preset": name, "back": False}
                    self.cfg['skills']['teach_replay'] = self._build_replay([step], scale, first_cap=False)
                except (S.SafetyError, ValueError, TypeError, KeyError) as e:
                    return 422, {"ok": False, "error": "「%s」没过检查：%s" % (PRESETS[name]["label"], e)}
                code, why = self.request('teach_replay')
                return code, {"ok": code == 200, "pose": 'teach_replay', "preset": name,
                              "replay": PRESETS[name]["label"], "from_step": 1, "error": why}
            if kind == "pose_save":
                try:
                    q = list(getattr(self.backend, 'last_command', None) or self.backend.joints())
                    saved, moved = self.save_pose(name, q)
                except (S.SafetyError, ValueError, OSError) as e:
                    return 422, {"ok": False, "error": str(e)}
                return 200, {"ok": True, "pose_saved": saved, "joints_deg": S.deg(q), "followers": moved}
            if kind == "pose_above":
                from hand_controller.approach import translate_fixed
                base = self.poses().get(name)
                if base is None:
                    return 404, {"ok": False, "error": "没有这个姿态"}
                if delta is None or not 0 < abs(delta) <= TEACH_MAX_CM / 100:
                    return 400, {"ok": False, "error": "高度差要 0~%d cm" % TEACH_MAX_CM}
                try:
                    q = translate_fixed(base["q"], dz=delta)
                    S.check_segment(q, base["q"], self.cfg, "垂直接近「%s」" % name)
                    new, _ = self.save_pose("%s·%s%gcm" % (name, "上方" if delta > 0 else "下方", round(abs(delta) * 100, 1)), q,
                                            {"from": name, "dz": delta})
                except (S.SafetyError, ValueError, OSError) as e:
                    return 422, {"ok": False, "error": str(e)}
                return 200, {"ok": True, "pose_saved": new, "joints_deg": S.deg(q)}
            if kind == "start":    # 零位待机点：手张开，慢速回零位（rest，全部关节 0°，收拢待机的姿态）
                target = list(self.cfg['points']['rest']['q'])
                try:
                    S.check_segment(self.backend.joints(), target, self.cfg, "零位待机点")
                except S.SafetyError as e:
                    return 422, {'ok': False, 'error': str(e)}
                self._teach_point('_teach_start', target, self.cfg['speed']['near_person_vel'])
                self.cfg['points']['_teach_start']['label'] = '零位待机点'
                self.cfg['skills']['teach_start']=[['grip','open'],['wait',1.5],['move','_teach_start'],['wait',1.0]]
                code,why=self.request('teach_start')
                return code, {'ok':code==200,'pose':'teach_start','error':why}
            if kind == "plan_pick":
                from hand_controller.approach import generate_approach
                from hand_controller.arm_teach import save_point
                try:
                    plan = generate_approach(self.cfg)
                    for key in ('pre_grasp','orient_clear'):
                        save_point(key, plan[key], self.skills_path)
                    save_point('above_tray', plan['pre_grasp'], self.skills_path)
                    self.cfg = S.load_config(self.effector, self.skills_path)
                    self.at = None
                    return 200, {'ok':True, 'clearance_cm':round(plan['clearance_m']*100),
                                 'pre_grasp_deg':S.deg(plan['pre_grasp'])}
                except (S.SafetyError, OSError) as e:
                    return 422, {'ok':False,'error':str(e)}
            if kind == "vertical":
                from hand_controller.approach import translate_fixed
                if delta is None or not 0 < abs(delta) <= TEACH_MAX_CM / 100:
                    return 400, {'ok':False,'error':'每次垂直移动 0~%d cm' % TEACH_MAX_CM}
                current = list(getattr(self.backend,'last_command',None) or self.backend.joints())
                try:
                    target = translate_fixed(current,dz=delta)
                    S.check_segment(self.backend.joints(),target,self.cfg,'垂直微调')
                except S.SafetyError as e:
                    return 422, {'ok':False,'error':str(e)}
                self._teach_point('_teach_target', target, vel)
                self.cfg['skills']['teach_vertical']=[['move','_teach_target'],['wait',2.5]]
                code,why=self.request('teach_vertical')
                return code, {'ok':code==200,'pose':'teach_vertical','error':why}
            if kind == "capture":
                if point not in TEACH_POINTS:
                    return 400, {"ok": False, "error": "未知业务点位"}
                from hand_controller.arm_teach import save_point
                try:
                    actual = self.backend.joints()
                    q = list(getattr(self.backend, 'last_command', None) or actual)
                    S.check_limits(q, point)
                    S.check_pose(q, self.cfg, point)
                    S.check_limits(actual, point)
                    S.check_pose(actual, self.cfg, point)
                    saved = save_point(point, q, self.skills_path, measured=actual)
                    self.cfg = S.load_config(self.effector, self.skills_path)
                    self.at = None
                    return 200, {"ok": True, "point": point, "saved": saved, "joints_deg": S.deg(q)}
                except (S.SafetyError, OSError) as e:
                    return 422, {"ok": False, "error": str(e)}
            if kind == "recall":
                if point not in TEACH_POINTS or not self.cfg['points'][point].get('taught'):
                    return 422, {"ok": False, "error": "该点尚未记录，不能移动到占位姿态"}
                route = (['orient_clear','pre_grasp',point] if 'pre_grasp' in self.cfg['points'] and self.cfg['points']['pre_grasp'].get('taught') else ['above_tray',point]) if point == 'pick_med' else (['above_sos', point] if point == 'sos_press' else [point])
                self.cfg['skills']['teach_recall'] = [['move', p] for p in route] + [['wait', 2.2]]
                name = 'teach_recall'
            elif kind in ("jog", "joint"):
                import math
                q = list(getattr(self.backend, 'last_command', None) or self.backend.joints())
                if kind == "joint":     # 拖动条：一个关节直接去目标角（°），沿路照样查禁区、桌面、限位
                    if joint not in range(1, 7) or target is None or not math.isfinite(target):
                        return 400, {"ok": False, "error": "需要关节号 1~6 和目标角度"}
                    delta = target - math.degrees(q[joint-1])
                    if abs(delta) < .05:
                        return 200, {"ok": True, "pose": None, "unchanged": True}
                elif joint not in range(1, 7) or not math.isfinite(delta) or not 0 < abs(delta) <= TEACH_MAX_DEG:
                    return 400, {"ok": False, "error": "每次只能点动一轴 0~%d°" % TEACH_MAX_DEG}
                if self.orient_lock and joint in (4, 5, 6):
                    return 423, {"ok": False, "error": "「保持手朝向」开着，J4~J6 由它管；要单独转腕先关掉"}
                actual = self.backend.joints()
                if max(abs(a-b) for a,b in zip(q, actual)) > .15:
                    return 422, {"ok": False, "error": "当前位置偏离上次目标较大，请先停止并重新校准"}
                q[joint-1] += math.radians(delta)
                if self.orient_lock:
                    q = orient_locked(q)
                if abs(q[0]) > math.radians(TEACH_BASE_LIMIT):
                    return 422, {"ok": False, "error": "点动示教底座不能超过 ±%d°" % TEACH_BASE_LIMIT}
                try:
                    check_teach_target(actual, q, [joint - 1] + ([3, 4, 5] if self.orient_lock else []))
                    S.check_segment(self.backend.joints(), q, self.cfg, "示教目标")
                except S.SafetyError as e:
                    return 422, {"ok": False, "error": str(e)}
                self._teach_point('_teach_target', q, vel)
                self.cfg['skills']['teach_jog'] = [["move", "_teach_target"], ["wait", 2.2]]
                self._jog_axis = (joint, 1 if delta > 0 else -1)
                name = 'teach_jog'
            elif kind == "pose_go":
                p = self.poses().get(name)
                if p is None:
                    return 404, {"ok": False, "error": "没有这个姿态"}
                try:
                    way = self.route_to(name, list(getattr(self.backend, 'last_command', None) or self.backend.joints()))
                    if self.orient_lock:
                        way = [(text + "（保持手朝向）", orient_locked(q)) for text, q in way]
                    cur = self.backend.joints()
                    for text, q in way:
                        self._check_q(q, "%s：" % text)
                        S.check_segment(cur, q, self.cfg, text)
                        cur = q
                except S.SafetyError as e:
                    return 422, {"ok": False, "error": str(e)}
                skill = []
                for k, (text, q) in enumerate(way):
                    key = '_pose_go_%d' % k
                    self._teach_point(key, list(q), vel)
                    self.cfg['points'][key]['label'] = text
                    self.cfg['points'][key]['blend' if k + 1 < len(way) else 'settle'] = True
                    skill.append(["move", key])
                self.cfg['skills']['teach_pose'] = skill + [["wait", 1.0]]
                self._pose_go = (name, round(speed, 1) if speed else None)
                name = 'teach_pose'
            elif kind == "hand":
                if point not in HAND_PRESETS:
                    return 400, {"ok": False, "error": "不认识的手部动作"}
                self.cfg['skills']['teach_hand'] = [["hand", point], ["wait", HAND_WAIT]]
                name = 'teach_hand'
            elif kind == "grip":
                if self.effector != 'gripper':
                    return 422, {"ok": False, "error": "当前末端不是独立夹爪"}
                self.cfg['skills']['teach_grip'] = [["grip", "close" if close else "open"], ["wait", 4.0]]
                name = 'teach_grip'
            else:
                return 400, {"ok": False, "error": "未知示教操作"}
            code, why = self.request(name)
            return code, {"ok": code == 200, "pose": name, "error": why}

    def status(self):
        st = dict(self.backend.status(), effector=self.effector, busy=self.running is not None,
                  current=self.current, step=self.step, queued=len(self.pending), at=self.at,
                  done=self.done, aborted=self.aborted, last_error=self.last_error, settle=self.settle,
                  fail_seq=self.fail_seq, fail_pose=self.fail_pose, grasp=self.grasp,
                  recording=None if self.recording is None else len(self.recording) - 1,
                  orient_lock=self.orient_lock)
        if self.cfg:
            sp = self.cfg["speed"]
            st["teach_limits"] = {"max_deg": TEACH_MAX_DEG, "max_cm": TEACH_MAX_CM,
                                  "max_speed": S.deg([self.max_vel])[0], "safe_speed": S.deg([TEACH_MAX_VEL])[0],
                                  "default_speed": S.deg([sp["near_person_vel"]])[0],
                                  "margin_deg": TEACH_LIMIT_MARGIN,
                                  "joints": [list(x) for x in zip(S.deg(teach_limits()[0]), S.deg(teach_limits()[1]))]}
            st["untaught"] = sorted(n for n, p in self.cfg["points"].items() if not p.get("taught"))
        if st["connected"]:
            try:
                q = self.backend.joints()
                st["joints_deg"] = S.deg(q)
                if self.cfg:
                    st["tool_m"] = [round(c, 3) for c in tool_position(q, self.cfg["tool_length"])]
            except Exception as e:  # noqa: BLE001 读不到状态不影响回 /status
                st["error"] = st.get("error") or "读不到关节角：%s" % e
        return st


_ARM = []
_ARM_MU = threading.Lock()


def arm():
    """臂的 worker，第一次用到时按环境变量建（import 时不碰 SDK）。"""
    with _ARM_MU:
        if not _ARM:
            _ARM.append(ArmWorker(make_backend(), skills_path=SKILLS_FILE))
        return _ARM[0]


def use_arm(worker):
    """换臂的 worker（测试用；以后换后端也走这里）。"""
    with _ARM_MU:
        _ARM[:] = [worker]
    return worker


def _mock_all():
    return arm().backend.mock and all(w.driver.mock for w in hands().values())


# caremate_hand2_kit 的动作名 → 仓库动作名。只映射手的动作：kit 的 /pose/<名字> 本来只动手，
# 映射完不能变成会动臂的动作（hold_card 在手#1 上 = 臂举牌，所以只认手#2）。
# thumbs_up 就是点赞手势（现场低风险验收姿态），不映射成会动臂的 sign_family_received
KIT_NAMES = {
    "comfort_rhythm": "gentle_hold",
    "rock": "rps_rock", "paper": "rps_paper", "scissors": "rps_scissors",
    "fingers_1": "finger_count_1", "fingers_2": "finger_count_2", "fingers_3": "finger_count_3",
    "fingers_4": "finger_count_4", "fingers_5": "finger_count_5",
    "hold_card": "sign_coming",
}
# kit 的整条动作端点 → (仓库动作名, 手)
KIT_ROUTES = {
    "/press_sos": ("press_sos", 1),
    "/handover_med": ("deliver_medicine", 1),
    "/return_med": ("return_medicine", 1),     # T5AI 大屏的「还药」键
    "/reset": ("home", 1),
    "/thumbs": ("thumbs_up", 1),
    "/card_up": ("sign_coming", 2),
    "/wave": ("wave", 1),
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/showcase"):
            from pathlib import Path
            body = Path(__file__).with_name("showcase.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/hand_positions":
            try:
                h = int(parse_qs(url.query).get("hand", ["1"])[0])
                d = hands()[h].driver
                return self._json(200, {"ok": True, "hand": h, "angles": d.read_angles(), "mock": d.mock})
            except (ValueError, KeyError, HandError) as e:
                return self._json(503, {"ok": False, "error": str(e)})
        if url.path.startswith("/static/"):   # 3D 视图用的 three.js：X5 现场没外网，由这里直接提供
            from pathlib import Path
            name = url.path[len("/static/"):]
            f = Path(__file__).with_name("static") / name
            if "/" in name or "\\" in name or not name.endswith(".js") or not f.is_file():
                return self._json(404, {"ok": False})
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            # three.js 库文件不会变，缓存一天；arm3d.js 随部署更新，每次都问一下
            self.send_header("Cache-Control", "max-age=86400" if name in ("three.module.min.js", "OrbitControls.js")
                             else "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/teach/model":   # 3D 视图：和安全检查同一套关节链、末端长度、限位
            from hand_controller.arm_kinematics import CHAIN
            a = arm()
            a._setup()
            return self._json(200, {"ok": True, "chain": [[n, list(xyz), list(axis)] for n, xyz, axis in CHAIN],
                                    "tool_length": a.cfg["tool_length"], "effector": a.effector,
                                    "lower_deg": S.deg(teach_limits()[0]), "upper_deg": S.deg(teach_limits()[1]),
                                    "base_limit_deg": TEACH_BASE_LIMIT, "floor_z": a.cfg.get("floor_z"),
                                    "keep_out": a.cfg["keep_out"]})
        if url.path == "/admin/restart_plan":
            try:
                return self._json(200, dict(restart_plan(), ok=True))
            except Exception as e:  # noqa: BLE001 算不出来也要告诉页面
                return self._json(200, {"ok": True, "moves": None, "why": "算不出回 rest 的路线：%s" % e})
        if url.path == "/teach/library":
            return self._json(200, dict(arm().library(), ok=True))
        if url.path == "/teach/recordings":
            return self._json(200, {"ok": True, "recordings": arm().recordings(), "bindings": arm().bindings(),
                                    "bindable": BINDABLE})
        if urlparse(self.path).path == "/status":
            return self._json(200, {"ok": True, "mock": _mock_all(), "verified": RM.VERIFIED,
                                    "hardware": {"hand_count": HAND_COUNT, "hand_layout": HAND_LAYOUT},
                                    "teaching": TEACH_MODE,
                                    "backend": "rh56_eb90", "arm_backend": arm().backend.name,
                                    "arm": arm().status(),
                                    "hands": {str(h): w.status() for h, w in hands().items()},
                                    "voice": voice_cmd.status(),
                                    "recent": HISTORY[-10:]})
        self._json(404, {"ok": False})

    def do_POST(self):
        global TEACH_MODE
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/teach/mode":
            if arm().busy():
                return self._json(409, {"ok": False, "error": "先等机械臂空闲或停止当前动作"})
            TEACH_MODE = q.get('on', ['0'])[0] == '1'
            return self._json(200, {"ok": True, "teaching": TEACH_MODE})
        if url.path == "/teach/script":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 200000:
                    return self._json(413, {"ok": False, "error": "脚本太大"})
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                name, missing = arm().save_script(q.get('name', [''])[0], body.get("steps"), body.get("voice"))
                return self._json(200, {"ok": True, "script_saved": name, "missing_poses": missing})
            except (ValueError, TypeError, KeyError, OSError) as e:
                return self._json(400, {"ok": False, "error": str(e)})
        if url.path == "/teach/pose_set":
            try:
                joints = dict(x.split(':') for x in q.get('joints', [''])[0].split(',') if x)
                q_deg = [float(joints[str(j)]) for j in range(1, 7)]
            except (ValueError, KeyError):
                return self._json(400, {"ok": False, "error": "要给 joints=1:..,2:..,…,6:..（六个关节角，°）"})
            try:
                return self._json(*arm().set_pose(q.get('name', [''])[0], q_deg))
            except OSError as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if url.path == "/teach/pose_route":
            try:
                return self._json(*arm().set_route(q.get('name', [''])[0], q.get('mode', ['direct'])[0],
                                                   float(q.get('cm', ['6'])[0]), q.get('via', [''])[0].split('|')))
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except OSError as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if url.path == "/teach/pose_lock":
            try:
                return self._json(*arm().lock_pose(q.get('name', [''])[0], q.get('on', ['1'])[0] == '1'))
            except OSError as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if url.path == "/teach/pose_delete":
            try:
                return self._json(*arm().delete_pose(q.get('name', [''])[0]))
            except OSError as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if url.path == "/teach/bind":
            try:
                return self._json(*arm().bind(q.get('action', [''])[0], q.get('name', [''])[0]))
            except OSError as e:
                return self._json(500, {"ok": False, "error": "保存绑定失败：%s" % e})
        if url.path.startswith('/teach/'):
            try:
                code, body = arm().teach(url.path.split('/')[-1],
                    joint=int(q.get('joint', ['0'])[0]), delta=float(q.get('delta', ['0'])[0]),
                    point=q.get('point', [''])[0], close=q.get('close', ['0'])[0] == '1',
                    speed=float(q.get('speed', ['0'])[0]) or None, name=q.get('name', [''])[0],
                    scale=float(q.get('scale', ['1'])[0]),
                    target=float(q['target'][0]) if 'target' in q else None,
                    start=int(q.get('start', ['0'])[0]),
                    targets={int(j): float(d) for j, d in (x.split(':') for x in q['joints'][0].split(',') if x)}
                    if 'joints' in q else None,
                    back=q.get('back', ['1'])[0] != '0')
                return self._json(code, body)
            except (ValueError, TypeError) as e:
                return self._json(400, {"ok": False, "error": str(e)})
        if url.path == "/hand/fingers":
            vals = {j: q[j][0] for j in FINGER_NAMES if j in q}
            try:
                return self._json(*set_fingers(int(q.get('hand', ['1'])[0]), vals, q.get('force', ['300'])[0]))
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
        if url.path.startswith("/screen/"):        # T5AI 大屏 v0.8 的键
            key = url.path[len("/screen/"):].strip("/")
            if key not in SCREEN_KEYS:
                return self._json(404, {"ok": False, "error": "大屏没有这个键：" + key})
            code, body = SCREEN_KEYS[key]()
            print("[大屏] %s -> %s %s" % (key, code, body.get("error") or ""), flush=True)
            return self._json(code, dict(body, key=key))
        if url.path == "/reset":                   # 旧版大屏的「归位」键：也改成零位待机点
            return self._json(*zero_standby("screen:/reset"))
        if url.path == "/admin/restart":
            plan = restart_plan()
            request_restart()
            return self._json(200, dict(plan, ok=True, restarting=True))
        if url.path == "/stop":
            stop_all()
            return self._json(200, {"ok": True, "stopped": True, "mock": _mock_all()})
        if url.path == "/pose":
            return self._pose(q.get("name", [""])[0], q.get("hand", ["1"])[0])
        if url.path.startswith("/pose/"):          # kit：/pose/<名字>?hand=
            kit = unquote(url.path[len("/pose/"):]).strip("/")
            name = KIT_NAMES.get(kit, kit)
            if name not in POSES:
                # kit 自己的约定是未知动作回 200 + ok:false；主脑用的查询写法回 400，ArmClient 才会记成"被拒绝"
                return self._json(200, {"ok": False, "error": "unknown pose: " + kit})
            hand = q.get("hand", [str(POSES[name]["hand"])])[0]
            if hand == "1" and name in ARM_SKILLS:
                return self._json(400, {"ok": False, "error": "/pose/<名字> 只做手的动作；「%s」会动臂，用 POST /pose?name=%s&hand=1"
                                                              % (label(name), name)})
            return self._pose(name, hand, alias_of=kit)
        if url.path in KIT_ROUTES:
            name, hand = KIT_ROUTES[url.path]
            return self._pose(name, str(hand), alias_of=url.path)
        if url.path == "/voice_cmd":
            n = int(self.headers.get("Content-Length") or 0)
            if n > voice_cmd.MAX_BYTES:
                return self._json(413, {"ok": False, "error": "录音太长（%d 字节）" % n})
            return self._json(*voice_cmd.handle(self.rfile.read(n), voice_dispatch, llm=(voice_preset, stop_all, voice_scripts, voice_script),
                                                 dry=q.get('dry', ['0'])[0] == '1'))
        self._json(404, {"ok": False})

    def _pose(self, name, hand_text, alias_of=None):
        self._json(*dispatch(name, hand_text, alias_of))

    def log_message(self, *args):
        pass


FINGER_NAMES = {"pinky": "小指", "ring": "无名指", "middle": "中指", "index": "食指", "thumb": "拇指弯曲",
                "thumb_rot": "拇指旋转"}


def clean_fingers(values, force):
    """单指控制的参数：只认 RH56 的六个关节，开合度 0（握紧）~100（张开）；力控 50 g ~ RM.FORCE_MAX。"""
    shape = {}
    for j, v in values.items():
        if j not in FINGER_NAMES:
            raise ValueError("不认识的手指：%s" % j)
        v = float(v)
        if not 0 <= v <= 100:
            raise ValueError("%s 开合度要 0~100" % FINGER_NAMES[j])
        shape[j] = round(v)
    force = int(force)
    if not 50 <= force <= RM.FORCE_MAX:
        raise ValueError("力控要 50~%d g" % RM.FORCE_MAX)
    return shape, force


def set_fingers(hand, values, force):
    """单指控制台：把给了的关节改成新开合度（没给的保持上一次），按力控走过去。手在臂上、臂正在用手时拒绝。"""
    try:
        shape, force = clean_fingers(values, force)
    except (ValueError, TypeError) as e:
        return 400, {"ok": False, "error": str(e)}
    if hand not in hands():
        return 400, {"ok": False, "error": "现场没有手#%s" % hand}
    if hand == 1 and arm().holds_hand1():
        return 409, {"ok": False, "error": "手#1 装在臂上，臂正在做「%s」" % label(arm().current)}
    w = hands()[hand]
    ok, why = w.ready()
    if not ok:
        return 503, {"ok": False, "error": "手#%d：%s" % (hand, why)}
    full = dict(w.driver.last or JOINTS["release"])
    full.update(shape)
    w.custom = (full, force)
    w.submit("_custom")
    a = arm()
    if hand == 1 and a.recording is not None:        # 录制：连续拖手指只记最后的手型
        step = {"fingers": dict(full), "force": force}
        if a.recording and "fingers" in a.recording[-1]:
            a.recording[-1].update(step)
        else:
            a.recording.append(step)
    return 200, {"ok": True, "fingers": full, "force": force}


def stop_all(source="/stop"):
    """急停：臂停住保持、双手停住，清掉排队、打断节拍。/stop 和语音「停下」都走这里。"""
    for w in hands().values():
        w.stop()
    arm().stop()
    print("[执行端] 急停 %s：臂停住保持、双手停住，清掉排队、打断节拍" % source, flush=True)
    _record_history({"ts": time.time(), "pose": "stop", "hand": 0})


def _script_summary(steps):
    """给语音理解模型看的脚本摘要：去哪些姿态、做哪些手部动作 / 预设（去重、最多 8 项）。"""
    out = []
    for st in steps:
        if "pose" in st:
            item = "去「%s」" % st["pose"]
        elif "hand" in st:
            item = HAND_PRESETS.get(st["hand"], (st["hand"],))[0]
        elif "preset" in st:
            item = PRESETS[st["preset"]]["label"] if st["preset"] in PRESETS else st["preset"]
        elif "fingers" in st:
            item = "手型"
        elif "grip" in st:
            item = "张开" if st["grip"] == "open" else "握取"
        else:
            continue
        if not out or out[-1] != item:
            out.append(item)
    return "、".join(out[:8]) + ("…" if len(out) > 8 else "")


def voice_scripts():
    """语音能触发的脚本：名字 → 「说法（页面上填的）；步骤：……」。"""
    out = {}
    for name, rec in arm().recordings().items():
        voice = (rec.get("voice") or "").strip()
        out[name] = ("说法：%s；" % voice if voice else "") + "步骤：" + (_script_summary(rec.get("steps", [])) or "（录制的动作）")
    return out


def voice_script(name, speed):
    """语音（大模型理解后）跑一个编排好的脚本：和页面「运行」同一条路，整条安全预检；默认原速。"""
    code, body = arm().teach("replay", name=name, scale=speed or 1.0)
    _record_history({"ts": time.time(), "pose": "script:" + name, "hand": 1, "requested": "voice"})
    return code, body


def zero_standby(source="screen"):
    """零位待机点：手张开，慢速回零位（全部关节 0°）。T5AI 大屏「零位待机」键、语音「归位」、页面按钮都走这里。"""
    code, body = arm().teach("start")
    _record_history({"ts": time.time(), "pose": "zero_standby", "hand": 1, "requested": source})
    return code, body


def voice_dispatch(name, hand_text, alias_of=None, requested=None):
    """语音的业务动作：「归位」改成零位待机点，其余照按键那条路。"""
    if name == "home":
        return zero_standby("voice")
    return dispatch(name, hand_text, alias_of, requested)


# T5AI 大屏 v0.8 的四个键（10-25 改版）：预设动作和语音同样的默认（1.5 倍速、做完停在原处）
SCREEN_KEYS = {
    "wave": lambda: voice_preset("wave", None, False, "screen"),
    "finger": lambda: dispatch("hand_exercise", "1", "/screen/finger", "screen:finger"),
    "shake": lambda: voice_preset("handshake", None, False, "screen"),
    "zero": lambda: zero_standby("screen"),
}


def voice_preset(name, scale, back, source="voice"):
    """语音（大模型理解后）做一个预设动作：和页面上的预设按钮同一条路，照样整条安全预检。"""
    from hand_controller.voice_llm import VOICE_DEFAULT_SCALE
    code, body = arm().teach("preset", name=name, scale=scale or VOICE_DEFAULT_SCALE, back=back)
    _record_history({"ts": time.time(), "pose": "preset:" + name, "hand": 1, "requested": source})
    return code, body


def dispatch(name, hand_text, alias_of=None, requested=None):
    """收下一个动作：(HTTP 码, 回包)。主脑、kit 端点、T5AI 按键和语音都走这里。"""
    requested = requested or (alias_of.lstrip("/") if alias_of else name)
    if name not in POSES:
        return 400, {"ok": False, "error": "unknown pose: " + requested}
    if TEACH_MODE and name in ARM_SKILLS:
        return 423, {"ok": False, "error": "正在点动示教；先完成示教再运行业务动作"}
    if name in ("arm_warmup", "arm_showcase", "gripper_cycle") and name not in ARM_SKILLS:
        return 422, {"ok": False, "error": "未加载展示点位文件 arm_showcase.json"}
    try:
        hand = int(hand_text)
    except ValueError:
        hand = 0
    if name == "gentle_hold":
        hand = 1 if HAND_COUNT == 1 else 2
    if hand not in hands():
        return 400, {"ok": False, "error": "现场没有手#%s；已配置 %d 只手" % (hand_text, HAND_COUNT)}
    replay = None
    if hand == 1 and name in ARM_SKILLS:
        a = arm()
        replay = a.bindings().get(name) if name in BINDABLE else None
        if replay:      # 绑定了录制：回放录制代替原来的业务动作（回放前照样整条安全预检）
            code, result = a.teach("replay", name=replay)
            if code != 200:
                return code, {"ok": False, "error": "「%s」绑定的录制「%s」没能回放：%s"
                                                    % (BINDABLE[name], replay, result.get("error"))}
        else:
            code, why = a.request(name)
            if code != 200:
                return code, {"ok": False, "error": why}
        mock = a.backend.mock
    else:
        if hand == 1 and arm().holds_hand1():
            return 409, {"ok": False, "error": "手#1 装在臂上，臂正在做「%s」" % label(arm().current)}
        w = hands()[hand]
        ok, why = w.ready()
        if not ok:
            return 503, {"ok": False, "error": "手#%d：%s" % (hand, why)}
        w.submit(name)
        mock = w.driver.mock
    _record_history(dict({"ts": time.time(), "pose": name, "hand": hand, "requested": requested},
                         **({"replay": replay} if replay else {})))
    body = {"ok": True, "pose": name, "requested": requested, "hand": hand, "mock": mock}
    if replay:
        body["replay"] = replay
    if alias_of:
        body["alias_of"] = alias_of
    return 200, body


def probe(out=print):
    """只读自检：每只手读一次实际角度 ANGLE_ACT、臂读一次关节角（都不写任何东西）。返回是否都读到了。"""
    good = True
    for h, w in hands().items():
        d = w.driver
        head = "手#%d（%s，ID %d）" % (h, "mock" if d.mock else d.port or "没配串口", d.slave)
        try:
            out("%s 实际角度（开合度 0 握紧~100 张开）= %s" % (head, d.read_angles()))
        except HandError as e:
            good = False
            out("%s 读不到：%s" % (head, e))
    b = arm().backend
    ok, why = b.connect()
    if not ok:
        out("臂（%s）读不到：%s" % (b.name, why))
        return False
    q = b.joints()
    out("臂（%s）关节角 = %s°，夹爪中心在 %s m" % (b.name, S.deg(q), [round(c, 3) for c in tool_position(q)]))
    return good


PARK_TOLERANCE = 0.05   # 离 rest 每个关节都在这么多 rad 以内，就算已经在 rest


def park(a, out=print):
    """退出前慢速回 rest（阻尼挡不住重力，抬着的 J2/J3 会往下沉）。路上不安全就不动；
    再按一次 Ctrl+C（或再来一个 SIGTERM）立刻停住保持。返回臂是否在 rest。"""
    b = a.backend
    ok, why = b.ready()
    if not ok:
        out("[臂] 退出：%s，不回 rest" % why)
        return False
    a._setup()
    rest = a.cfg["points"]["rest"]["q"]
    q = b.joints()
    if max(abs(x - r) for x, r in zip(q, rest)) <= PARK_TOLERANCE:
        return True
    try:
        route = park_route(q, rest, a.cfg)
    except S.SafetyError as e:
        out("[臂] 退出：%s，停在原地" % e)
        return False
    secs = [S.duration(p, w, a.cfg, slow=True) for p, (_, w) in zip([q] + [w for _, w in route], route)]
    out("[臂] 退出前慢速回 rest：%s（共 %.0f 秒）；再按一次 Ctrl+C 立刻停住"
        % (" → ".join(t for t, _ in route), sum(secs)))
    try:
        for (text, w), s in zip(route, secs):
            if not b.move(w, s, threading.Event()):
                break
        else:
            return True
    except KeyboardInterrupt:
        out("[臂] 停住")
    except ArmError as e:
        out("[臂] 回 rest 没成功：%s" % e)
    b.hold()
    return False


def park_route(q, rest, cfg):
    """回 rest 的分段路线：直走不安全时先抬起（保持手掌朝向垂直上移，或把手腕回正），再收拢落回 rest。"""
    from hand_controller.approach import translate_fixed
    lifts = []
    for dz in (.04, .08, .12):
        try:
            lifts.append(("垂直抬起 %d cm" % round(dz * 100), translate_fixed(q, dz=dz)))
        except S.SafetyError:
            pass
    lifts.append(("手腕回正", list(q[:3]) + [rest[3], rest[4], q[5]]))
    return S.plan_route(q, rest, cfg, lifts)


def restart_plan():
    """重启 arm_service 时臂会怎么动（页面在「重启服务」前给人看）：
    锁定 / 没连上 = 不动；在 rest = 不动；否则按 park_route 慢速回 rest；找不到安全路线 = 原地转阻尼（会下沉）。"""
    from hand_controller.arm_kinematics import body_points
    a = arm()
    b = a.backend
    ok, why = b.ready()
    if b.mock:
        return {"moves": False, "why": "模拟臂"}
    if not ok:
        return {"moves": False, "why": "臂%s，重启时不动" % why}
    a._setup()
    rest = a.cfg["points"]["rest"]["q"]
    q = b.joints()
    if max(abs(x - r) for x, r in zip(q, rest)) <= PARK_TOLERANCE:
        return {"moves": False, "why": "已经在 rest"}
    try:
        route = park_route(q, rest, a.cfg)
    except S.SafetyError as e:
        return {"moves": False, "sinks": True, "why": "%s：重启时原地转阻尼，抬着的臂会往下沉，先扶住臂" % e}
    secs, low, cur = 0.0, 9.0, q
    for _, w in route:
        secs += S.duration(cur, w, a.cfg, slow=True)
        for k in range(1, 31):
            p = [x + (y - x) * k / 30 for x, y in zip(cur, w)]
            low = min(low, min(v[2] for v in body_points(p, a.cfg["tool_length"]).values()))
        cur = w
    return {"moves": True, "route": [t for t, _ in route], "seconds": round(secs), "lowest_cm": round(low * 100)}


def request_restart(delay=0.5):
    """页面的「重启服务」：回完包再给自己发 SIGTERM —— 和 systemctl restart 同一条退出路
    （停住、按路线回 rest、转阻尼），systemd 的 Restart=always 两秒后再拉起来。"""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    print("[执行端] 页面要求重启 arm_service", flush=True)


def _shutdown():
    """退出：作废排队、停住，慢速回 rest，再转阻尼（进程退出时 SDK 也会让电机阻尼）。"""
    a = arm()
    a.stop()
    a.wait_idle(3.0)
    if a.backend.mock or not a.backend.status()["connected"]:
        return
    if not park(a, out=lambda s: print(s, flush=True)):
        print("[臂] 不在 rest：转阻尼后 J2/J3 会慢慢往下沉，扶住臂", flush=True)
    try:
        a.backend.brake()
    except Exception as e:  # noqa: BLE001
        print("[臂] 阻尼指令没发出去：%s（进程退出时 SDK 还会再发一次）" % e, flush=True)


def _terminate(*_):
    raise KeyboardInterrupt   # systemd stop 发 SIGTERM，和 Ctrl+C 走同一条退出路


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--probe", action="store_true", help="只读两只手的实际角度、臂的关节角就退出")
    args = ap.parse_args()
    if args.probe:
        raise SystemExit(0 if probe() else 1)
    for h, w in hands().items():
        ok, why = w.ready()
        d = w.driver
        print("手#%d：%s，ID %d，%s" % (h, "mock" if d.mock else "串口 " + (d.port or "（没配）"), d.slave,
                                     "可以动" if ok else "不能动：" + why), flush=True)
    a = arm()
    code, why = a.ready()   # 真臂在这里连 SDK（要几秒），别等第一个动作来了才连，主脑 1.5 秒就算超时
    print("臂：%s，末端 %s，%s" % (a.backend.name, a.effector or "未定", "可以动" if code == 200 else "不能动：" + why),
          flush=True)
    if code == 200 and not a.backend.mock:
        untaught = a.status().get("untaught")
        if untaught:
            print("臂：这些点位还没示教，用到它们的动作会被拒绝：%s" % "、".join(untaught), flush=True)
    threading.Thread(target=voice_cmd.preload, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    signal.signal(signal.SIGTERM, _terminate)
    print("arm_service on http://%s:%d（%s）" % (args.host, args.port, "mock" if _mock_all() else "真机"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _shutdown()


if __name__ == "__main__":
    main()
