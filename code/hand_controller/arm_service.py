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
  POST /press_sos  /handover_med  /reset  /thumbs  /card_up  /wave
  kit 名字 comfort_rhythm、rock、fingers_3、hold_card…… 换成仓库的动作名，见 KIT_NAMES / KIT_ROUTES。

环境变量（手的写法和 caremate_hand2_kit docs/20 一致）：
  ARM_BACKEND   mock（默认，臂只打印）| panthera；真臂见 arm_backend 的说明
  ARM_EFFECTOR  gripper | hand1（不设自动选）
  ARM_SKILLS_FILE  点位 / 动作文件，不设用 arm_skills.json；上电自检用 hand_controller/arm_selftest.json（小角度）
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
from hand_controller.arm_backend import TOLERANCE, ArmError, make_backend
from hand_controller.arm_kinematics import tool_position
from hand_controller.poses import JOINTS, POSES, RHYTHMS, SAFETY, SEQUENCES, label
from hand_controller.rh56f2_driver import RH56F2, HandError

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
            for h, default_id in ((1, RM.SLAVE_ID_HAND1), (2, RM.SLAVE_ID_HAND2)):
                driver = RH56F2(os.environ.get("HAND%d_PORT" % h, ""), int(os.environ.get("HAND%d_ID" % h, default_id)))
                _HANDS[h] = HandWorker(h, driver)
        return _HANDS


def _execute(pose, hand):
    """真正动手，在这只手的线程里跑（臂的动作归 ArmWorker）。查 poses.JOINTS（一次到位）、
    SEQUENCES（按顺序走一遍）或 RHYTHMS（循环节拍）。"""
    w = hands()[hand]
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
            try:
                ok = self._run_skill(name)
                if ok:
                    self.done += 1
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
        for kind, arg in cfg["skills"][name]:
            if self.abort.is_set():
                return False
            if kind == "move":
                p = cfg["points"][arg]
                q0 = self.backend.joints()
                S.check_segment(q0, p["q"], cfg, arg)
                slow = self.at is None or p.get("near_person") or cfg["points"][self.at].get("near_person")
                secs = S.duration(q0, p["q"], cfg, slow)
                self.step = "→ %s（%.1f 秒%s）" % (arg, secs, "，慢速" if slow else "")
                print("[臂%s] %s %s" % (" mock" if self.backend.mock else "", label(name), self.step), flush=True)
                if not self.backend.move(p["q"], secs, self.abort, p.get("torque_scale", 1.0)):
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
            elif kind == "wait":
                self.step = "等 %s 秒" % arg
                if self.backend.wait(float(arg), self.abort):
                    return False
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

    def holds_hand1(self):
        """手#1 装在臂上、臂正忙：手#1 不接别的动作。"""
        return self.effector == "hand1" and self.busy()

    def status(self):
        st = dict(self.backend.status(), effector=self.effector, busy=self.running is not None,
                  current=self.current, step=self.step, queued=len(self.pending), at=self.at,
                  done=self.done, aborted=self.aborted, last_error=self.last_error, settle=self.settle,
                  fail_seq=self.fail_seq, fail_pose=self.fail_pose)
        if self.cfg:
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
        if urlparse(self.path).path == "/status":
            return self._json(200, {"ok": True, "mock": _mock_all(), "verified": RM.VERIFIED,
                                    "backend": "rh56_eb90", "arm_backend": arm().backend.name,
                                    "arm": arm().status(),
                                    "hands": {str(h): w.status() for h, w in hands().items()},
                                    "recent": HISTORY[-10:]})
        self._json(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/stop":
            for w in hands().values():
                w.stop()
            arm().stop()
            print("[执行端] 急停 /stop：臂停住保持、双手停住，清掉排队、打断节拍", flush=True)
            _record_history({"ts": time.time(), "pose": "stop", "hand": 0})
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
        self._json(404, {"ok": False})

    def _pose(self, name, hand_text, alias_of=None):
        requested = alias_of.lstrip("/") if alias_of else name
        if name not in POSES:
            return self._json(400, {"ok": False, "error": "unknown pose: " + requested})
        try:
            hand = int(hand_text)
        except ValueError:
            hand = 0
        if name == "gentle_hold":
            hand = 2                 # 轻握安抚只归手#2
        if hand not in hands():
            return self._json(400, {"ok": False, "error": "hand 只能是 1 或 2"})
        if hand == 1 and name in ARM_SKILLS:
            a = arm()
            code, why = a.request(name)
            if code != 200:
                return self._json(code, {"ok": False, "error": why})
            mock = a.backend.mock
        else:
            if hand == 1 and arm().holds_hand1():
                return self._json(409, {"ok": False, "error": "手#1 装在臂上，臂正在做「%s」" % label(arm().current)})
            w = hands()[hand]
            ok, why = w.ready()
            if not ok:
                return self._json(503, {"ok": False, "error": "手#%d：%s" % (hand, why)})
            w.submit(name)
            mock = w.driver.mock
        _record_history({"ts": time.time(), "pose": name, "hand": hand, "requested": requested})
        body = {"ok": True, "pose": name, "requested": requested, "hand": hand, "mock": mock}
        if alias_of:
            body["alias_of"] = alias_of
        self._json(200, body)

    def log_message(self, *args):
        pass


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
        S.check_segment(q, rest, a.cfg, "rest")
    except S.SafetyError as e:
        out("[臂] 退出：回 rest 的路不安全（%s），停在原地" % e)
        return False
    secs = S.duration(q, rest, a.cfg, slow=True)
    out("[臂] 退出前慢速回 rest（%.0f 秒）；再按一次 Ctrl+C 立刻停住" % secs)
    try:
        if b.move(rest, secs, threading.Event()):
            return True
    except KeyboardInterrupt:
        out("[臂] 停住")
    except ArmError as e:
        out("[臂] 回 rest 没成功：%s" % e)
    b.hold()
    return False


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
