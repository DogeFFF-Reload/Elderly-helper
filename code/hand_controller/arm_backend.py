"""臂的后端：MockArm（笔记本、回归测试）和 PantheraArm（X5 上的真臂）。两者接口一样，arm_service 不分真假。

  connect()      → (ok, 原因)；真臂第一次调用时才建 Panthera 对象（会占 SDK 的单实例锁）
  ready()        → (ok, 原因)：连上了、电机层没越界锁定
  joints()       → 6 个关节角（rad）
  move(q, 秒, abort, torque_scale) → 走到 q 返回 True；abort 被置位就立刻返回 False（停在哪由调用方 hold）
  wait(秒, abort) → 技能里的停顿；被打断返回 True
  grip(close)    → 夹爪开合（只有装了夹爪才有）
  hold()         → 停在当前位置（位置模式，固件会一直执行这条指令）
  brake()        → 全部电机阻尼（关机前最后一步；J2/J3 会慢慢下沉，不会掉下来）

真臂只发位置类指令（moveJ / Joint_Pos_Vel / 夹爪位置），**不用 Joint_Vel**：X5 上 motor_timeout_ms 必须是 0
（固件超时后电机卡在 mode 11、只能断电恢复），所以 X5 一崩，电机会一直执行最后一条指令——
位置指令就停在原地保持，速度指令会一直转下去。

选后端：环境变量 ARM_BACKEND=mock（默认）| panthera。
真臂只在 X5 上跑，用装了 SDK 的 Python：~/panthera_venv/bin/python -m hand_controller.arm_service
  PANTHERA_SCRIPTS  SDK 的 panthera_python/scripts 目录，默认 ~/Panthera-HT_SDK/panthera_python/scripts
  PANTHERA_CONFIG   Follower.yaml 路径，不设用 SDK 默认（关节限位、电机层限位、失联保护都在那套配置里）
  MOCK_ARM_SPEED    mock 走一段要多久：0（默认）= 瞬间到；1 = 和真臂一样的时长（彩排看节奏用）

自检（X5 上，不碰电机）：python -m hand_controller.arm_backend --check-fk   拿 SDK 的 pinocchio 正解和 arm_kinematics 对一遍
"""
import argparse
import math
import os
import random
import sys
import threading
import time

from hand_controller.arm_kinematics import GRIPPER_TCP, body_points

REST = [0.0] * 6
TOLERANCE = 0.08     # 到位判定（rad，约 4.6°）；SDK 默认 0.1
# 位置模式（pos_vel_MAXtqe）没有重力前馈，带载的关节停稳后会差几度（2026-10-24 实测：J3 抬 15° 差 2.5°）。
# 规定时长到了、臂已经停稳（STILL_SECONDS 内每个关节变化都不到 STILL_RAD），差得不超过 SAG_TOLERANCE 也算到位，
# 差多少记进 last_errors；差得更多 = 被挡住
SAG_TOLERANCE = 0.15   # 约 8.6°
STILL_RAD = 0.004      # 约 0.2°
STILL_SECONDS = 0.4
SETTLE = 3.0         # 规定时长之后再等这么久还没到位、也没停稳在 SAG_TOLERANCE 内 = 被挡住了
HOLD_VEL = 0.2       # hold 时每个关节的速度上限（rad/s）：目标就是当前位置，只为把运动刹住
HOLD_CLIP = 0.0175   # 约 1°：当前位置超出限位不到这么多，夹进限位再保持；更多 = 真越界


class ArmError(Exception):
    """臂做不了 / 出了问题；消息给人看。"""


