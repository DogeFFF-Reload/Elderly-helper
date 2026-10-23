"""主脑总线 :8000 —— /mock /api /ui 路由、事件中心、AI 口播。

注意：这是交互端开发用的 stand-in 版（按 v2.5 文档接口重建），正式版归软件同学维护。
对外接口与文档一致：
  POST /mock/voice  {"text": "..."}       老人说的话（ASR 桥也走这里）
  POST /mock/fall?room=客厅                跌倒事件（视觉桥同格式）
  POST /mock/button?key=ok                床头盒绿键"我没事"
  GET  /ui/monitor | /ui/family | /ui/console

运行：python -m master_service [--host 0.0.0.0] [--port 8000]
"""
import argparse
import json
import mimetypes
import queue
import random
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from agent import intent
from agent.device_helper import DeviceHelper
from agent.health_tracker import HealthTracker
from agent.llm_client import LLMClient
from agent.supervisor import Supervisor
from agent.voice_profile import StyleProfile
from gesture import rps_engine
from hand_controller.poses import FINGER_MOVES, label as pose_label
from state_machine.guard_fsm import ASKING, GuardFSM
from voice.text_normalize import is_filler, normalize

UI_DIR = config.CODE_DIR / "ui"
MAX_EVENTS = 500
TEACH_CARD_SECONDS = 45.0


class EventLog:
    def __init__(self, path=None, clock=time.time):
        self.items = []
        self.next_id = 1
        self.path = Path(path) if path else None
        self.clock = clock

    def add(self, type_, text, level="info", who="system", **data):
        ev = {"id": self.next_id, "ts": self.clock(), "type": type_, "text": text,
              "level": level, "who": who}
        if data:
            ev["data"] = data
        self.next_id += 1
        self.items.append(ev)
        del self.items[:-MAX_EVENTS]
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return ev

    def since(self, since_id, limit=200):
        return [e for e in self.items if e["id"] > since_id][-limit:]

    def clear(self):
        self.items = []


class ArmClient:
    """把动作排队发给 arm_service（:8013）；连不上只记 offline，不阻塞总线。"""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.online = None
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, pose, hand):
        self.q.put((pose, hand))

    def _worker(self):
        while True:
            pose, hand = self.q.get()
            url = "%s/pose?name=%s&hand=%d" % (self.base, urllib.parse.quote(pose), hand)
            try:
                urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=1.5).read()
                self.online = True
            except Exception:
                self.online = False


def _say(key, **params):
    return {"do": "say", "key": key, "params": params}


def _ev(type_, text, level="info", who="system"):
    return {"do": "event", "type": type_, "text": text, "level": level, "who": who}


