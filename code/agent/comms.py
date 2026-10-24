"""通话 + 语音留言：老人端（ui/elder）↔ 家属端（ui/family）。

通话用浏览器 WebRTC 直连（同一 Wi-Fi，不走外网、不打洞），主脑只做信令：谁呼谁、两边的 SDP、接通 / 挂断。
SDP 不进 /api/poll 的快照（几 KB，每 0.4 秒拉一次太重），页面看到 has_offer / has_answer 再单独 GET。
发起方页面负责出 offer：语音“给女儿打电话”、跌倒升级这类主脑发起的呼叫，也由老人端页面看到后补上，
所以老人端页面不在线时主脑不发起（语音呼叫回落成原来的“让家属回电话”通知）。

和保命链的关系：不改状态机。跌倒升级通知某位家属时（notify kind=fall），主脑顺带让老人端向他发起紧急通话；
保命链进行中家属打进来，老人端自动接通（老人可能够不着屏幕）。家属确认“马上到”仍走 /api/family/ack。

页面在不在线看 /api/poll?client=...：老人端 "elder"，家属端 "family:<联系人 id>"。
"""
import time

RING_TIMEOUT = 45.0        # 响铃这么久没人接 → 未接
MAX_CALL = 30 * 60.0
PAGE_TIMEOUT = 15.0        # 页面这么久没来拉 /api/poll 算离线（后台标签页的定时器最慢 1 秒一次）
ENDED_SHOWN = 8.0          # 通话结束后快照里再留这么久，两边页面都来得及显示“通话结束”
MAX_SDP = 20000
MAX_VOICE_BYTES = 2 * 1024 * 1024
MAX_VOICE_SECONDS = 60.0
MIN_VOICE_SECONDS = 0.5
KEEP_VOICE = 30
AUDIO_TYPES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav")

END_TEXT = {"declined": "拒接", "cancelled": "已取消", "missed": "未接", "ended": "已挂断",
            "dropped": "断线", "replaced": "转给下一位家人", "offline": "发起方页面已关闭", "reset": "演示重置"}


def _ev(type_, text, level="info", who="system"):
    return {"do": "event", "type": type_, "text": text, "level": level, "who": who}


def _say(key, **params):
    return {"do": "say", "key": key, "params": params}


def _notify(contact, kind, text, **extra):
    return {"do": "notify", "contact": contact, "kind": kind, "text": text, "extra": extra}