class MockArm:
    """只记账的臂。gate：测试用，给一个 Event，move 走完后要等它置位才算到位（模拟慢动作）。"""

    name = "mock"
    mock = True
    vel_limit = 1.0     # 和 Follower.yaml 的 velocity_limits 一样（rad/s）

    def __init__(self, time_scale=None, has_gripper=False, q=None):
        if time_scale is None:
            time_scale = float(os.environ.get("MOCK_ARM_SPEED", "0") or 0)
        self.time_scale = time_scale
        self.has_gripper = has_gripper
        self.q = list(q or REST)
        self.last_command = None
        self.moves = []     # (目标, 时长, torque_scale, 是否走完)
        self.last_errors = [0.0] * 6
        self.grips = []     # True = 合上
        self.holds = 0
        self.braked = False
        self.gate = None
        self.tripped = None   # 测试用：设成一句话 = 模拟电机层越界锁定

    def connect(self):
        return True, ""

    def ready(self):
        if self.tripped:
            return False, self.tripped
        return True, ""

    def joints(self):
        cmd = getattr(self, "_cmd", None)
        if cmd:   # 跟随模式的一帧还在走：按时间插值（time_scale = 0 时 command 直接到位，不会走到这里）
            start, goal, t0, total = cmd
            frac = min(1.0, (time.monotonic() - t0) / total) if total > 0 else 1.0
            self.q = [a + (b - a) * frac for a, b in zip(start, goal)]
            if frac >= 1.0:
                self._cmd = None
        return list(self.q)

    def move(self, q, seconds, abort, torque_scale=1.0):
        self._cmd = None
        self.last_command = list(q)
        start, total, t0 = list(self.q), seconds * self.time_scale, time.monotonic()
        while True:
            frac = 1.0 if total <= 0 else min(1.0, (time.monotonic() - t0) / total)
            self.q = [a + (b - a) * frac for a, b in zip(start, q)]
            if abort.is_set():
                self.moves.append((list(q), seconds, torque_scale, False))
                return False
            if frac >= 1.0 and (self.gate is None or self.gate.is_set()):
                break
            abort.wait(0.01)
        self.q = list(q)
        self.moves.append((list(q), seconds, torque_scale, True))
        return True

    def command(self, q, seconds, torque_scale=1.0):
        """跟随模式的一帧：不等走到。time_scale = 0 直接到位；否则从当前位置按时长插值，下一帧随时改目标。"""
        self.last_command = list(q)
        if self.time_scale > 0:
            self._cmd = (self.joints(), list(q), time.monotonic(), seconds * self.time_scale)
        else:
            self.q = list(q)
        self.moves.append((list(q), seconds, torque_scale, True))

    def wait(self, seconds, abort):
        """技能里的停顿，和走路一样按 time_scale 缩放；被打断返回 True。"""
        return abort.wait(seconds * self.time_scale) if self.time_scale > 0 else abort.is_set()

    def grip(self, close):
        if not self.has_gripper:
            raise ArmError("臂上没装夹爪")
        self.grips.append(bool(close))

    def hold(self):
        self.holds += 1

    def brake(self):
        self.braked = True

    def status(self):
        return {"backend": "mock", "mock": True, "connected": True, "has_gripper": self.has_gripper,
                "error": self.tripped}