class Bus:
    def __init__(self, cfg=None, data_dir=None, clock=time.time, arm=None):
        self.cfg = cfg or config.load()
        self.clock = clock
        self.lock = threading.RLock()
        d = Path(data_dir) if data_dir else None
        self.events = EventLog(d / "events.jsonl" if d else None, clock)
        self.fsm = GuardFSM(self.cfg, clock)
        self.health = HealthTracker(d / "health.json" if d else None, clock, self.cfg["health_confirm_window"])
        self.style = StyleProfile(d / "voice_profile.json" if d else None, clock)
        self.guides = DeviceHelper()
        self.llm = LLMClient(self.cfg["llm_mode"])
        self.supervisor = Supervisor(self.cfg["contacts"])
        self.arm = arm if arm is not None else ArmClient(self.cfg["arm_url"])
        self.night_mode = "auto"
        self.rng = random.SystemRandom()
        self.next_rps_id = 1
        self._reset_runtime()
        self.events.add("system", "系统启动（台词通道：%s）" % self.cfg["llm_mode"])

    def _reset_runtime(self):
        self.messages = []
        self.notifications = []
        self.next_notif_id = 1
        self.teach_card = None
        self.exercise = None
        self.skeleton = {"pose": "standing", "keypoints": None, "ts": self.clock()}
        self.last_say = None
        self.last_heard = None
        self.rps = None
        self.rps_last = None

    # ---------- 工具 ----------

    @property
    def primary(self):
        return self.cfg["contacts"][0]

    def contact(self, contact_id):
        return config.contact_by_id(self.cfg, contact_id)

    def is_night(self):
        if self.night_mode == "on":
            return True
        if self.night_mode == "off":
            return False
        hour = time.localtime(self.clock()).tm_hour
        return config.is_night(hour, self.cfg["night_start_hour"], self.cfg["night_end_hour"])

    def _speak(self, text):
        if not text:
            return
        ev = self.events.add("say", text, who="ai")
        self.last_say = {"id": ev["id"], "text": text, "ts": ev["ts"]}

    def _notify(self, contact_id, kind, text):
        c = self.contact(contact_id or self.primary["id"]) or self.primary
        n = {"id": self.next_notif_id, "ts": self.clock(), "contact": c["id"], "kind": kind,
             "text": text, "acked": False}
        self.next_notif_id += 1
        self.notifications.append(n)
        del self.notifications[:-50]
        level = "danger" if kind in ("fall", "120") else "info"
        self.events.add("notify", "通知%s：%s" % (c["name"], text), level, contact=c["id"], kind=kind)

    def _run(self, acts):
        for a in acts:
            kind = a["do"]
            if kind == "say":
                params = {"contact": self.primary["name"], "pron": self.primary["pron"]}
                params.update(a.get("params", {}))
                self._speak(self.llm.say(a["key"], **params))
            elif kind == "say_text":
                self._speak(a["text"])
            elif kind == "event":
                self.events.add(a["type"], a["text"], a.get("level", "info"), who=a.get("who", "system"))
            elif kind == "arm":
                self.arm.send(a["pose"], a["hand"])
                if not a.get("quiet"):
                    self.events.add("arm", pose_label(a["pose"]), who="arm", pose=a["pose"], hand=a["hand"])
            elif kind == "notify":
                self._notify(a["contact"], a["kind"], a["text"])
            # "dial" 已由状态机配套的 event 记录；真实拨号接口待定

    def _blocked(self, what, reason, line_key, **params):
        return [_ev("blocked", "越权拦截：%s（%s）" % (what, reason), "warn"), _say(line_key, **params)]

    # ---------- 输入：老人 ----------

    def voice(self, text):
        with self.lock:
            text = normalize(text)
            if not text or is_filler(text):
                return {"ok": False, "reason": "empty_or_filler"}
            self.last_heard = {"text": text, "ts": self.clock()}
            self.events.add("heard", text, who="elder")
            g = self.fsm.snapshot()
            ctx = {
                "asking": g["state"] == ASKING,
                "dial_pending": g["dial_pending"],
                "health_pending": self.health.pending is not None,
                "exercise_active": self.exercise is not None,
                "contacts": self.cfg["contacts"],
            }
            name, slots = intent.classify(text, ctx)
            self._run(self._handle(name, slots, text))
            return {"ok": True, "intent": name}

    def _handle(self, name, slots, text):
        sup = self.supervisor
        if name == "dial_confirm":
            return self.fsm.confirm_dial()
        if name == "dial_cancel":
            return self.fsm.cancel_dial()
        if name == "elder_ok":
            acts = self.fsm.elder_ok("语音“%s”" % text)
            if acts:
                self.skeleton = {"pose": "standing", "keypoints": None, "ts": self.clock()}
            return acts or [_say("elder_ok_idle")]
        if name == "elder_distress":
            acts = self.fsm.elder_distress(text)
            if slots.get("dial"):
                acts += self.fsm.request_dial()
            return acts
        if name == "dial_request":
            return self.fsm.request_dial()
        if name == "health_record":
            return self.health.propose(text)
        if name == "health_confirm":
            return self.health.confirm()
        if name == "health_reject":
            return self.health.reject()
        if name == "deliver":
            ok, reason = sup.check("deliver_medicine", item=slots["item"])
            if not ok:
                return self._blocked("递“%s”" % slots["item"], reason, "deliver_unknown", item=slots["item"])
            return [_ev("deliver", "老人要药 → 递药盒", "info", "system"),
                    {"do": "arm", "pose": "deliver_medicine", "hand": 1},
                    _say("deliver_start")]
        if name == "call_family":
            c = self.contact(slots["contact"])
            return [{"do": "notify", "contact": c["id"], "kind": "call",
                     "text": "%s请您回个电话" % self.cfg["elder_title"]},
                    _say("call_family", contact=c["name"], pron=c["pron"])]
        if name == "call_blocked":
            who = slots["who"]
            sup.check("call_family", contact=who)
            names = "、".join(c["name"] for c in self.cfg["contacts"])
            return self._blocked("呼叫“%s”" % who, "联系人不在白名单", "contact_blocked", contacts=names, who=who)
        if name == "message":
            c = self.contact(slots["contact"]) or self.primary
            body = (slots.get("text") or "").strip()
            if not body:
                return [_say("message_empty", contact=c["name"])]
            self.messages.append({"ts": self.clock(), "from": "elder", "to": c["id"], "text": body})
            return [{"do": "notify", "contact": c["id"], "kind": "message",
                     "text": "%s留言：%s" % (self.cfg["elder_title"], body)},
                    _say("message_sent", contact=c["name"])]
        if name == "safe_report":
            return [{"do": "notify", "contact": c["id"], "kind": "safe",
                     "text": "%s报平安：今天一切都好" % self.cfg["elder_title"]}
                    for c in self.cfg["contacts"]] + [_say("safe_report")]
        if name == "teach":
            return self._teach(text)
        if name == "echo":
            c = self.contact(slots["contact"]) or self.primary
            latest = next((m["text"] for m in reversed(self.messages) if m["from"] == c["id"]), None)
            line = self.style.speak(c["id"], c["pron"], latest)
            if line is None:
                return [_ev("echo", "%s未授权回声代聊，未代聊" % c["name"]),
                        _say("echo_off", contact=c["name"], pron=c["pron"])]
            return [_ev("echo", "回声代聊（%s已授权，仅用%s的原话风格）" % (c["name"], c["pron"]), "ok"),
                    {"do": "say_text", "text": line}]
        if name == "exercise_start":
            if self.exercise:
                return []
            self.exercise = {"beat": 0, "next": self.clock() + 3.0}
            self.teach_card = None
            return [_ev("exercise", "开始手指操（13 拍）"), _say("exercise_start")]
        if name == "exercise_stop":
            return self._stop_exercise("老人喊停")
        if name == "rps":
            return self._rps_start()
        return [_say("chitchat")]

    # ---------- 猜拳（陪）----------

    def _rps_start(self):
        if self.rps:
            return []
        rid = self.next_rps_id
        self.next_rps_id += 1
        now = self.clock()
        # 机器人在看到老人手势之前就定好出什么，揭晓时手#1 才摆出来 —— 公平
        self.rps = {"round": rid, "robot": self.rng.choice(rps_engine.MOVES), "revealed": False,
                    "reveal_at": now + self.cfg["rps_reveal_delay"],
                    "deadline": now + self.cfg["rps_reveal_delay"] + self.cfg["rps_timeout"]}
        self.rps_last = None
        self.teach_card = None
        self.events.add("rps", "猜拳第 %d 局：等老人出手势" % rid, round=rid, phase="start",
                        reveal_in=self.cfg["rps_reveal_delay"])
        return [_say("rps_start")]

    def _rps_reveal(self):
        r = self.rps
        if r and not r["revealed"]:
            r["revealed"] = True
            return [{"do": "arm", "pose": "rps_" + r["robot"], "hand": 1}]
        return []

    def rps_gesture(self, round_id, gesture):
        """视觉（或操作台兜底按钮）回传老人的手势；判输赢由这里按 rps_engine.judge 做。"""
        with self.lock:
            r = self.rps
            try:
                same = round_id is None or int(round_id) == r["round"]
            except (TypeError, ValueError):
                same = False
            if not r or not same:
                return {"ok": False, "reason": "no such round"}
            acts = self._rps_reveal()
            robot, names = r["robot"], rps_engine.NAMES
            self.rps = None
            if gesture not in rps_engine.MOVES:
                result = None
                acts += [_ev("rps", "猜拳第 %d 局：没看清老人的手势（我出%s）" % (r["round"], names[robot]), "warn"),
                         _say("rps_unclear")]
            else:
                result = rps_engine.judge(gesture, robot)
                verdict = {"win": "老人赢", "lose": "机器人赢", "draw": "平局"}[result]
                acts += [_ev("rps", "猜拳第 %d 局：老人出%s，我出%s → %s" % (r["round"], names[gesture], names[robot], verdict), "ok"),
                         _say("rps_" + result, player=names[gesture], robot=names[robot])]
            self.rps_last = {"round": r["round"], "robot": robot, "player": gesture if result else None,
                             "result": result, "ts": self.clock()}
            self._run(acts)
            return {"ok": True, "result": result, "robot": robot}

    def _rps_view(self):
        r = self.rps
        if r:
            return {"round": r["round"], "phase": "revealed" if r["revealed"] else "countdown",
                    "robot": r["robot"] if r["revealed"] else None,
                    "reveal_in": max(0.0, round(r["reveal_at"] - self.clock(), 1))}
        if self.rps_last:
            return dict(self.rps_last, phase="done")
        return None

    def _teach(self, text):
        r = self.guides.match(text)
        if r["status"] == "ok":
            self.teach_card = {"device": r["device"], "task": r["task"], "steps": r["steps"],
                               "status": "ok", "ts": self.clock()}
            steps = "；".join("第%d步，%s" % (i + 1, s) for i, s in enumerate(r["steps"]))
            return [_ev("teach", "教学卡：%s · %s（来自 guides.json）" % (r["device"], r["task"])),
                    _say("teach_steps", device=r["device"], task=r["task"], n=len(r["steps"]), steps=steps)]
        if r["status"] == "unknown_task":
            self.teach_card = {"device": r["device"], "task": None, "steps": [], "known": r["tasks"],
                               "status": "unknown", "ts": self.clock()}
            return [_ev("teach", "没学过的问法：“%s” → 不现编" % text, "warn"),
                    _say("teach_unknown_task", device=r["device"], tasks="、".join(r["tasks"]))]
        return [_say("teach_unknown_device", devices="、".join(r["devices"]))]

    def _stop_exercise(self, why):
        if not self.exercise:
            return []
        beat = self.exercise["beat"]
        self.exercise = None
        return [_ev("exercise", "手指操中断（%s，做到第 %d 拍）" % (why, beat)),
                {"do": "arm", "pose": "home", "hand": 1, "quiet": True},
                _say("exercise_stop")]

    def fall(self, room=None, source="mock"):
        with self.lock:
            room = room or self.cfg["default_room"]
            acts = []
            if self.exercise:
                self.exercise = None
                acts.append(_ev("exercise", "手指操因跌倒事件中断"))
            self.teach_card = None
            self.rps = None
            if self.skeleton["pose"] != "live":
                self.skeleton = {"pose": "fallen", "keypoints": None, "ts": self.clock()}
            acts += self.fsm.fall_detected(room, self.is_night())
            self._run(acts)
            return {"ok": True, "state": self.fsm.state}

    def button(self, key="ok"):
        with self.lock:
            if key != "ok":
                return {"ok": False, "reason": "unknown key"}
            self.events.add("button", "床头盒绿键按下", who="elder")
            acts = self.fsm.elder_ok("绿键")
            if acts:
                self.skeleton = {"pose": "standing", "keypoints": None, "ts": self.clock()}
            self._run(acts or [_say("elder_ok_idle")])
            return {"ok": True, "state": self.fsm.state}

    def update_skeleton(self, body):
        with self.lock:
            if body.get("pose") in ("standing", "fallen"):
                self.skeleton = {"pose": body["pose"], "keypoints": None, "ts": self.clock()}
            elif body.get("keypoints"):
                self.skeleton = {"pose": "live", "keypoints": body["keypoints"],
                                 "fallen": bool(body.get("fallen")), "ts": self.clock()}
            return {"ok": True}

    # ---------- 输入：家属端 ----------

    def family_ack(self, contact_id):
        with self.lock:
            c = self.contact(contact_id)
            if c is None:
                return {"ok": False, "reason": "unknown contact"}
            for n in self.notifications:
                if n["contact"] == c["id"]:
                    n["acked"] = True
            acts = self.fsm.family_ack(c["id"])
            self._run(acts)
            return {"ok": True, "state": self.fsm.state}

    def family_message(self, contact_id, text):
        with self.lock:
            c = self.contact(contact_id)
            text = (text or "").strip()
            if c is None or not text:
                return {"ok": False}
            self.messages.append({"ts": self.clock(), "from": c["id"], "to": "elder", "text": text})
            learned = self.style.observe(c["id"], text)
            self._run([_ev("message", "%s留言：%s" % (c["name"], text), "info", "family"),
                       _say("family_message", contact=c["name"], text=text)])
            return {"ok": True, "learned": learned}

    def echo_authorize(self, contact_id):
        with self.lock:
            c = self.contact(contact_id)
            if c is None:
                return {"ok": False}
            history = [m["text"] for m in self.messages if m["from"] == c["id"]]
            style = self.style.authorize(c["id"], history)
            self.events.add("echo", "%s亲手授权回声代聊（只学文字风格，不做声音克隆）" % c["name"], "ok", "family")
            return {"ok": True, "style": style}

    def echo_revoke(self, contact_id):
        with self.lock:
            c = self.contact(contact_id)
            if c is None:
                return {"ok": False}
            self.style.revoke(c["id"])
            self.events.add("echo", "%s一键收回授权，风格档案已清空" % c["name"], "info", "family")
            return {"ok": True}

    # ---------- 操作台 ----------

    def set_night(self, mode):
        with self.lock:
            if mode not in ("auto", "on", "off"):
                return {"ok": False}
            self.night_mode = mode
            text = {"auto": "夜间模式：按时钟（%d:00–%d:00）" % (self.cfg["night_start_hour"], self.cfg["night_end_hour"]),
                    "on": "夜间模式：强制开启（询问 %g 秒）" % self.cfg["ask_wait_night"],
                    "off": "夜间模式：强制关闭（询问 %g 秒）" % self.cfg["ask_wait_day"]}[mode]
            self.events.add("night", text)
            return {"ok": True, "night": self.is_night()}

    def agent_propose(self, skill):
        """演示/测试用：模拟 LLM 直接请求某个技能，看 supervisor 拦不拦。"""
        with self.lock:
            ok, reason = self.supervisor.check(skill)
            if ok:
                self.events.add("agent", "agent 请求“%s”：白名单内，放行" % skill, "ok")
            else:
                self.events.add("blocked", "越权拦截：agent 请求“%s”（%s）" % (skill, reason), "warn")
            return {"ok": ok, "reason": reason}

    def reset(self, clear_data=False):
        with self.lock:
            self.fsm.reset()
            self.health.reset(clear_records=clear_data)
            if clear_data:
                self.style.reset()
            self.night_mode = "auto"
            self._reset_runtime()
            self.events.clear()
            self.events.add("reset", "演示已重置" + ("（健康记录与风格档案已清空）" if clear_data else ""))
            return {"ok": True}

    # ---------- 时钟 ----------

    def tick(self):
        with self.lock:
            acts = self.fsm.tick() + self.health.tick()
            now = self.clock()
            ex = self.exercise
            if ex and now >= ex["next"]:
                i = ex["beat"]
                if i >= len(FINGER_MOVES):
                    self.exercise = None
                    acts += [_ev("exercise", "手指操完成（13 拍）", "ok"),
                             {"do": "arm", "pose": "home", "hand": 1, "quiet": True},
                             _say("exercise_done")]
                else:
                    move, shape = FINGER_MOVES[i]
                    ex["beat"] = i + 1
                    ex["next"] = now + self.cfg["exercise_beat_seconds"]
                    acts += [{"do": "arm", "pose": "finger_" + shape, "hand": 1, "quiet": True},
                             _say("exercise_beat", n=i + 1, move=move)]
            if self.teach_card and now - self.teach_card["ts"] > TEACH_CARD_SECONDS:
                self.teach_card = None
            r = self.rps
            if r and now >= r["reveal_at"]:
                acts += self._rps_reveal()
            if r and now >= r["deadline"]:
                self.rps = None
                acts += [_ev("rps", "猜拳第 %d 局超时：视觉没有回传手势" % r["round"], "warn"), _say("rps_no_camera")]
            if self.rps_last and now - self.rps_last["ts"] > 10:
                self.rps_last = None
            if acts:
                self._run(acts)

    # ---------- 输出 ----------

    def snapshot(self):
        with self.lock:
            ex = self.exercise
            return {
                "now": self.clock(),
                "mode": self.cfg["llm_mode"],
                "elder_title": self.cfg["elder_title"],
                "contacts": [{"id": c["id"], "name": c["name"], "pron": c["pron"]} for c in self.cfg["contacts"]],
                "guard": self.fsm.snapshot(),
                "night": {"active": self.is_night(), "mode": self.night_mode,
                          "window": [self.cfg["night_start_hour"], self.cfg["night_end_hour"]]},
                "health": self.health.snapshot(),
                "echo": self.style.snapshot(),
                "messages": self.messages[-30:],
                "notifications": self.notifications[-30:],
                "teach": self.teach_card,
                "exercise": {"active": ex is not None, "beat": ex["beat"] if ex else 0, "total": len(FINGER_MOVES)},
                "rps": self._rps_view(),
                "skeleton": self.skeleton,
                "arm": {"online": self.arm.online},
                "last_say": self.last_say,
                "last_heard": self.last_heard,
                "params": {k: self.cfg[k] for k in ("ask_wait_day", "ask_wait_night", "dial_confirm_window",
                                                    "family_ack_timeout", "health_confirm_window")},
            }

    def poll(self, since):
        with self.lock:
            return {"state": self.snapshot(), "events": self.events.since(since),
                    "last_id": self.events.next_id - 1}


