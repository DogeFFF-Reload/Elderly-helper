# -*- coding: utf-8 -*-
"""push_v57.py — 推 v5.7 服务(event_id 去重 + dry 无扰测试)到 .252 + 重启 + 全自检
自检流程全部无扰:先 dry POST,再 event_id 去重对(只造 1 条真警报并立即 ack),
最后核对 /alarms 只多 1 条、其余端点无恙。
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

_, o, e = c.exec_command("python3 -c \"import sys; sys.path.insert(0,'/root/caremate'); import hand_controller.arm_service; print('remote import OK')\"", timeout=20)
print(o.read().decode("utf-8", "replace").strip() or e.read().decode("utf-8", "replace").strip())
_, o, e = c.exec_command("systemctl restart arm_service && sleep 2 && systemctl is-active arm_service", timeout=30)
print("service:", o.read().decode("utf-8", "replace").strip())
c.close()
print("== deployed, now testing (无扰优先)...")

import urllib.request

def post(path, body, ctype="application/json"):
    r = urllib.request.Request("http://10.153.47.252:8013" + path, data=body,
                               method="POST", headers={"Content-Type": ctype})
    return urllib.request.urlopen(r, timeout=6).read().decode()

def get(path):
    return urllib.request.urlopen("http://10.153.47.252:8013" + path, timeout=6).read().decode()

time.sleep(1)
print("\n[1] GET  /fall_alarm (健康检查):")
print("   ", get("/fall_alarm"))

print("\n[2] POST /fall_alarm {\"dry\":true} (无扰,不应建警报):")
r2 = json.loads(post("/fall_alarm", json.dumps(
    {"kind": "fall", "src": "cam-A", "conf": 0.9, "event_id": "drycheck-1", "dry": True}).encode()))
print("   ", r2)

n0 = len(json.loads(get("/alarms"))["alarms"])
print("\n    警报条数(前):", n0, "→ dry 后应不变")
n1 = len(json.loads(get("/alarms"))["alarms"])
print("    警报条数(后):", n1, "→", "OK 无扰 ✓" if n1 == n0 else "✗ dry 竟建了警报!")

print("\n[3] POST 首发同 event_id=teammate-e100 (真警报,马上 ack):")
r3 = json.loads(post("/fall_alarm", json.dumps(
    {"kind": "fall", "src": "cam-A", "conf": 0.88, "event_id": "teammate-e100"}).encode()))
print("   ", r3)
fid = r3.get("id")

print("\n[4] POST 重发同 event_id=teammate-e100 (应 dedup=true 且 id 不变):")
r4 = json.loads(post("/fall_alarm", json.dumps(
    {"kind": "fall", "src": "cam-A", "event_id": "teammate-e100"}).encode()))
print("   ", r4)
if r4.get("dedup") and r4.get("id") == fid:
    print("    去重 ✓")
else:
    print("    ✗ 去重没生效!")

n2 = len(json.loads(get("/alarms"))["alarms"])
print("\n    警报条数变化: %d → %d (两次同号 POST 只增 1 = %s)"
      % (n1, n2, "OK ✓" if n2 == n1 + 1 else "✗"))
print("    ack 刚才那条:", post("/alarms/ack", json.dumps({"id": fid}).encode()))

print("\n[5] 兼容性:空 body POST(老姿势,仍收):")
r5 = json.loads(post("/fall_alarm", b""))
print("   ", r5, "→ 立即 ack:", post("/alarms/ack", json.dumps({"id": r5.get("id")}).encode()))

print("\n[6] 其余端点无恙:")
print("    GET /status 前120字:", get("/status")[:120])
print("    GET /messages 前80字:", get("/messages")[:80])
print("\n== all done ==")