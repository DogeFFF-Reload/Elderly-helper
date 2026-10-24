"""用药提醒闭环：定时提醒 → 递药 → 服药打卡 → 家属端可见；过时没打卡算漏服，通知家属。

和保命链完全分开：只产出台词 / 事件 / 通知（和状态机一样返回 acts，由总线执行），不碰臂、不碰状态机。
保命链进行中（询问 / SOS / 等家属）不提醒、不判漏服，等回到空闲再说。

开关 cfg["med_reminder"]：off（默认）/ on；运行中可经 /api/med/enable 切换。
定时按本机时钟（med_times，"HH:MM"）；演示用操作台“用药提醒”按钮当场触发，不依赖钟点。
"""
import time

STATE_TEXT = {"reminded": "已提醒", "delivered": "已递药", "taken": "已服", "missed": "漏服"}
SCHEDULE_GRACE = 30 * 60   # 到点后这么多秒内还能补提醒；主脑晚于这个时间才启动，就当这顿已经过了


def _ev(type_, text, level="info"):
    return {"do": "event", "type": type_, "text": text, "level": level, "who": "system"}


def _say(key, **params):
    return {"do": "say", "key": key, "params": params}


def parse_hhmm(s):
    """"08:00" → (8, 0)；格式不对抛 ValueError。"""
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(s)
    return h, m


class MedReminder:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.enabled = cfg["med_reminder"] == "on"
        self.fired = set()        # 已经提醒过的定时（"日期 HH:MM"）：重置演示也不重提
        self.reset()

    def reset(self):
        self.doses = []
        self.next_id = 1

    @property
    def name(self):
        return self.cfg["med_name"]

    @property
    def primary(self):
        return self.cfg["contacts"][0]

    @property
    def pending(self):
        """正在等打卡的那一顿（已提醒 / 已递药），没有就 None。"""
        d = self.doses[-1] if self.doses else None
        return d if d and d["state"] in ("reminded", "delivered") else None

    # ---------- 输入 ----------

    def set_enabled(self, on):
        on = bool(on)
        if on == self.enabled:
            return []
        self.enabled = on
        return [_ev("med", "用药提醒已%s（%s）" % ("开启" if on else "关闭", self.name), "ok" if on else "info")]

    def remind(self, slot=None, source="console"):
        """提醒一次。slot = 定时的 "HH:MM"，操作台手动触发时为 None（显示当前时间）。"""
        now = self.clock()
        p = self.pending
        if p:   # 上一顿还没打卡：再提醒一声，不新开一顿
            return [_ev("med", "再次提醒：%s（%s）还没打卡" % (self.name, p["slot"])), _say("med_remind", med=self.name)]
        slot = slot or time.strftime("%H:%M", time.localtime(now))
        self.doses.append({"id": self.next_id, "name": self.name, "slot": slot, "ts": now, "source": source,
                           "state": "reminded", "delivered": False, "taken_ts": None})
        self.next_id += 1
        del self.doses[:-20]
        return [_ev("med", "用药提醒：%s（%s）" % (self.name, slot)), _say("med_remind", med=self.name)]

    def on_delivered(self):
        """老人要药、臂递出药盒之后调：有待打卡的一顿就标成已递药，并提醒吃完说一声。"""
        p = self.pending
        if not p:
            return []
        p["delivered"] = True
        p["state"] = "delivered"
        return [_say("med_ask_taken", med=self.name)]

    def taken(self):
        p = self.pending
        if not p:
            return [_say("med_none")]
        p["state"] = "taken"
        p["taken_ts"] = self.clock()
        at = time.strftime("%H:%M", time.localtime(p["taken_ts"]))
        return [_ev("med", "服药打卡：%s（%s 的那顿）%s 已服" % (self.name, p["slot"], at), "ok"),
                {"do": "notify", "contact": self.primary["id"], "kind": "med",
                 "text": "%s按时吃了%s（%s 提醒，%s 打卡）" % (self.cfg["elder_title"], self.name, p["slot"], at)},
                _say("med_taken", med=self.name)]

    def not_yet(self):
        return [_say("med_not_yet")] if self.pending else [_say("med_none")]

    # ---------- 时钟 ----------

    def tick(self, idle):
        """idle = 状态机空闲。保命链进行中什么都不做（不提醒、不判漏服）。"""
        if not self.enabled or not idle:
            return []
        now = self.clock()
        acts = []
        p = self.pending
        if p and now - p["ts"] > self.cfg["med_missed_after"]:
            p["state"] = "missed"
            acts += [_ev("med", "漏服：%s（%s）提醒后 %d 分钟没打卡，已通知%s" % (
                         self.name, p["slot"], round((now - p["ts"]) / 60), self.primary["name"]), "warn"),
                     {"do": "notify", "contact": self.primary["id"], "kind": "med_missed",
                      "text": "%s %s 的%s还没吃（提醒后没有打卡），方便的话打个电话问问" % (
                          self.cfg["elder_title"], p["slot"], self.name)},
                     _say("med_missed", med=self.name)]
        lt = time.localtime(now)
        day = time.strftime("%Y-%m-%d", lt)
        for slot in self.cfg["med_times"]:
            key = "%s %s" % (day, slot)
            if key in self.fired:
                continue
            h, m = parse_hhmm(slot)
            due = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
            if now < due:
                continue
            self.fired.add(key)
            if now - due <= SCHEDULE_GRACE and not self.pending:
                acts += self.remind(slot, source="schedule")
        return acts

    # ---------- 输出 ----------

    def snapshot(self):
        p = self.pending
        return {"enabled": self.enabled, "name": self.name, "times": list(self.cfg["med_times"]),
                "missed_after": self.cfg["med_missed_after"],
                "pending": dict(p, state_text=STATE_TEXT[p["state"]]) if p else None,
                "doses": [dict(d, state_text=STATE_TEXT[d["state"]]) for d in self.doses[-8:]]}
