"""健康口述记账 + 复述确认 + 健康日报。

流程（§2.3）：老人说"我刚量了血压 138比85"
  → 复述"我听到的是高压 138、低压 85，对吗？"（进入待确认，不落盘）
  → 说"对" 才记账；说"不对" 或直接报新数字 → 重新复述；超时 → 丢弃不记。
喝水低风险，直接记。

判定规则（家庭自测标准）：收缩压 ≥135 或 舒张压 ≥85 记"偏高"。只做解读，不做诊断。
档案只存数字和时间，不存原话/音频（§5.1-6）。
"""
import datetime
import json
import re
import time
from pathlib import Path

from voice.text_normalize import DIGITS, normalize

HIGH_SYS = 135
HIGH_DIA = 85
CUP_ML = 200
NOTE = "仅为家庭自测记录，不作诊断；家庭自测参考值 135/85 mmHg。"

_BP_CONTEXT = ("血压", "高压", "低压", "收缩压", "舒张压")
_LABELED = re.compile(r"(?:高压|收缩压)\D{0,4}?(\d{2,3}).{0,8}?(?:低压|舒张压)\D{0,4}?(\d{2,3})")
_LABELED_REV = re.compile(r"(?:低压|舒张压)\D{0,4}?(\d{2,3}).{0,8}?(?:高压|收缩压)\D{0,4}?(\d{2,3})")
_PAIR = re.compile(r"(\d{2,3})\s*(?:比|/|over)\s*(\d{2,3})", re.I)
_PAIR_LOOSE = re.compile(r"(\d{2,3})\s*[,，、\s]\s*(\d{2,3})")
_PULSE = re.compile(r"(?:心率|脉搏|心跳)\D{0,4}?(\d{2,3})")
_WATER_CUPS = re.compile(r"喝了?(?:大概|差不多|约|有)?([\d一二两三四五六七八九十半]+)\s*(?:杯|碗|瓶)")
_WATER_ML = re.compile(r"喝了?.{0,6}?(\d{2,4})\s*(?:毫升|ml)", re.I)


def _say(key, **params):
    return {"do": "say", "key": key, "params": params}


def _event(text, level="info"):
    return {"do": "event", "type": "health", "text": text, "level": level}


def classify_bp(sys_, dia):
    return "偏高" if sys_ >= HIGH_SYS or dia >= HIGH_DIA else "正常"


def _plausible(sys_, dia):
    return 70 <= sys_ <= 250 and 40 <= dia <= 150 and sys_ > dia


def _parse_bp(t):
    has_ctx = any(k in t for k in _BP_CONTEXT)
    m = _LABELED.search(t)
    if m:
        s, d = int(m.group(1)), int(m.group(2))
    else:
        m = _LABELED_REV.search(t)
        if m:
            d, s = int(m.group(1)), int(m.group(2))
        else:
            m = _PAIR.search(t)
            if not m and has_ctx:
                m = _PAIR_LOOSE.search(t)
            if not m:
                return None
            s, d = int(m.group(1)), int(m.group(2))
    if not _plausible(s, d):
        # 没提"血压"的"10比0"之类不当成血压，免得闲聊被打断
        return {"kind": "bp_invalid", "sys": s, "dia": d} if has_ctx else None
    pulse = None
    pm = _PULSE.search(t)
    if pm and 30 <= int(pm.group(1)) <= 200:
        pulse = int(pm.group(1))
    return {"kind": "bp", "sys": s, "dia": d, "pulse": pulse}


def _cups_value(token):
    if token.isdigit():
        return float(token)
    if token == "半":
        return 0.5
    if token == "十":
        return 10.0
    if len(token) == 1 and token in DIGITS:
        return float(DIGITS[token])
    return None


def _parse_water(t):
    if "喝" not in t or not any(k in t for k in ("水", "茶", "杯")):
        return None
    m = _WATER_ML.search(t)
    if m:
        ml = int(m.group(1))
        return {"kind": "water", "ml": ml, "estimated": False} if 0 < ml <= 2000 else None
    m = _WATER_CUPS.search(t)
    if m:
        cups = _cups_value(m.group(1))
        if cups and 0 < cups <= 10:
            return {"kind": "water", "ml": int(cups * CUP_ML), "estimated": False}
        return None
    return {"kind": "water", "ml": CUP_ML, "estimated": True}


def parse_reading(text):
    """返回 {"kind": "bp"|"bp_invalid"|"water", ...}；不是健康口述返回 None。"""
    t = normalize(text)
    return _parse_bp(t) or _parse_water(t)


def _fmt_cups(ml):
    cups = ml / CUP_ML
    return str(int(cups)) if cups == int(cups) else "%.1f" % cups


