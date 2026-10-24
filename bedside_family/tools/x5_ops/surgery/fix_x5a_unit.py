# -*- coding: utf-8 -*-
"""fix_x5a_unit.py — X5#A:补 arm_service.service unit + 真机 env + 启动验证。"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

UNIT = """[Unit]
Description=Mocen CareMate arm service (hand#2, S3 peer, agent voice)
After=network.target

[Service]
WorkingDirectory=/root/caremate
ExecStart=/usr/bin/python3 -m hand_controller.arm_service
EnvironmentFile=/root/caremate/unit.env
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
"""
ENV = "MOCK_SERIAL=0\nRH56_PROTO=eb90\nHAND1_PORT=/dev/ttyUSB0\nARM_BACKEND=mock\n"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.153.47.251", username="root", password="root", timeout=10)

def run(cmd, label, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print("### " + label)
    print(out if out else ("(stderr) " + err if err else "(empty)"))
    print()
    return out

# 0) 板上现状:unit 文件在不在、源码是 agent 版吗
run("ls -la /etc/systemd/system/arm_service.service 2>/dev/null || echo NO_UNIT_FILE; "
    "grep -c VOICE_OPEN /root/caremate/hand_controller/arm_service.py 2>/dev/null || echo NO_SRC",
    "现状检查")
# 1) 写 unit + env(真机)
sftp = c.open_sftp()
f = sftp.open("/etc/systemd/system/arm_service.service", "w")
f.write(UNIT)
f.close()
f = sftp.open("/root/caremate/unit.env", "w")
f.write(ENV)
f.close()
sftp.close()
print("=== unit + env(真机) 已写 ===\n")
# 2) 清残留 → 启动
run("pkill -9 -f 'hand_[c]ontroller.arm_service' 2>/dev/null; sleep 1; "
    "systemctl daemon-reload && systemctl enable --now arm_service 2>&1 | tail -1; "
    "sleep 3; systemctl is-active arm_service", "启用 + 启动")
run("ss -tlnp | grep 8013 | head -1", "port 8013")
run("curl -s -m 4 http://127.0.0.1:8013/status | head -c 330", "status")
run("journalctl -u arm_service --no-pager | tail -6", "journal")
run("ls /dev/ttyUSB* 2>/dev/null || echo NO_TTYUSB", "CH340")
# 3) 兜底:确认旧三样再也不会抢(residual: master/mic 也确认死透)
run("systemctl is-enabled caremate.service caremate-arm.service caremate-mic.service 2>&1", "旧 unit 自启状态")
c.close()
print("=== Done ===")