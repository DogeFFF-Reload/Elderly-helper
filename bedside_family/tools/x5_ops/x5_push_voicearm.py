import os
# -*- coding: utf-8 -*-
"""push patched arm_service.py + switch hand#1 to REAL (EB90) if ttyUSB0 present."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.153.47.251", username="root", password=os.environ.get("BOARD_PWD", "root"), timeout=10)

def run(cmd, label, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print("### " + label)
    print(out if out else ("(stderr) " + err if err else "(empty)"))
    print()
    return out

# 0) 看手在不在
usb = run("ls /dev/ttyUSB* 2>/dev/null || echo NONE", "ttyUSB?")
has_hand = "ttyUSB0" in usb

# 1) 推补丁版 arm_service.py(语音臂路由)
sftp = c.open_sftp()
sftp.put(r"D:\文档\Default Project\caremate_code\code\hand_controller\arm_service.py",
         "/root/caremate/hand_controller/arm_service.py")
print("=== pushed arm_service.py ===\n")

# 2) 单元环境:手就位→切真机;否则维持 mock
if has_hand:
    env = "MOCK_SERIAL=0\nRH56_PROTO=eb90\nHAND1_PORT=/dev/ttyUSB0\nARM_BACKEND=mock\n"
    print(">>> ttyUSB0 在,切真机模式 (MOCK_SERIAL=0, RH56_PROTO=eb90)\n")
else:
    env = "MOCK_SERIAL=1\nRH56_PROTO=mock\nHAND1_PORT=/dev/ttyUSB0\nARM_BACKEND=mock\n"
    print(">>> 没看到 /dev/ttyUSB0,维持 mock——检查 CH340 线是否插稳\n")
f = sftp.open("/root/caremate/unit.env", "w")
f.write(env)
f.close()
sftp.close()

# 3) 重启服务 + 体检
run("systemctl daemon-reload && systemctl restart arm_service && sleep 3; "
    "systemctl is-active arm_service", "restart service")
run("ss -tlnp | grep 8013 | head -1", "port 8013")
run("curl -s -m 5 http://127.0.0.1:8013/status | head -c 220", "/status")
run("journalctl -u arm_service --since '-40 sec' --no-pager | tail -12",
    "journal (看 EB90 握手/mock 标志)")

c.close()
print("=== Done ===")