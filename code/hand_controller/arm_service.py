"""臂 + 双手执行服务（:8013）。和主脑的约定不变（README §10.8、§10.11）：

POST /pose?name=press_sos&hand=1   收到就回 200，动作在这只手自己的线程里做；做不了回 4xx/503，error 写原因
POST /stop                         软件急停：清掉排队、打断节拍；之后的 /pose 照常接收（主脑急停期间不会发）
GET  /status                       在线 + mock + 每只手的驱动状态 + 最近动作

kit 兼容写法（只在 HTTP 边界换成仓库的规范名，主脑和 HISTORY 里始终是规范名）：
POST /pose/comfort_rhythm?hand=2   → gentle_hold；未知动作按 kit 的约定回 200 {"ok": false}
POST /press_sos  /handover_med  /reset  /card_up  /thumbs  /wave

手：rh56f2_driver（手册 §2.2 的 EB 90 协议），动作 → 五指开合度查 poses.JOINTS / RHYTHMS / SEQUENCES。
臂（Panthera-HT）：ARM_BACKEND=vendor_sdk 时交给 arm_backend_vendor.execute()；SDK 没装就 503，不伪装成功。

环境变量：
  MOCK_SERIAL   不设 = mock，只打印不碰串口；=0 走真串口（rh56_register_map.VERIFIED 之前只读，不发动作）
  HAND1_PORT / HAND1_ID   手#1 串口（COM7、/dev/ttyUSB0…）和手的 ID，默认 1
  HAND2_PORT / HAND2_ID   手#2，默认 ID 2（手出厂都是 1：各接各的串口时设 HAND2_ID=1）
  ARM_BACKEND   mock（默认，臂只打印）/ vendor_sdk（Panthera SDK）

运行：python -m hand_controller.arm_service [--host 0.0.0.0] [--port 8013]
     python -m hand_controller.arm_service --probe     只读：读两只手的实际角度（ANGLE_ACT）就退出（上 24V 后先跑这个）
"""
import argparse
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from config import utf8_console
from hand_controller import rh56_register_map as RM
from hand_controller.poses import JOINTS, POSES, RHYTHMS, SEQUENCES, label
from hand_controller.rh56f2_driver import RH56F2, HandError

HISTORY = []   # 收下的指令（按到达顺序），/status 和回归测试看它
HISTORY_LIMIT = 200
ARM_BACKEND = os.environ.get("ARM_BACKEND", "mock").strip().lower()

# kit 的动作名 → 仓库规范名（kit 词表里其余的名字 poses.POSES 里直接有）
POSE_ALIASES = {
    "rock": "rps_rock",
    "comfort_rhythm": "gentle_hold",
    "thumbs_up": "sign_family_received",
    "handover_med": "deliver_medicine",
}
PATH_ALIASES = {
    "/press_sos": "press_sos",
    "/handover_med": "deliver_medicine",
    "/reset": "home",
    "/card_up": "hold_card",
    "/thumbs": "thumbs_up",
    "/wave": "wave",
}
# 要动臂的动作 → Panthera 后端的轨迹名（arm_backend_vendor 的词表）
ARM_WAYPOINTS = {"press_sos": "press_sos", "deliver_medicine": "handover_med", "sign_coming": "sign_coming", "home": "reset"}


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
                print("[手#%d] %s 没做成：%s" % (self.hand, label(pose), e), flush=True)
            finally:
                self.running = None
                self.q.task_done()

    def status(self):
        return dict(self.driver.status(), current=self.current, busy=self.running is not None,
                    queued=self.q.qsize(), done=self.done, last_error=self.last_error)


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


def _arm_ready(pose):
    """要动臂的动作：vendor_sdk 后端要 SDK 真装上了；mock 后端只打印，总能。"""
    if pose not in ARM_WAYPOINTS or ARM_BACKEND == "mock":
        return True, ""
    if ARM_BACKEND != "vendor_sdk":
        return False, "ARM_BACKEND 只能是 mock 或 vendor_sdk（现在是 %s）" % ARM_BACKEND
    from hand_controller import arm_backend_vendor
    if not arm_backend_vendor.BRAND_SDK_IMPORTED:
        return False, "Panthera SDK 没装，臂的后端拒绝伪装成功"
    return True, ""


