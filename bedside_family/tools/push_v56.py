# -*- coding: utf-8 -*-
"""push_v56.py — 推 v5.6 服务(跌倒警报)到 .252 + 重启 + 端点验证 + 模拟一条测试警报"""
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
print("== deployed, now testing...")

import urllib.request

def post(path, body, ctype="application/json"):
    r = urllib.request.Request("http://10.153.47.252:8013" + path, data=body,
                               method="POST", headers={"Content-Type": ctype})
    return urllib.request.urlopen(r, timeout=6).read().decode()

def get(path):
    return urllib.request.urlopen("http://10.153.47.252:8013" + path, timeout=6).read().decode()

time.sleep(1)
print("GET /fall_alarm :", get("/fall_alarm"))
print("POST test alarm :", post("/fall_alarm", json.dumps(
    {"kind": "fall", "src": "selftest", "conf": 0.9}).encode()))
print("GET /alarms     :", get("/alarms"))
time.sleep(1)
print("POST 2nd alarm  :", post("/fall_alarm", b""))   # 空 body 也应能收
print("GET /messages   (前 200 字):", get("/messages")[:200])
print("ack test        :", post("/alarms/ack", b'{"id":"' + get("/alarms").split('"id": "')[1].split('"')[0].encode() + b'"}'))