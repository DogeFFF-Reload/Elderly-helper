"""主脑总线 :8000 —— /mock /api /ui 路由、事件中心、AI 口播。软件模块维护，其他人要新接口提需求。

对外接口与文档一致：
  POST /mock/voice  {"text": "..."}       老人说的话（ASR 桥也走这里）
  POST /mock/fall?room=客厅                跌倒事件（视觉桥同格式）
  POST /mock/button?key=ok                床头盒绿键"我没事"
  GET  /api/box?id=box1                   床头盒轮询：灯色 + 心跳（协议草案，见 README）
  GET  /api/nodes                         节点自检：执行端 / 床头盒 / 视觉 / 喇叭 / 麦克风 / LLM（tools/preflight 用）
  GET/POST /api/params                    演示参数查看 / 运行中调整
  POST /api/call/start|offer|answer|hangup  老人端 ↔ 家属端语音通话的信令（WebRTC，见 agent/comms.py）
  GET  /api/call/sdp?id=&which=offer      取对方的 SDP
  POST /api/voice_msg?from=&to=&dur=      语音留言（正文是录音原始字节，或床头盒的 multipart：audio/duration/elder）；GET ?id= 取回放
  GET  /ui/monitor | /ui/family | /ui/console | /ui/elder

运行：python -m master_service [--host 0.0.0.0] [--port 8000]
"""
import argparse
import email.parser
import email.policy
import hmac
import json
import mimetypes
import queue
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
from agent import intent
from agent.comms import MAX_VOICE_BYTES, Comms
from agent.device_helper import DeviceHelper
from agent.health_tracker import HealthTracker
from agent.llm_client import LLMClient
from agent.med_reminder import MedReminder
from agent.supervisor import Supervisor
from agent.voice_profile import StyleProfile
from hand_controller.poses import FINGER_MOVES, label as pose_label
from state_machine.guard_fsm import ASKING, HELP_COMING, IDLE, SOS, GuardFSM
from voice.text_normalize import is_filler, normalize

UI_DIR = config.CODE_DIR / "ui"
MAX_EVENTS = 500
MAX_JSON_BYTES = 1024 * 1024
TEACH_CARD_SECONDS = 45.0
BOX_TIMEOUT = 3.0
VISION_TIMEOUT = 3.0   # 视觉约 5 次/秒推骨架，3 秒没来算离线
ARM_PROBE_EVERY = 2.0

# 公网（Cloudflare 隧道）进来的请求只能碰家属端用得到的这些，而且要带公网钥匙；
# 动臂、操作台、大屏、急停、重置、调参只在局域网（隧道进来的请求带 Cf-Connecting-Ip 头，据此区分）
PUBLIC_GET = ("/api/poll", "/api/state", "/api/voice_msg", "/api/fall_photo", "/api/call/sdp", "/api/family_replies")
PUBLIC_POST = ("/api/family/ack", "/api/family/message", "/api/voice_msg", "/api/voice_msg/heard",
               "/api/call/start", "/api/call/offer", "/api/call/answer", "/api/call/hangup",
               "/api/echo/authorize", "/api/echo/revoke", "/api/dial120")
PUBLIC_UI = ("/ui/family", "/ui/shared/")

# 保命链里的臂/手动作：没做成要马上告诉演示员，不能让时间线上只写着"已按下"
SAFETY_POSES = ("press_sos", "gentle_hold", "sign_family_received", "sign_coming")

# 意图 → 技能：老人提的每个请求都先过 supervisor（白名单 + 资源锁）
# 猜拳规则归主脑（谁赢由确定性代码判）；视觉只负责认出老人出了什么，按这三个字符串回传
RPS_NAMES = {"rock": "石头", "scissors": "剪刀", "paper": "布"}
RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def rps_judge(player, robot):
    """站在老人角度：'win' / 'lose' / 'draw'。"""
    if player == robot:
        return "draw"
    return "win" if RPS_BEATS[player] == robot else "lose"


SKILL_OF = {
    "deliver": "deliver_medicine", "exercise_start": "finger_exercise",
    "call_family": "call_family", "message": "leave_message", "safe_report": "safe_report",
    "health_record": "record_health", "health_confirm": "record_health", "health_reject": "record_health",
    "teach": "teach_device", "echo": "echo_chat", "dial_request": "request_dial",
    "rps": "rock_paper_scissors",
}

