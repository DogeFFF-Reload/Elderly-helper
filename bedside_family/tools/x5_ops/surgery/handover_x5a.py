# -*- coding: utf-8 -*-
"""handover_x5a.py — 确认 .251=手板X5#A,清掉昨晚 nohup 老服务,让 systemd(agent版) 接管。"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

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

# 1) 身份确认(主机名/开机时长/USB 串口)
run("hostname; uptime -p; ls /dev/ttyUSB* 2>/dev/null || echo NO_TTYUSB", "identity")
# 2) 老进程是谁:命令行+父进程(防复活线索)
run("ps -ef | grep arm_service | grep -v grep", "old process")
# 3) 自启动挂哪了:rc.local / crontab / autostart
run("cat /etc/rc.local 2>/dev/null; (crontab -l 2>/dev/null | grep -i arm) || echo no-cron; "
    "ls -la /root/.config/autostart/ 2>/dev/null || echo no-autostart", "autostart probe")
# 4) 板上资源:env/vosk 模型
run("cat /root/caremate/unit.env 2>/dev/null; echo ---; ls /root/caremate/vosk-cn 2>/dev/null | head -3", "resources")
# 5) 击杀老服务,systemd 接管
run("pkill -9 -f arm_service; sleep 1; ps -ef | grep arm_service | grep -v grep || echo ALL-KILLED",
    "kill legacy")
run("systemctl daemon-reload; systemctl restart arm_service; sleep 3; systemctl is-active arm_service",
    "systemd takeover")
run("ss -tlnp | grep 8013 | head -1", "port 8013")
run("curl -s -m 4 http://127.0.0.1:8013/status | head -c 330", "status")
run("journalctl -u arm_service --no-pager | tail -8", "journal")
c.close()
print("=== Done ===")