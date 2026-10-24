"""X5#3 上的动作 HTTP 服务 —— 全系统「物理动作」唯一入口。

启动：
    cd code && python -m hand_controller.arm_service     (默认 mock, 端口 8013)
    MOCK_SERIAL=0 python -m hand_controller.arm_service  (真机)

端点（全部动作名词表来自 poses.ALL_ACTIONS，supervisor 与此同源）
    GET  /status
    POST /pose/{name}           摆姿态（或执行时间序列如 wave）
    POST /handover_med          递药盒（示教轨迹回放）
    POST /handover_item         递手机
    POST /press_sos             按 SOS 键（状态机才允许调）
    POST /reset                 归位
"""
from __future__ import annotations
import json
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hand_controller.poses import POSE_VOCAB, SEQUENCES, ALL_ACTIONS
from hand_controller.rh56f2_driver import RH56F2

MOCK = os.environ.get("MOCK_SERIAL", "1") != "0"   # 与 driver 同一开关

RH56_PROTO = os.environ.get("RH56_PROTO", "modbus").lower()   # modbus | eb90
if RH56_PROTO == "eb90":                       # 真机: 因时 EB90 串口协议
    from hand_controller.rh56f2_eb90 import RH56F2 as RH56F2_Impl
else:                                          # 占位 Modbus（历史/兼容）
    RH56F2_Impl = RH56F2

try:    # Windows GBK 控制台兜底
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SERVICE_PORT = int(os.environ.get("ARM_PORT", "8013"))
SOS_BUTTON_HOST = os.environ.get("SOS_ENDPOINT",
                                 "http://127.0.0.1:8000/api/sos_pressed")
ARM_BACKEND = os.environ.get("ARM_BACKEND", "mock")   # mock | vendor_sdk | waypoint_seq

_action_log: list[dict] = []
_lock = threading.Lock()


def _log(kind: str, detail: str):
    with _lock:
        _action_log.append({"t": time.strftime("%H:%M:%S"), "kind": kind, "detail": detail})


# ---------- 手（Modbus） ----------

# 手实例规则（10/25 定：单板双手同步）：
#   MOCK        → 一个哑实例，hand1=hand2，零硬件依赖。
#   DUAL_HAND=1 → 两只手各占一个 USB-RS485（HAND1_PORT/HAND2_PORT），
#                 所有动作同步扇出到两只手（屏端零改动）。
#   其余        → 单手模式：hand=1/2 都指向 HAND1_PORT 同一实例。
# 任一真手端口缺失/失灵 → 灰手占位（记错误日志），服务与另一只手照常。
DUAL_HAND = (not MOCK) and os.environ.get("DUAL_HAND", "0") == "1"


class _DeadHand:
    """端口缺失/串口失灵时的占位实例：move 只记错，HTTP 与另一只手不炸。"""

    def __init__(self, name: str, err: str):
        self.name = name
        self.err = err
        self.log: list[str] = []

    def move(self, joints):
        _log("hand_err", "%s 无法动: %s" % (self.name, self.err))

    def status(self) -> dict:
        return {"mock": MOCK, "proto": "none", "error": self.err}


def _open_hand(port: str, sid: int, name: str):
    try:
        return RH56F2_Impl(port=port, slave_id=sid)
    except Exception as e:
        err = "%s(%s): %s" % (type(e).__name__, port, e)
        _log("hand_down", "%s %s" % (name, err))
        return _DeadHand(name, err)


if MOCK:
    _hand1 = RH56F2_Impl()
    _hand2 = _hand1
elif DUAL_HAND:
    _hand1 = _open_hand(os.environ.get("HAND1_PORT", "COM5"),
                        int(os.environ.get("HAND1_ID", "1")), "hand1")
    _hand2 = _open_hand(os.environ.get("HAND2_PORT", "COM6"),
                        int(os.environ.get("HAND2_ID", "2")), "hand2")
else:
    _hand1 = _open_hand(os.environ.get("HAND1_PORT", "COM6"),
                        int(os.environ.get("HAND1_ID", "1")), "hand1")
    _hand2 = _hand1


def do_pose(name: str, hand: int = 0) -> dict:
    """执行一个姿态或时间序列（挥手等）。
    hand=0（默认）→ 自动：DUAL_HAND=1 时双手同步扇出，否则手#1；
    hand=1/2 → 指定单手。"""
    if name == "rps_random":                    # 猜拳：每次随机出一手(陪伴互动)
        pick = random.choice(("rock", "scissors", "paper"))
        hands = ([_hand1, _hand2] if (DUAL_HAND and hand == 0)
                 else [_hand2 if hand == 2 else _hand1])
        for h in hands:
            _safe_apply(pick, h)
        _log("pose", "rps_random:" + pick)
        return {"ok": True, "did": "rps:" + pick,
                "hand": ("both" if (DUAL_HAND and hand == 0) else (hand or 1))}
    if hand == 0:
        hands = [_hand1, _hand2] if DUAL_HAND else [_hand1]
    else:
        hands = [_hand2 if hand == 2 else _hand1]
    tag = ("both:" if (hand == 0 and DUAL_HAND) else
           "hand2:" if hand == 2 else "")
    hand_lbl = ("both" if tag == "both:" else (hand or 1))
    if name in SEQUENCES:
        for pose, hold in SEQUENCES[name]:
            for h in hands:
                _safe_apply(pose, h)
            time.sleep(hold)
        _log("seq", tag + name)
        return {"ok": True, "did": "seq:" + name, "hand": hand_lbl}
    if name in POSE_VOCAB:
        for h in hands:
            _safe_apply(name, h)
        _log("pose", tag + name)
        return {"ok": True, "did": "pose:" + name, "hand": hand_lbl}
    return {"ok": False, "error": "unknown pose: %s" % name}


def _safe_apply(pose_name: str, h):
    """单手单姿态应用；任何串口异常都只记日志，绝不炸 HTTP。"""
    try:
        h.move(POSE_VOCAB[pose_name])
    except Exception as e:
        _log("hand_err", "%s %s: %s" % (getattr(h, "name", "hand"),
                                        pose_name, e))


def _apply(pose_name: str, hand=None):
    (hand or _hand1).move(POSE_VOCAB[pose_name])


# ---------- 臂（后端可换） ----------

