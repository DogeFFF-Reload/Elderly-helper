"""演示参数集中配置（§2.2：演示参数 ≠ 真实参数）。

覆盖优先级：DEFAULTS < code/config.local.json < 环境变量 CAREMATE_<KEY 大写>。
例：CAREMATE_ASK_WAIT_DAY=30 python -m master_service
"""
import copy
import json
import os
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CAREMATE_DATA_DIR", str(CODE_DIR / "data")))

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
    # 手指操每拍间隔（秒）
    "exercise_beat_seconds": 2.0,
    # 猜拳：喊完"石头剪刀布"手#1 揭晓的时间、揭晓后等视觉回传手势的时间（秒）
    "rps_reveal_delay": 3.5,
    "rps_timeout": 6.0,
    "arm_url": "http://127.0.0.1:8013",
    "llm_mode": "scripted",
    # 家属端对老人的称呼
    "elder_title": "妈妈",
    "default_room": "卧室",
    # 联系人白名单，顺序 = 升级顺序
    "contacts": [
        {"id": "daughter", "name": "女儿", "pron": "她", "aliases": ["女儿", "闺女", "小芳"]},
        {"id": "son", "name": "儿子", "pron": "他", "aliases": ["儿子", "小军"]},
    ],
}


def load(overrides=None):
    cfg = copy.deepcopy(DEFAULTS)
    local = CODE_DIR / "config.local.json"
    if local.exists():
        cfg.update(json.loads(local.read_text(encoding="utf-8")))
    for key, default in DEFAULTS.items():
        raw = os.environ.get("CAREMATE_" + key.upper())
        if raw is None or isinstance(default, (list, dict)):
            continue
        cfg[key] = type(default)(raw)
    if overrides:
        cfg.update(overrides)
    return cfg


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
