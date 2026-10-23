"""RH56/Panthera 现场前置检查。

This command is deliberately read-only.  It does not open a serial port or
send a Modbus frame.  It answers the questions that can be answered before
the hardware is powered, and makes an unsafe real-mode configuration visible
before ``arm_service`` is started.

Examples (from ``code/``)::

    python -m tools.hardware_preflight
    python -m tools.hardware_preflight --mode real --hand1-port COM5 \
        --hand2-port COM5 --require-real
    python -m tools.hardware_preflight --arm-url http://127.0.0.1:8013
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import urllib.error
import urllib.request

from hand_controller import rh56_register_map as register_map


def serial_ports() -> tuple[list[dict], str | None]:
    """Return visible serial devices without opening any of them."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return [], "pyserial is not installed"
    rows = []
    for port in list_ports.comports():
        rows.append({
            "device": port.device,
            "description": port.description or "",
            "hwid": port.hwid or "",
        })
    return rows, None


def plan(mode: str, hand1_port: str, hand2_port: str, confirmed: bool,
         ports: list[dict], serial_error: str | None = None) -> dict:
    """Build a serialisable, side-effect-free readiness report."""
    visible = {p["device"].lower() for p in ports}
    real = mode == "real"
    missing = []
    if real:
        for label, port in (("HAND1_PORT", hand1_port), ("HAND2_PORT", hand2_port)):
            # A shared RS485 bus intentionally uses the same port twice.
            if port.lower() not in visible:
                missing.append("%s=%s" % (label, port))
    checks = {
        "mode": mode,
        "mock_serial": not real,
        "register_map_provisional": bool(register_map.PROVISIONAL),
        "register_map_confirmed": bool(confirmed),
        "ports_visible": ports,
        "missing_ports": missing,
        "serial_error": serial_error,
        "safe_to_start_real": bool(
            real and not register_map.PROVISIONAL and confirmed and not missing and not serial_error
        ),
    }
    if not real:
        checks["safe_to_start_real"] = False
    return checks


def fetch_arm(url: str, timeout: float = 2.0) -> dict:
    """Read an arm service status endpoint; never POSTs an action."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/status", timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return {"reachable": True, "http": response.status, "body": json.loads(body)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail)
        except ValueError:
            pass
        return {"reachable": True, "http": exc.code, "body": detail}
    except Exception as exc:  # noqa: BLE001 - command should explain the field failure
        return {"reachable": False, "error": str(getattr(exc, "reason", exc))}


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or not value.strip() else value.strip()


def _print(report: dict, arm: dict | None, out=print) -> None:
    out("CareMate 硬件前置检查 · %s" % platform.platform())
    out("  模式: %s (MOCK_SERIAL=%s)" % (report["mode"], "1" if report["mock_serial"] else "0"))
    out("  寄存器表: %s; 环境确认: %s" % (
        "仍是占位，禁止真写" if report["register_map_provisional"] else "已标记非占位",
        "是" if report["register_map_confirmed"] else "否"))
    if report["serial_error"]:
        out("  串口扫描: ❌ %s" % report["serial_error"])
    elif report["ports_visible"]:
        out("  串口扫描: " + ", ".join(p["device"] for p in report["ports_visible"]))
    else:
        out("  串口扫描: 未发现设备")
    if report["missing_ports"]:
        out("  目标串口缺失: " + ", ".join(report["missing_ports"]))
    if report["mode"] == "real":
        out("  真手启动: %s" % ("✅ 条件满足" if report["safe_to_start_real"] else "❌ 仍阻塞"))
    else:
        out("  真手启动: 当前为 mock；需要现场核对寄存器后再切 real")
    if arm is not None:
        if arm.get("reachable"):
            out("  arm_service: HTTP %s" % arm.get("http"))
            body = arm.get("body")
            if isinstance(body, dict):
                out("    backend=%s mock=%s arm_backend=%s" % (
                    body.get("backend", "?"), body.get("mock", body.get("mock_serial", "?")),
                    body.get("arm_backend", "?")))
        else:
            out("  arm_service: 未连接 (%s)" % arm.get("error", "unknown"))


def main(argv=None, out=print) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="只读的 RH56/Panthera 现场前置检查")
    ap.add_argument("--mode", choices=("mock", "real"), default=_env("MOCK_SERIAL", "1") == "0" and "real" or "mock")
    ap.add_argument("--hand1-port", default=_env("HAND1_PORT", "COM5"))
    ap.add_argument("--hand2-port", default=_env("HAND2_PORT", "COM6"))
    ap.add_argument("--arm-url", help="可选：只 GET 该 arm_service 的 /status")
    ap.add_argument("--require-real", action="store_true", help="检查失败时返回 1")
    ap.add_argument("--json", action="store_true", dest="as_json", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    ports, serial_error = serial_ports()
    confirmed = register_map.confirmed_from_environment()
    report = plan(args.mode, args.hand1_port, args.hand2_port, confirmed, ports, serial_error)
    arm = fetch_arm(args.arm_url) if args.arm_url else None
    if args.as_json:
        out(json.dumps({"hardware": report, "arm": arm}, ensure_ascii=False, indent=2))
    else:
        _print(report, arm, out)
    if args.require_real:
        return 0 if report["safe_to_start_real"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
