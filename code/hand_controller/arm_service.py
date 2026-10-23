"""臂 + 双手执行服务（:8013）。和主脑的约定不变（README §10.8、§10.11）：

POST /pose?name=press_sos&hand=1   收到就回 200，动作在这只手自己的线程里做；做不了回 4xx/503，error 写原因
POST /stop                         软件急停：清掉排队、打断节拍；之后的 /pose 照常接收（主脑急停期间不会发）
GET  /status                       在线 + 每只手的驱动状态 + 最近动作

手：rh56f2_driver（Modbus RTU），动作 → 五指开合度查 poses.JOINTS / poses.RHYTHMS。
臂（Panthera-HT）：SDK 只在 X5 上跑，还没接，臂的部分先只打印。

环境变量（和 caremate_hand2_kit docs/20 的写法一致）：
  MOCK_SERIAL   不设 = mock，只打印不碰串口；=0 走真串口（rh56_register_map.VERIFIED 之前只读，不发动作）
  HAND1_PORT / HAND1_ID   手#1 串口（COM5、/dev/ttyUSB0…）和从站号，默认 1
  HAND2_PORT / HAND2_ID   手#2，默认从站 2；两只手在同一条总线上就填同一个口

运行：python -m hand_controller.arm_service [--host 0.0.0.0] [--port 8013]
     python -m hand_controller.arm_service --probe     只读：读两只手的状态寄存器就退出（上 24V 后先跑这个）
"""
import argparse
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import utf8_console
from hand_controller import rh56_register_map as RM
from hand_controller.poses import JOINTS, POSES, RHYTHMS, label
from hand_controller.rh56f2_driver import RH56F2, ModbusError

HISTORY = []   # 收下的指令（按到达顺序），/status 和回归测试看它


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


def _execute(pose, hand):
    """真正动手，在这只手的线程里跑。查 poses.JOINTS（一次到位）或 poses.RHYTHMS（循环节拍）。"""
    w = hands()[hand]
    if pose in RHYTHMS:
        r = RHYTHMS[pose]
        if r.get("force") is not None:
            try:
                w.driver.set_force(r["force"])
            except ModbusError as e:   # 力度寄存器还没核对：照样做，开合度本身已经限着
                print("[手#%d] 力度上限没写进去：%s" % (hand, e), flush=True)
        end = time.monotonic() + r["max_seconds"]
        print("[手#%d] %s：节拍开始（最长 %d 秒）" % (hand, label(pose), r["max_seconds"]), flush=True)
        while time.monotonic() < end and not w.interrupted():
            for joints, hold in r["steps"]:
                w.driver.move(joints)
                if w.pause(hold):
                    return
        return
    w.driver.move(JOINTS[pose])
    print("[手#%d%s] %s" % (hand, " mock" if w.driver.mock else "", label(pose)), flush=True)


def _mock_all():
    return all(w.driver.mock for w in hands().values())


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
                                    "hands": {str(h): w.status() for h, w in hands().items()},
                                    "recent": HISTORY[-10:]})
        self._json(404, {"ok": False})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/stop":
            for w in hands().values():
                w.stop()
            print("[执行端] 急停 /stop：清掉排队、打断节拍", flush=True)
            HISTORY.append({"ts": time.time(), "pose": "stop", "hand": 0})
            return self._json(200, {"ok": True, "stopped": True, "mock": _mock_all()})
        if url.path != "/pose":
            return self._json(404, {"ok": False})
        q = parse_qs(url.query)
        name = q.get("name", [""])[0]
        if name not in POSES:
            return self._json(400, {"ok": False, "error": "unknown pose: " + name})
        try:
            hand = int(q.get("hand", ["1"])[0])
        except ValueError:
            hand = 0
        if hand not in hands():
            return self._json(400, {"ok": False, "error": "hand 只能是 1 或 2"})
        w = hands()[hand]
        ok, why = w.ready()
        if not ok:
            return self._json(503, {"ok": False, "error": "手#%d：%s" % (hand, why)})
        HISTORY.append({"ts": time.time(), "pose": name, "hand": hand})
        del HISTORY[:-200]
        w.submit(name)
        self._json(200, {"ok": True, "pose": name, "hand": hand, "mock": w.driver.mock})

    def log_message(self, *args):
        pass


def probe(out=print):
    """只读自检：每只手读一次状态寄存器。返回是否都读到了。"""
    good = True
    for h, w in hands().items():
        d = w.driver
        head = "手#%d（%s，从站 %d）" % (h, "mock" if d.mock else d.port or "没配串口", d.slave)
        try:
            out("%s 状态寄存器 = %s" % (head, d.read_status()))
        except ModbusError as e:
            good = False
            out("%s 读不到：%s" % (head, e))
    return good


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--probe", action="store_true", help="只读两只手的状态寄存器就退出")
    args = ap.parse_args()
    if args.probe:
        raise SystemExit(0 if probe() else 1)
    for h, w in hands().items():
        ok, why = w.ready()
        d = w.driver
        print("手#%d：%s，从站 %d，%s" % (h, "mock" if d.mock else "串口 " + (d.port or "（没配）"), d.slave,
                                     "可以动" if ok else "不能动：" + why), flush=True)
    print("arm_service on http://%s:%d（%s）" % (args.host, args.port, "mock" if _mock_all() else "真机"), flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