class PantheraArm:
    """Panthera-HT（SDK 在 X5 的 ~/Panthera-HT_SDK，配合用户改过的 Panthera_lib：单实例锁、电机层越界锁定）。"""

    name = "panthera"
    mock = False

    def __init__(self, scripts_dir=None, config_path=None):
        self.scripts_dir = scripts_dir or os.environ.get("PANTHERA_SCRIPTS") or os.path.expanduser(
            "~/Panthera-HT_SDK/panthera_python/scripts")
        self.config_path = config_path or os.environ.get("PANTHERA_CONFIG") or None
        self.robot = None
        self.error = None
        self.last_errors = None   # 最近一次 move 结束时每个关节离目标多远（rad）
        self.last_command = None
        self._mu = threading.RLock()

    @property
    def has_gripper(self):
        return bool(self.robot is not None and getattr(self.robot, "has_gripper", False))

    @property
    def vel_limit(self):
        """SDK 的关节速度限幅（rad/s，取最小的一个）；没连上返回 None。"""
        lim = getattr(self.robot, "velocity_limits", None) if self.robot is not None else None
        return float(min(lim)) if lim is not None and len(lim) else None

    def connect(self):
        with self._mu:
            if self.robot is not None:
                return True, ""
            try:
                if self.scripts_dir not in sys.path:
                    sys.path.insert(0, self.scripts_dir)
                from Panthera_lib import Panthera
                self.robot = Panthera(self.config_path) if self.config_path else Panthera()
                self.joints()   # 先读一次状态，get_current_pos 才有数
                self.error = None
            except Exception as e:  # noqa: BLE001 SDK 没装、通信板没起来、另一个程序占着臂……都只是"连不上"
                self.robot = None
                self.error = "连不上臂：%s" % e
                return False, self.error
            return True, ""

    def ready(self):
        ok, why = self.connect()
        if not ok:
            return False, why
        if getattr(self.robot, "_hard_limit_tripped", False):
            return False, "电机层越界锁定（第 2 层限位）：对着零位标记把关节推回范围，再重启 arm_service"
        return True, ""

    # SDK（Panthera_lib + hightorque_robot）没有线程锁：臂线程在走、HTTP 线程查 /status 读关节，
    # 两边的"写发送缓存 + 发送"会交错。所有碰 SDK 的地方都拿 self._mu，一次只一个线程

    def joints(self):
        with self._mu:
            r = self.robot
            r.send_get_motor_state_cmd()
            r.motor_send_cmd()
            return [float(x) for x in r.get_current_pos()]

    def move(self, q, seconds, abort, torque_scale=1.0):
        r = self.robot
        max_tqu = [t * torque_scale for t in r.max_torque]
        with self._mu:
            accepted = r.moveJ(list(q), seconds, max_tqu=max_tqu, iswait=False)
        if accepted is False:
            raise ArmError("SDK 拒绝了这次 moveJ（多半是关节限位，看 arm_service 窗口里 SDK 打印的警告）")
        self.last_command = list(q)
        t0 = time.monotonic()
        deadline = t0 + seconds + SETTLE
        errors, still = [0.0], None   # still = (从什么时候起, 那时的偏差)：判断停稳
        while time.monotonic() < deadline:
            if abort.wait(0.02):
                return False
            with self._mu:
                if getattr(r, "_hard_limit_tripped", False):
                    raise ArmError("电机层越界锁定，全部电机已阻尼")
                reached, errors = r.check_position_reached(list(q), TOLERANCE)
            self.last_errors = [float(e) for e in errors]
            if reached:
                return True
            now = time.monotonic()
            if now - t0 < seconds:
                continue
            if still is None or max(abs(a - b) for a, b in zip(errors, still[1])) > STILL_RAD:
                still = (now, list(errors))
            elif now - still[0] >= STILL_SECONDS and max(errors) <= SAG_TOLERANCE:
                return True   # 停稳了，差的是重力下垂；调用方看 last_errors
        raise ArmError("%.1f 秒还没走到位（最大偏差 %.1f°）：可能被挡住了" % (
            seconds + SETTLE, math.degrees(max(errors))))

    def command(self, q, seconds, torque_scale=1.0):
        """跟随模式的一帧：moveJ 只发一帧位置 + 每关节速度（各关节同时到），不等走到；下一帧直接改目标。"""
        r = self.robot
        with self._mu:
            if getattr(r, "_hard_limit_tripped", False):
                raise ArmError("电机层越界锁定，全部电机已阻尼")
            accepted = r.moveJ(list(q), seconds, max_tqu=[t * torque_scale for t in r.max_torque], iswait=False)
        if accepted is False:
            raise ArmError("SDK 拒绝了这次 moveJ（多半是关节限位）")
        self.last_command = list(q)

    def wait(self, seconds, abort):
        return abort.wait(seconds)

    def grip(self, close):
        if not self.has_gripper:
            raise ArmError("臂上没装夹爪（Follower.yaml gripper_installed: false）")
        with self._mu:
            (self.robot.gripper_close if close else self.robot.gripper_open)()

    def hold(self):
        """停在当前位置。收拢时 J2/J3 可能读到 -0.04° 这样的小负值（编码器分辨率 0.036°），
        原样发出去会被 SDK 的第 1 层限位（下限正好 0）拒绝，所以先夹进限位；夹掉超过 HOLD_CLIP 才算真越界。"""
        with self._mu:
            cur = self.joints()
            lim = self.robot.joint_limits
            target = [min(max(x, float(lo)), float(hi)) for x, lo, hi in zip(cur, lim["lower"], lim["upper"])]
            off = max(abs(a - b) for a, b in zip(cur, target))
            if off > HOLD_CLIP:
                raise ArmError("当前位置超出关节限位 %.1f°，不能原地保持（看是不是越界锁定了）" % math.degrees(off))
            if self.robot.Joint_Pos_Vel(target, [HOLD_VEL] * len(target)) is False:
                raise ArmError("SDK 拒绝了停住保持指令")
            self.last_command = list(target)

    def brake(self):
        with self._mu:
            self.robot.set_brake()

    def status(self):
        st = {"backend": "panthera", "mock": False, "connected": self.robot is not None,
              "has_gripper": self.has_gripper, "error": self.error}
        if self.has_gripper and hasattr(self.robot, "get_current_pos_gripper"):
            with self._mu:
                st["gripper_position"] = float(self.robot.get_current_pos_gripper())
                st["gripper_torque"] = float(self.robot.get_current_torque_gripper())
        if self.robot is not None and getattr(self.robot, "_hard_limit_tripped", False):
            st["error"] = "电机层越界锁定"
        return st


