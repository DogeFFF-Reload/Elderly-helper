"""保命状态机：跌倒 → 询问 → 升级 → SOS → 家属确认；打 120 双重确认。

这里垄断所有"能不能做"。LLM/agent 只能调用公开方法提请求，
返回的是动作列表（由总线执行），本模块不碰网络、不碰硬件，时钟可注入便于测试。

动作格式（dict）：
  {"do": "say",    "key": 台词键, "params": {...}}
  {"do": "event",  "type": ..., "text": ..., "level": info|ok|warn|danger}
  {"do": "arm",    "pose": ..., "hand": 1|2}
  {"do": "notify", "contact": id, "kind": fall|cancel|120|call, "text": ...}
  {"do": "dial",   "number": "120"}
"""
import time

IDLE = "IDLE"
ASKING = "ASKING"
SOS = "SOS"
HELP_COMING = "HELP_COMING"


def say(key, **params):
    return {"do": "say", "key": key, "params": params}


def event(type_, text, level="info"):
    return {"do": "event", "type": type_, "text": text, "level": level}


def arm(pose, hand=1):
    return {"do": "arm", "pose": pose, "hand": hand}


def notify(contact, kind, text):
    return {"do": "notify", "contact": contact, "kind": kind, "text": text}


class GuardFSM:
    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.contacts = cfg["contacts"]
        self.title = cfg.get("elder_title", "老人")
        self.reset()

    def reset(self):
        self.state = IDLE
        self.room = None
        self.night = False
        self.wait = None
        self.deadline = None
        self.contact_idx = 0
        self.ack_deadline = None
        self.acked_by = None
        self.dial_deadline = None

    # ---------- 跌倒链 ----------

    def fall_detected(self, room, night):
        if self.state != IDLE:
            return [event("fall", "再次检测到倒地（处理中，不重复升级）", "warn")]
        self.state = ASKING
        self.room = room
        self.night = night
        self.wait = self.cfg["ask_wait_night"] if night else self.cfg["ask_wait_day"]
        self.deadline = self.clock() + self.wait
        mode = "夜间" if night else "日间"
        return [
            event("fall", "检测到%s有人倒地（骨架规则）" % room, "danger"),
            event("ask", "%s模式：语音询问，等待 %g 秒" % (mode, self.wait), "warn"),
            say("ask_ok_night" if night else "ask_ok", room=room, wait=int(self.wait)),
        ]

    def elder_ok(self, via):
        if self.state == ASKING:
            self._clear_guard()
            return [
                event("ok", "老人回应没事（%s），已取消升级" % via, "ok"),
                say("cancel_ok"),
            ]
        if self.state in (SOS, HELP_COMING):
            notified = self.contacts[: self.contact_idx + 1]
            self._clear_guard()
            acts = [event("ok", "老人表示没事（%s），已告知家属" % via, "ok")]
            for c in notified:
                acts.append(notify(c["id"], "cancel", "%s说没事了，建议打个电话确认一下" % self.title))
            acts += [arm("release", 2), arm("home", 1), say("cancel_after_sos")]
            return acts
        return []

    def elder_distress(self, text):
        """询问期间老人说了话但不是"没事" → 视为呼救，立即升级。"""
        if self.state != ASKING:
            return []
        return self._escalate("老人回应：“%s”" % text)

    def family_ack(self, contact_id):
        if self.state != SOS:
            return []
        c = self._find(contact_id)
        if c is None:
            return []
        self.state = HELP_COMING
        self.acked_by = c["id"]
        self.ack_deadline = None
        return [
            event("family_ack", "%s已确认：马上到" % c["name"], "ok"),
            arm("sign_family_received", 1),
            arm("sign_coming", 2),
            say("family_coming", contact=c["name"], pron=c["pron"]),
        ]

    def _escalate(self, reason):
        self.state = SOS
        self.deadline = None
        self.contact_idx = 0
        self.ack_deadline = self.clock() + self.cfg["family_ack_timeout"]
        c = self.contacts[0]
        return [
            event("sos", "%s → 升级求助" % reason, "danger"),
            arm("press_sos", 1),
            notify(c["id"], "fall", "%s在%s可能摔倒了，询问没有得到“没事”的回应" % (self.title, self.room)),
            arm("gentle_hold", 2),
            say("sos_sent", contact=c["name"], pron=c["pron"]),
        ]

    def _clear_guard(self):
        self.state = IDLE
        self.room = None
        self.deadline = None
        self.ack_deadline = None
        self.acked_by = None
        self.contact_idx = 0

    # ---------- 打 120 双重确认 ----------

    def request_dial(self):
        window = self.cfg["dial_confirm_window"]
        self.dial_deadline = self.clock() + window
        return [
            event("dial", "老人要求拨打 120，进入 %g 秒确认窗" % window, "warn"),
            say("dial_confirm", secs=int(window)),
        ]

    def confirm_dial(self):
        if self.dial_deadline is None:
            return []
        self.dial_deadline = None
        c = self.contacts[0]
        return [
            {"do": "dial", "number": "120"},
            event("dial", "已拨打 120（模拟）", "danger"),
            notify(c["id"], "120", "%s确认拨打了 120（模拟），请尽快联系" % self.title),
            say("dial_done", contact=c["name"]),
        ]

    def cancel_dial(self):
        if self.dial_deadline is None:
            return []
        self.dial_deadline = None
        return [event("dial", "老人取消拨打 120，未拨", "info"), say("dial_cancelled")]

    # ---------- 时钟 ----------

    def tick(self):
        now = self.clock()
        acts = []
        if self.state == ASKING and now >= self.deadline:
            acts += self._escalate("%g 秒内无回应" % self.wait)
        elif self.state == SOS and self.ack_deadline is not None and now >= self.ack_deadline:
            prev = self.contacts[self.contact_idx]
            if self.contact_idx + 1 < len(self.contacts):
                self.contact_idx += 1
                c = self.contacts[self.contact_idx]
                self.ack_deadline = now + self.cfg["family_ack_timeout"]
                acts += [
                    event("notify", "%s未确认，升级通知%s" % (prev["name"], c["name"]), "danger"),
                    notify(c["id"], "fall", "%s在%s可能摔倒了，%s暂未确认" % (self.title, self.room, prev["name"])),
                    say("next_contact", prev=prev["name"], contact=c["name"]),
                ]
            else:
                self.ack_deadline = None
                acts += [
                    event("notify", "家属均未确认，提示老人可说“帮我打120”", "danger"),
                    say("all_contacts_silent"),
                ]
        if self.dial_deadline is not None and now >= self.dial_deadline:
            self.dial_deadline = None
            acts += [event("dial", "确认窗超时，未拨 120", "info"), say("dial_timeout")]
        return acts

    # ---------- 查询 ----------

    def _find(self, contact_id):
        for c in self.contacts:
            if c["id"] == contact_id:
                return c
        return None

    def snapshot(self):
        now = self.clock()

        def left(t):
            return None if t is None else max(0.0, round(t - now, 1))

        current = self.contacts[self.contact_idx] if self.state in (SOS, HELP_COMING) else None
        return {
            "state": self.state,
            "room": self.room,
            "night": self.night,
            "wait": self.wait,
            "remaining": left(self.deadline),
            "contact": current["id"] if current else None,
            "notified": [c["id"] for c in self.contacts[: self.contact_idx + 1]] if current else [],
            "ack_remaining": left(self.ack_deadline),
            "acked_by": self.acked_by,
            "dial_pending": self.dial_deadline is not None,
            "dial_remaining": left(self.dial_deadline),
        }
