"""测试共用：可拨的假时钟、假执行端、建一条不落盘的总线。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

# 本机时区的 2026-10-23 10:00。按本机时区算，不写死时间戳：
# 写死的话换到 UTC / 太平洋时区的机器上，"上午"会变成夜里，夜间相关的测试就挂了
T0 = time.mktime((2026, 10, 23, 10, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds, bus=None, step=0.2):
        """推进时间；给了 bus 就像真实 ticker 一样每 step 秒 tick 一次。"""
        end = self.t + seconds
        while self.t < end - 1e-9:
            self.t = min(end, self.t + step)
            if bus is not None:
                bus.tick()


class FakeArm:
    online = True

    def __init__(self):
        self.sent = []

    def send(self, pose, hand):
        self.sent.append((pose, hand))

    def poses(self):
        return [p for p, _ in self.sent]

    def status(self):
        return {"online": self.online, "url": "fake"}


def make_bus(data_dir=None, llm_transport=None, arm=None, **overrides):
    """llm_transport 给了就开 llm 通道（假接口：fn(messages, timeout) -> str）。
    arm 不给就用 FakeArm（全部成功、只记录）。
    配置只用 DEFAULTS + overrides，不受本机 config.local.json / 环境变量影响。"""
    import master_service
    from agent.llm_client import LLMClient
    clock = Clock()
    arm = arm if arm is not None else FakeArm()
    if llm_transport is not None:
        overrides.setdefault("llm_mode", "llm")
    cfg = config.load(overrides, use_local=False)
    llm = LLMClient.from_config(cfg, transport=llm_transport) if llm_transport is not None else None
    bus = master_service.Bus(cfg, data_dir=data_dir, clock=clock, arm=arm, llm=llm)
    return bus, clock, arm


def said(bus):
    return [e["text"] for e in bus.events.items if e["type"] == "say"]


def events(bus, type_):
    return [e for e in bus.events.items if e["type"] == type_]
