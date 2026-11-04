"""Configure the standalone bedside vision process, beside the existing service.

--check-config is entirely offline. No shell config is sourced or evaluated.
Missing/invalid fields are reported without echoing phone URLs or credentials.
"""
import argparse
import importlib
import json
import math
from pathlib import Path
import signal
import sys

import config
from vision.family_alarm import AlarmClient
from vision import family_watch


DEFAULTS = {"phone_stream": "", "mode": "off", "family": "http://127.0.0.1:8013",
            "src": "bedside-vision", "room": "卧室", "fps_cap": 10,
            "continuous": True, "duration": 300,
            "monitor_host": "127.0.0.1", "monitor_port": 8015,
            "rules_file": None, "output_root": "../code/data/bedside_vision"}


class ConfigurationError(ValueError):
    """A safe, locally authored message which contains no configuration values."""


def load_configuration(path):
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ConfigurationError("无法读取配置，请检查文件路径和JSON格式") from None
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ConfigurationError("配置含未知字段，须使用bedside_vision.json.example中的字段")
    values = dict(DEFAULTS, **raw)
    for name in ("phone_stream", "mode", "family", "src", "room", "monitor_host", "output_root"):
        if not isinstance(values[name], str):
            raise ConfigurationError("配置字段%s须为文字" % name)
    if not values["phone_stream"].strip():
        raise ConfigurationError("请在phone_stream填写已验证可用的iPhone完整MJPEG地址")
    if len(values["room"]) > 80 or not values["room"].strip():
        raise ConfigurationError("room须为1至80字符")
    if values["monitor_host"] not in ("127.0.0.1", "0.0.0.0"):
        raise ConfigurationError("monitor_host须为127.0.0.1或0.0.0.0")
    if type(values["monitor_port"]) is not int or not 1 <= values["monitor_port"] <= 65535:
        raise ConfigurationError("monitor_port须为1至65535的整数")
    if type(values["continuous"]) is not bool:
        raise ConfigurationError("continuous须为true或false")
    for name, maximum in (("fps_cap", 60), ("duration", 600)):
        value = values[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ConfigurationError("%s须大于0且不超过%d" % (name, maximum))
    if values["rules_file"] is not None and not isinstance(values["rules_file"], str):
        raise ConfigurationError("rules_file须为路径或null")
    AlarmClient(values["family"], mode=values["mode"], src=values["src"])
    # Validation only: neither constructor opens a device, model or connection.
    from camera.phone_source import PhoneFrameSource
    from vision.live_input import LiveInput
    import os
    PhoneFrameSource(values["phone_stream"], mjpeg=True, username=os.environ.get("PHONE_USERNAME") or None,
                     password=os.environ.get("PHONE_PASSWORD") or None)
    from urllib.parse import urlsplit
    try:
        port = urlsplit(values["phone_stream"]).port
        if port == 0:
            raise ValueError
    except ValueError:
        raise ConfigurationError("phone_stream地址端口无效") from None
    LiveInput(phone_stream=values["phone_stream"], duration=values["duration"],
              fps=values["fps_cap"], continuous=values["continuous"])
    for name in ("output_root", "rules_file"):
        if values[name] is not None:
            if not values[name].strip():
                raise ConfigurationError("%s不得为空" % name)
            values[name] = str((path.parent / values[name]).resolve())
    return values


def watch_arguments(values):
    args = family_watch.parser().parse_args([
        "--phone-stream", values["phone_stream"], "--mode", values["mode"], "--family", values["family"],
        "--src", values["src"], "--room", values["room"], "--fps-cap", str(values["fps_cap"]),
        "--duration", str(values["duration"]), "--web-monitor", "--monitor-host", values["monitor_host"],
        "--monitor-port", str(values["monitor_port"])])
    args.continuous = values["continuous"]
    args.rules = values["rules_file"]
    from datetime import datetime, timezone
    import uuid
    args.output = Path(values["output_root"]) / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])
    return args


def check_runtime(log=print):
    """CPU blank image only: proves packages/model load, not X5 camera or speed."""
    import numpy as np
    for name in ("cv2", "mediapipe"):
        module = importlib.import_module(name)
        log("%s: %s" % (name, getattr(module, "__version__", "unknown")))
    from vision.pose_backends import PoseEstimator
    estimator = PoseEstimator(complexity=0)
    try:
        estimator(np.zeros((240, 320, 3), dtype=np.uint8))
        log("姿态模型空白图自检通过；不代表设备实测帧率或识别准确率")
    finally:
        estimator.close()


def main(argv=None):
    config.utf8_console()
    ap = argparse.ArgumentParser(description="床头视觉与只读监看；不启动或修改既有硬件服务")
    ap.add_argument("--config", type=Path, help="现场JSON配置文件")
    checks = ap.add_mutually_exclusive_group()
    checks.add_argument("--check-config", action="store_true", help="只验证配置，不联网、不加载模型")
    checks.add_argument("--check-runtime", action="store_true", help="只用内存空白图检查模型，不打开摄像头、不发送警报")
    args = ap.parse_args(argv)
    if args.check_runtime:
        try:
            check_runtime()
            return 0
        except (Exception, SystemExit) as exc:
            print("运行环境自检失败（%s）；请队友核对ARM64/Python版本、视觉依赖和pose_lite模型。" % type(exc).__name__)
            return 2
    if args.config is None:
        ap.error("请提供--config；仅--check-runtime可不提供配置")
    try:
        values = load_configuration(args.config)
        watch_args = watch_arguments(values)
        from vision.kpio import load_rules
        load_rules(watch_args.rules)
    except (Exception, SystemExit) as exc:
        print("配置检查失败（%s）；请按示例核对字段、MJPEG地址、规则文件和输出路径。" % type(exc).__name__)
        # Safe messages created by this module can be useful without reflecting arbitrary inputs.
        if isinstance(exc, ConfigurationError):
            print(str(exc))
        return 2
    if args.check_config:
        print("配置检查通过；模式=%s；监看端口=%d；%s；未联网、未打开摄像头、未加载模型。" % (
            values["mode"], values["monitor_port"], "持续运行" if values["continuous"] else "限时运行"))
        return 0

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        report = family_watch.execute(watch_args)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0 if report["ok"] or report["status"] == "interrupted" else 2


if __name__ == "__main__":
    sys.exit(main())
