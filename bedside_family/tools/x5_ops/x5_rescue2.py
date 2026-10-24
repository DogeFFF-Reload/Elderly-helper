import os
# -*- coding: utf-8 -*-
"""X5 rescue2: wifi connect + static .251 + start arm_service + verify."""
import sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.128.10", username="root", password=os.environ.get("BOARD_PWD", "root"), timeout=10)

def run(cmd, label, timeout=30, silent=False):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    if not silent:
        print("### " + label)
        print(out if out else ("(stderr) " + err if err else "(empty)"))
        print()
    return out, err

# 1) 扫描确认热点能看到
run("nmcli dev wifi list | grep -i mocen || echo AP-NOT-SEEN", "wifi scan");

# 2) 连热点(建立 profile)
_, err = run("nmcli dev wifi connect 'Mocen_Bear' password 'YOUR_WIFI_PASSWORD' ifname wlan0 2>&1", "connect hotspot", timeout=45)
if "successfully" not in (err or "").lower():
    run("nmcli -t -f NAME,DEVICE,ACTIVE con show | grep -i -E 'mocen|caremate|wifi' || echo NO-PROFILE", "profile check")
run("nmcli -t -f IP4 dev show wlan0 | head -3", "wlan0 ip")

# 3) 静态化 .251(网关沿用 DHCP 路由)
run("ip route | grep default", "current route")
run("GW=$(ip route | grep default | awk '{print $3}' | head -1); "
    "nmcli con mod 'Mocen_Bear' ipv4.method manual ipv4.addresses 10.153.47.251/24 ipv4.gateway $GW "
    "ipv4.dns '10.153.47.167 223.5.5.5' connection.autoconnect yes; "
    "nmcli con down 'Mocen_Bear' 2>/dev/null; nmcli con up 'Mocen_Bear' 2>&1 | tail -2", "set static .251 + reconnect", timeout=45)
run("nmcli -t -f IP4 dev show wlan0 | head -2; ip -4 addr show wlan0 | grep inet", "wlan0 addr after static")
run("nmcli con mod 'Mocen_Bear' connection.autoconnect yes; nmcli -t -f NAME,DEVICE,ACTIVE con show", "profile final")

# 4) arm_service
run("systemctl is-enabled arm_service; systemctl status arm_service --no-pager -l | head -8", "arm_service status")
run("ls -l /dev/ttyUSB* 2>/dev/null || echo NO-TTYUSB", "ttyUSB")
run("systemctl start arm_service 2>&1; sleep 2; systemctl is-active arm_service", "start arm_service")
run("curl -s -m 3 http://127.0.0.1:8013/status | head -c 260", "local /status")

# 5) 验证本机热点视角
time.sleep(2)
run("ping -c2 -W2 10.153.47.212 | tail -2", "ping laptop")
c.close()