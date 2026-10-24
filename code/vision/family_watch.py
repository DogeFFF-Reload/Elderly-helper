"""Pose -> confirmed rule event -> native family App's bedside alarm endpoint.

Default mode is OFF. Live sending accepts camera/phone input or an explicit
test alarm; offline replays and synthetic inputs cannot select live mode.
Importing this module and --help never open a camera or contact any service.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

import config
from gesture.fall_rules import FallDetector
from vision import synth
from vision.family_alarm import AlarmClient, AlarmPoster, DEFAULT_FAMILY, MODES, new_event_id
from vision.fall_bridge import keypoint_source, run
from vision.input_quality import InputQuality
from vision.kpio import load_rules, read_keypoints
from vision.live_input import LiveInput
from vision.pc_observe import write_json


def parser():
    ap = argparse.ArgumentParser(description="姿态跌倒 → 家属App；默认off，dry无扰，live才建真实警报")
    inputs = ap.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--camera", type=int, nargs="?", const=0, help="电脑摄像头编号")
    inputs.add_argument("--phone-stream", help="手机完整MJPEG地址")
    inputs.add_argument("--keypoints", type=Path, help="离线坐标，只能off或dry")
    inputs.add_argument("--synthetic", choices=sorted(synth.SCENARIOS), help="合成输入，只能off或dry")
    inputs.add_argument("--check-link", action="store_true", help="仅健康检查和dry回执；不打开摄像头")
    inputs.add_argument("--send-test", action="store_true", help="单条接口验收；live会建真实警报，不打开摄像头")
    ap.add_argument("--mode", choices=MODES, default="off")
    ap.add_argument("--family", default=DEFAULT_FAMILY, help="床头服务根地址")
    ap.add_argument("--src", default="cam-A", help="来源标签，最多40字符")
    ap.add_argument("--event-id", help="仅--send-test：重发失败事件时沿用原号")
    ap.add_argument("--duration", type=float, default=300, help="实时观察秒数，默认300，最多600；到时停止")
    ap.add_argument("--continuous", action="store_true", help="实时输入持续运行，直到退出或输入故障；忽略duration")
    ap.add_argument("--fps-cap", type=float, default=10)
    ap.add_argument("--preview", action="store_true", help="显示骨架窗口；Q或Esc退出")
    ap.add_argument("--web-monitor", action="store_true", help="启动只读监看网页，不需要桌面窗口")
    ap.add_argument("--monitor-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    ap.add_argument("--monitor-port", type=int, default=8015)
    ap.add_argument("--phone-username", default=os.environ.get("PHONE_USERNAME") or None)
    ap.add_argument("--phone-password", default=os.environ.get("PHONE_PASSWORD") or None)
    ap.add_argument("--room", default="现场")
    ap.add_argument("--rules")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE")
    ap.add_argument("--bed", action="append", metavar="x1,y1,x2,y2")
    ap.add_argument("--output", type=Path, help="新的结果目录，不覆盖旧记录")
    return ap


def execute(args, *, client=None, live_factory=LiveInput, preview_factory=None, monitor_factory=None, log=print):
    """Injection points let the whole entry run against fake inputs and loopback HTTP."""
    if args.mode == "live" and (args.keypoints or args.synthetic):
        raise ValueError("离线输入不能发送真实警报")
    if args.continuous and not (args.camera is not None or args.phone_stream):
        raise ValueError("持续运行只用于实时摄像头输入")
    client = client or AlarmClient(args.family, mode=args.mode, src=args.src)
    if client.mode != args.mode:
        raise ValueError("发送器模式与入口模式不一致")
    rules = load_rules(args.rules, args.set, args.bed)
    if args.synthetic:
        rules = dict(synth.SCENARIOS[args.synthetic].get("rules", {}), **rules)
    output = args.output or config.CODE_DIR / "data" / "family_alarm" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "mode": args.mode, "status": "starting", "events": [], "detections": [],
              "input_kind": "pc_camera" if args.camera is not None else "phone_mjpeg" if args.phone_stream
              else "synthetic" if args.synthetic else "keypoints" if args.keypoints else "interface_check",
              "images_sent": False, "alarm_images_sent": False,
              "monitor_enabled": args.web_monitor, "monitor_frames_served": 0,
              "continuous": args.continuous, "master_connected": False, "actuator_connected": False,
              "phone_received": None, "preflight": None}
    quality, poster, preview, live, monitor = InputQuality(log=log), None, None, None, None

    def journal(record):
        with (output / "events.jsonl").open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        if monitor:
            monitor.update(alarm=record)

    try:
        log("[模式] %s；来源 %s" % (args.mode, args.src))
        if args.web_monitor:
            if monitor_factory is None:
                from vision.web_monitor import WebMonitor
                monitor_factory = WebMonitor
            monitor = monitor_factory(host=args.monitor_host, port=args.monitor_port,
                                      mode=args.mode, input_kind=report["input_kind"])
            log("[监看] http://127.0.0.1:%d/；%s" % (monitor.address[1],
                "同网络电脑可用运行端IP访问；画面会经局域网传输" if args.monitor_host == "0.0.0.0" else "仅运行端本机可看"))
        report["preflight"] = client.preflight()
        if monitor:
            monitor.update(preflight=report["preflight"])
        if args.check_link:
            log("[检查] 健康检查和dry回执通过；没有建立真实警报")
            report["status"] = "link_checked"
        elif args.send_test:
            result = client.deliver(args.event_id or new_event_id())
            report["events"].append(result)
            journal(result)
            log("[接口验收] " + json.dumps(result, ensure_ascii=False))
            report["status"] = "completed"
        else:
            poster = AlarmPoster(client, log=log, on_change=journal)
            detector = FallDetector(rules)
            if args.preview:
                if preview_factory is None:
                    from vision.preview import Preview
                    preview_factory = Preview
                preview = preview_factory(rules)
            if monitor:
                from vision.web_monitor import MonitorPreview
                preview = MonitorPreview(monitor, preview)
            if args.camera is not None or args.phone_stream:
                log("[画面] 正在加载姿态模型、连接摄像头；首次登录可能需要数秒。")
                live = live_factory(camera=args.camera, phone_stream=args.phone_stream,
                                    username=args.phone_username, password=args.phone_password,
                                    duration=args.duration, fps=args.fps_cap, continuous=args.continuous)
                with live:
                    run(iter(live), detector, poster, args.room, preview=preview,
                        quality=quality, log=log, photos=False, alarms=report["detections"])
            else:
                if args.keypoints:
                    meta, frames = read_keypoints(args.keypoints)
                    aspect = meta.get("aspect", 4 / 3)
                else:
                    frames, aspect = synth.build(args.synthetic), 4 / 3
                run(keypoint_source(frames, aspect, realtime=False), detector, poster, args.room,
                    preview=preview, quality=quality, log=log, photos=False, alarms=report["detections"])
            report["status"] = "completed" if quality.frames and quality.usable_frames else "no_usable_input"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except (Exception, SystemExit) as exc:
        report.update(status="error", error_type=type(exc).__name__)
        from camera.phone_calibrate import safe_camera_message
        message = safe_camera_message(exc)
        if message:
            report["message"] = message
            log("[停止] " + message)
        else:
            log("[停止] %s；请检查输入%s，详情见结果文件" % (
                type(exc).__name__, "和床头服务" if args.mode != "off" else "及模型环境"))
    finally:
        if preview:
            preview.close()
        if poster:
            report["sender_drained"] = poster.close()
            report["events"] = poster.snapshot()
        if monitor:
            monitor.update(run_status=report["status"])
            report["monitor_final"] = monitor.snapshot()
            report["monitor_frames_served"] = monitor.served_frames
            report["images_sent"] = monitor.served_frames > 0
            monitor.close()
        report["input_quality"] = quality.snapshot()
        report["ended_utc"] = datetime.now(timezone.utc).isoformat()
        report["ok"] = report["status"] in ("completed", "link_checked") and all(
            e["status"] in ("disabled", "dry_accepted", "bedside_accepted") for e in report["events"])
        write_json(output / "summary.json", report)
        log("[结果] " + str((output / "summary.json").resolve()))
    return report


def main(argv=None):
    config.utf8_console()
    ap = parser()
    args = ap.parse_args(argv)
    if args.mode == "live" and (args.keypoints or args.synthetic):
        ap.error("离线回放和合成输入不能发送真实警报，请用off或dry")
    if (args.check_link or args.send_test) and args.mode == "off":
        ap.error("接口验收须显式选择--mode dry或live；--check-link始终无扰")
    if args.event_id and not args.send_test:
        ap.error("--event-id仅用于--send-test重发同一事件")
    if args.preview and (args.check_link or args.send_test):
        ap.error("接口验收不打开预览")
    if args.web_monitor and (args.check_link or args.send_test):
        ap.error("接口验收不启动监看网页")
    if args.continuous and not (args.camera is not None or args.phone_stream):
        ap.error("持续运行只用于实时摄像头输入")
    if not 1 <= args.monitor_port <= 65535:
        ap.error("监看端口须为1至65535")
    try:
        client = AlarmClient(args.family, mode=args.mode, src=args.src)
        if args.event_id:
            client.payload(args.event_id)
        if args.camera is not None or args.phone_stream:
            # Constructor only validates; enter() is the first device access.
            LiveInput(camera=args.camera, phone_stream=args.phone_stream,
                      duration=args.duration, fps=args.fps_cap, continuous=args.continuous)
        if args.output and args.output.exists():
            raise ValueError("结果目录已存在，请指定新目录")
    except ValueError as exc:
        ap.error(str(exc))
    report = execute(args, client=client)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