def arm_execute(waypoint_seq_name: str) -> dict:
    """
    waypoint_seq_name: handover_med / handover_item / press_sos / reset
    mock: 打印步骤带延时——整条链路在笔记本上肉眼可跑。
    waypoint_seq: 现场示教后把点写进 WAYPOINTS 再启用。
    vendor_sdk: 拿到 SDK 后把厂商替换，本函数签名不动。
    """
    seq = WAYPOINTS.get(waypoint_seq_name)
    if not seq:
        return {"ok": False, "error": "unknown waypoint seq %s" % waypoint_seq_name}
    for step in seq:
        kind, arg, hold = step
        if kind == "pose":
            do_pose(arg)
        elif kind == "wait_put":                   # 伸手等待放置→合拢+夹持检测
            _wait_put(hold, arg)
        elif kind == "move":                       # move = 臂移动占位（现场换成真调用）
            _log("arm_move", arg)
        elif kind == "http":                       # e.g. 触发 SOS 键的"咔哒"反馈
            try:
                urllib.request.urlopen(urllib.request.Request(
                    arg, data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"}), timeout=2)
                _log("http_ok", arg)
            except Exception as e:
                _log("http_fail", "%s (%s)" % (arg, e))
        time.sleep(hold)
    _log("seq", "arm:" + waypoint_seq_name)
    return {"ok": True, "did": "arm:" + waypoint_seq_name}


def _wait_put(timeout_s: float, tag: str):
    """还药交互：手已张开,等老人把物品放进掌心(最长 timeout_s 秒),
    到点后合拢并用手指实际位置(EB90 0x0422 组读)判断夹没夹住;
    空手→再开一次给 6 秒第二次机会,复检后照常收回(不空转卡死)。
    WAIT_PUT_FAST=1 时秒变 2 秒/6 秒(开发调试用,现场不设)。"""
    if os.environ.get("WAIT_PUT_FAST", "0") == "1":
        timeout_s = min(timeout_s, 2.0)
    _log("put_wait", "%s 手已张开,等待放置(最长%d秒)" % (tag, int(timeout_s)))
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(1.0)                    # 真臂版此处可轮询臂末端接近传感器
    _grasp_detect(tag)


def _grasp_detect(tag: str):
    """合拢+夹持判定:≥2 指停在 8~90 开度=有物;全指≈0=空握。手缺失时仅记日志。"""
    try:
        h = _hand1
        h.close_hand()                     # 力控阈值已在 init 时限 500g,不会捏碎药盒
        time.sleep(1.2)
        st = h.status() or {}
        pct = st.get("act_pct") or {}
        vals = [v for v in pct.values() if isinstance(v, (int, float))]
        holding = sum(1 for v in vals if 8 <= v <= 90) >= 2
        _log("put_detect", "%s: %s (%s)" % (
            tag, "夹住了" if holding else "空的",
            ",".join("%s=%.0f" % kv for kv in pct.items())))
        if not holding:
            h.open_hand()
            _log("put_wait", "%s 第二次机会(6秒)..." % tag)
            time.sleep(6.0)
            h.close_hand()
            time.sleep(1.2)
            st = h.status() or {}
            pct = st.get("act_pct") or {}
            vals = [v for v in pct.values() if isinstance(v, (int, float))]
            holding = sum(1 for v in vals if 8 <= v <= 90) >= 2
            _log("put_detect", "%s 复检: %s" % (tag, "夹住了" if holding else "仍空,照常收回"))
    except Exception as e:
        _log("hand_err", "%s 夹持检测失败: %s" % (tag, e))


# 示教点占位序列（现场拖动示教后替换成真实 waypoint 表）
WAYPOINTS = {
    "reset": [
        ("pose", "release", 0.3), ("move", "home", 1.0),
    ],
    "press_sos": [
        ("move", "above_sos", 1.2), ("pose", "grip_pillow", 0.2),
        ("move", "press_sos", 0.8),                 # 按下去
        ("http", SOS_BUTTON_HOST, 0.3),             # 状态机收到"键已被物理按下"
        ("move", "above_sos", 0.8), ("pose", "release", 0.2),
        ("pose", "thumbs_up", 0.5),                 # 「收到」的手势
    ],
    "handover_med": [
        ("move", "above_tray", 1.0), ("pose", "release", 0.3),
        ("move", "pick_med", 0.8), ("pose", "hold_card", 0.3),
        ("move", "above_tray", 0.8), ("move", "bedside", 1.5),
        ("move", "bedside_low", 0.8), ("pose", "release", 0.4),
        ("move", "home", 1.2),
    ],
    "return_med": [                             # 还药:伸过来→张开→等放入→夹持检测→放回托盘
        ("move", "bedside", 1.0),
        ("pose", "release", 0.4),               # 手全开
        ("wait_put", "return_med", 30),         # 等老人放入(最长30s)→合拢+夹持检测
        ("move", "bedside", 0.6),               # 抬起
        ("move", "above_tray", 1.0),
        ("move", "tray_put", 0.8),              # 回到放回点
        ("pose", "release", 0.4),               # 松手落药
        ("pose", "wave", 0.4),                  # 「收好了」小手势
        ("move", "home", 1.2),
    ],
    "handover_item": [
        ("move", "above_tray", 1.0), ("pose", "release", 0.3),
        ("move", "pick_item", 0.8), ("pose", "hold_card", 0.3),
        ("move", "above_tray", 0.8), ("move", "bedside", 1.5),
        ("move", "bedside_low", 0.8), ("pose", "release", 0.4),
        ("move", "home", 1.2),
    ],
}


# ---------- 语音（X5 离线识别：vosk 小模型,按键录音 POST 进来） ----------
_VOICE = {"model": None, "err": ""}
VOICE_MODEL_DIR = "/root/caremate/vosk-cn"
VOICE_CMDS = {          # 听到的文本含关键词即命中(长词优先);词表=屏上六键能力+口语同义
    "握拳": "clench_open", "张开": "clench_open",
    "握住": "clench_open", "抓": "clench_open", "拳": "clench_open",
    "挥手": "wave", "你好": "wave", "招手": "wave", "打招呼": "wave",
    "点赞": "thumbs_up", "大拇指": "thumbs_up", "棒": "thumbs_up",
    "厉害": "thumbs_up", "夸": "thumbs_up",
    "石头": "rps_random", "剪刀": "rps_random", "布": "rps_random",
    "猜拳": "rps_random", "玩": "rps_random",
    "手指操": "hand_exercise", "手指": "hand_exercise",
    "做操": "hand_exercise", "操": "hand_exercise",
    "锻炼": "hand_exercise", "动动": "hand_exercise",
    "归位": "reset", "复位": "reset", "回去": "reset",
}
# 语音→臂序列(臂主导键的语音入口;与按键/示教序列同名)
VOICE_ARM = {
    "递药": "handover_med", "送药": "handover_med", "发药": "handover_med",
    "拿药": "handover_med", "药盒": "handover_med",
    "还药": "return_med", "交回": "return_med", "放回": "return_med",
    "按铃": "press_sos", "呼叫": "press_sos", "按铃铛": "press_sos",
    "救命": "press_sos", "紧急": "press_sos",
}

# ---------- 开放语音:LLM agent 优先(配 key),同义词词表作离线兜底;理解开放/执行封闭 ----------
# 口语词 → 动作(长词优先包含匹配)。覆盖 POSE_VOCAB/SEQUENCES 全部动作+按钮上没有的。
VOICE_OPEN = {
    # 手势互动(ok/剪刀手/摊手 等按钮上没有的)
    "ok手势": "ok", "比个ok": "ok", "比ok": "ok", "okay": "ok", "欧k": "ok",
    "圆圈手势": "ok", "ok": "ok",
    "剪刀手": "scissors", "石头": "rock", "剪刀": "scissors", "布": "paper",
    "摊开手": "paper", "摊手": "paper", "摊开": "paper",
    "挥挥手": "wave", "摆摆手": "wave", "打招呼": "wave", "招手": "wave",
    "再见": "wave", "拜拜": "wave", "你好": "wave", "挥手": "wave",
    "竖大拇指": "thumbs_up", "竖个大拇指": "thumbs_up", "大拇指": "thumbs_up",
    "真棒": "thumbs_up", "太棒了": "thumbs_up", "厉害": "thumbs_up",
    "点赞": "thumbs_up", "棒": "thumbs_up",
    "夸夸我": "nod_like", "表扬我": "nod_like", "谢谢": "nod_like", "点头": "nod_like", "夸": "nod_like",
    # 数字手指(比一~比五/几根手指)
    "一根手指": "fingers_1", "一只手指": "fingers_1", "比一": "fingers_1", "数一": "fingers_1",
    "两根手指": "fingers_2", "两只手指": "fingers_2", "比二": "fingers_2", "数二": "fingers_2",
    "耶": "fingers_2", "胜利": "fingers_2",
    "三根手指": "fingers_3", "比三": "fingers_3", "数三": "fingers_3",
    "四根手指": "fingers_4", "比四": "fingers_4", "数四": "fingers_4",
    "五指全开": "fingers_5", "比五": "fingers_5", "数五": "fingers_5",
    # 精细动作(对指/波浪卷指,康复向,全不在按钮上)
    "拉勾": "pinch_4", "拉钩": "pinch_4", "捏小指": "pinch_4",
    "捏一下": "pinch_1", "捏合": "pinch_1", "对指": "pinch_1", "捻一捻": "pinch_1",
    "捏中指": "pinch_2", "捏无名指": "pinch_3",
    "波浪": "curl_pk", "卷指": "curl_pk", "卷小指": "curl_pk",
    "卷无名指": "curl_pk_rg", "卷中指": "curl_mid",
    "全卷": "curl_all", "都卷起来": "curl_all",
    "轻握": "fist_soft", "轻握拳": "fist_soft", "虚拳": "fist_soft",
    "捧杯": "cup", "端杯": "cup", "半握": "cup", "捧": "cup",
    "捏卡": "hold_card", "拿卡": "hold_card", "捏卡片": "hold_card",
    "松手": "release", "松开": "release", "放开": "release", "放手": "release",
    "按一按": "grip_pillow", "轻按": "grip_pillow",
    # 序列/操/玩
    "握拳": "clench_open", "握住": "clench_open", "抓一下": "clench_open",
    "开合": "clench_open", "张开": "clench_open",
    "数数": "count_five", "数一数": "count_five", "数一到五": "count_five",
    "从一数到五": "count_five", "数到五": "count_five", "一二三": "count_five",
    "手指操": "hand_exercise", "做操": "hand_exercise", "活动手指": "hand_exercise",
    "动动手指": "hand_exercise", "康复操": "hand_exercise", "锻炼": "hand_exercise", "操": "hand_exercise",
    "安抚": "comfort_rhythm", "安慰": "comfort_rhythm", "拍拍": "comfort_rhythm",
    "轻拍": "comfort_rhythm", "哄哄我": "comfort_rhythm",
    "石头剪刀布": "rps_random", "猜个拳": "rps_random", "出一把拳": "rps_random",
    "来一局": "rps_random", "猜拳": "rps_random",
    # 留言/听留言(v5.2:agent 白名单含 message_record/message_play;词表作离线兜底)
    "听留言": "message_play", "放留言": "message_play", "有没有回话": "message_play",
    "有没有新留言": "message_play", "有没有给我留言": "message_play",
    "我想留言": "message_record", "给家人留言": "message_record", "录留言": "message_record",
    "说两句话": "message_record", "说几句话": "message_record", "跟儿子说两句": "message_record",
    # 归位
    "回到原位": "reset", "回原位": "reset", "归位": "reset", "复位": "reset", "回去": "reset",
    # 臂序列词(臂板生效;手板上仅记日志,无害)
    "递药": "handover_med", "送药": "handover_med", "拿药": "handover_med", "药盒": "handover_med",
    "还药": "return_med", "放回": "return_med", "交回": "return_med",
    "按铃": "press_sos", "呼叫": "press_sos", "按铃铛": "press_sos",
    "救命": "press_sos", "紧急": "press_sos",
}
_VOICE_ACTIONS = set(ALL_ACTIONS) | {"rps_random"} | set(WAYPOINTS) | {"message_record", "message_play"}   # LLM 只许从这个白名单选(含臂路点+留言)

# 动作中文名(LLM 提示词用):让模型分清“拉勾=pinch_4”这类歧义,不用猜英文名
_ACTION_ZH = {
    # 姿态
    "rock": "握拳(石头)", "paper": "五指全张开(布/摊手)", "scissors": "剪刀手(耶/V字)",
    "ok": "OK手势(拇指食指捏个圈)",
    "thumbs_up": "竖大拇指(点赞)",
    "fingers_1": "伸出一根手指(=1)", "fingers_2": "伸出两根手指(=2)",
    "fingers_3": "伸出三根手指(=3)", "fingers_4": "伸出四根手指(=4)", "fingers_5": "五指全开(=5)",
    "hold_card": "捏住卡片的姿势", "release": "全部松开(张开手)", "grip_pillow": "手掌轻按住(按一按)",
    "fist_soft": "松松地握拳", "cup": "半握像捧着杯子",
    "curl_pk": "小指先卷起的波浪卷指", "curl_pk_rg": "无名指小指依次卷起(波浪)", "curl_mid": "中指起的波浪卷指",
    "curl_all": "四根手指全卷起来",
    "pinch_1": "拇指捏食指(捏一下/对指)", "pinch_2": "拇指捏中指", "pinch_3": "拇指捏无名指",
    "pinch_4": "拇指捏小指=拉勾(拉钩约定)",
    # 序列
    "wave": "挥手摆动(打招呼/再见)", "nod_like": "竖大拇指轻轻点头(夸我/谢谢)",
    "clench_open": "握拳→张开 反复开合", "count_five": "从一数到五(手指逐根伸出)",
    "comfort_rhythm": "轻拍安抚的节律(拍拍/哄哄)", "hand_exercise": "全套手指康复操(约37秒)",
    "rps_game": "玩一局石头剪刀布", "rps_random": "随机出一个拳(猜拳)",
    # 臂路点(臂板生效;手板上仅记日志)
    "reset": "回到原位(收回默认姿势)", "handover_med": "递药:把药盒递过来",
    "handover_item": "递东西:把物品递给用户", "return_med": "还药:把药盒放回托盘", "press_sos": "按呼叫铃",
    # 留言(v5.2)
    "message_record": "给家人录一段语音留言(我想和儿子说两句话→自动开始录)",
    "message_play": "听家人的新留言/回话(儿子有没有给我回话)",
}

# LLM agent(可选):设了 AGENT_API_KEY 才启用;不配就纯同义词层,离线也够用
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "")
AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.openai.com/v1")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")