# 床头盒灯：(颜色, 闪法)。固件只管照着亮，逻辑全在主脑
BOX_LED = {IDLE: ("green", "breath"), ASKING: ("yellow", "blink"), SOS: ("red", "blink"),
           HELP_COMING: ("blue", "solid")}


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
    """把动作排队发给 arm_service（:8013），不阻塞总线。

    - 每条动作做没做成都回调 on_result(pose, hand, ok, why, detail)，why = offline / rejected / stale
    - 在线状态一变就回调 on_link(online)；start_probing() 后每 ARM_PROBE_EVERY 秒 GET /status，开演前就知道执行端在不在
    - 排队超过 stale 秒还没发出去的动作直接丢：链路断了又恢复时，臂不补做一串过期动作（§5.2 断链保护）
    - 不重发：超时的请求可能已经到了执行端，重发 press_sos 等于按两下
    """

    def __init__(self, base_url, timeout=1.5, stale=5.0, clock=time.monotonic):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.stale = stale
        self.clock = clock
        self.on_result = None
        self.on_link = None
        self.online = None
        self.mock = None          # 执行端 /status 自报的 mock：True 时手和臂不会真动，大屏不能写“真机同步”
        self._fail_seen = {}      # 执行端每只手后台失败的序号（/status 的 fail_seq），涨了就补报一次
        self.last_ok = None       # 执行端最近一次有回应的时间
        self.last_error = None
        self.counts = {"done": 0, "failed": 0, "dropped": 0}
        self.halted = False
        self._mu = threading.Lock()
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def start_probing(self):
        """回调挂好之后再开始探测，否则第一次"连上 / 连不上"会漏记。"""
        threading.Thread(target=self._prober, daemon=True).start()
        return self

    def send(self, pose, hand):
        self.q.put((self.clock(), pose, hand))

    def halt(self):
        """软件急停：置位并丢弃还没发出去的动作；另起线程叫执行端停（尽力而为）。"""
        self.halted = True
        while True:
            try:
                _queued, pose, hand = self.q.get_nowait()
            except queue.Empty:
                break
            self.counts["dropped"] += 1
            if self.on_result:
                _safe_call(self.on_result, pose, hand, False, "estop", "急停")
            self.q.task_done()
        threading.Thread(target=lambda: self._call("POST", "/stop"), daemon=True).start()

    def resume(self):
        """软件急停解除：恢复接收指令，不补做急停期间的动作。"""
        self.halted = False

    def drain(self):
        self.q.join()

    def probe(self):
        return self._call("GET", "/status")[0]

    def _note_status(self, body):
        try:
            st = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001 老版执行端或非 JSON：不知道就当不知道
            self.mock = None
            return
        self.mock = bool(st.get("mock"))
        # 执行端收到就回 200、动作在后台做：做到一半失败（串口断了、臂后端报错）只能从这里知道
        # 臂的动作都是 hand=1 发的，臂线程的失败按手#1 报
        workers = [(key, int(key), h) for key, h in (st.get("hands") or {}).items()]
        if isinstance(st.get("arm"), dict):
            workers.append(("arm", 1, st["arm"]))
        for key, hand, h in workers:
            seq = h.get("fail_seq") if isinstance(h, dict) else None
            if not isinstance(seq, int):
                continue
            seen = self._fail_seen.get(key)
            self._fail_seen[key] = seq
            if seen is not None and seq > seen and h.get("fail_pose") and self.on_result:
                self.counts["failed"] += 1
                self.last_error = h.get("last_error")
                _safe_call(self.on_result, h["fail_pose"], hand, False, "failed", h.get("last_error") or "")

    def status(self):
        ago = None if self.last_ok is None else round(self.clock() - self.last_ok, 1)
        return dict(self.counts, online=self.online, url=self.base, ok_ago=ago, last_error=self.last_error,
                    halted=self.halted, mock=self.mock)

    def _call(self, method, path):
        """返回 (ok, why, detail)。HTTP 错误码 = 执行端在线但拒绝（多半是动作表里没有这个 pose）。"""
        req = urllib.request.Request(self.base + path, data=b"" if method == "POST" else None, method=method)
        try:
            body = urllib.request.urlopen(req, timeout=self.timeout).read()
            if path == "/status":
                self._note_status(body)
        except urllib.error.HTTPError as e:
            # 4xx means the service is alive but rejected this action.  A
            # 5xx means its backend (for example the RH56 serial map) is not
            # usable, so preflight must treat it as offline/unavailable.
            self._link(e.code < 500)
            return False, "rejected" if e.code < 500 else "offline", _http_error_text(e)
        except Exception as e:  # noqa: BLE001 连不上、超时、对端断开都算离线
            self._link(False)
            return False, "offline", str(getattr(e, "reason", e))
        self._link(True)
        return True, "", ""

    def _link(self, online):
        with self._mu:
            if online:
                self.last_ok = self.clock()
            changed = online != self.online
            self.online = online
        if changed and self.on_link:
            _safe_call(self.on_link, online)

    def _worker(self):
        while True:
            queued, pose, hand = self.q.get()
            try:
                if self.halted:
                    ok, why, detail = False, "estop", "急停"
                    self.counts["dropped"] += 1
                else:
                    waited = self.clock() - queued
                    if waited > self.stale:
                        ok, why, detail = False, "stale", "排队 %.1f 秒" % waited
                        self.counts["dropped"] += 1
                    else:
                        ok, why, detail = self._call("POST", "/pose?name=%s&hand=%d" % (urllib.parse.quote(pose), hand))
                        self.counts["done" if ok else "failed"] += 1
                if not ok:
                    self.last_error = "%s：%s %s" % (pose, why, detail)
                if self.on_result:
                    _safe_call(self.on_result, pose, hand, ok, why, detail)
            finally:
                self.q.task_done()

    def _prober(self):
        while True:
            self.probe()
            time.sleep(ARM_PROBE_EVERY)


def _http_error_text(e):
    try:
        return json.loads(e.read().decode("utf-8")).get("error") or "HTTP %d" % e.code
    except Exception:  # noqa: BLE001
        return "HTTP %d" % e.code


def _safe_call(fn, *args):
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 回调出错不能弄死发送线程，否则之后连 SOS 都发不出去
        pass


class SpeechQueue:
    """llm 模式的台词线程：LLM 生成不占总线锁（状态机照常 tick），台词按入队顺序播出；
    后面还有台词在排队就不等 LLM、这句直接用 scripted，紧急台词最多晚几十毫秒。"""

    def __init__(self, bus):
        self.bus = bus
        self.q = queue.Queue()
        self.gen = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def put(self, key, params=None, text=None):
        self.q.put((self.gen, key, params or {}, text))

    def clear(self):
        """重置演示时作废还没播的台词。"""
        self.gen += 1

    def drain(self):
        self.q.join()

    def _worker(self):
        while True:
            gen, key, params, text = self.q.get()
            info = None
            try:
                if text is None and gen == self.gen:
                    try:
                        text, info = self.bus.llm.render(key, params, abort=lambda: not self.q.empty())
                    except Exception:  # noqa: BLE001 台词线程不能死，否则之后连 SOS 台词都播不出来
                        text = self.bus.llm.scripted(key, **params)
                with self.bus.lock:
                    if gen == self.gen:
                        self.bus._speak(text)
                        if info:
                            self.bus._note_llm(info)
            finally:
                self.q.task_done()


def _say(key, **params):
    return {"do": "say", "key": key, "params": params}


def _ev(type_, text, level="info", who="system"):
    return {"do": "event", "type": type_, "text": text, "level": level, "who": who}


