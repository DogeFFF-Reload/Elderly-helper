import os
# -*- coding: utf-8 -*-
"""x5_push_agent.py — 把 agent 版 arm_service.py 推到手板 X5#A(或任意 X5)并重启验证。

用法:
  py x5_push_agent.py 10.153.47.XXX     # 手板回来后查到的 IP(DHCP,扫出来再跑)
  py x5_push_agent.py                    # 不带参数=推 .251(臂板,队友救活后也可用此脚本)
前置: 该 X5 已部署过 caremate(caremate/hand_controller 存在),SSH root/root。
"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else "10.153.47.251"
SRC = r"D:\文档\Default Project\caremate_code\code\hand_controller\arm_service.py"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="root", password=os.environ.get("BOARD_PWD", "root"), timeout=10)

def run(cmd, label, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print("### " + label)
    print(out if out else ("(stderr) " + err if err else "(empty)"))
    print()
    return out

run("ls /root/caremate/hand_controller >/dev/null 2>&1 && echo HAVE || echo NONE",
    "caremate 在不在(在才继续)")

sftp = c.open_sftp()
sftp.put(SRC, "/root/caremate/hand_controller/arm_service.py")
sftp.close()
print("=== pushed arm_service.py (agent 三层语音版) ===\n")

run("systemctl restart arm_service && sleep 2; systemctl is-active arm_service", "restart")
run("curl -s -m 5 http://127.0.0.1:8013/status | head -c 160", "/status")
run("journalctl -u arm_service --since '-30 sec' --no-pager | tail -6", "journal")
c.close()
print("=== Done (%s) ===" % HOST)