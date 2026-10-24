"""Offline fall -> actual receiver core -> simulated family polling/ack check.

No address argument is accepted. Every connection is guarded to loopback, the
bedside module top level is never imported, and no handset notification occurs.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from family_alarm_receiver import AlarmReceiver  # noqa: E402
from gesture.fall_rules import FallDetector  # noqa: E402
from vision import synth  # noqa: E402
from vision.family_alarm import AlarmClient, AlarmPoster, request_json  # noqa: E402
from vision.fall_bridge import keypoint_source, run  # noqa: E402


def check_case(scenario):
    if scenario not in ("fall", "slow_floor", "sit"):
        raise ValueError("Only the three fixed offline cases are accepted")
    connect = socket.create_connection

    def loopback_only(address, *args, **kwargs):
        if address[0] != "127.0.0.1":
            raise AssertionError("Hardware/external connection forbidden")
        return connect(address, *args, **kwargs)

    with patch("socket.create_connection", side_effect=loopback_only), AlarmReceiver() as receiver:
        client = AlarmClient(receiver.url, mode="live", src="offline-chain-check", sleep=lambda _: None)
        preflight = client.preflight()
        poster = AlarmPoster(client, log=lambda _: None)
        try:
            detections = run(keypoint_source(synth.build(scenario), 4/3, realtime=False),
                             FallDetector(synth.SCENARIOS[scenario].get("rules")), poster, "offline",
                             photos=False, log=lambda _: None)
        finally:
            drained = poster.close(timeout=3)
        receipts = poster.snapshot()
        alarms = request_json("GET", receiver.url + "/alarms")["alarms"]
        expected = len(synth.SCENARIOS[scenario]["expect"])
        assert drained and len(detections) == len(receipts) == len(alarms) == expected
        result = {"scenario": scenario, "expected_events": expected, "detections": detections,
                  "preflight": preflight["status"], "sender_receipts": receipts,
                  "simulated_family_poll_count": len(alarms), "phone_received": None,
                  "real_phone_notification_verified": False, "checks": []}
        if expected:
            receipt, alarm = receipts[0], alarms[0]
            assert receipt["status"] == "bedside_accepted"
            assert receipt["event_id"] == alarm["event_id"]
            assert receipt["alarm_id"] == alarm["id"] and alarm["acked"] is False
            result["checks"].extend(["detector_emitted", "receiver_accepted_matching_event",
                                     "simulated_family_polled_same_alarm"])
            acknowledged = request_json("POST", receiver.url + "/alarms/ack", {"id": alarm["id"]})
            assert acknowledged == {"ok": True, "acked": alarm["id"]}
            after = request_json("GET", receiver.url + "/alarms")["alarms"]
            assert len(after) == 1 and after[0]["acked"] is True
            # A retry of the same event after ACK must not make the phone ring again.
            retry = client.deliver(receipt["event_id"])
            after_retry = request_json("GET", receiver.url + "/alarms")["alarms"]
            assert retry["dedup"] is True and retry["alarm_id"] == alarm["id"]
            assert len(after_retry) == 1 and after_retry[0]["acked"] is True
            result["checks"].extend(["family_ack_stored", "retry_after_ack_no_duplicate_alarm"])
            result["alarm_id"] = alarm["id"]
            result["event_id"] = alarm["event_id"]
        else:
            assert not alarms and not receipts
            result["checks"].append("normal_sitting_no_alarm")
        result["ok"] = True
        return result


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="纯离线模拟全链检查；不接摄像头、不发真实手机通知")
    ap.add_argument("--output", type=Path, help="可选的新JSON结果文件")
    args = ap.parse_args(argv)
    if args.output and args.output.exists():
        ap.error("结果文件已存在，请换新路径")
    cases = [check_case(name) for name in ("fall", "slow_floor", "sit")]
    report = {"schema_version": 1, "verified_utc": datetime.now(timezone.utc).isoformat(),
              "kind": "offline_loopback_contract_check", "ok": all(case["ok"] for case in cases),
              "cases": cases, "real_camera_opened": False, "real_bedside_contacted": False,
              "real_phone_notification_verified": False,
              "limits": "Synthetic coordinates bypass the pose model; family GET/ACK is simulated. Real camera recognition and Android UI/sound/background delivery require field acceptance."}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[离线通过] 快速倒地、缓慢倒地：规则识别 → HTTP上报 → 模拟家属读取 → 确认处理；普通坐下无警报。")
    print("[尚未验收] 真实摄像头识别、家属手机界面/铃声/震动、熄屏后台通知。")
    if args.output:
        print("[结果] " + str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
