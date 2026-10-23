"""测试共用：可拨的假时钟、假执行端、建一条不落盘的总线。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

# 2026-10-23 10:00 本地时间附近；只要是固定值即可
T0 = 1792720800.0


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


def make_bus(data_dir=None, **overrides):
    import master_service
    clock = Clock()
    arm = FakeArm()
    bus = master_service.Bus(config.load(overrides), data_dir=data_dir, clock=clock, arm=arm)
    return bus, clock, arm


def said(bus):
    return [e["text"] for e in bus.events.items if e["type"] == "say"]


def events(bus, type_):
    return [e for e in bus.events.items if e["type"] == type_]
