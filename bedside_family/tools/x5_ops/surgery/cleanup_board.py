# -*- coding: utf-8 -*-
"""cleanup_board.py — 板上清理自测残留的 messages 测试件 + 复核服务"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.153.47.252", username="root", password="root", timeout=10)
_, o, _ = c.exec_command(
    "ls /root/caremate/messages/ 2>/dev/null; "
    "rm -f /root/caremate/messages/*.wav /root/caremate/messages/*.part; "
    "rm -f /root/caremate/board_msg_test.py /root/caremate/t2.py; "
    "echo '--- after cleanup ---'; ls -la /root/caremate/messages/; "
    "systemctl is-active arm_service", timeout=30)
print(o.read().decode("utf-8", "replace"))
c.close()