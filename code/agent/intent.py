"""scripted 模式的意图识别：关键词规则，零网络依赖。

classify(text, ctx) -> (intent, slots)
ctx 由总线给出当前上下文：asking / dial_pending / health_pending / exercise_active / med_pending /
call_incoming / call_active / contacts。
上下文相关的短回答（"对""确认""我没事"）优先于通用意图。
"""
import re

from agent.health_tracker import parse_reading

OK_WORDS = ("我没事", "没事", "没关系", "不用", "挺好", "我很好", "还好", "我好着")
CONFIRM_WORDS = ("确认", "确定", "拨吧", "打吧", "快打", "是的打")
CANCEL_WORDS = ("取消", "不用", "不打", "算了", "别打", "不要")
YES_WORDS = ("对", "是", "没错", "正确", "嗯对", "确认", "记吧")
NO_WORDS = ("不对", "不是", "错了", "说错", "不准")
STOP_WORDS = ("停", "不做了", "歇会", "累了", "不练了")
EMERGENCY = ("120", "急救", "救护车")
CALL_VERBS = ("叫我", "叫一下", "帮我叫", "找我", "喊我", "打给", "打电话", "打个电话", "联系")
ECHO_WORDS = ("想女儿", "想儿子", "想她", "想他", "陪我聊", "聊聊", "说说话", "想孩子")
EXERCISE_WORDS = ("手指操", "做操", "活动手指", "动动手")
TEACH_HINTS = ("怎么", "咋", "教我", "不会", "如何", "怎样")
MED_TAKEN = ("吃了", "吃过了", "吃完了", "吃好了", "服了", "吃过药", "药吃了")
MED_NOT_YET = ("没吃", "还没", "等会", "待会", "一会儿吃")
DISTRESS_WORDS = ("起不来", "动不了", "救命", "救救", "很疼", "好疼", "疼得", "很痛", "需要帮助", "需要帮忙")
CALL_ANSWER = ("接", "喂")
CALL_HANGUP = ("挂", "不接", "不听", "拒接")
VOICE_PLAY = ("听留言", "放留言", "播放留言", "听语音", "放语音", "播放语音", "听一下留言", "放一下留言")

_PUNCT = re.compile(r"[\s，。？！,.?!~～、：:]")


def _core(text):
    return _PUNCT.sub("", text)


def _starts(text, words):
    return any(text.startswith(w) for w in words)


def _has(text, words):
    return any(w in text for w in words)


def _says_ok(text):
    # A quoted/negated assurance or explicit distress must not cancel the alarm.
    negated = re.search(r"(?:不是|并非|不算|哪里|哪儿).{0,3}(?:没事|挺好|还好|很好)", text)
    return not negated and not _has(text, DISTRESS_WORDS) and _has(text, OK_WORDS)


def find_contact(text, contacts):
    for c in contacts:
        if any(a in text for a in c.get("aliases", [c["name"]])):
            return c
    return None


def classify(text, ctx):
    core = _core(text)
    contacts = ctx.get("contacts", [])

    if ctx.get("dial_pending"):
        if _has(core, CANCEL_WORDS):
            return "dial_cancel", {}
        if _starts(core, CONFIRM_WORDS) or core in ("对", "是", "是的"):
            return "dial_confirm", {}

    if ctx.get("asking"):
        if _says_ok(core):
            return "elder_ok", {}
        return "elder_distress", {"dial": _has(core, EMERGENCY)}

    if ctx.get("exercise_active") and _has(core, STOP_WORDS):
        return "exercise_stop", {}

    if ctx.get("health_pending"):
        if parse_reading(text) is not None:
            return "health_record", {}
        if _starts(core, NO_WORDS):
            return "health_reject", {}
        if _starts(core, YES_WORDS):
            return "health_confirm", {}

    # 只有等服药打卡的时候才认“吃了 / 还没吃”，平时“我吃了饭”不会被当成打卡
    if ctx.get("med_pending"):
        if _has(core, MED_NOT_YET):
            return "med_not_yet", {}
        if _has(core, MED_TAKEN):
            return "med_taken", {}

    # 来电时“接 / 不接”；老人在通话里（含自己打出去还在响铃）说“挂了”
    if ctx.get("call_incoming"):
        if _has(core, CALL_HANGUP):
            return "call_hangup", {}
        if _starts(core, CALL_ANSWER):
            return "call_answer", {}
    if ctx.get("call_active") and _has(core, CALL_HANGUP):
        return "call_hangup", {}

    if _has(core, EMERGENCY):
        return "dial_request", {}

    # 教学放在"打电话"前面：如"微信视频电话怎么打"
    if _has(core, TEACH_HINTS) or "用法" in core:
        return "teach", {"text": text}

    # “听留言”也带“留言”两个字，要放在留言前面
    if _has(core, VOICE_PLAY):
        return "voice_play", {}

    if "留言" in core or re.search(r"(告诉|跟|和)(.{1,3})(说|讲)", core):
        c = find_contact(core, contacts) or (contacts[0] if contacts else None)
        body = re.split(r"[：:]", text, maxsplit=1)
        msg = body[1].strip() if len(body) > 1 else re.sub(r"^.*?(留言|说|讲)", "", core)
        return "message", {"contact": c["id"] if c else None, "text": msg}

    if "报平安" in core or "报个平安" in core:
        return "safe_report", {}

    if _has(core, ECHO_WORDS):
        c = find_contact(core, contacts) or (contacts[0] if contacts else None)
        return "echo", {"contact": c["id"] if c else None}

    if _has(core, CALL_VERBS):
        c = find_contact(core, contacts)
        if c:
            return "call_family", {"contact": c["id"]}
        m = re.search(r"(?:给|打给|叫|找|联系|喊)(?:一下)?([一-龥]{1,4}?)(?:打电话|打个电话|过来|来|$)", core)
        who = m.group(1) if m and m.group(1) not in ("我", "人") else "陌生号码"
        return "call_blocked", {"who": who}

    if _says_ok(core):
        return "elder_ok", {}

    if parse_reading(text) is not None:
        return "health_record", {}

    if "递" in core or "拿" in core or core.startswith("把"):
        m = re.search(r"把(.+?)(递|拿|给)", core)
        if m:
            item = m.group(1)
        elif "药" in core:
            item = "药"
        else:
            item = re.sub(r"^(帮我|给我|麻烦)?(递|拿)(一下|个)?", "", core) or core
        return "deliver", {"item": item}

    if _has(core, EXERCISE_WORDS):
        return "exercise_start", {}

    if "猜拳" in core or "石头剪刀布" in core:
        return "rps", {}

    return "chitchat", {"text": text}
