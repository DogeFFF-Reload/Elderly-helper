# -*- coding: utf-8 -*-
"""push_v57b.py — 推毫秒 id 修正版 v5.7 + 快速同秒连发验证 + 清理测试遗留
目标状态:板上只留 1 条未确认警报(al_20261024_215159,给用户 v2 首开实测),
其余测试警报全部 ack。
"""
import sys, json, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

HOST = "10.153.47.252"
LOCAL = r"D:\文档\Default Project\caremate_code\code\hand_controller\arm_service.py"
REMOTE = "/root/caremate/hand_controller/arm_service.py"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="root", password="root", timeout=10)
sftp = c.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.close()
print("uploaded")
_, o, e = c.exec_command("systemctl restart arm_service && sleep 2 && systemctl is-active arm_service", timeout=30)
print("service:", o.read().decode("utf-8", "replace").strip())
c.close()

import urllib.request

def post(path, body):
    r = urllib.request.Request("http://10.153.47.252:8013" + path, data=body,
                               method="POST", headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=6).read().decode()

def get(path):
    return urllib.request.urlopen("http://10.153.47.252:8013" + path, timeout=6).read().decode()

time.sleep(1)
print("\n[1] 同秒连发两条空 body(旧版会撞号,现在应各拿各的毫秒 id):")
r1 = json.loads(post("/fall_alarm", b""))
r2 = json.loads(post("/fall_alarm", b""))
print("    A:", r1["id"])
print("    B:", r2["id"])
print("    不撞号 ✓" if r1["id"] != r2["id"] else "    ✗ 还是撞号!")

print("\n[2] 立即 ack 这两条测试警报:")
print("   ", post("/alarms/ack", json.dumps({"id": r1["id"]}).encode()))
print("   ", post("/alarms/ack", json.dumps({"id": r2["id"]}).encode()))

print("\n[3] 清理其它测试遗留(bridge-selftest / teammate-e100 / drycheck 副作用):")
lst = json.loads(get("/alarms"))["alarms"]
for a in lst:
    src = a.get("src", "")
    eid = a.get("event_id", "")
    mine = ("selftest" in src or "bridge" in src or "teammate-e100" in eid)
    if a["id"].startswith("al_20261024_221") and not a.get("acked") and mine:
        print("    ack", a["id"], "→", post("/alarms/ack", json.dumps({"id": a["id"]}).encode()))
    elif not a.get("acked"):
        print("    保留未确认:", a["id"], "src=%s eid=%s" % (src, eid))

print("\n[4] 终态 /alarms:")
for a in json.loads(get("/alarms"))["alarms"]:
    print("    ", a["id"], "acked=%s" % a["acked"], "src=%s" % a.get("src", "-"),
          "eid=%s" % a.get("event_id", "-"))

print("\n[5] GET /fall_alarm(队友健康检查长这样):")
print("   ", get("/fall_alarm"))
print("\n== done ==")