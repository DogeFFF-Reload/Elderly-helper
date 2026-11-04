# -*- coding: utf-8 -*-
"""watch_usb_x5.py — X5 接本机 USB 后持续盯:WiFi 是否冒出(.252)+ PnP 设备变化"""
import subprocess, sys, time, os
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def ping():
    r = subprocess.run(["ping", "-n", "1", "-w", "1200", "10.153.47.252"],
                       capture_output=True)
    return b"TTL=" in r.stdout

print("盯 100 秒,每 5 秒 ping 一次 .252(WiFi 起来会立刻发现)…", flush=True)
t0 = time.time()
hit = False
while time.time() - t0 < 100:
    if ping():
        print("★ %.0fs: .252 回来了!" % (time.time() - t0), flush=True)
        hit = True
        break
    time.sleep(5)
if not hit:
    print("100 秒内 .252 没冒出来", flush=True)

print("--- PnP 异常/未知设备(USB 插入应有痕迹)---", flush=True)
r = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-PnpDevice | Where-Object { $_.Status -ne 'OK' } | "
     "Select-Object -First 12 Class, FriendlyName | Format-Table -AutoSize"],
    capture_output=True, text=True, errors="replace")
print(r.stdout or "(无异常设备)", flush=True)