def make_backend(kind=None):
    kind = kind or os.environ.get("ARM_BACKEND", "mock")
    if kind == "mock":
        return MockArm()
    if kind == "panthera":
        return PantheraArm()
    raise ValueError("ARM_BACKEND 只能是 mock 或 panthera，现在是 %r" % kind)


def check_fk(scripts_dir=None, samples=200, out=print):
    """拿 SDK 的 URDF + pinocchio 算正解，和 arm_kinematics 比；不连电机、不占单实例锁。返回最大误差（m）。"""
    import pinocchio as pin
    from hand_controller.arm_safety import JOINT_LOWER, JOINT_UPPER
    scripts_dir = scripts_dir or PantheraArm().scripts_dir
    urdf = os.path.normpath(os.path.join(scripts_dir, "..", "Panthera-HT_description_V1.1.0", "urdf",
                                         "Panthera-HT_description_V1.1.0_follower.urdf"))
    model = pin.buildModelFromUrdf(urdf)
    data = model.createData()
    frames = {"flange": model.getFrameId("link6"), "tool": model.getFrameId("gripper_center_link")}
    rnd = random.Random(0)
    worst = 0.0
    for k in range(samples):
        q = REST if k == 0 else [rnd.uniform(lo, hi) for lo, hi in zip(JOINT_LOWER, JOINT_UPPER)]
        qv = pin.neutral(model)
        for i, x in enumerate(q):
            qv[model.joints[model.getJointId("joint%d" % (i + 1))].idx_q] = x
        pin.forwardKinematics(model, data, qv)
        pin.updateFramePlacements(model, data)
        ours = body_points(q, GRIPPER_TCP)
        for part, fid in frames.items():
            sdk = data.oMf[fid].translation
            worst = max(worst, max(abs(a - b) for a, b in zip(ours[part], sdk)))
    out("正解对比 %d 组关节角：最大误差 %.6f m（%s）" % (samples, worst, "一致" if worst < 1e-3 else "不一致，别用禁区检查"))
    return worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-fk", action="store_true", help="和 SDK 的 pinocchio 正解对一遍（不碰电机）")
    args = ap.parse_args()
    if args.check_fk:
        raise SystemExit(0 if check_fk() < 1e-3 else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
