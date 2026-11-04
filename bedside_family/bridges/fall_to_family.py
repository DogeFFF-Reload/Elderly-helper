#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Master events -> bedside v5.7 native App alarm, with durable pending ids.

Only the initial structured `fall`/`danger` event is forwarded. Dialog,
photos, repeated fall observations and cancellation messages are ignored.
Default mode OFF; `--mode live` must be explicit. Start before detecting a
fall: on first startup existing history is skipped. Do not run this alongside
a second detector for the same scene; family_watch already sends directly.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))

from vision.family_alarm import AlarmClient, DEFAULT_FAMILY, MODES, base_url, request_json  # noqa: E402


def looks_like_fall(event):
    return isinstance(event, dict) and event.get("type") == "fall" and event.get("level") == "danger"


def event_id(master, event):
    """Include timestamp and master identity because bus ids restart from 1."""
    if (not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] < 1
            or type(event.get("ts")) not in (int, float) or not math.isfinite(event["ts"])):
        raise ValueError("主脑事件缺少有效id或ts")
    identity = json.dumps([master, event["id"], event["ts"]], separators=(",", ":"))
    return "bus-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:40]


class Bridge:
    def __init__(self, master, client, state_path, *, from_start=False, get=request_json, log=print):
        self.master, self.client = base_url(master), client
        self.path, self.get, self.log = Path(state_path), get, log
        self.identity = [self.master, client.base, client.mode, client.src]
        self.fresh, self.from_start = not self.path.exists(), from_start
        self.seen, self.pending = [], []
        if not self.fresh:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("identity") != self.identity:
                raise ValueError("桥接状态与主脑、床头、模式或来源不符，请使用新的状态文件")
            self.seen, self.pending = state["seen"], state["pending"]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"identity": self.identity, "seen": self.seen[-2000:],
                                         "pending": self.pending}, ensure_ascii=False,
                                        allow_nan=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def poll(self):
        # Snapshot polling also sees new ids after a master restart. Once captured,
        # pending alarms survive leaving the master's bounded in-memory history.
        try:
            data = self.get("GET", self.master + "/api/events?since=0", timeout=5, max_bytes=1024 * 1024)
            events = data.get("events")
            if not isinstance(events, list):
                raise ValueError("主脑事件列表无效")
            identified = [(event_id(self.master, ev), ev) for ev in events]
        except Exception as exc:
            self.log("[主脑不可达/回执无效] " + type(exc).__name__)
            # Still retry durable pending alarms when the master is offline.
            identified = None
        if identified is not None:
            skip_history = self.fresh and not self.from_start
            known = set(self.seen)
            for eid, event in identified:
                if eid in known:
                    continue
                known.add(eid)
                self.seen.append(eid)
                if not skip_history and looks_like_fall(event):
                    self.pending.append({"event_id": eid})
            self.seen = self.seen[-2000:]
            self.fresh = False
            self.save()  # Persist stable ids before POST, including crash/restart.
            if skip_history:
                self.log("[启动] 已跳过旧事件；现在开始等待新倒地事件")
        failed = False
        for item in list(self.pending):
            result = self.client.deliver(item["event_id"])
            self.log("[桥接] " + json.dumps(result, ensure_ascii=False))
            if result["status"] in ("disabled", "dry_accepted", "bedside_accepted"):
                self.pending.remove(item)
            else:
                item.update(result)
                failed = True
            self.save()
        return identified is not None and not failed


def main(argv=None):
    ap = argparse.ArgumentParser(description="主脑初次倒地事件 → 家属App；默认off，live须显式启用")
    ap.add_argument("--master", default="http://127.0.0.1:8000")
    ap.add_argument("--family", default=DEFAULT_FAMILY)
    ap.add_argument("--mode", choices=MODES, default="off")
    ap.add_argument("--src", default="master-bus")
    ap.add_argument("--interval", type=float, default=1)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--state", type=Path)
    ap.add_argument("--from-start", action="store_true", help="包含已有事件，仅限off/dry验证")
    ap.add_argument("--selftest", action="store_true", help="兼容旧参数：现在仅健康检查+dry，不建立真实警报")
    args = ap.parse_args(argv)
    if not math.isfinite(args.interval) or not .1 <= args.interval <= 60:
        ap.error("轮询间隔须为0.1至60秒")
    if args.mode == "live" and args.from_start:
        ap.error("live模式不补发启动前旧事件，请先启动桥接器再开始检测")
    try:
        master = base_url(args.master)
        client = AlarmClient(args.family, mode="dry" if args.selftest else args.mode, src=args.src)
        if args.selftest:
            print(json.dumps(client.preflight(), ensure_ascii=False))
            return 0
        identity = json.dumps([master, client.base, client.mode, client.src])
        state = args.state or ROOT / "code/data/family_bridge" / (
            hashlib.sha256(identity.encode()).hexdigest()[:16] + ".json")
        bridge = Bridge(master, client, state, from_start=args.from_start)
        client.preflight()
        print("[启动] %s → %s；模式%s" % (master, client.base, client.mode), flush=True)
        while True:
            ok = bridge.poll()
            if args.once:
                return 0 if ok else 2
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print("[停止] " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