# ---------- HTTP ----------

def _read_json(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(n) if n else b""
    if not raw:
        return {}
    for enc in ("utf-8", "gb18030"):
        try:
            data = json.loads(raw.decode(enc))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, ValueError):
            continue
    return {}


def make_handler(bus):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _static(self, path):
            rel = path[len("/ui/"):].strip("/")
            target = (UI_DIR / rel).resolve()
            if target.is_dir():
                target = target / "index.html"
            if UI_DIR.resolve() not in target.parents or not target.is_file():
                return self._json({"ok": False, "error": "not found"}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            path = url.path
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/ui/console")
                self.end_headers()
                return
            if path == "/api/state":
                return self._json(bus.snapshot())
            if path == "/api/poll":
                return self._json(bus.poll(int(q.get("since", ["0"])[0])))
            if path == "/api/events":
                with bus.lock:
                    return self._json({"events": bus.events.since(int(q.get("since", ["0"])[0])),
                                       "last_id": bus.events.next_id - 1})
            if path == "/api/health/report":
                with bus.lock:
                    return self._json(bus.health.daily_report())
            if path == "/api/guides":
                return self._json(bus.guides.catalog())
            if path.startswith("/ui/"):
                return self._static(path)
            self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            body = _read_json(self)
            arg = lambda k, d=None: body.get(k, q.get(k, d))  # noqa: E731
            path = url.path
            routes = {
                "/mock/voice": lambda: bus.voice(arg("text", "")),
                "/mock/fall": lambda: bus.fall(arg("room")),
                "/mock/button": lambda: bus.button(arg("key", "ok")),
                "/api/skeleton": lambda: bus.update_skeleton(body or q),
                "/api/family/ack": lambda: bus.family_ack(arg("contact", bus.primary["id"])),
                "/api/family/message": lambda: bus.family_message(arg("contact", bus.primary["id"]), arg("text", "")),
                "/api/echo/authorize": lambda: bus.echo_authorize(arg("contact", bus.primary["id"])),
                "/api/echo/revoke": lambda: bus.echo_revoke(arg("contact", bus.primary["id"])),
                "/api/night": lambda: bus.set_night(arg("mode", "auto")),
                "/api/agent/propose": lambda: bus.agent_propose(arg("skill", "")),
                "/api/reset": lambda: bus.reset(clear_data=str(arg("all", "0")) == "1"),
                "/api/rps/gesture": lambda: bus.rps_gesture(arg("round"), arg("gesture")),
            }
            fn = routes.get(path)
            if fn is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            self._json(fn())

        def log_message(self, *args):
            pass

    return Handler


def serve(bus, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), make_handler(bus))
    server.daemon_threads = True
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            bus.tick()
            stop.wait(0.2)

    threading.Thread(target=ticker, daemon=True).start()
    server.stop_ticker = stop
    return server


def main():
    config.utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="多节点联调用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    bus = Bus(data_dir=config.DATA_DIR)
    server = serve(bus, args.host, args.port)
    base = "http://127.0.0.1:%d" % args.port
    print("master_service on %s:%d" % (args.host, args.port))
    for name in ("monitor", "family", "console"):
        print("  %-8s %s/ui/%s" % (name, base, name))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
