"""Interactive phone live viewer. Observation only; no bedside/actuator access.

Credentials stay in process memory. Import/--help do not contact a device or
open a browser. The camera connection begins only after the prompts finish.
"""
import argparse
import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit
import webbrowser

import config
from camera.phone_source import PhoneFrameSource
from vision import family_watch
from vision.web_monitor import WebMonitor


SETTINGS = config.CODE_DIR / "data" / "live_monitor" / "connection.json"


def main(argv=None, *, prompt=input, secret=getpass.getpass, execute=family_watch.execute,
         monitor_factory=WebMonitor, open_browser=webbrowser.open, settings=SETTINGS, log=print):
    config.utf8_console()
    ap = argparse.ArgumentParser(description="CareMate手机直播监看：输入地址和登录信息，自动打开网页；仅观察")
    ap.add_argument("--phone-stream", help="摄像头App的HTTP首页或完整MJPEG地址")
    ap.add_argument("--port", type=int, default=8015, help="本机监看端口，默认8015")
    ap.add_argument("--no-browser", action="store_true", help="只显示网址，不自动打开浏览器")
    cli = ap.parse_args(argv)
    if not 1 <= cli.port <= 65535:
        ap.error("监看端口须为1至65535")
    try:
        settings = Path(settings)
        try:
            saved = json.loads(settings.read_text(encoding="utf-8"))
            previous = saved.get("phone_stream", "") if isinstance(saved, dict) else ""
            if not isinstance(previous, str):
                previous = ""
        except (OSError, ValueError):
            previous = ""
        log("CareMate 直播监看 · 仅观察")
        log("请保持iPhone摄像头App在前台，电脑与手机连接同一Wi-Fi。")
        log("登录信息请使用刚才在浏览器中验证成功的那一组；密码输入时不显示字符。")
        url = cli.phone_stream or (prompt("手机App的HTTP地址%s：" % (
            " [回车使用 %s]" % previous if previous else "")).strip() or previous)
        # Validate before asking for credentials, loading a model or opening any socket.
        PhoneFrameSource(url, mjpeg=True)
        username = prompt("摄像头账号 [回车使用admin；无登录要求则输入 -]：").strip() or "admin"
        if username == "-":
            username = password = None
        else:
            password = secret("摄像头密码（输入后按回车，不会显示字符）：")
        args = family_watch.parser().parse_args([
            "--phone-stream", url, "--mode", "off", "--continuous", "--web-monitor",
            "--monitor-host", "127.0.0.1", "--monitor-port", str(cli.port), "--src", "phone-live-monitor",
        ])
        args.phone_username, args.phone_password = username, password
        # Retain the convenient LAN address only. Never save credentials or query tokens.
        if not urlsplit(url).query:
            try:
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text(json.dumps({"phone_stream": url}, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                log("[提示] 无法记住本次地址，不影响直播。")

        def start_monitor(**options):
            monitor = monitor_factory(**options)
            address = "http://127.0.0.1:%d/" % monitor.address[1]
            log("[打开直播] " + address)
            if not cli.no_browser:
                try:
                    if not open_browser(address):
                        log("[提示] 请在浏览器手动打开上方网址。")
                except Exception:
                    log("[提示] 浏览器未自动打开，请手动打开上方网址。")
            return monitor

        log("画面接通后会显示人体骨架。只显示当前画面，没有暂停或回放功能。")
        log("请保留此窗口；结束整套识别请按 Ctrl+C。关闭网页不会停止识别。")
        report = execute(args, monitor_factory=start_monitor, log=log)
        return 0 if report["ok"] or report["status"] == "interrupted" else 2
    except (KeyboardInterrupt, EOFError):
        log("\n已结束直播启动。")
        return 130
    except ValueError:
        log("[未启动] 地址或设置无效。请使用手机App显示的 http://IP:端口/，账号密码另行输入。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