def agent_intent(text: str):
    """LLM agent:开放理解 → 只能选 _VOICE_ACTIONS 白名单(选不了=none)。
    返回 (action|None, reply|None, llm_ok);llm_ok=True=LLM 正常应答(含明确 none)。
    任何异常都打日志并回退词表层(调用方据此决定走离线路径)。"""
    try:
        import json as _j, re as _re, urllib.request
        names = ", ".join("%s=%s" % (a, _ACTION_ZH.get(a, a)) for a in sorted(_VOICE_ACTIONS))
        sys_prompt = (
            "你是养老陪伴机械手的语音理解器,用户多为老人短句。"
            "从固定动作表选一个,只输出JSON{\"action\":\"名字或none\",\"reply\":\"可选的一句暖心回话\","
            "\"object\":null}。动作表:" + names + "。"
            "注意:转写来自中文语音识别,音近字很乱(ok 常被写成 欧开/偶K/区开/圈开),"
            "请按读音大胆还原真实意图再选动作。"
            "给家人留言/听家人回话也是正常指令,别当成闲聊;"
            "纯聊天、要拿东西(手不会自己取物)、听不懂 → action=none。只输出JSON。")
        body = _j.dumps({"model": AGENT_MODEL,
                         "messages": [{"role": "system", "content": sys_prompt},
                                      {"role": "user", "content": text}],
                         "temperature": 0}).encode()
        req = urllib.request.Request(
            AGENT_BASE_URL.rstrip("/") + "/chat/completions", data=body,
            headers={"Authorization": "Bearer " + AGENT_API_KEY,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            txt = _j.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
        m = _re.search(r"\{.*\}", txt, _re.S)       # 剥掉可能的代码围栏
        obj = _j.loads(m.group(0) if m else "{}")
        act = obj.get("action")
        if act not in _VOICE_ACTIONS:
            act = None
        return act, (obj.get("reply") or None), True
    except Exception as e:
        _log("agent_err", str(e)[:120])
        return None, None, False


def voice_init():
    """懒加载 vosk 模型;失败仅记原因,服务不炸。"""
    if _VOICE["model"] is not None or _VOICE["err"]:
        return
    try:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        _VOICE["model"] = Model(VOICE_MODEL_DIR)
        _log("voice_up", "vosk 模型就绪")
    except Exception as e:
        _VOICE["err"] = "%s: %s" % (type(e).__name__, e)
        _log("voice_down", _VOICE["err"])


def voice_cmd(pcm: bytes) -> dict:
    """开放语音三层:S3 录音(16k/16bit/mono 裸PCM) → ①(可选)LLM agent 语义理解(优先) →
    ②同义词词表兜底 → ③词表语法救援;都 miss 才回「没听清」。"""
    if len(pcm) < 8000:
        return {"ok": False, "error": "audio too short"}
    voice_init()
    if _VOICE["model"] is None:
        return {"ok": False, "error": _VOICE["err"] or "asr unavailable"}
    from vosk import KaldiRecognizer
    import json as _json

    def _pick(rec):
        if rec.AcceptWaveform(pcm):
            return (_json.loads(rec.FinalResult()).get("text") or "").strip()
        return (_json.loads(rec.FinalResult()).get("text") or "").strip()

    # ① 开放转写(不限词表),自然文本
    rec = KaldiRecognizer(_VOICE["model"], 16000)
    heard = _pick(rec)
    if not heard:
        return {"ok": False, "heard": "", "error": "没听清"}
    norm = heard.replace(" ", "").lower()      # vosk 分词带空格;latin 统一小写
    action = None
    reply = None
    agent_ok = False
    # ① LLM agent(配了 key 即优先):开放语义理解,vosk 音近字再糊也能按意思选对;
    #    agent 应答过(含明确 none)就不再让词表抢答,避免“比一”这类短词误配
    if AGENT_API_KEY:
        action, reply, agent_ok = agent_intent(heard)
    # ② 同义词直配(没配 key / agent 挂了时的离线兜底;长词优先)
    if action is None and not agent_ok:
        for kw in sorted(VOICE_OPEN, key=len, reverse=True):
            if kw in norm:
                action = VOICE_OPEN[kw]
                break
    # ③ 救援:词表语法约束重识别一次(仅离线路径:转写糊但声音在时,强制从关键词里挑最贴近的)
    if action is None and not agent_ok:
        grammar = _json.dumps(list(VOICE_OPEN) + ["[unk]"], ensure_ascii=False)
        rec2 = KaldiRecognizer(_VOICE["model"], 16000, grammar)
        heard2 = _pick(rec2)
        if heard2:
            norm2 = heard2.replace(" ", "").lower()
            for kw in sorted(VOICE_OPEN, key=len, reverse=True):
                if kw in norm2:
                    action = VOICE_OPEN[kw]
                    heard = heard2
                    break
    if action in (None, "none"):
        out = {"ok": False, "heard": heard, "error": "没听清或不在动作表"}
        if reply:
            out["reply"] = reply              # agent 听懂了但不执行(白名单外),回话保留
        _log("voice_out", "%s → 无匹配%s" % (heard, "(agent)" if agent_ok else ""))
        return out
    if action == "message_record":             # 语音说想留言 → S3 收到该 action 播提示音后自动开录
        _log("voice_out", "%s → message_record%s" % (heard, "(agent)" if agent_ok else ""))
        out = {"ok": True, "heard": heard, "action": "message_record", "did": "msg:record"}
        if reply:
            out["reply"] = reply
        return out
    if action == "message_play":               # 语音说想听留言 → 板上查信箱,只数家属回话(fam_);msg_=老人自己录的,老人侧不播
        fams = [m for m in messages_list() if str(m.get("id", "")).startswith("fam_")]
        _log("voice_out", "%s → message_play(%d条)%s" % (heard, len(fams), "(agent)" if agent_ok else ""))
        if fams:
            return {"ok": True, "heard": heard, "action": "message_play",
                    "did": "msg:play:" + fams[0]["id"]}
        out = {"ok": True, "heard": heard, "action": "message_none", "did": "msg:empty"}
        if reply:
            out["reply"] = reply
        return out
    if action in WAYPOINTS:                    # 臂序列(归位/递药/还药/按铃)——后台线程,别堵 HTTP
        threading.Thread(target=arm_execute, args=(action,), daemon=True).start()
        did = "arm:" + action
    else:
        r = do_pose(action, 1)
        did = r.get("did", action)
    _log("voice_out", "%s → %s%s" % (heard, action, "(agent)" if agent_ok else ""))
    out = {"ok": True, "heard": heard, "action": action, "did": did}
    if reply:
        out["reply"] = reply
    return out


# ---------- 老人留言(v5.1):S3 分块上传 → 板上合成 WAV → 家属端拉取/推送 ----------
# 落盘:/root/caremate/messages/msg_<时间戳>.wav(16k/16bit/mono)
# 家属端:GET /messages(列表) GET /messages/<id>.wav(音频) GET /messages/inbox(网页收件箱)
#        POST /family_message(家属上传语音回话→fam_*.wav,老人「听留言」播放)(v5.2)
# 可选:FAMILY_PUSH_URL=收件接口,每条新留言主动 POST multipart(audio+time+duration+elder)
MSG_DIR = "/root/caremate/messages"
FAMILY_PUSH_URL = os.environ.get("FAMILY_PUSH_URL", "")

_MSG_LOCK = threading.Lock()          # 同一留言的分块追加串行化
import re as _msg_re
_ID_OK = _msg_re.compile(r"^[A-Za-z0-9_]{1,64}$")   # id 白名单,防路径穿越

try:
    os.makedirs(MSG_DIR, exist_ok=True)
except Exception:
    pass


def _msg_paths(mid):
    """合法 id → (未合成的 part 路径, 最终 wav 路径)。id 不合法返回 (None, None)。"""
    if not _ID_OK.match(mid or ""):
        return None, None
    return (os.path.join(MSG_DIR, mid + ".part"),
            os.path.join(MSG_DIR, mid + ".wav"))


def message_start():
    mid = "msg_" + time.strftime("%Y%m%d_%H%M%S")
    # 同秒重开:加 _b/_c 后缀保证唯一
    part, wav = _msg_paths(mid)
    n = 0
    while part and (os.path.exists(part) or (wav and os.path.exists(wav))):
        n += 1
        mid = "msg_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), chr(96 + n))
        part, wav = _msg_paths(mid)
    try:
        with open(part, "wb"):
            pass
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    return {"ok": True, "id": mid}


def message_chunk(mid, data):
    part, _ = _msg_paths(mid)
    if not part:
        return {"ok": False, "error": "bad id"}
    if not data:
        return {"ok": True, "appended": 0}
    try:
        with _MSG_LOCK:
            with open(part, "ab") as f:
                f.write(data)
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    return {"ok": True, "appended": len(data)}


def _wav_header(pcm_bytes):
    import struct
    sr, ch, bits = 16000, 1, 16
    dsz = len(pcm_bytes) // 2 * 2          # 16bit 对齐
    return (b"RIFF" + struct.pack("<I", 36 + dsz) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, ch, sr, sr * ch * bits // 8,
                        ch * bits // 8, bits) +
            b"data" + struct.pack("<I", dsz))


def _push_message(mid):
    """可选:把已合成留言 POST 给家属端服务器(FAMILY_PUSH_URL)。失败只记日志不重试。"""
    if not FAMILY_PUSH_URL:
        return
    _, wav = _msg_paths(mid)
    try:
        with open(wav, "rb") as f:
            audio = f.read()
        bnd = "====CareMate%s====" % time.strftime("%H%M%S")
        dur = len(audio) / (16000 * 2)
        body = b""
        body += ("--%s\r\nContent-Disposition: form-data; name=\"time\"\r\n\r\n%s\r\n"
                 % (bnd, time.strftime("%Y-%m-%d %H:%M:%S"))).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"duration\"\r\n\r\n%.1f\r\n"
                 % (bnd, dur)).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"elder\"\r\n\r\nhand-A\r\n"
                 % bnd).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"%s.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n" % (bnd, mid)).encode()
        body += audio + ("\r\n--%s--\r\n" % bnd).encode()
        req = urllib.request.Request(FAMILY_PUSH_URL, data=body, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + bnd})
        urllib.request.urlopen(req, timeout=10)
        _log("push_ok", mid)
    except Exception as e:
        _log("push_fail", "%s %s" % (mid, str(e)[:100]))


