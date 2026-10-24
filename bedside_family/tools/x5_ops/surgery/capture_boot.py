# -*- coding: utf-8 -*-
"""capture_boot.py — 抓 COM8 启动串口日志(ES8311 自检验证)"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import serial, time

port = serial.Serial("COM8", 115200, timeout=1)
port.setDTR(False); port.setRTS(False)   # 不打断已运行的程序(刚上传会自动重启)
t0 = time.time()
lines = []
while time.time() - t0 < 22:
    ln = port.readline().decode("utf-8", "replace").strip()
    if ln:
        lines.append(ln)
        if "[spk]" in ln and "PA on" in ln:
            break
port.close()
seen_spk = False
for ln in lines:
    print(ln)
    if ln.startswith("[spk]"):
        seen_spk = True
print()
print("=== 播放链路日志出现:", seen_spk)