class HealthTracker:
    def __init__(self, path=None, clock=time.time, confirm_window=20.0):
        self.path = Path(path) if path else None
        self.clock = clock
        self.confirm_window = confirm_window
        self.pending = None
        self.records = self._load()

    # ---------- 存储 ----------

    def _load(self):
        if self.path and self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8")).get("records", [])
            except (ValueError, OSError):
                return []
        return []

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"records": self.records}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def reset(self, clear_records=False):
        self.pending = None
        if clear_records:
            self.records = []
            self._save()

    # ---------- 口述流程 ----------

    def propose(self, text):
        reading = parse_reading(text)
        if reading is None:
            return []
        if reading["kind"] == "bp_invalid":
            self.pending = None
            return [
                _event("听到的血压 %d/%d 不合理，请老人重说" % (reading["sys"], reading["dia"]), "warn"),
                _say("health_invalid"),
            ]
        if reading["kind"] == "water":
            return self._save_water(reading)
        self.pending = {"reading": reading, "deadline": self.clock() + self.confirm_window}
        pulse = reading["pulse"]
        return [
            _event("听到血压 %d/%d%s，复述待确认" % (reading["sys"], reading["dia"],
                                             "，心率 %d" % pulse if pulse else "")),
            _say("health_readback", sys=reading["sys"], dia=reading["dia"],
                 pulse_part="、心率 %d" % pulse if pulse else ""),
        ]

    def confirm(self):
        if not self.pending:
            return []
        if self.clock() >= self.pending["deadline"]:
            return self.tick()
        r = self.pending["reading"]
        self.pending = None
        flag = classify_bp(r["sys"], r["dia"])
        rec = {"ts": self.clock(), "kind": "bp", "sys": r["sys"], "dia": r["dia"],
               "pulse": r["pulse"], "flag": flag}
        self.records.append(rec)
        self._save()
        label = "%d/%d" % (r["sys"], r["dia"])
        if flag == "偏高":
            return [
                _event("已记录血压 %s（偏高）" % label, "warn"),
                {"do": "notify", "contact": None, "kind": "health",
                 "text": "血压 %s，高于家庭自测参考值 135/85" % label},
                _say("health_saved_high", sys=r["sys"], dia=r["dia"]),
            ]
        return [_event("已记录血压 %s（正常）" % label, "ok"),
                _say("health_saved_normal", sys=r["sys"], dia=r["dia"])]

    def reject(self):
        if not self.pending:
            return []
        self.pending = None
        return [_event("老人说不对，本次未记录"), _say("health_retry")]

    def tick(self):
        if self.pending and self.clock() >= self.pending["deadline"]:
            r = self.pending["reading"]
            self.pending = None
            return [_event("血压 %d/%d 未得到确认，未记录" % (r["sys"], r["dia"]))]
        return []

    def _save_water(self, reading):
        rec = {"ts": self.clock(), "kind": "water", "ml": reading["ml"]}
        self.records.append(rec)
        self._save()
        today_ml = sum(r["ml"] for r in self._records_on(self._today()) if r["kind"] == "water")
        cups = _fmt_cups(reading["ml"])
        return [
            _event("喝水 +%s 杯%s（今日约 %d 毫升）" % (cups, "（按一杯估）" if reading["estimated"] else "", today_ml), "ok"),
            _say("water_saved", cups=cups, today=_fmt_cups(today_ml)),
        ]

    # ---------- 日报 ----------

    def _today(self):
        return datetime.date.fromtimestamp(self.clock())

    def _records_on(self, day):
        return [r for r in self.records if datetime.date.fromtimestamp(r["ts"]) == day]

    def daily_report(self, day=None):
        day = day or self._today()
        recs = self._records_on(day)
        bps = [r for r in recs if r["kind"] == "bp"]
        water_ml = sum(r["ml"] for r in recs if r["kind"] == "water")
        bp_rows = [{
            "time": datetime.datetime.fromtimestamp(r["ts"]).strftime("%H:%M"),
            "sys": r["sys"], "dia": r["dia"], "pulse": r.get("pulse"), "flag": r["flag"],
        } for r in bps]
        high = [r for r in bps if r["flag"] == "偏高"]

        parts = []
        if not bps:
            parts.append("今天还没有量血压。")
        else:
            last = bps[-1]
            parts.append("今天量了 %d 次血压，最近一次 %d/%d%s。" % (
                len(bps), last["sys"], last["dia"],
                "，心率 %d" % last["pulse"] if last.get("pulse") else ""))
            if high:
                parts.append("其中 %d 次高于家庭自测参考值 135/85，建议连续几天在同一时间复测；"
                             "如果持续偏高，请带上记录咨询医生。" % len(high))
            else:
                parts.append("均在家庭自测参考范围内。")
        if water_ml:
            parts.append("喝水约 %s 杯（%d 毫升）。" % (_fmt_cups(water_ml), water_ml))
        else:
            parts.append("还没有喝水记录。")
        return {
            "date": day.isoformat(),
            "bp": bp_rows,
            "water_ml": water_ml,
            "water_cups": _fmt_cups(water_ml),
            "high_count": len(high),
            "summary": "".join(parts),
            "note": NOTE,
        }

    def snapshot(self):
        pending = None
        if self.pending:
            r = self.pending["reading"]
            pending = {"sys": r["sys"], "dia": r["dia"], "pulse": r["pulse"],
                       "remaining": max(0.0, round(self.pending["deadline"] - self.clock(), 1))}
        return {"pending": pending, "today": self.daily_report()}
