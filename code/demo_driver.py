"""演示驱动：按 §3"一天"主线把演示自动走一遍，每一步都检查结果、计时。

  python -m demo_driver                      # 连 :8000 跑主线：health → medicine → night_fall
  python -m demo_driver --pause              # 每段前等回车：演示员一键一段，跟主讲节奏
  python -m demo_driver --all                # 主线 + 手指操（P2，展台用）
  python -m demo_driver --steps exercise     # 只跑某几段
  python -m demo_driver --server http://192.168.1.20:8000   # Day1 联调冒烟
  python -m demo_driver --local              # 不连服务：进程内假时钟，几秒跑完（回归测试也用它）
  python -m demo_driver --list

只走公开接口（/mock/* 和 /api/*），和操作台按钮、视觉桥、ASR 桥是同一入口。
每段打印"用时 / §3 预算"，彩排时看哪段超时。任何一步没过，退出码非 0。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import config

STEPS = {}  # name -> (标题, §3 预算秒数, fn)
MAIN_LINE = ("health", "medicine", "night_fall")


def step(name, title, budget):
    def deco(fn):
        STEPS[name] = (title, budget, fn)
        return fn
    return deco


def _arm_moved(events, pose):
    return any(e["type"] == "arm" and e.get("data", {}).get("pose") == pose for e in events)


@step("health", "0:20 早上：量血压（康）", 30)
def _health(d):
    before = len(d.state()["health"]["today"]["bp"])
    d.say("我刚量了血压 138比85")
    d.expect("复述“高压 138、低压 85，对吗？”，还没记账",
             lambda s, ev: (s["health"]["pending"] or {}).get("sys") == 138
             and len(s["health"]["today"]["bp"]) == before)
    d.wait(1.5)
    d.say("对")
    d.expect("老人说“对”才记账，138/85 标“偏高”",
             lambda s, ev: len(s["health"]["today"]["bp"]) == before + 1
             and s["health"]["today"]["bp"][-1]["flag"] == "偏高")


@step("medicine", "0:50 中午：吃药（递）", 30)
def _medicine(d):
    d.say("把药递给我")
    d.expect("臂 + 手#1 递出药盒", lambda s, ev: _arm_moved(ev, "deliver_medicine"))


@step("night_fall", "1:20 夜里：起夜摔倒（救）", 70)
def _night_fall(d):
    d.post("/api/night", {"mode": "on"})
    d.expect("大屏亮 🌙（时间覆盖开关）", lambda s, ev: s["night"]["active"])
    d.post("/mock/fall", {"room": "卧室"})
    d.expect("骨架倒地 → 语音询问", lambda s, ev: s["guard"]["state"] == "ASKING")
    wait = d.state()["params"]["ask_wait_night"]
    d.expect("%g 秒无回应 → 臂按 SOS、手#2 轻握、通知女儿" % wait,
             lambda s, ev: s["guard"]["state"] == "SOS" and _arm_moved(ev, "press_sos")
             and any(n["kind"] == "fall" for n in s["notifications"]),
             timeout=wait + 3)
    d.wait(3)  # 留给观众看家属端弹出紧急卡
    d.post("/api/family/ack", {"contact": d.state()["contacts"][0]["id"]})
    d.expect("家属点“我收到了” → 手#1 举牌“家人已收到”、手#2 举牌“女儿马上到”",
             lambda s, ev: s["guard"]["state"] == "HELP_COMING"
             and _arm_moved(ev, "sign_family_received") and _arm_moved(ev, "sign_coming"))


@step("exercise", "展台：手指操（陪）", 40)
def _exercise(d):
    if d.state()["guard"]["state"] != "IDLE":
        # 保命链没结束时臂归状态机，手指操会被 supervisor 挂起；展台段单独演示，先回空闲态
        d.post("/api/reset", {"all": "0"})
        d.out("  ↺ 救援场景还没结束，先重置回空闲态")
    d.say("做个手指操吧")
    d.expect("开始带练", lambda s, ev: s["exercise"]["active"])
    beat = d.state()["params"]["exercise_beat_seconds"]
    d.expect("13 拍全部口播、做完自动收手",
             lambda s, ev: not s["exercise"]["active"]
             and sum(1 for e in ev if e["type"] == "say" and e["text"].startswith("第")) == 13,
             timeout=3 + 13 * beat + 3)


class Driver:
    def __init__(self, target, pause=False, out=print):
        self.t = target
        self.pause = pause
        self.out = out
        self.failures = []
        self.step_since = 0

    def say(self, text):
        self.out("  🗣  老人：“%s”" % text)
        self.t.post("/mock/voice", {"text": text})

    def post(self, path, body=None):
        return self.t.post(path, body or {})

    def state(self):
        return self.t.state()

    def wait(self, seconds):
        self.t.wait(seconds)

    def expect(self, desc, pred, timeout=5.0):
        """轮询直到 pred(state, 本段事件) 为真；超时记一笔失败，继续往下跑。"""
        waited = 0.0
        while True:
            if pred(self.t.state(), self.t.events(self.step_since)):
                self.out("  ✓ " + desc)
                return True
            if waited >= timeout:
                self.out("  ✗ %s（等了 %.0f 秒）" % (desc, waited))
                self.failures.append(desc)
                return False
            self.t.wait(0.2)
            waited += 0.2

    def run(self, names, reset=True):
        if reset:
            self.t.post("/api/reset", {"all": "0"})
            self.out("↺ 已重置（健康记录保留）")
        for name in names:
            title, budget, fn = STEPS[name]
            if self.pause:
                input("\n回车开始「%s」… " % title)
            self.out("\n▶ " + title)
            self.step_since = self.t.last_id()
            t0 = self.t.now()
            fn(self)
            used = self.t.now() - t0
            self.out("  用时 %.1f 秒 / §3 预算 %d 秒%s" % (used, budget, "（超了）" if used > budget else ""))
        return not self.failures


class HttpTarget:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _req(self, method, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path, body):
        return self._req("POST", path, body)

    def state(self):
        return self._req("GET", "/api/state")

    # client=demo_driver：别让节点自检把演示驱动当成喇叭那头的 tts_player
    def events(self, since):
        return self._req("GET", "/api/events?since=%d&client=demo_driver" % since)["events"]

    def last_id(self):
        return self._req("GET", "/api/events?since=999999999&client=demo_driver")["last_id"]  # 只要编号，不拉事件

    def wait(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.monotonic()


class _SimClock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class _NullArm:
    online = True

    def send(self, pose, hand):
        pass

    def status(self):
        return {"online": True, "url": "local"}


class LocalTarget:
    """进程内总线 + 假时钟：不连网、不落盘、不等真时间。台词强制 scripted，结果可复现。"""

    def __init__(self, cfg=None):
        import master_service
        self.ms = master_service
        self.clock = _SimClock()
        cfg = cfg or config.load({"llm_mode": "scripted"})
        self.bus = master_service.Bus(cfg, clock=self.clock, arm=_NullArm())

    def post(self, path, body):
        return self.ms.dispatch_post(self.bus, path, body)

    def state(self):
        return self.bus.snapshot()

    def events(self, since):
        with self.bus.lock:
            return self.bus.events.since(since)

    def last_id(self):
        return self.bus.events.next_id - 1

    def wait(self, seconds):
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.t = min(end, self.clock.t + 0.2)
            self.bus.tick()

    def now(self):
        return self.clock()


def main(argv=None):
    config.utf8_console()
    ap = argparse.ArgumentParser(description="按 §3 主线自动走一遍演示并检查结果")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--local", action="store_true", help="不连服务，进程内假时钟跑")
    ap.add_argument("--steps", default=",".join(MAIN_LINE), help="逗号分隔，默认主线")
    ap.add_argument("--all", action="store_true", help="主线 + 手指操")
    ap.add_argument("--pause", action="store_true", help="每段前等回车")
    ap.add_argument("--no-reset", action="store_true", help="开始前不重置")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        for name, (title, budget, _) in STEPS.items():
            print("  %-11s %s（预算 %d 秒）%s" % (name, title, budget, "  ← 主线" if name in MAIN_LINE else ""))
        return 0
    names = list(STEPS) if args.all else [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [n for n in names if n not in STEPS]
    if unknown:
        print("没有这一段：%s（--list 看全部）" % "、".join(unknown))
        return 2

    target = LocalTarget() if args.local else HttpTarget(args.server)
    if not args.local:
        try:
            target.state()
        except (urllib.error.URLError, OSError):
            print("连不上 %s —— 先另开终端 python -m master_service" % args.server)
            return 2
    driver = Driver(target, pause=args.pause)
    try:
        ok = driver.run(names, reset=not args.no_reset)
    except (urllib.error.URLError, OSError) as e:
        print("\n和主脑的连接断了：%s —— 检查 master_service 是否还在跑" % e)
        return 2
    print()
    if ok:
        print("演示全流程通过（%s）" % " → ".join(names))
        return 0
    print("有 %d 项没过：" % len(driver.failures))
    for f in driver.failures:
        print("  ✗ " + f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
