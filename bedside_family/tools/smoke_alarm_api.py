#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_alarm_api.py — /fall_alarm 契约冒烟测试(v5.7)

面向两个场景:
  1) 跌倒检测端接入前的链路自检(队友侧直接跑,不用问人);
  2) 家属端改版后的回归(改完服务跑一遍,确认契约没破)。

自建自收、最后恢复原状:全程只在步骤 [5][6] 造 2 条真警报(会短暂响铃),
随后立即 ack;其余步骤全部无扰。退出码 0=全过 1=有失败。

用法: python tools/smoke_alarm_api.py [--base http://10.153.47.252:8013]
"""
import argparse
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def call(base, path, body=None, method=None):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method or
                                 ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://10.153.47.252:8013")
    a = ap.parse_args()
    fails = []

    def check(name, cond, detail=""):
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
        if not cond:
            fails.append(name)

    try:
        # [1] 健康检查:版本核对
        r = call(a.base, "/fall_alarm")
        check("健康检查", r.get("ok") is True and r.get("service") == "arm_service",
              "ver=%s" % r.get("ver", "?"))

        # [2] 基线:当前警报条数
        n0 = len(call(a.base, "/alarms")["alarms"])
        print("      警报基线 %d 条" % n0)

        # [3] dry 无扰:回执对 + 不落账
        r = call(a.base, "/fall_alarm", {"dry": True, "kind": "fall",
                                         "src": "smoke-test", "conf": 0.9,
                                         "event_id": "smoke-dry-1"})
        n1 = len(call(a.base, "/alarms")["alarms"])
        check("dry 测试", r.get("ok") and r.get("dry") is True and n1 == n0,
              "echo=%s 条数 %d→%d" % (bool(r.get("echo")), n0, n1))
    except Exception as e:
        print("[FAIL] 床头不可达:%s" % e)
        return 1

    made = []
    try:
        # [4] 真警报(短响一声,稍后 ack):带 event_id 首发
        eid = "smoke-%d" % int(time.time())
        r = call(a.base, "/fall_alarm", {"kind": "fall", "src": "smoke-test",
                                         "conf": 0.9, "event_id": eid})
        aid = r.get("id", "")
        made.append(aid)
        n2 = len(call(a.base, "/alarms")["alarms"])
        check("首发建账", r.get("ok") and r.get("dedup") is False
              and n2 == n0 + 1, "id=%s 条数→%d" % (aid, n2))

        # [5] 同号重发:去重,不增条
        r = call(a.base, "/fall_alarm", {"kind": "fall", "src": "smoke-test",
                                         "event_id": eid})
        n3 = len(call(a.base, "/alarms")["alarms"])
        check("event_id 去重", r.get("dedup") is True and r.get("id") == aid
              and n3 == n0 + 1, "")

        # [6] 空 body 兼容(短响一声):老姿势仍收,id 不撞号
        r = call(a.base, "/fall_alarm", b"")
        aid2 = r.get("id", "")
        made.append(aid2)
        check("空 body 兼容", r.get("ok") is True and aid2 and aid2 != aid,
              "id=%s" % aid2)

        # [7] ack 所有自建警报 → 状态复原
        ok = all(call(a.base, "/alarms/ack", {"id": i}).get("ok") for i in made)
        lst = call(a.base, "/alarms")["alarms"]
        acked = all(x.get("acked") for x in lst if x.get("id") in made)
        check("ack 复原", ok and acked, "共 %d 条" % len(made))
    except Exception as e:
        print("[FAIL] 中途异常:%s(尝试恢复自建警报)" % e)
        for i in made:
            try:
                call(a.base, "/alarms/ack", {"id": i})
            except Exception:
                pass
        check("自建警报已尽量 ack", False)
        return 1

    print("\n== 结果:%s ==\n" % ("ALL PASS ✓" if not fails else "失败 %d 项 ✗" % len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())