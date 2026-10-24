#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fall_to_family.py — 视觉/主脑 → 家属 App 跌倒警报桥(零侵入姿势)

订阅主脑事件流(GET /api/events?since=<id>),看到跌倒类事件立刻转发到
床头执行端的 POST /fall_alarm,家属手机 App 3~10 秒内响警报。
转发时带 event_id=bus-<主脑事件id>:同一事件的轮询重放/网络重试在床头
自动去重,只响一次(SVC_VER >= 5.7)。

用法:
    python bridges/fall_to_family.py --master http://127.0.0.1:8000 \\
                                    --family http://10.153.47.252:8013
    python bridges/fall_to_family.py --master http://127.0.0.1:8000 --once   # 只处理一轮

事件匹配(宽松,宁误报不漏报):event["type"] 含 fall / 跌倒,
或 text/detail 字段含 "跌倒/摔倒/倒地/fall"。

部署:任意能同时访问主脑和床头的机器都行(X5-B 本机/笔记本);
纯标准库,Python 3.8+。退出码:0 正常停止,2 一轮内主脑完全不可达。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FALL_WORDS = ("跌倒", "摔倒", "倒地", "fall")


def http_post_json(url: str, body: bytes, timeout: float = 3.5) -> dict:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def looks_like_fall(ev: dict) -> bool:
    t = str(ev.get("type", "")).lower()
    if "fall" in t:
        return True
    blob = " ".join(str(ev.get(k, "")) for k in ("type", "text", "detail", "label", "msg"))
    return any(w in blob for w in FALL_WORDS)


def one_round(master: str, family: str, since: int, warn_missing: bool) -> int:
    """处理一轮事件流;返回新的 since 游标。"""
    try:
        data = http_get_json("%s/api/events?since=%d" % (master, since), timeout=5)
    except Exception as e:
        if warn_missing:
            print("[link] 主脑暂不可达:%s(继续重试)" % e, flush=True)
        return since
    events = data.get("events") or []
    last_id = since
    for ev in events:
        eid = int(ev.get("id", last_id + 1))
        last_id = max(last_id, eid)
        if looks_like_fall(ev):
            body = json.dumps({"kind": "fall", "src": "master-bus",
                               "event_id": "bus-%s" % ev.get("id", ""),
                               "conf": None if ev.get("conf") is None
                               else round(float(ev.get("conf") or 0), 2)}).encode()
            try:
                r = http_post_json(family + "/fall_alarm", body)
                print("[alarm→] %s → family ok=%s id=%s"
                      % (ev.get("time", time.strftime("%H:%M:%S")),
                         r.get("ok"), r.get("id", "")), flush=True)
            except Exception as e:
                print("[alarm✗] 转发失败:%s(床头不可达?)" % e, flush=True)
    return last_id


def main():
    ap = argparse.ArgumentParser(description="主脑事件流 → 家属 App 跌倒警报")
    ap.add_argument("--master", default="http://127.0.0.1:8000")
    ap.add_argument("--family", default="http://10.153.47.252:8013")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒(默认 1)")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--selftest", action="store_true",
                    help="直接向 family 发一条测试警报然后退出")
    a = ap.parse_args()

    if a.selftest:
        print(http_post_json(a.family + "/fall_alarm",
                             b'{"kind":"fall","src":"bridge-selftest"}'), flush=True)
        return

    since = 0
    print("[start] master=%s → family=%s interval=%.1fs"
          % (a.master, a.family, a.interval), flush=True)
    misses = 0
    while True:
        n0 = since
        since = one_round(a.master, a.family, since, warn_missing=(misses % 20 == 0))
        if since == n0:
            misses += 1
        else:
            misses = 0
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()