def _arm(pose, hand):
    """臂的那一半：vendor_sdk 交给 Panthera 槽位；mock 只打印。"""
    if pose not in ARM_WAYPOINTS:
        return
    if ARM_BACKEND != "vendor_sdk":
        print("[臂 mock] %s" % label(pose), flush=True)
        return
    from hand_controller.arm_backend_vendor import execute
    result = execute(ARM_WAYPOINTS[pose]) or {}
    if not result.get("ok", False):
        raise HandError("臂没做成：%s" % result.get("error", "backend rejected"))


def _execute(pose, hand):
    """真正动手，在这只手的线程里跑：臂（如有）+ 手。手查 poses.JOINTS（一次到位）、
    SEQUENCES（按顺序走一遍）或 RHYTHMS（循环节拍）。"""
    _arm(pose, hand)
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


def _mock_all():
    return all(w.driver.mock for w in hands().values()) and ARM_BACKEND == "mock"


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
                                    "backend": "rh56_eb90", "arm_backend": ARM_BACKEND,
                                    "hands": {str(h): w.status() for h, w in hands().items()},
                                    "recent": HISTORY[-10:]})
        self._json(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/stop":
            for w in hands().values():
                w.stop()
            print("[执行端] 急停 /stop：清掉排队、打断节拍", flush=True)
            _record_history({"ts": time.time(), "pose": "stop", "hand": 0})
            return self._json(200, {"ok": True, "stopped": True, "mock": _mock_all()})
        kit_route, name = False, None
        if url.path == "/pose":
            requested = q.get("name", [""])[0]
        elif url.path.startswith("/pose/"):
            kit_route = True
            requested = unquote(url.path[len("/pose/"):]).strip("/")
        elif url.path in PATH_ALIASES:
            requested, name = url.path.lstrip("/"), PATH_ALIASES[url.path]
        else:
            return self._json(404, {"ok": False})
        name = name or POSE_ALIASES.get(requested, requested)
        if name not in POSES:
            # kit 自己的约定是未知动作回 200 + ok:false；主脑用的查询写法回 400，ArmClient 才会记成"被拒绝"
            return self._json(200 if kit_route else 400, {"ok": False, "error": "unknown pose: " + requested})
        try:
            hand = int(q.get("hand", [POSES[name].get("hand", 1)])[0])
        except ValueError:
            hand = 0
        if name == "gentle_hold":
            hand = 2                 # 轻握安抚只归手#2
        if hand not in hands():
            return self._json(400, {"ok": False, "error": "hand 只能是 1 或 2"})
        w = hands()[hand]
        for ok, why in (w.ready(), _arm_ready(name)):
            if not ok:
                return self._json(503, {"ok": False, "error": "手#%d：%s" % (hand, why)})
        _record_history({"ts": time.time(), "pose": name, "hand": hand, "requested": requested})
        w.submit(name)
        self._json(200, {"ok": True, "pose": name, "requested": requested, "hand": hand, "mock": w.driver.mock})

    def log_message(self, *args):
        pass


def probe(out=print):
    """只读自检：每只手读一次实际角度 ANGLE_ACT（一条读指令，不写任何东西）。返回是否都读到了。"""
    good = True
    for h, w in hands().items():
        d = w.driver
        head = "手#%d（%s，ID %d）" % (h, "mock" if d.mock else d.port or "没配串口", d.slave)
        try:
            out("%s 实际角度（开合度 0 握紧~100 张开）= %s" % (head, d.read_angles()))
        except HandError as e:
            good = False
            out("%s 读不到：%s" % (head, e))
    return good


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--probe", action="store_true", help="只读两只手的实际角度就退出")
    args = ap.parse_args()
    if args.probe:
        raise SystemExit(0 if probe() else 1)
    for h, w in hands().items():
        ok, why = w.ready()
        d = w.driver
        print("手#%d：%s，ID %d，%s" % (h, "mock" if d.mock else "串口 " + (d.port or "（没配）"), d.slave,
                                     "可以动" if ok else "不能动：" + why), flush=True)
    print("臂：ARM_BACKEND=%s" % ARM_BACKEND, flush=True)
    print("arm_service on http://%s:%d（%s）" % (args.host, args.port, "mock" if _mock_all() else "真机"), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
