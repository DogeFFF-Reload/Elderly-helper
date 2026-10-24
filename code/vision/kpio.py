"""骨架录制/回放格式 + 规则参数的命令行覆盖。

录制文件是 JSON Lines，只有关键点坐标，不含任何图像（§5.1-6 不存影像）：
  {"meta": {"aspect": 1.333, "fps": 15.0, "source": "camera:0", "created": "..."}}
  {"t": 0.067, "kp": {"head": [x, y, 可见度], ...}}
  {"t": 0.133, "kp": null}            ← 这一帧没检测到人
"""
import datetime
import json
from pathlib import Path

from gesture.fall_rules import DEFAULT_RULES, validate_rules


class KeypointWriter:
    """meta 在第一帧时才写：摄像头的真实宽高比要读到画面才知道。"""

    def __init__(self, path, source):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open("w", encoding="utf-8", newline="\n")
        self.source = source
        self.started = False

    def write(self, t, kp, aspect):
        if not self.started:
            meta = {"aspect": round(aspect, 4), "source": self.source,
                    "created": datetime.datetime.now().isoformat(timespec="seconds")}
            self.f.write(json.dumps({"meta": meta}, ensure_ascii=False) + "\n")
            self.started = True
        self.f.write(json.dumps({"t": round(t, 3), "kp": kp}, ensure_ascii=False) + "\n")

    def close(self):
        self.f.close()


def read_keypoints(path):
    """返回 (meta, [(t, kp), ...])。"""
    meta, frames = {}, []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "meta" in obj:
                meta = obj["meta"]
            else:
                frames.append((float(obj["t"]), obj.get("kp")))
    return meta, frames


def parse_set(items):
    """--set lying_angle=50 --set confirm_hold=1.5 → dict（值按 JSON 解析，失败当字符串）。"""
    out = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        key = key.strip()
        if key not in DEFAULT_RULES:
            raise SystemExit("未知参数 %s，可用：%s" % (key, ", ".join(DEFAULT_RULES)))
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def parse_zone(text):
    try:
        vals = [float(v) for v in text.split(",")]
        validate_rules({"bed_zones": [vals]})
    except ValueError as exc:
        raise SystemExit("--bed 无效：%s" % exc) from None
    return vals


def load_rules(path=None, sets=None, beds=None):
    rules = {}
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit("跌倒规则文件必须是 JSON 对象")
        rules.update(data)
    rules.update(parse_set(sets))
    try:
        validate_rules(rules)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if beds:
        rules["bed_zones"] = list(rules.get("bed_zones", [])) + [parse_zone(b) for b in beds]
    unknown = set(rules) - set(DEFAULT_RULES)
    if unknown:
        raise SystemExit("规则文件里有未知参数：%s" % ", ".join(sorted(unknown)))
    return rules