def _mmss(secs):
    secs = int(round(secs))
    return "%d 分 %02d 秒" % (secs // 60, secs % 60) if secs >= 60 else "%d 秒" % secs


class Comms:
    def __init__(self, cfg, clock=time.time, clients=None):
        self.cfg, self.clock = cfg, clock
        self.clients = clients if clients is not None else {}   # 总线的 /api/poll 订阅者表（同一个 dict）
        self.next_call_id = 1
        self.next_voice_id = 1
        self.play_seq = 0
        self.reset()

    def reset(self):
        self.call = None        # 当前（或刚结束的）通话
        self.history = []       # 结束的通话（不含 SDP）
        self.audio = {}         # voice_id → {"mime", "data"}
        self.play_req = None    # 老人说“听留言” → 老人端播放 {"seq", "id"}

    # ---------- 工具 ----------

    def contact(self, cid):
        return next((c for c in self.cfg["contacts"] if c["id"] == cid), None)

    def name(self, party):
        if party == "elder":
            return self.cfg["elder_title"]
        c = self.contact(party)
        return c["name"] if c else str(party)

    def _family_of(self, a, b):
        """一头是老人、另一头是白名单联系人 → 联系人 id；否则 None。"""
        fam = b if a == "elder" else a if b == "elder" else None
        return fam if fam is not None and self.contact(fam) else None

    def online(self, party):
        t = self.clients.get("elder" if party == "elder" else "family:%s" % party)
        return t is not None and self.clock() - t < PAGE_TIMEOUT

    @property
    def live(self):
        c = self.call
        return c if c and c["state"] in ("ringing", "active") else None

    def _who(self, party):
        return "elder" if party == "elder" else "family"

    # ---------- 通话 ----------

    def start(self, frm, to, emergency=False, urgent=False, manual=False):
        """发起呼叫 → (结果, acts)。urgent = 保命链进行中：家属打给老人时老人端自动接。
        manual = 老人在床头屏长按了紧急呼叫（跌倒升级那种已经由状态机发过通知了）。"""
        fam = self._family_of(frm, to)
        if fam is None:
            return {"ok": False, "error": "只能在%s和白名单家属之间通话" % self.cfg["elder_title"]}, []
        if self.live:
            return {"ok": False, "error": "正在通话中", "busy": True}, []
        if not self.online(frm):
            return {"ok": False, "error": "%s的页面没打开" % self.name(frm)}, []
        if to == "elder" and not self.online("elder"):
            return {"ok": False, "error": "%s的床头屏不在线" % self.cfg["elder_title"]}, []
        now = self.clock()
        self.call = {"id": self.next_call_id, "from": frm, "to": to, "state": "ringing",
                     "emergency": bool(emergency), "auto_answer": to == "elder" and bool(urgent or emergency),
                     "answer_req": False, "ts": now, "answered_ts": None, "ended_ts": None,
                     "end_reason": None, "ended_by": None, "offer": None, "answer": None}
        self.next_call_id += 1
        what = "紧急呼叫" if emergency else "语音通话"
        acts = [_ev("call", "%s → %s：%s，响铃中" % (self.name(frm), self.name(to), what),
                    "danger" if emergency else "info", self._who(frm))]
        if manual and emergency and to != "elder":
            acts.append(_notify(to, "call", "%s在床头屏按了紧急呼叫，正在呼叫你" % self.cfg["elder_title"]))
        if to == "elder" and not self.call["auto_answer"]:
            c = self.contact(frm)
            acts.append(_say("call_incoming", contact=c["name"], pron=c["pron"]))
        return {"ok": True, "call": self.view(self.call)}, acts

    def emergency(self, contact_id):
        """跌倒升级通知了某位家属 → 老人端向他发起紧急通话。老人端页面不在线就不打（通知照常已发）。"""
        if not self.online("elder") or self.contact(contact_id) is None:
            return []
        acts = []
        c = self.live
        if c:
            if not (c["emergency"] and c["from"] == "elder" and c["state"] == "ringing" and c["to"] != contact_id):
                return []   # 已经连着家属（或正打给同一个人），不打断
            acts += self._end("replaced")
        return acts + self.start("elder", contact_id, emergency=True)[1]

    def _match(self, call_id, party, role):
        c = self.live
        if not c or str(c["id"]) != str(call_id):
            return None, {"ok": False, "error": "通话已结束"}
        if party != c[role]:
            return None, {"ok": False, "error": "不是这通电话的%s" % ("发起方" if role == "from" else "接听方")}
        return c, None

    def set_offer(self, call_id, party, sdp):
        c, err = self._match(call_id, party, "from")
        if err:
            return err
        if c["state"] != "ringing":
            return {"ok": False, "error": "已经接通"}
        if not sdp or len(sdp) > MAX_SDP:
            return {"ok": False, "error": "SDP 为空或太长"}
        c["offer"] = sdp
        return {"ok": True}

    def answer(self, call_id, party, sdp):
        c, err = self._match(call_id, party, "to")
        if err:
            return err, []
        if c["state"] != "ringing" or not c["offer"]:
            return {"ok": False, "error": "对方还没准备好"}, []
        if not sdp or len(sdp) > MAX_SDP:
            return {"ok": False, "error": "SDP 为空或太长"}, []
        c["answer"] = sdp
        c["state"] = "active"
        c["answered_ts"] = self.clock()
        what = "紧急通话" if c["emergency"] else "电话"
        auto = "（保命链中自动接通）" if c["auto_answer"] and party == "elder" else ""
        return {"ok": True, "call": self.view(c)}, [
            _ev("call", "%s接通了%s的%s%s" % (self.name(party), self.name(c["from"]), what, auto), "ok", self._who(party))]

    def request_answer(self):
        """老人说“接电话” → 让老人端页面替他点接听。"""
        c = self.live
        if not c or c["state"] != "ringing" or c["to"] != "elder":
            return []
        c["answer_req"] = True
        return [_ev("call", "%s说接电话" % self.cfg["elder_title"], who="elder")]

    def hangup(self, call_id, party):
        c = self.live
        if not c or (call_id is not None and str(c["id"]) != str(call_id)):
            return {"ok": True, "already": True}, []   # 两边同时挂断很常见，不算错
        if party not in (c["from"], c["to"]):
            return {"ok": False, "error": "不是这通电话的一方"}, []
        if c["state"] == "ringing":
            reason = "declined" if party == c["to"] else "cancelled"
        else:
            reason = "ended"
        acts = self._end(reason, by=party)
        return {"ok": True, "call": self.view(c)}, acts

    def _end(self, reason, by=None):
        c = self.call
        now = self.clock()
        c.update(state="ended", ended_ts=now, end_reason=reason, ended_by=by, offer=None, answer=None)
        self.history.append(self.view(c))
        del self.history[:-20]
        pair = "%s ↔ %s" % (self.name(c["from"]), self.name(c["to"]))
        if c["answered_ts"]:
            text = "%s：通话结束，%s" % (pair, _mmss(now - c["answered_ts"]))
        else:
            text = "%s：%s%s" % (pair, "紧急呼叫" if c["emergency"] else "呼叫", END_TEXT.get(reason, reason))
        acts = [_ev("call", text, "warn" if reason in ("missed", "dropped") else "info",
                    self._who(by) if by else "system")]
        if reason == "missed" and c["to"] != "elder":
            what = "紧急呼叫" if c["emergency"] else "电话"
            acts.append(_notify(c["to"], "call", "%s给你打了%s，没接通，请尽快回电" % (self.cfg["elder_title"], what)))
            if not c["emergency"]:
                callee = self.contact(c["to"])
                acts.append(_say("call_missed", contact=callee["name"], pron=callee["pron"]))
        return acts

    def end_for_reset(self):
        if self.live:
            self._end("reset")

    def sdp(self, call_id, which):
        c = self.live
        if which not in ("offer", "answer") or not c or str(c["id"]) != str(call_id) or not c[which]:
            return {"ok": False, "error": "没有这份 SDP"}
        return {"ok": True, "sdp": c[which]}

    def tick(self):
        c = self.live
        if not c:
            return []
        now = self.clock()
        if c["state"] == "ringing":
            if now - c["ts"] >= RING_TIMEOUT:
                return self._end("missed")
            if not self.online(c["from"]):
                return self._end("offline")
        else:
            if now - c["answered_ts"] >= MAX_CALL:
                return self._end("ended")
            if not (self.online(c["from"]) and self.online(c["to"])):
                return self._end("dropped")
        return []

    # ---------- 语音留言 ----------

    def add_voice(self, frm, to, mime, data, dur):
        """存一段录音 → (消息, acts, 错误)。消息由总线放进 messages（和文字留言一条时间线）。"""
        fam = self._family_of(frm, to)
        if fam is None:
            return None, [], "只能在%s和白名单家属之间留言" % self.cfg["elder_title"]
        base = (mime or "").split(";")[0].strip().lower()
        if base not in AUDIO_TYPES:
            return None, [], "不支持的录音格式：%s" % (mime or "空")
        if not data:
            return None, [], "录音是空的"
        if len(data) > MAX_VOICE_BYTES:
            return None, [], "录音太大（超过 %d MB）" % (MAX_VOICE_BYTES // 1024 // 1024)
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            return None, [], "缺少录音时长"
        if dur < MIN_VOICE_SECONDS:
            return None, [], "说话时间太短"
        dur = min(dur, MAX_VOICE_SECONDS)
        vid = self.next_voice_id
        self.next_voice_id += 1
        self.audio[vid] = {"mime": mime, "data": bytes(data)}
        for old in sorted(self.audio)[:-KEEP_VOICE]:
            del self.audio[old]
        msg = {"ts": self.clock(), "from": frm, "to": to, "kind": "voice", "voice_id": vid,
               "dur": round(dur, 1), "heard": False, "heard_ts": None, "text": None}
        secs = max(1, int(round(dur)))
        c = self.contact(fam)
        if to == "elder":
            acts = [_ev("voice", "%s发来语音留言（%d 秒）" % (c["name"], secs), who="family"),
                    _say("voice_from_family", contact=c["name"], pron=c["pron"])]
        else:
            acts = [_ev("voice", "%s给%s留了语音（%d 秒）" % (self.cfg["elder_title"], c["name"], secs), who="elder"),
                    _notify(to, "voice", "%s发来一段语音留言（%d 秒）" % (self.cfg["elder_title"], secs),
                            voice_id=vid, dur=msg["dur"])]
        return msg, acts, None

    def get_audio(self, vid):
        try:
            return self.audio.get(int(vid))
        except (TypeError, ValueError):
            return None

    def request_play(self, vid):
        self.play_seq += 1
        self.play_req = {"seq": self.play_seq, "id": vid}

    # ---------- 输出 ----------

    def view(self, c):
        if not c:
            return None
        v = {k: c[k] for k in ("id", "from", "to", "state", "emergency", "auto_answer", "answer_req", "ts",
                               "answered_ts", "ended_ts", "end_reason", "ended_by")}
        v.update(has_offer=bool(c["offer"]), has_answer=bool(c["answer"]),
                 from_name=self.name(c["from"]), to_name=self.name(c["to"]),
                 end_text=END_TEXT.get(c["end_reason"]) if c["end_reason"] else None)
        return v

    def snapshot(self):
        c = self.call
        shown = c and (c["state"] != "ended" or self.clock() - c["ended_ts"] < ENDED_SHOWN)
        return {"call": self.view(c) if shown else None,
                "history": self.history[-10:],
                "play": self.play_req,
                "online": dict({"elder": self.online("elder")},
                               **{x["id"]: self.online(x["id"]) for x in self.cfg["contacts"]})}