class Bus:
    def __init__(self, cfg=None, data_dir=None, clock=time.time, arm=None, llm=None):
        self.cfg = cfg or config.load()
        self.clock = clock
        self.lock = threading.RLock()
        d = Path(data_dir) if data_dir else None
        self.events = EventLog(d / "events.jsonl" if d else None, clock)
        self.fsm = GuardFSM(self.cfg, clock)
        self.health = HealthTracker(d / "health.json" if d else None, clock, self.cfg["health_confirm_window"])
        self.style = StyleProfile(d / "voice_profile.json" if d else None, clock)
        self.guides = DeviceHelper()
        self.llm = llm or LLMClient.from_config(self.cfg)
        self.speech = SpeechQueue(self) if self.llm.enabled else None
        self.supervisor = Supervisor(self.cfg["contacts"])
        self.arm = arm if arm is not None else ArmClient(
            self.cfg["arm_url"], self.cfg["arm_timeout"], self.cfg["arm_stale"])
        self.arm.on_result = self._arm_result
        self.arm.on_link = self._arm_link
        self.arm_down_noted = False
        # 每只手最近一次收到的动作，给大屏的执行端动画用（手指操每拍是 quiet 动作，不进时间线，只能从这里看）。
        # 重置演示不清：重置不会让臂动，真机停在哪就显示哪
        self.hands = {"1": self._hand("home", "idle"), "2": self._hand("release", "idle")}
        if arm is None:
            self.arm.start_probing()
        self.clients = {}   # /api/events 的订阅者 → 最近一次来拉的时间（tts_player、asr_bridge…）
        self.started = clock()
        self.night_mode = self.cfg["night_override"]
        self.box_seen = None
        self.rng = random.SystemRandom()
        self.next_rps_id = 1
        self.estop = False   # 不放进 _reset_runtime：重置演示不能解除急停
        self.med = MedReminder(self.cfg, clock)
        self.comms = Comms(self.cfg, clock, self.clients)
        self._reset_runtime()
        channel = self.llm.mode
        if self.llm.mode == "llm":
            channel += "，超时 %g 秒回落 scripted" % self.llm.timeout if self.llm.enabled else "，接口未配置 → 全部 scripted"
        self.events.add("system", "系统启动（台词通道：%s）" % channel)

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
        self.fall_alerted = []   # 这次摔倒已经报过警的家属（老人说没事时给他们发“解除”）
        self.fall_photo = None   # 视觉判定跌倒时拍的一张现场照片，只给家属端看；老人说没事或回到空闲 30 秒后删除
        if hasattr(self, "med"):
            self.med.reset()
        if hasattr(self, "comms"):
            self.comms.reset()   # 通话中重置：两边页面看到 call 没了就自己挂断

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

    def _hand(self, pose, status):
        """status：idle 开机 / sent 已发 / ok 执行端已回执 / offline、rejected、stale 没做成"""
        return {"pose": pose, "label": pose_label(pose), "ts": self.clock(), "status": status}

    def _busy(self):
        """状态机当前占用的资源：保命链一启动，臂和手#1 就归状态机；软件急停时臂也一直占用。"""
        return {"arm"} if self.fsm.state != IDLE or self.estop else set()

    def _speak(self, text):
        if not text:
            return
        ev = self.events.add("say", text, who="ai")
        self.last_say = {"id": ev["id"], "text": text, "ts": ev["ts"]}

    def _say_line(self, key, params):
        if self.speech:
            self.speech.put(key, params)
        else:
            self._speak(self.llm.scripted(key, **params))

    def _say_text(self, text):
        if self.speech:
            self.speech.put(None, text=text)
        else:
            self._speak(text)

    def _note_llm(self, info):
        """LLM 回落只在刚进冷却或内容没过检查时记一笔（type=system，大屏不显示，操作台看得到）。"""
        reason = info["reason"] or ""
        if info["tripped"]:
            self.events.add("system", "LLM %s（%s）→ 回落 scripted，%g 秒内台词都走 scripted" % (
                "超时" if reason == "timeout" else "出错", self.llm.stats["last_error"], self.llm.cooldown), "warn")
        elif reason.startswith("rejected"):
            self.events.add("system", "LLM 台词没过检查（%s）→ 这句用 scripted" % reason.split(": ", 1)[1], "warn")

    def _notify(self, contact_id, kind, text, **extra):
        c = self.contact(contact_id or self.primary["id"]) or self.primary
        n = dict(extra, id=self.next_notif_id, ts=self.clock(), contact=c["id"], kind=kind, text=text, acked=False, delivery="local_demo_record")
        self.next_notif_id += 1
        self.notifications.append(n)
        del self.notifications[:-50]
        level = "danger" if kind in ("fall", "fall_alert", "120") else "info"
        self.events.add("notify", "通知%s（家属演示页面记录，未外部发送）：%s" % (c["name"], text), level, contact=c["id"], kind=kind, delivery="local_demo_record")

    def _run(self, acts):
        for a in acts:
            kind = a["do"]
            if kind == "say":
                params = {"contact": self.primary["name"], "pron": self.primary["pron"]}
                params.update(a.get("params", {}))
                self._say_line(a["key"], params)
            elif kind == "say_text":
                self._say_text(a["text"])
            elif kind == "event":
                self.events.add(a["type"], a["text"], a.get("level", "info"), who=a.get("who", "system"))
            elif kind == "arm":
                if self.estop:
                    if a["pose"] in SAFETY_POSES:
                        self.events.add("system", "急停中，保命动作未执行：%s；家属演示页面仍会生成通知记录（未外部发送）" % pose_label(a["pose"]),
                                        "danger", pose=a["pose"], hand=a["hand"])
                    continue
                self.hands[str(a["hand"])] = self._hand(a["pose"], "sent")   # 先记再发：回执可能在 send 里同步回来
                self.arm.send(a["pose"], a["hand"])
                if not a.get("quiet"):
                    self.events.add("arm", pose_label(a["pose"]), who="arm", pose=a["pose"], hand=a["hand"])
            elif kind == "notify":
                self._notify(a["contact"], a["kind"], a["text"], **a.get("extra", {}))
                if a["kind"] == "fall":   # 跌倒升级通知到谁，老人端就向谁发起紧急通话（不改状态机）
                    self._run(self.comms.emergency(a["contact"]))
            # "dial" 已由状态机配套的 event 记录；真实拨号接口待定

    # ---------- 执行端回执（ArmClient 的线程里调） ----------

    def _arm_result(self, pose, hand, ok, why, detail):
        """保命动作没做成 → 马上告诉演示员；其他动作只在刚开始失败时记一笔，免得手指操刷屏。
        type=system：操作台看得到、大屏不显示。家属通知仅生成本地演示记录，不依赖臂。"""
        with self.lock:
            h = self.hands.get(str(hand))
            if h and h["pose"] == pose and (h["status"] == "sent" or not ok):
                h["status"] = "ok" if ok else why
        if ok:
            self.arm_down_noted = False
            return
        reason = {"offline": "执行端连不上", "rejected": "执行端拒绝：%s" % detail,
                  "stale": "%s没发出去，按断链保护丢弃" % detail, "estop": "软件急停，已丢弃",
                  "failed": "执行端做到一半出错：%s" % detail}.get(why, why)
        with self.lock:
            if pose in SAFETY_POSES:
                self.events.add("system", "保命动作没做成：%s；%s。家属演示页面仍会生成通知记录（未外部发送）；现场可切 Plan B" % (
                    pose_label(pose), reason), "danger", pose=pose, hand=hand, why=why)
            elif not self.arm_down_noted:
                self.arm_down_noted = True
                self.events.add("system", "臂/手动作没做成：%s；%s。恢复之前后面的失败只计数，不再逐条记" % (
                    pose_label(pose), reason), "warn", pose=pose, hand=hand, why=why)

    def _arm_link(self, online):
        with self.lock:
            url = self.cfg["arm_url"]
            if online:
                self.events.add("system", "执行端已连上（%s）" % url, "ok")
            else:
                self.events.add("system", "执行端连不上（%s）：臂/手动作只记录不执行，保命链照常走软件通道" % url,
                                "warn" if self.fsm.state == IDLE else "danger")

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
            live = self.comms.live
            ctx = {
                "asking": g["state"] == ASKING,
                "dial_pending": g["dial_pending"],
                "health_pending": self.health.pending is not None,
                "exercise_active": self.exercise is not None,
                "med_pending": self.med.enabled and self.med.pending is not None,
                "call_incoming": bool(live and live["state"] == "ringing" and live["to"] == "elder"),
                "call_active": bool(live and "elder" in (live["from"], live["to"])),
                "contacts": self.cfg["contacts"],
            }
            name, slots = intent.classify(text, ctx)
            self._run(self._handle(name, slots, text))
            return {"ok": True, "intent": name}

    def _handle(self, name, slots, text):
        sup = self.supervisor
        skill = SKILL_OF.get(name)
        if skill:
            ok, reason = sup.check(skill, contact=slots.get("contact"), item=slots.get("item"), busy=self._busy())
            if not ok:
                what = "递“%s”" % slots["item"] if name == "deliver" else sup.allowed[skill]["desc"]
                if sup.blocked[-1]["busy"]:
                    return [_ev("blocked", "已挂起：%s（%s）" % (what, reason), "warn"),
                            _say("estop_hold" if self.estop else "skill_busy")]
                if name == "deliver":
                    return self._blocked(what, reason, "deliver_unknown", item=slots["item"])
                return self._blocked(what, reason, "blocked_skill")
        if name == "dial_confirm":
            return self.fsm.confirm_dial()
        if name == "dial_cancel":
            return self.fsm.cancel_dial()
        if name == "elder_ok":
            acts = self._elder_ok("语音“%s”" % text)
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
            acts = [_ev("deliver", "老人要药 → 递药盒", "info", "system"),
                    {"do": "arm", "pose": "deliver_medicine", "hand": 1},
                    _say("deliver_start")]
            return acts + (self.med.on_delivered() if self.med.enabled else [])
        if name == "med_taken":
            return self.med.taken()
        if name == "med_not_yet":
            return self.med.not_yet()
        if name == "call_family":
            c = self.contact(slots["contact"])
            # 床头屏在线就真打过去（语音通话）；不在线照旧发消息让家属回电话
            if self.comms.online("elder") and not self.comms.live:
                res, acts = self.comms.start("elder", c["id"])
                if res["ok"]:
                    return acts + [_say("call_dialing", contact=c["name"], pron=c["pron"])]
            return [{"do": "notify", "contact": c["id"], "kind": "call",
                     "text": "%s请您回个电话" % self.cfg["elder_title"]},
                    _say("call_family", contact=c["name"], pron=c["pron"])]
        if name == "call_answer":
            return self.comms.request_answer()
        if name == "call_hangup":
            live = self.comms.live
            return self.comms.hangup(live["id"] if live else None, "elder")[1]
        if name == "voice_play":
            return self._voice_play()
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
            latest = next((m["text"] for m in reversed(self.messages) if m["from"] == c["id"] and m.get("text")), None)
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
        return [_say("chitchat", text=text)]  # text 只给 llm 通道参考，scripted 模板不用

    # ---------- 猜拳（陪）----------

    def _rps_start(self):
        if self.rps:
            return []
        rid = self.next_rps_id
        self.next_rps_id += 1
        now = self.clock()
        # 机器人在看到老人手势之前就定好出什么，揭晓时手#1 才摆出来 —— 公平
        self.rps = {"round": rid, "robot": self.rng.choice(sorted(RPS_NAMES)), "revealed": False,
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
        """视觉（或操作台兜底按钮）回传老人的手势；输赢在这里判（rps_judge）。"""
        with self.lock:
            r = self.rps
            try:
                same = round_id is None or int(round_id) == r["round"]
            except (TypeError, ValueError):
                same = False
            if not r or not same:
                return {"ok": False, "reason": "no such round"}
            acts = self._rps_reveal()
            robot, names = r["robot"], RPS_NAMES
            self.rps = None
            if gesture not in RPS_NAMES:
                result = None
                acts += [_ev("rps", "猜拳第 %d 局：没看清老人的手势（我出%s）" % (r["round"], names[robot]), "warn"),
                         _say("rps_unclear")]
            else:
                result = rps_judge(gesture, robot)
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
            was_idle = self.fsm.state == IDLE
            acts += self.fsm.fall_detected(room, self.is_night())
            if was_idle and self.fsm.state == ASKING:
                acts += self._fall_alert(room)
            self._run(acts)
            return {"ok": True, "state": self.fsm.state}

    def _fall_alert(self, room):
        """识别到摔倒就立刻给所有家属报警，不等询问结果。询问、升级（按 SOS、紧急通话、逐个通知）照旧由状态机走；
        老人说“没事”会再给收到警报的家属发“解除”。"""
        g = self.fsm.snapshot()
        self.fall_alerted = [c["id"] for c in self.cfg["contacts"]]
        text = "%s在%s可能摔倒了，CareMate 正在确认情况；%d 秒内没有回应会自动按 SOS 并呼叫你" % (
            self.cfg["elder_title"], room, int(g["wait"]))
        return [{"do": "notify", "contact": cid, "kind": "fall_alert", "text": text} for cid in self.fall_alerted]

    def _elder_ok(self, via):
        """老人表示没事：状态机照旧处理；另外给收到过摔倒警报、但状态机没通知到的家属补一条“解除”。"""
        before = self.fsm.snapshot()
        acts = self.fsm.elder_ok(via)
        if acts:
            self.fall_photo = None   # 警报解除，现场照片立刻删
        if acts and self.fall_alerted:
            told = set(before["notified"]) if before["state"] in (SOS, HELP_COMING) else set()
            acts += [{"do": "notify", "contact": cid, "kind": "cancel",
                      "text": "%s说没事了，摔倒警报解除" % self.cfg["elder_title"]}
                     for cid in self.fall_alerted if cid not in told]
            self.fall_alerted = []
        return acts

    def button(self, key="ok"):
        with self.lock:
            if key != "ok":
                return {"ok": False, "reason": "unknown key"}
            self.events.add("button", "床头盒绿键按下", who="elder")
            acts = self._elder_ok("绿键")
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
            history = [m["text"] for m in self.messages if m["from"] == c["id"] and m.get("text")]
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

    # ---------- 通话 / 语音留言（老人端 ui/elder ↔ 家属端 ui/family） ----------

    def call_start(self, frm, to, emergency=False):
        with self.lock:
            emergency = bool(emergency) and frm == "elder"   # 紧急呼叫只有老人能发
            res, acts = self.comms.start(frm, to, emergency=emergency, urgent=self.fsm.state != IDLE,
                                         manual=emergency)
            self._run(acts)
            return res

    def call_offer(self, call_id, party, sdp):
        with self.lock:
            return self.comms.set_offer(call_id, party, sdp)

    def call_answer(self, call_id, party, sdp):
        with self.lock:
            res, acts = self.comms.answer(call_id, party, sdp)
            self._run(acts)
            return res

    def call_hangup(self, call_id, party):
        with self.lock:
            res, acts = self.comms.hangup(call_id, party)
            self._run(acts)
            return res

    def call_sdp(self, call_id, which):
        with self.lock:
            return self.comms.sdp(call_id, which)

    def voice_upload(self, frm, to, mime, data, dur, via=None):
        with self.lock:
            msgs, acts, err = self.comms.add_voice(frm, to, mime, data, dur, via)
            if err:
                return {"ok": False, "error": err}
            self.messages.extend(msgs)
            self._run(acts)
            return {"ok": True, "voice_id": msgs[0]["voice_id"], "dur": msgs[0]["dur"], "to": [m["to"] for m in msgs]}

    def voice_audio(self, voice_id):
        with self.lock:
            return self.comms.get_audio(voice_id)

    def voice_heard(self, voice_id, party):
        """收听方放完一条语音留言（老人这边：床头屏页面或床头盒都报 party=elder）。"""
        with self.lock:
            same = [m for m in self.messages if m.get("kind") == "voice" and str(m["voice_id"]) == str(voice_id)]
            if not same:
                return {"ok": False, "error": "没有这条留言"}
            m = next((x for x in same if x["to"] == party), None)
            if m is None:
                return {"ok": False, "error": "这条留言不是发给你的"}
            if not m["heard"]:
                m["heard"], m["heard_ts"] = True, self.clock()
            return {"ok": True}

    def _voice_play(self):
        """老人说“听留言”：让播放端（床头屏页面优先，其次床头盒）放，先放没听过的、最早的先放。"""
        target = self.comms.player()
        if target is None:
            return [_say("voice_no_screen")]
        mine = [m for m in self.messages if m.get("kind") == "voice" and m["to"] == "elder"
                and self.comms.get_audio(m["voice_id"])]
        if not mine:
            return [_say("voice_none")]
        m = next((x for x in mine if not x["heard"]), mine[-1])
        self.comms.request_play(m["voice_id"], target)
        return [_ev("voice", "播放%s的语音留言" % self.comms.name(m["from"]), who="elder")]

    def fall_photo_upload(self, data, mime, src="vision"):
        """视觉判定跌倒时上传一张现场照片（JPEG/PNG），家属端警报卡里显示。只留内存、只留这一张。"""
        with self.lock:
            base = (mime or "").split(";")[0].strip().lower()
            if base not in ("image/jpeg", "image/png", "image/webp"):
                return {"ok": False, "error": "只收 JPEG / PNG 照片"}
            if not data or len(data) > 2 * 1024 * 1024:
                return {"ok": False, "error": "照片为空或超过 2 MB"}
            self.photo_seq = getattr(self, "photo_seq", 0) + 1
            self.fall_photo = {"id": self.photo_seq, "ts": self.clock(), "mime": base, "data": bytes(data), "src": src}
            self.events.add("fall", "收到摔倒现场照片（%s，%d KB），只给家属端看，解除后删除" % (src, len(data) // 1024), "warn")
            return {"ok": True, "id": self.photo_seq}

    def dial120(self, by="elder", via="床头屏"):
        """一键 120。老人端（床头屏长按 2 秒 / 床头盒的 120 键）：长按本身就是确认，直接走模拟拨号 + 通知所有家属
        （真拨要 4G 模块或云通信，接口待定；手机上页面会另外打开系统拨号盘）。家属端：手机拨号盘已打开，这里只记一笔。"""
        with self.lock:
            title = self.cfg["elder_title"]
            if by == "elder":
                acts = [{"do": "dial", "number": "120"},
                        _ev("dial", "%s在%s一键拨打 120（模拟拨号）" % (title, via), "danger", "elder")]
                acts += [{"do": "notify", "contact": c["id"], "kind": "120",
                          "text": "%s在%s按了一键 120（模拟拨号），请尽快联系确认" % (title, via)}
                         for c in self.cfg["contacts"]]
                acts.append(_say("dial_done", contact=self.primary["name"]))
            else:
                c = self.contact(by)
                if c is None:
                    return {"ok": False, "error": "unknown contact"}
                acts = [_ev("dial", "%s在家属端点了拨打 120（手机拨号盘已打开）" % c["name"], "danger", "family")]
            self._run(acts)
            return {"ok": True}

    def family_replies(self, after=None, base="", key=""):
        """床头盒（手板）每 5 秒来拉家属发给老人的语音留言（《对接卡 v2》格式），按时间升序，只给 after 之后的。
        wav_url 是 WAV 16k/16bit/单声道；公网拉的时候 URL 里带上钥匙，手板取音频不用再加请求头。"""
        with self.lock:
            self.saw_client("elder-box")   # 手板在拉 = 床头盒在线，家属语音由它放
            try:
                after = int(after) if after not in (None, "") else 0
            except ValueError:
                return {"ok": False, "error": "after 要是上次拿到的 id（数字）"}
            replies = []
            for m in self.messages:
                clip = self.comms.get_audio(m["voice_id"]) if m.get("kind") == "voice" else None
                if not clip or m["to"] != "elder" or m["voice_id"] <= after or "wav" not in clip["mime"]:
                    continue
                url = "%s/api/voice_msg?id=%d" % (base, m["voice_id"]) + ("&key=" + urllib.parse.quote(key) if key else "")
                replies.append({"id": str(m["voice_id"]), "wav_url": url, "from": self.comms.name(m["from"]),
                                "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                      time.gmtime(m["ts"] + self.cfg["expected_utc_offset"] * 3600)),
                                "duration": m["dur"]})
                m["delivered_box"] = True
            return {"ok": True, "replies": replies}

    def elder_reread(self, ts):
        """老人端点“读给我听”：把那条家属文字留言再念一遍（走喇叭，和收到时同一句台词）。"""
        with self.lock:
            m = next((x for x in self.messages if x["to"] == "elder" and x.get("text")
                      and str(x["ts"]) == str(ts)), None)
            if m is None:
                return {"ok": False, "error": "没有这条留言"}
            self._run([_say("family_message", contact=self.comms.name(m["from"]), text=m["text"])])
            return {"ok": True}

    # ---------- 操作台 ----------

    def set_estop(self, on):
        """软件急停开关：开=臂和双手一律不再动作，关=恢复接收指令（不补做急停期间的动作）。"""
        with self.lock:
            on = bool(on)
            if on and not self.estop:
                self.estop = True
                halt = getattr(self.arm, "halt", None)
                if halt:
                    halt()
                self._run(self._stop_exercise("急停"))
                self.rps = None
                self.events.add("estop", "急停：臂和双手全部停止，动作指令一律不发；"
                                "保命链继续生成家属演示页面通知记录（未外部发送）", "danger")
            elif not on and self.estop:
                self.estop = False
                resume = getattr(self.arm, "resume", None)
                if resume:
                    resume()
                self.events.add("estop", "急停已解除：臂和双手恢复接收指令（不补做急停期间的动作）", "ok")
            return {"ok": True, "estop": self.estop}

    def med_remind(self):
        """操作台“用药提醒”：当场提醒一次（演示不依赖钟点）。没开开关、保命链进行中都不提醒。"""
        with self.lock:
            if not self.med.enabled:
                return {"ok": False, "error": "用药提醒没开：操作台打开开关，或 config med_reminder=on"}
            if self.fsm.state != IDLE:
                return {"ok": False, "error": "保命链进行中，先不提醒吃药"}
            self._run(self.med.remind(source="console"))
            return {"ok": True, "pending": self.med.snapshot()["pending"]}

    def med_enable(self, on):
        with self.lock:
            self._run(self.med.set_enabled(on))
            return {"ok": True, "enabled": self.med.enabled}

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

    def params(self):
        return {k: self.cfg[k] for k in config.TUNABLE}

    def set_params(self, updates):
        """运行中调演示参数（彩排时按访谈调等待时长、夜间窗口）。下一次事件起生效，重启恢复配置文件的值。"""
        with self.lock:
            before = dict(self.cfg)
            changed, errors = config.apply_params(self.cfg, updates)
            if errors:
                return {"ok": False, "errors": errors, "params": self.params()}
            self.health.confirm_window = self.cfg["health_confirm_window"]
            self.llm.timeout = self.cfg["llm_timeout"]
            self.llm.cooldown = self.cfg["llm_cooldown"]
            self.arm.timeout = self.cfg["arm_timeout"]
            self.arm.stale = self.cfg["arm_stale"]
            if changed:
                self.events.add("system", "演示参数调整：" + "，".join(
                    "%s %g→%g" % (k, before[k], v) for k, v in changed.items()))
            return {"ok": True, "changed": changed, "params": self.params()}

    def agent_propose(self, skill):
        """演示/测试用：模拟 LLM 直接请求某个技能，看 supervisor 拦不拦。"""
        with self.lock:
            ok, reason = self.supervisor.check(skill, busy=self._busy())
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
            if self.speech:
                self.speech.clear()
            self.night_mode = self.cfg["night_override"]
            self._reset_runtime()
            self.events.clear()
            self.events.add("reset", "演示已重置" + ("（健康记录与风格档案已清空）" if clear_data else ""))
            return {"ok": True}

    # ---------- 时钟 ----------

    def tick(self):
        with self.lock:
            acts = (self.fsm.tick() + self.health.tick() + self.med.tick(idle=self.fsm.state == IDLE)
                    + self.comms.tick())
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
            p = self.fall_photo
            if p and self.fsm.state == IDLE and now - p["ts"] > 30:
                self.fall_photo = None
            if acts:
                self._run(acts)

    # ---------- 床头盒（协议草案，待硬件同学确认） ----------

    def box_poll(self, box_id="box1"):
        """床头盒每 0.5 秒 GET 一次：拿灯色，同时当心跳。绿键仍走 POST /mock/button?key=ok。"""
        with self.lock:
            self.box_seen = {"id": box_id, "ts": self.clock()}
            g = self.fsm.snapshot()
            led, pattern = BOX_LED[g["state"]]
            return {"ok": True, "state": g["state"], "led": led, "pattern": pattern,
                    "night": self.is_night(), "remaining": g["remaining"], "dial_pending": g["dial_pending"]}

    def _box_online(self):
        return bool(self.box_seen and self.clock() - self.box_seen["ts"] < BOX_TIMEOUT)

    # ---------- 节点自检（tools/preflight 用） ----------

    def saw_client(self, name):
        """/api/events 的订阅者报到（voice/bus_client 带 client=脚本名）：据此判断喇叭、麦克风那头在不在。"""
        with self.lock:
            self.clients[name] = self.clock()

    def nodes(self):
        """只报事实，怎么修由 tools/preflight 给提示。"""
        with self.lock:
            now = self.clock()
            sk = self.skeleton
            live = sk["pose"] == "live"
            heard = self.last_heard
            watched = config.TUNABLE + ("night_override",)
            return {
                "bus": {"uptime": round(now - self.started, 1), "state": self.fsm.state,
                        "events": self.events.next_id - 1, "night": self.is_night()},
                "estop": self.estop,
                "tz": {"offset": round(time.localtime(self.clock()).tm_gmtoff / 3600, 2),
                       "expected": self.cfg["expected_utc_offset"]},
                "arm": self.arm.status(),
                "box": {"online": self._box_online(), "id": self.box_seen["id"] if self.box_seen else None,
                        "age": round(now - self.box_seen["ts"], 1) if self.box_seen else None},
                "vision": {"online": live and now - sk["ts"] < VISION_TIMEOUT,
                           "age": round(now - sk["ts"], 1) if live else None},
                "clients": {k: round(now - t, 1) for k, t in self.clients.items()},
                "heard": {"text": heard["text"], "age": round(now - heard["ts"], 1)} if heard else None,
                "llm": self.llm.status(),
                "params_changed": {k: [self.cfg[k], config.DEFAULTS[k]] for k in watched
                                   if self.cfg[k] != config.DEFAULTS[k]},
            }

    # ---------- 输出 ----------

    def snapshot(self):
        with self.lock:
            ex = self.exercise
            return {
                "now": self.clock(),
                "mode": self.cfg["llm_mode"],
                "estop": self.estop,
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
                "med": self.med.snapshot(),
                "comms": self.comms.snapshot(),
                "fall_photo": ({k: self.fall_photo[k] for k in ("id", "ts", "src")} if self.fall_photo else None),
                "skeleton": self.skeleton,
                "arm": self.arm.status(),
                "hands": {k: dict(v) for k, v in self.hands.items()},
                "box": {"online": self._box_online(), "id": self.box_seen["id"] if self.box_seen else None},
                "llm": self.llm.status(),
                "last_say": self.last_say,
                "last_heard": self.last_heard,
                "params": self.params(),
            }

    def poll(self, since):
        with self.lock:
            return {"state": self.snapshot(), "events": self.events.since(since),
                    "last_id": self.events.next_id - 1}


# ---------- HTTP ----------

class RequestBodyError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _reject_json_constant(value):
    raise RequestBodyError("non-finite JSON number: " + value)


def _read_json(handler):
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise RequestBodyError("invalid Content-Length") from None
    if n < 0:
        raise RequestBodyError("negative Content-Length")
    if n > MAX_JSON_BYTES:
        raise RequestBodyError("JSON body too large", 413)
    raw = handler.rfile.read(n) if n else b""
    if len(raw) != n:
        raise RequestBodyError("incomplete JSON body")
    if not n:
        return {}
    for enc in ("utf-8", "gb18030"):
        try:
            data = json.loads(raw.decode(enc), parse_constant=_reject_json_constant)
            if not isinstance(data, dict):
                raise RequestBodyError("JSON body must be an object")
            return data
        except RequestBodyError:
            raise
        except (UnicodeDecodeError, ValueError):
            continue
    raise RequestBodyError("invalid JSON body")


def _truthy(v):
    """开关参数：bool 直接用；字符串 0/false/off/no/空 算关，其余算开；数字按 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(v)


def _voice_from_form(bus, ctype, raw, query):
    """床头盒（S3 屏 + X5）按需求单发的 multipart/form-data：audio=WAV 文件、duration、time、elder（盒子编号），
    可选 to（默认 all = 发给所有家属）、from（默认 elder）。字段也可以放在 URL 查询串里。"""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + raw)
    fields, audio, mime = dict(query), None, "audio/wav"
    for part in msg.iter_parts() if msg.is_multipart() else ():
        name = part.get_param("name", header="content-disposition")
        if name == "audio":
            audio = part.get_payload(decode=True)
            mime = part.get_content_type() if part.get_content_type() != "application/octet-stream" else "audio/wav"
        elif name:
            fields[name] = part.get_payload(decode=True).decode("utf-8", "replace").strip()
    if not audio:
        return {"ok": False, "error": "multipart 里没有 audio 文件字段"}
    return bus.voice_upload(fields.get("from", "elder"), fields.get("to", "all"), mime, audio,
                            fields.get("duration") or fields.get("dur"), fields.get("elder"))


def dispatch_post(bus, path, body, query=None):
    """POST 路由表；HTTP 层和 demo_driver --local 共用。没有这个路由返回 None。"""
    q = query or {}
    arg = lambda k, d=None: body.get(k, q.get(k, d))  # noqa: E731
    routes = {
        "/mock/voice": lambda: bus.voice(arg("text", "")),
        "/mock/fall": lambda: bus.fall(arg("room")),
        "/mock/button": lambda: bus.button(arg("key", "ok")),
        "/api/skeleton": lambda: bus.update_skeleton(body or q),
        "/api/family/ack": lambda: bus.family_ack(arg("contact")),
        "/api/family/message": lambda: bus.family_message(arg("contact", bus.primary["id"]), arg("text", "")),
        "/api/echo/authorize": lambda: bus.echo_authorize(arg("contact", bus.primary["id"])),
        "/api/echo/revoke": lambda: bus.echo_revoke(arg("contact", bus.primary["id"])),
        "/api/night": lambda: bus.set_night(arg("mode", "auto")),
        "/api/estop": lambda: bus.set_estop(_truthy(arg("on", True))),
        "/api/med/remind": lambda: bus.med_remind(),
        "/api/med/enable": lambda: bus.med_enable(_truthy(arg("on", True))),
        "/api/params": lambda: bus.set_params(body or q),
        "/api/agent/propose": lambda: bus.agent_propose(arg("skill", "")),
        "/api/reset": lambda: bus.reset(clear_data=str(arg("all", "0")) == "1"),
        "/api/rps/gesture": lambda: bus.rps_gesture(arg("round"), arg("gesture")),
        "/api/call/start": lambda: bus.call_start(arg("from"), arg("to"), _truthy(arg("emergency", False))),
        "/api/call/offer": lambda: bus.call_offer(arg("id"), arg("party"), arg("sdp", "")),
        "/api/call/answer": lambda: bus.call_answer(arg("id"), arg("party"), arg("sdp", "")),
        "/api/call/hangup": lambda: bus.call_hangup(arg("id"), arg("party")),
        "/api/voice_msg/heard": lambda: bus.voice_heard(arg("id"), arg("party")),
        "/api/elder/reread": lambda: bus.elder_reread(arg("ts")),
        "/api/dial120": lambda: bus.dial120(arg("by", "elder"), arg("via", "床头屏")),
    }
    fn = routes.get(path)
    return fn() if fn else None


CALL_ROUTES = ("/api/call/start", "/api/call/offer", "/api/call/answer", "/api/call/hangup")


def make_handler(bus):
    class Handler(BaseHTTPRequestHandler):
        # ---------- 公网门 ----------

        def _public(self):
            return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("Cf-Ray"))

        def _key(self, q):
            return self.headers.get("X-CareMate-Token") or (q.get("key", [""])[0] if isinstance(q.get("key"), list)
                                                           else q.get("key", ""))

        def _gate(self, method, path, q):
            """局域网请求一律放行；公网请求只放家属端页面和家属端接口，接口要带钥匙。返回 None 放行，否则 (状态码, 原因)。"""
            if not self._public():
                return None
            if method == "GET" and (path == "/" or path.startswith(PUBLIC_UI)):
                return None
            if path not in (PUBLIC_GET if method == "GET" else PUBLIC_POST):
                return 403, "公网只开放家属端功能（动臂、操作台、大屏只在局域网）"
            token = bus.cfg.get("public_token") or ""
            if not token:
                return 403, "主脑没有设置 public_token，公网访问已关闭"
            if not hmac.compare_digest(self._key(q).encode("utf-8"), token.encode("utf-8")):
                return 401, "公网钥匙不对，请用家属端链接重新打开"
            return None

        def _base(self):
            visitor = self.headers.get("Cf-Visitor", "")
            scheme = "https" if "https" in visitor or self.headers.get("X-Forwarded-Proto") == "https" else "http"
            return "%s://%s" % (scheme, self.headers.get("Host", "127.0.0.1"))

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _reject_unread(self, obj, code):
            """Send rejection first, then briefly drain bytes to avoid Windows TCP reset.

            Parsing oversized bodies remains forbidden. Draining uses fixed-size
            chunks, a byte cap and a deadline; a slow/missing body cannot hold the
            request open indefinitely or postpone the error response.
            """
            self.close_connection = True
            self._json(obj, code)
            try:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
                remaining = min(max(int(self.headers.get("Content-Length") or 0), 0), 8 * 1024 * 1024)
                self.connection.settimeout(.1)
                deadline = time.monotonic() + .25
                while remaining and time.monotonic() < deadline:
                    chunk = self.rfile.read1(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, ValueError):
                pass

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
            denied = self._gate("GET", path, q)
            if denied:
                return self._json({"ok": False, "error": denied[1]}, denied[0])
            since = 0
            if path in ("/api/poll", "/api/events"):
                try:
                    since = int(q.get("since", ["0"])[0])
                    if since < 0:
                        raise ValueError
                except ValueError:
                    return self._json({"ok": False, "error": "since must be a nonnegative integer"}, 400)
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/ui/family" if self._public() else "/ui/console")
                self.end_headers()
                return
            if path == "/api/family_replies":
                return self._json(bus.family_replies(q.get("after", [""])[0], self._base(),
                                                     self._key(q) if self._public() else ""))
            if path == "/api/state":
                return self._json(bus.snapshot())
            if path == "/api/poll":
                client = q.get("client", [""])[0]
                if client:   # 老人端 / 家属端页面报到：通话要知道对方页面开没开
                    bus.saw_client(client[:40])
                return self._json(bus.poll(since))
            if path == "/api/fall_photo":
                p = bus.fall_photo
                if not p or str(p["id"]) != q.get("id", [""])[0]:
                    return self._json({"ok": False, "error": "没有这张照片（已解除并删除）"}, 404)
                return self._send(200, p["data"], p["mime"])
            if path == "/api/call/sdp":
                return self._json(bus.call_sdp(q.get("id", [""])[0], q.get("which", [""])[0]))
            if path == "/api/voice_msg":
                clip = bus.voice_audio(q.get("id", [""])[0])
                if clip is None:
                    return self._json({"ok": False, "error": "留言不存在或已过期"}, 404)
                return self._send(200, clip["data"], clip["mime"])
            if path == "/api/events":
                bus.saw_client(q.get("client", [""])[0] or "未标名@" + self.client_address[0])
                with bus.lock:
                    return self._json({"events": bus.events.since(since),
                                       "last_id": bus.events.next_id - 1})
            if path == "/api/health/report":
                with bus.lock:
                    return self._json(bus.health.daily_report())
            if path == "/api/guides":
                return self._json(bus.guides.catalog())
            if path == "/api/box":
                return self._json(bus.box_poll(q.get("id", ["box1"])[0]))
            if path == "/api/params":
                with bus.lock:
                    return self._json({"params": bus.params(), "limits": config.LIMITS})
            if path == "/api/nodes":
                return self._json(bus.nodes())
            if path.startswith("/ui/"):
                return self._static(path)
            self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            denied = self._gate("POST", url.path, q)
            if denied:
                return self._reject_unread({"ok": False, "error": denied[1]}, denied[0])
            if url.path == "/api/fall_photo":   # 正文是照片原始字节
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2 * 1024 * 1024:
                    return self._reject_unread({"ok": False, "error": "照片太大"}, 413)
                return self._json(bus.fall_photo_upload(self.rfile.read(n) if n else b"",
                                                        self.headers.get("Content-Type", ""), q.get("src", "vision")))
            if url.path == "/api/voice_msg":   # 正文是录音（原始字节或 multipart），不是 JSON
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_VOICE_BYTES + 64 * 1024:
                    return self._reject_unread({"ok": False, "error": "录音太大"}, 413)
                ctype = self.headers.get("Content-Type", "")
                raw = self.rfile.read(n) if n else b""
                if ctype.lower().startswith("multipart/form-data"):
                    return self._json(_voice_from_form(bus, ctype, raw, q))
                return self._json(bus.voice_upload(q.get("from"), q.get("to"), ctype, raw,
                                                   q.get("dur") or q.get("duration"), q.get("elder")))
            try:
                body = _read_json(self)
            except RequestBodyError as exc:
                if exc.status == 413:
                    return self._reject_unread({"ok": False, "error": str(exc)}, exc.status)
                return self._json({"ok": False, "error": str(exc)}, exc.status)
            if self._public():
                # 公网只能以家属身份通话 / 拨 120，不能冒充老人那头（老人那头在局域网）
                who = body.get("party", q.get("party")) if url.path != "/api/call/start" else body.get("from", q.get("from"))
                if (url.path in CALL_ROUTES and who == "elder") or \
                        (url.path == "/api/dial120" and body.get("by", q.get("by", "elder")) == "elder"):
                    return self._json({"ok": False, "error": "公网只能以家属身份操作"}, 403)
            result = dispatch_post(bus, url.path, body, q)
            if result is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            self._json(result)

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
    warnings = []
    try:
        cfg = config.load(warnings=warnings)
    except config.ConfigError as e:
        sys.exit(str(e))
    for w in warnings:
        print("[配置提示] " + w)
    bus = Bus(cfg, data_dir=config.DATA_DIR)
    server = serve(bus, args.host, args.port)
    base = "http://127.0.0.1:%d" % args.port
    print("master_service on %s:%d（台词通道：%s）" % (args.host, args.port, cfg["llm_mode"]))
    for name in ("monitor", "family", "console"):
        print("  %-8s %s/ui/%s" % (name, base, name))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
