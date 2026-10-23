"""演示参数集中配置（§2.2：演示参数 ≠ 真实参数）。

覆盖优先级：DEFAULTS < code/config.local.json < 环境变量 CAREMATE_<KEY 大写>。
例：CAREMATE_ASK_WAIT_DAY=30 python -m master_service
运行中可调的参数（TUNABLE）还能用操作台 POST /api/params 改，重启后回到上面三层的值。
启动时整体校验一次：填错直接报中文错误退出，不要等到演示中途才炸。
"""
import copy
import json
import os
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAREMATE_DATA_DIR", str(CODE_DIR / "data")))
LOCAL_PATH = CODE_DIR / "config.local.json"

DEFAULTS = {
    # 救：询问等待（秒）。真实老人反应慢得多，正式部署按访谈调整
    "ask_wait_day": 5.0,
    "ask_wait_night": 3.0,
    # 打 120 的确认窗（秒）
    "dial_confirm_window": 10.0,
    # 家属多久没确认就通知下一位联系人（秒）
    "family_ack_timeout": 30.0,
    # 健康口述复述后，等老人说"对"的时间（秒）
    "health_confirm_window": 20.0,
    # 夜间窗口 [start, end)，跨零点
    "night_start_hour": 19,
    "night_end_hour": 6,
    # 时间覆盖开关：auto 按时钟 / on 强制夜间 / off 强制日间。演示时不依赖真实钟点
    "night_override": "auto",
    # 手指操每拍间隔（秒）
    "exercise_beat_seconds": 2.0,
    "arm_url": "http://127.0.0.1:8013",
    # 台词通道：scripted（默认，零网络）/ llm（OpenAI 兼容接口，超时或出错自动回落 scripted）
    "llm_mode": "scripted",
    "llm_base_url": "",          # 例 https://api.example.com/v1
    "llm_api_key": "",           # 只用环境变量 CAREMATE_LLM_API_KEY 或 config.local.json，别进仓库
    "llm_model": "",
    "llm_timeout": 3.0,          # 单句超时（秒），超时这句就用 scripted
    "llm_cooldown": 60.0,        # 连续失败后这么多秒内直接走 scripted，断网时不再每句干等
    # 家属端对老人的称呼
    "elder_title": "妈妈",
    "default_room": "卧室",
    # 联系人白名单，顺序 = 升级顺序
    "contacts": [
        {"id": "daughter", "name": "女儿", "pron": "她", "aliases": ["女儿", "闺女", "小芳"]},
        {"id": "son", "name": "儿子", "pron": "他", "aliases": ["儿子", "小军"]},
    ],
}

# 数值参数的合法范围（含两端）。也是 /api/params 的白名单
LIMITS = {
    "ask_wait_day": (1, 600),
    "ask_wait_night": (1, 600),
    "dial_confirm_window": (3, 120),
    "family_ack_timeout": (5, 3600),
    "health_confirm_window": (5, 300),
    "night_start_hour": (0, 23),
    "night_end_hour": (0, 23),
    "exercise_beat_seconds": (0.5, 10),
    "llm_timeout": (0.5, 30),
    "llm_cooldown": (0, 3600),
}
TUNABLE = tuple(LIMITS)
CHOICES = {
    "llm_mode": ("scripted", "llm"),
    "night_override": ("auto", "on", "off"),
}


class ConfigError(ValueError):
    pass


def _coerce(key, value):
    """把 env / JSON / HTTP 来的值转成 DEFAULTS 里的类型；转不了抛 ConfigError。"""
    default = DEFAULTS[key]
    try:
        if isinstance(default, int):
            f = float(value)
            if f != int(f):
                raise ValueError
            return int(f)
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        raise ConfigError("%s=%r 不是合法的%s" % (key, value, "整数" if isinstance(default, int) else "数字"))
    return value if isinstance(default, (list, dict)) else str(value)


def validate(cfg):
    errors = []
    for key, (lo, hi) in LIMITS.items():
        v = cfg.get(key)
        if not isinstance(v, (int, float)) or not lo <= v <= hi:
            errors.append("%s=%r 超出范围 [%g, %g]" % (key, v, lo, hi))
    for key, options in CHOICES.items():
        if cfg.get(key) not in options:
            errors.append("%s=%r 只能是 %s" % (key, cfg.get(key), " / ".join(options)))
    contacts = cfg.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        errors.append("contacts 至少要有一位联系人（第一位是首要联系人）")
    else:
        ids = [c.get("id") for c in contacts if isinstance(c, dict)]
        if len(ids) != len(contacts) or not all(isinstance(c.get("name"), str) and c.get("pron") for c in contacts):
            errors.append("contacts 每项都要有 id / name / pron")
        elif len(set(ids)) != len(ids):
            errors.append("contacts 的 id 重复：%s" % ", ".join(ids))
    return errors


def load(overrides=None, warnings=None, use_local=True):
    """warnings 传一个 list 进来，会收到"不影响启动但该看一眼"的提示（拼错的键、llm 没配全）。
    use_local=False 只用 DEFAULTS + overrides（回归测试用，不受本机 config.local.json 和环境变量影响）。"""
    warnings = warnings if warnings is not None else []
    cfg = copy.deepcopy(DEFAULTS)
    local = LOCAL_PATH
    if use_local and local.exists():
        try:
            data = json.loads(local.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ConfigError("config.local.json 不是合法 JSON：%s" % e)
        for key, value in data.items():
            if key.startswith("_"):
                continue
            if key not in DEFAULTS:
                warnings.append("config.local.json 里的 %s 不认识（拼错了？），已忽略" % key)
                continue
            cfg[key] = _coerce(key, value)
    for key, default in DEFAULTS.items():
        raw = os.environ.get("CAREMATE_" + key.upper()) if use_local else None
        if raw is None or isinstance(default, (list, dict)):
            continue
        cfg[key] = _coerce(key, raw)
    if overrides:
        cfg.update(overrides)
    errors = validate(cfg)
    if errors:
        raise ConfigError("配置有误：\n  " + "\n  ".join(errors))
    if cfg["llm_mode"] == "llm" and not (cfg["llm_base_url"] and cfg["llm_model"]):
        warnings.append("llm_mode=llm 但 llm_base_url / llm_model 没配，所有台词会走 scripted")
    return cfg


def apply_params(cfg, updates):
    """运行中改参数（操作台 /api/params）。全部合法才生效，返回 (changed, errors)。"""
    trial = dict(cfg)
    errors = []
    for key, value in updates.items():
        if key not in TUNABLE:
            errors.append("%s 不能在运行中修改" % key)
            continue
        try:
            trial[key] = _coerce(key, value)
        except ConfigError as e:
            errors.append(str(e))
    errors += [e for e in validate(trial) if e.split("=")[0] in updates]
    if errors:
        return {}, errors
    changed = {k: trial[k] for k in updates if trial[k] != cfg[k]}
    cfg.update(changed)
    return changed, []


def is_night(hour, start, end):
    """hour 是否落在夜间窗口 [start, end)，支持跨零点（19→6）。"""
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def contact_by_id(cfg, contact_id):
    for c in cfg["contacts"]:
        if c["id"] == contact_id:
            return c
    return None


def utf8_console():
    """Windows 控制台默认 cp1252/GBK，中文 print 会崩；各入口 main() 先调用它。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
