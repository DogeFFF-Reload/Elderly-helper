"""Interactive phone live viewer. Observation only; no bedside/actuator access.

Credentials stay in process memory. Import/--help do not contact a device or
open a browser. Before asking for a login the address is probed without
credentials, so "app in background", "other Wi-Fi" and "needs login" each get
their own hint. A dropped stream (iPhone app left the foreground) reconnects
automatically with the same login; login and stream-format errors still wait
for the user in the launcher window.
"""
import argparse
import getpass
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import webbrowser

import config
from camera.phone_calibrate import CAMERA_ERROR_MESSAGES
from camera.phone_source import PhoneFrameSource, probe as probe_camera
from vision import family_watch
from vision.web_monitor import WebMonitor


SETTINGS = config.CODE_DIR / "data" / "live_monitor" / "connection.json"
HINTS = {
    "refused": "手机连得上，但摄像头 App 的服务器没在运行：把摄像头 App 切回前台、保持亮屏（iPhone 切到后台就会停）。",
    "timeout": "连不上这个地址：电脑和手机要连同一个 Wi-Fi / 热点；热点重开后手机地址会变，请看 App 上显示的新地址。",
    "error": "这个地址打不开，请核对 App 上显示的地址。",
}
# Only a dropped connection retries by itself; a wrong login or a non-MJPEG page needs the user.
AUTO_RETRY = {CAMERA_ERROR_MESSAGES[k] for k in ("connection", "timeout", "stale")}
FIRST_WAIT_TRIES = 10       # 约 20 秒（每 2 秒一次）后问要不要换地址
RETRY_GAP_S = 3.0


def wait_for_camera(url, *, probe, sleep, log, tries=None):
    """Poll until the address answers. Returns the probe result, or None after `tries` failed probes."""
    last, failed = None, 0
    while True:
        status = probe(url)
        if status in ("ok", "auth"):
            if last is not None:
                log("[已连上] 摄像头 App 有响应了。")
            return status
        if status != last:
            log("[等待摄像头] " + HINTS.get(status, HINTS["error"]) + " 每 2 秒重试，Ctrl+C 结束。")
            last = status
        failed += 1
        if tries is not None and failed >= tries:
            return None
        sleep(2)


def main(argv=None, *, prompt=input, secret=getpass.getpass, execute=family_watch.execute,
         monitor_factory=WebMonitor, open_browser=webbrowser.open, settings=SETTINGS, log=print,
         probe=probe_camera, sleep=time.sleep):
    config.utf8_console()
    ap = argparse.ArgumentParser(description="CareMate手机直播监看：输入地址和登录信息，自动打开网页；仅观察")
    ap.add_argument("--phone-stream", help="摄像头App的HTTP首页或完整MJPEG地址")
    ap.add_argument("--port", type=int, default=8015, help="本机监看端口，默认8015")
    ap.add_argument("--no-browser", action="store_true", help="只显示网址，不自动打开浏览器")
    cli = ap.parse_args(argv)
    if not 1 <= cli.port <= 65535:
        ap.error("监看端口须为1至65535")
    monitor = None
    browser_opened = False

    def tell_monitor(**fields):
        if monitor is not None and hasattr(monitor, "update"):
            monitor.update(**fields)

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
        if previous:
            log("上次用的地址：%s（换过 Wi-Fi 或重开热点后地址会变，以 App 上显示的为准）" % previous)

        def start_monitor(**options):
            nonlocal monitor, browser_opened
            if monitor is not None:
                monitor.close()
                monitor = None
            monitor = monitor_factory(**dict(options, fps=10))   # page view at 10 fps (default 5)
            address = "http://127.0.0.1:%d/" % monitor.address[1]
            log("[打开直播] " + address)
            if not cli.no_browser and not browser_opened:
                try:
                    browser_opened = True
                    if not open_browser(address):
                        log("[提示] 请在浏览器手动打开上方网址。")
                except Exception:
                    log("[提示] 浏览器未自动打开，请手动打开上方网址。")
            return monitor

        log("画面接通后会显示人体骨架。只显示当前画面，没有暂停或回放功能。")
        log("请保留此窗口；结束整套识别请按 Ctrl+C。关闭网页不会停止识别。")
        login = None              # (username, password) kept only for automatic reconnects
        while True:
            if login is None:
                url = cli.phone_stream or (prompt("手机App的HTTP地址%s：" % (
                    " [回车使用 %s]" % previous if previous else "")).strip() or previous)
                # Validation never opens a connection or loads a model.
                PhoneFrameSource(url, mjpeg=True)
                reachable = wait_for_camera(url, probe=probe, sleep=sleep, log=log, tries=FIRST_WAIT_TRIES)
                while reachable is None:
                    other = prompt("仍然连不上。回车继续等待，或输入 App 上显示的新地址：").strip()
                    if other:
                        PhoneFrameSource(other, mjpeg=True)
                        url = other
                    reachable = wait_for_camera(url, probe=probe, sleep=sleep, log=log, tries=FIRST_WAIT_TRIES)
                if reachable == "ok":
                    log("[提示] 这个地址没有要求登录；App 没设账号的话，下面账号处输入 -")
                username = prompt("摄像头账号 [回车使用admin；无登录要求则输入 -]：").strip() or "admin"
                if username == "-":
                    username = password = None
                else:
                    password = secret("摄像头密码（输入后按回车，不会显示字符）：")
                login = (username, password)
                # Never save credentials or URLs containing query tokens.
                if not urlsplit(url).query:
                    try:
                        settings.parent.mkdir(parents=True, exist_ok=True)
                        settings.write_text(json.dumps({"phone_stream": url}, ensure_ascii=False) + "\n", encoding="utf-8")
                    except OSError:
                        log("[提示] 无法记住本次地址，不影响直播。")
            args = family_watch.parser().parse_args([
                "--phone-stream", url, "--mode", "off", "--continuous", "--web-monitor", "--fps-cap", "15",
                "--monitor-host", "127.0.0.1", "--monitor-port", str(cli.port), "--src", "phone-live-monitor",
            ])
            args.phone_username, args.phone_password = login
            # The launcher owns the viewer so an input failure can remain visible
            # while the user corrects credentials.
            report = execute(args, monitor_factory=start_monitor, close_monitor=False, log=log)
            if report["status"] != "error":
                return 0 if report["ok"] or report["status"] == "interrupted" else 2
            if report.get("message") in AUTO_RETRY:
                log("[自动重连] 画面断了（多半是摄像头 App 被切到后台或锁屏），用同一组登录信息重连。")
                tell_monitor(run_status="reconnecting", input_message=CAMERA_ERROR_MESSAGES["reconnecting"])
                sleep(RETRY_GAP_S)
                wait_for_camera(url, probe=probe, sleep=sleep, log=log)
                continue
            log("[等待处理] 请查看网页中的具体错误；检查手机App的HTTP登录设置。")
            if prompt("按回车重新输入地址、账号和密码；输入 Q 后回车结束：").strip().lower() == "q":
                return 2
            login = None
            previous, cli.phone_stream = (url if not urlsplit(url).query else ""), None
    except (KeyboardInterrupt, EOFError):
        log("\n已结束直播启动。")
        return 130
    except ValueError:
        log("[未启动] 地址或设置无效。请使用手机App显示的 http://IP:端口/，账号密码另行输入。")
        return 2
    finally:
        if monitor is not None:
            monitor.close()


if __name__ == "__main__":
    raise SystemExit(main())