def message_end(mid):
    part, wav = _msg_paths(mid)
    if not part or not os.path.exists(part):
        return {"ok": False, "error": "no such message"}
    try:
        with open(part, "rb") as f:
            pcm = f.read()
        if len(pcm) < 8000:                 # <0.25s 当空留言
            os.remove(part)
            return {"ok": False, "error": "empty"}
        with open(wav, "wb") as f:
            f.write(_wav_header(pcm))
            f.write(pcm[:len(pcm) // 2 * 2])
        os.remove(part)
        dur = len(pcm) // 2 / 16000.0
        _log("message_out", "%s %.1fs" % (mid, dur))
        if FAMILY_PUSH_URL:
            threading.Thread(target=_push_message, args=(mid,), daemon=True).start()
        return {"ok": True, "id": mid, "duration": round(dur, 1)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def message_cancel(mid):
    part, wav = _msg_paths(mid)
    if not part:
        return {"ok": False, "error": "bad id"}
    for p in (part, wav):                   # 取消=残件丢弃;若同名 wav 已在(极小概率)也不覆盖语义,只删 part
        try:
            if os.path.exists(p) and p.endswith(".part"):
                os.remove(p)
        except Exception:
            pass
    _log("message_cancel", mid)
    return {"ok": True, "cancelled": mid}


def family_message(data: bytes) -> dict:
    """家属端给老人发语音回话(裸 PCM 或 WAV,16k/16bit/mono,≤60s)→ 存 fam_<时间戳>.wav。
    老人按「听留言」键或说「听留言」即播最新一条(含家属回话)。"""
    if data[:4] == b"RIFF" and len(data) > 44:
        data = data[44:]                       # 带 WAV 头也收,剥到裸 PCM
    if len(data) < 8000:
        return {"ok": False, "error": "audio too short(≥0.25s)"}
    if len(data) > 32000 * 60:
        data = data[:32000 * 60]               # 上限 60 秒
    mid = "fam_" + time.strftime("%Y%m%d_%H%M%S")
    part, wavp = _msg_paths(mid)
    if not part:
        return {"ok": False, "error": "bad id"}
    n = 0
    while os.path.exists(wavp):                # 同秒重发:n+1 后缀保证唯一
        n += 1
        part, wavp = _msg_paths("fam_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), chr(96 + n)))
        if not part:
            return {"ok": False, "error": "bad id"}
    try:
        with open(wavp, "wb") as f:
            f.write(_wav_header(data))
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    _log("family_in", "%s %.1fs" % (mid, len(data) / 32000.0))
    return {"ok": True, "id": mid, "duration": round(len(data) / 32000.0, 1)}


# ---------- 家属回话拉取(v5.4b):手板出网定时拉中转站,家属 app 零配置 ----------
# 公网中转由后端(或云中转)承担:手板在热点内网,外面进不来,但手板能主动出网。
# 配置 FAMILY_PULL_URL 后,每 5 秒 GET 一次:
#   GET {FAMILY_PULL_URL}?elder=hand-A&after=<上次已取的回复id>
#   → {"ok":true,"replies":[{"id":"...","pcm_b64":"<16k/16bit/mono 裸PCM的base64>"}
#                            或 {"id":"...","wav_url":"https://.../x.wav"}]}
#   replies 按时间升序;不配置 FAMILY_PULL_URL 时线程直接退出(功能关闭)。
FAMILY_PULL_URL = os.environ.get("FAMILY_PULL_URL", "")
FAMILY_PULL_TOKEN = os.environ.get("FAMILY_PULL_TOKEN", "")
_PULL_CURSOR = os.path.join(MSG_DIR, ".pull_cursor")


def _pull_seen_ids():
    try:
        with open(_PULL_CURSOR, "r", encoding="utf-8") as f:
            return set(x for x in f.read().splitlines() if x)
    except Exception:
        return set()


def _family_pull_once(seen):
    """拉一次家属回话。返回本轮处理的条数。异常抛给上层。"""
    import base64 as _b64
    import urllib.request as _ur
    sep = "&" if "?" in FAMILY_PULL_URL else "?"
    after = ""
    try:
        with open(_PULL_CURSOR + ".last", "r", encoding="utf-8") as f:
            after = f.read().strip()
    except Exception:
        pass
    url = "%s%selder=hand-A&after=%s" % (FAMILY_PULL_URL, sep, _ur.quote(after or ""))
    req = _ur.Request(url, headers={"Accept": "application/json",
                                    **({"X-CareMate-Token": FAMILY_PULL_TOKEN} if FAMILY_PULL_TOKEN else {})})
    r = _ur.urlopen(req, timeout=8)
    data = json.loads(r.read().decode("utf-8", "replace"))
    if not data.get("ok"):
        return 0
    n = 0
    for rep in data.get("replies", []) or []:
        rid = str(rep.get("id", "")).strip()
        if not rid or rid in seen:
            continue
        try:
            if rep.get("pcm_b64"):
                pcm = _b64.b64decode(rep["pcm_b64"])
            elif rep.get("wav_url"):
                raw = _ur.urlopen(rep["wav_url"], timeout=15).read()
                pcm = raw[44:] if raw[:4] == b"RIFF" else raw
            else:
                _log("pull_fail", "%s no audio field" % rid)
                continue
            out = family_message(pcm)
            if out.get("ok"):
                seen.add(rid)
                with open(_PULL_CURSOR, "a", encoding="utf-8") as f:
                    f.write(rid + "\n")
                with open(_PULL_CURSOR + ".last", "w", encoding="utf-8") as f:
                    f.write(rid)
                _log("pull_ok", "%s → %s" % (rid, out["id"]))
                n += 1
            else:
                _log("pull_fail", "%s %s" % (rid, out.get("error", "")))
        except Exception as e:
            _log("pull_fail", "%s %s" % (rid, str(e)[:100]))
    return n


def _family_pull_loop():
    if not FAMILY_PULL_URL:
        return                                   # 未配置=功能关闭,零影响
    seen = _pull_seen_ids()
    _log("boot", "family_pull on: %s" % FAMILY_PULL_URL)
    while True:
        try:
            _family_pull_once(seen)
            time.sleep(5)
        except Exception as e:
            _log("pull_fail", str(e)[:120])
            time.sleep(15)


def messages_list():
    """扫描 MSG_DIR 的 wav,按修改时间倒序(新在前,老人和家属的混排)。返回 dict 列表。"""
    out = []
    try:
        fns = [fn for fn in os.listdir(MSG_DIR) if fn.endswith(".wav")]
        fns.sort(key=lambda fn: os.path.getmtime(os.path.join(MSG_DIR, fn)), reverse=True)
        for fn in fns:
            p = os.path.join(MSG_DIR, fn)
            mid = fn[:-4]
            try:
                dur = os.path.getsize(p) - 44
                out.append({"id": mid, "file": "/messages/" + fn,
                            "time": "%s-%s-%s %s:%s:%s" % (mid[4:8], mid[8:10], mid[10:12],
                                                           mid[13:15], mid[15:17], mid[17:19]),
                            "duration": round(dur / 32000.0, 1),
                            "bytes": os.path.getsize(p)})
            except Exception:
                continue
    except Exception:
        pass
    return out[:50]


_INBOX_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>家人留言收件箱</title><meta http-equiv=refresh content="20">
<style>
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei";background:#f5f1e8;margin:0;padding:12px}
h1{font-size:22px;color:#5d4037;margin:8px 0 12px}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin:10px 0;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.t{font-size:18px;color:#333;font-weight:600}
.d{font-size:13px;color:#999;margin:2px 0 6px}
audio{width:100%;height:44px}
.empty{color:#999;font-size:16px;text-align:center;padding:40px 0}
.hint{font-size:12px;color:#bbb;text-align:center;margin-top:14px}
</style></head><body>
<h1>✉ 家人留言收件箱</h1>
__LIST__
<div class=hint>页面每 20 秒自动刷新 · Mocen CareMate</div>
</body></html>"""


def messages_inbox_html():
    rows = []
    for m in messages_list():
        rows.append(
            '<div class=card><div class=t>留言 · %s</div>'
            '<div class=d>时长 %.0f 秒 · 共 %d KB</div>'
            '<audio controls preload=none src="%s"></audio></div>'
            % (m["time"], m["duration"], m["bytes"] // 1024, m["file"]))
    if not rows:
        rows.append('<div class=empty>还没有留言,去床头屏按「留言」说一句吧</div>')
    return _INBOX_HTML.replace("__LIST__", "\n".join(rows))


# ---------- 家属端 App(PWA,v5.5):手机浏览器打开 →「添加到主屏幕」当原生 app 用 ----------
# 页面/图标等静态文件在 WWW_DIR(/root/caremate/www);手机麦克风要求 HTTPS,所以开
# APP_TLS_PORT 自签证书端口(证书缺失时启动自动 openssl 生成一次)。
WWW_DIR = os.environ.get("APP_WWW_DIR", "/root/caremate/www")
APP_TLS_PORT = int(os.environ.get("APP_TLS_PORT", "8014"))
_CERT_DIR = "/root/caremate/certs"
_APP_MIME = {".html": "text/html; charset=utf-8", ".json": "application/manifest+json",
             ".js": "application/javascript", ".png": "image/png",
             ".svg": "image/svg+xml", ".css": "text/css"}


def app_static(fname):
    """读 www 下的静态文件;空=不存在或非法路径。"""
    if not _ID_OK.match(os.path.splitext(fname)[0]) or ".." in fname:
        return None, None
    fp = os.path.join(WWW_DIR, fname)
    if not os.path.isfile(fp):
        return None, None
    with open(fp, "rb") as f:
        return f.read(), _APP_MIME.get(os.path.splitext(fname)[1], "application/octet-stream")


def _ensure_app_cert():
    """自签证书:缺失则用 openssl 生成一次(CN=本机 IP,10 年)。返回 (key,cert) 路径或 None。"""
    keyp = os.path.join(_CERT_DIR, "app.key")
    certp = os.path.join(_CERT_DIR, "app.crt")
    if os.path.isfile(keyp) and os.path.isfile(certp):
        return keyp, certp
    os.makedirs(_CERT_DIR, exist_ok=True)
    ip = "10.153.47.252"
    cmd = ("openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes "
           "-keyout %s -out %s -subj '/CN=%s' "
           "-addext 'subjectAltName=IP:%s,DNS:caremate.local'" % (keyp, certp, ip, ip))
    r = os.system(cmd + " >/dev/null 2>&1")
    if r == 0 and os.path.isfile(keyp):
        _log("boot", "app cert generated")
        return keyp, certp
    _log("boot", "app cert FAILED(openssl missing?)")
    return None


# ---------- 跌倒警报(v5.6):跌倒检测端 POST /fall_alarm → 家属 app 拉 /alarms ----------
# 检测端接入(一行):POST http://10.153.47.252:8013/fall_alarm
#   body 可选 JSON {"kind":"fall","src":"cam-A","conf":0.9}(没有 body 也收)
# 家属 app:GET /alarms(列 50 条,新在前);POST /alarms/ack {"id":..} 确认
AL_DIR = os.environ.get("APP_ALARM_DIR", "/root/caremate/alarms")
ALARMS_JSON = os.path.join(AL_DIR, "alarms.json")
_alarm_lock = threading.Lock()
_alarm_mem = []          # 内存态(新在前),boot 时从磁盘恢复


def alarms_load():
    _alarm_mem.clear()
    try:
        with open(ALARMS_JSON, "r", encoding="utf-8") as f:
            _alarm_mem.extend(json.load(f))
    except Exception:
        pass


def alarm_new(kind="fall", src="", conf=None):
    """记录一条警报;返回带 id 的 dict。"""
    import time as _t
    now = _t.localtime()
    aid = "al_" + _t.strftime("%Y%m%d_%H%M%S", now)
    a = {"id": aid, "time": _t.strftime("%Y-%m-%d %H:%M:%S", now),
         "kind": kind, "acked": False}
    if src:
        a["src"] = str(src)[:40]
    if conf is not None:
        try:
            a["conf"] = round(float(conf), 2)
        except Exception:
            pass
    with _alarm_lock:
        _alarm_mem.insert(0, a)
        del _alarm_mem[100:]
        try:
            os.makedirs(AL_DIR, exist_ok=True)
            with open(ALARMS_JSON, "w", encoding="utf-8") as f:
                json.dump(_alarm_mem[:50], f, ensure_ascii=False)
        except Exception as e:
            _log("alarm", "persist fail %s" % e)
        _log("alarm", "FALL %s src=%s" % (aid, src))
    return a


def alarms_ack(aid):
    with _alarm_lock:
        for a in _alarm_mem:
            if a["id"] == aid:
                a["acked"] = True
                try:
                    os.makedirs(AL_DIR, exist_ok=True)
                    with open(ALARMS_JSON, "w", encoding="utf-8") as f:
                        json.dump(_alarm_mem[:50], f, ensure_ascii=False)
                except Exception:
                    pass
                return {"ok": True, "acked": aid}
    return {"ok": False, "error": "no such alarm"}

# ---------- 双板联动（各手在各板 → 按一键,两手做"呼应的不同动作"） ----------
# RELAY_TARGET = 对面板 arm_service 的 http://IP:8013;空 = 不联动。
# 配对表:本手动作 → 对面手动作。只联动"顶层按键",序列内部嵌套不触发,无回环。
RELAY_TARGET = os.environ.get("RELAY_TARGET", "")
RELAY_PAIRS = {
    "wave":      "ok",          # 招手   → 对面手回 OK「收到」
    "thumbs_up": "wave",        # 点赞   → 对面手挥手回应
    "fingers_5": "fingers_3",   # 数五个 → 对面手比三
    "release":   "release",     # 松手   → 对面同步松
}


def _relay(other_action: str, orig: str):
    """把呼应动作异步 POST 给对面板；不阻塞本手，失败只记日志。"""
    if not RELAY_TARGET:
        return
    def _t():
        try:
            urllib.request.urlopen(urllib.request.Request(
                RELAY_TARGET + "/pose/" + other_action,
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json"}), timeout=3)
            _log("relay_ok", "%s → 对面:%s" % (orig, other_action))
        except Exception as e:
            _log("relay_fail", "%s → %s (%s)" % (orig, other_action, e))
    threading.Thread(target=_t, daemon=True).start()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass          # 客户端断开（浏览器刷新等）：静默丢弃，不炸日志

    def _send_bytes(self, body, ctype, code=200):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self._send({"ok": True}, 200)

    def log_message(self, *a):  # 静音默认日志
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/status":
            h = _hand1.status()
            resp = {"backend": ARM_BACKEND, "mock_serial": h.get("mock"),
                    "poses": sorted(ALL_ACTIONS), "dual": DUAL_HAND,
                    "recent": _action_log[-20:],
                    "hand": {k: h[k] for k in
                             ("act_raw", "act_pct", "last_ack_ok", "proto")
                             if k in h}}
            if _hand2 is not _hand1:
                resp["hand2"] = _hand2.status()
            self._send(resp)
        elif p == "/messages":                          # 家属端:留言列表(新在前)
            self._send({"ok": True, "messages": messages_list()})
        elif p == "/messages/inbox":                    # 网页收件箱(手机直接开)
            self._send_bytes(messages_inbox_html().encode("utf-8"),
                             "text/html; charset=utf-8")
        elif p == "/app" or p == "/app/":               # 家属端 App(PWA 主页)
            body, ct = app_static("app.html")
            if not body:
                self._send({"error": "app files missing on board"}, 500)
                return
            self._send_bytes(body, ct)
        elif p.startswith("/app/"):                     # PWA 静态文件(manifest/sw/icon)
            body, ct = app_static(p[len("/app/"):])
            if not body:
                self._send({"error": "not found"}, 404)
                return
            self._send_bytes(body, ct)
        elif p.startswith("/messages/") and p.endswith(".wav"):   # 单条音频
            fn = p[len("/messages/"):]
            if not _ID_OK.match(fn[:-4]):
                self._send({"ok": False, "error": "bad id"}, 400)
                return
            fp = os.path.join(MSG_DIR, fn)
            if not os.path.isfile(fp):
                self._send({"ok": False, "error": "not found"}, 404)
                return
            with open(fp, "rb") as f:
                data = f.read()
            self._send_bytes(data, "audio/wav")
        elif p == "/alarms":                            # 家属端:警报列表(新在前)
            self._send({"ok": True, "alarms": _alarm_mem[:50]})
        elif p == "/fall_alarm":                        # 检测端连通性(GET 即回 ok)
            self._send({"ok": True, "note": "POST here on fall detected"})
        else:
            self._send({"error": "use POST"}, 404)

    def do_POST(self):
        try:
            if self.path.startswith("/pose/"):
                name = self.path.split("?")[0].split("/")[-1]
                hand = int(dict(p.split("=") for p in
                                (self.path.split("?")[1].split("&")
                                 if "?" in self.path else [])).get("hand", "1"))
                r = do_pose(name, hand)
                self._send(r)
                if r.get("ok") and name in RELAY_PAIRS and hand in (0, 1):
                    _relay(RELAY_PAIRS[name], name)   # 双板联动:配对动作发给对面手
            elif self.path == "/voice_cmd":
                ln = int(self.headers.get("Content-Length", 0) or 0)
                pcm = self.rfile.read(ln) if ln else b""
                self._send(voice_cmd(pcm))
            elif self.path.startswith("/message_chunk"):          # 留言分块(裸 PCM)
                ln = int(self.headers.get("Content-Length", 0) or 0)
                data = self.rfile.read(ln) if ln else b""
                qs = dict(p.split("=", 1) for p in
                          (self.path.split("?")[1].split("&") if "?" in self.path else []))
                self._send(message_chunk(qs.get("id", ""), data))
            elif self.path.startswith("/message_end"):
                qs = dict(p.split("=", 1) for p in
                          (self.path.split("?")[1].split("&") if "?" in self.path else []))
                self._send(message_end(qs.get("id", "")))
            elif self.path.startswith("/message_cancel"):
                qs = dict(p.split("=", 1) for p in
                          (self.path.split("?")[1].split("&") if "?" in self.path else []))
                self._send(message_cancel(qs.get("id", "")))
            elif self.path == "/message_start":
                self._send(message_start())
            elif self.path.startswith("/family_message"):      # 家属端上传语音回话(v5.2)
                ln = int(self.headers.get("Content-Length", 0) or 0)
                data = self.rfile.read(ln) if ln else b""
                self._send(family_message(data))
            elif self.path == "/fall_alarm":                    # 跌倒警报上报(v5.6)
                ln = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(ln) if ln else b""
                kind, src, conf = "fall", "", None
                if raw:
                    try:
                        j = json.loads(raw.decode("utf-8", "replace"))
                        kind = str(j.get("kind", "fall"))[:20]
                        src = str(j.get("src", ""))[:40]
                        conf = j.get("conf")
                    except Exception:
                        src = "@raw%d" % ln      # 非法 body 也当警报收
                self._send({"ok": True, **alarm_new(kind, src, conf)})
            elif self.path == "/alarms/ack":                    # app 确认警报
                ln = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(ln) if ln else b""
                aid = ""
                try:
                    aid = str(json.loads(raw.decode("utf-8", "replace")).get("id", ""))
                except Exception:
                    pass
                self._send(alarms_ack(aid))
            elif self.path in ("/handover_med", "/handover_item", "/return_med",
                               "/press_sos", "/reset", "/card_up", "/thumbs", "/wave"):
                seq = {"handover_med": "handover_med", "handover_item": "handover_item",
                       "return_med": "return_med",
                       "press_sos": "press_sos", "reset": "reset",
                       "card_up": None, "thumbs": None, "wave": None}[self.path[1:]]
                if seq is None:      # 兼容旧端点 → 姿态/序列
                    alias = {"card_up": "hold_card", "thumbs": "thumbs_up", "wave": "wave"}
                    self._send(do_pose(alias[self.path[1:]]))
                else:
                    threading.Thread(target=arm_execute, args=(seq,), daemon=True).start()
                    self._send({"ok": True, "queued": self.path})
            else:
                self._send({"error": "unknown"}, 404)
        except Exception as e:
            self._send({"ok": False, "error": str(e)}, 500)


def main():
    alarms_load()                                   # 恢复历史警报(若磁盘有)
    _log("boot", "arm_service mock=%s backend=%s dual=%s alarms=%d"
         % (_hand1.status()["mock"], ARM_BACKEND, DUAL_HAND, len(_alarm_mem)))
    if FAMILY_PULL_URL:
        threading.Thread(target=_family_pull_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", SERVICE_PORT), Handler)
    print("arm_service on :%d (MOCK_SERIAL=%s, ARM_BACKEND=%s)" % (
        SERVICE_PORT, "1" if MOCK else "0", ARM_BACKEND))
    # 家属端 App 的 HTTPS 端口(手机麦克风必须 https);失败不影响主服务
    try:
        ck = _ensure_app_cert()
        if ck:
            import ssl as _ssl
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(ck[1], ck[0])
            srv_tls = ThreadingHTTPServer(("0.0.0.0", APP_TLS_PORT), Handler)
            srv_tls.socket = ctx.wrap_socket(srv_tls.socket, server_side=True)
            threading.Thread(target=srv_tls.serve_forever, daemon=True).start()
            print("app https on :%d" % APP_TLS_PORT)
            _log("boot", "app https on :%d" % APP_TLS_PORT)
        else:
            _log("boot", "app https DISABLED(no cert)")
    except Exception as e:
        _log("boot", "app https error: %s" % str(e)[:100])
    srv.serve_forever()


if __name__ == "__main__":
    main()
