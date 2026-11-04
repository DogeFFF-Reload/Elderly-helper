# -*- coding: utf-8 -*-
"""diag_buttons.py — 硬复位重启并完整抓 45 秒串口,看播放后主循环是否活着"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import serial, time

port = serial.Serial("COM8", 115200, timeout=1, rtscts=False, dsrdtr=False)
# 复位:RTS->EN 拉低再放;DTR 保持 False(GPIO0=1,正常启动)
port.setDTR(True); port.setRTS(True)
time.sleep(0.1)
port.setDTR(False)
port.setRTS(False)
t0 = time.time()
n = 0
while time.time() - t0 < 45:
    ln = port.readline().decode("utf-8", "replace").strip()
    if ln:
        print("%.1fs| %s" % (time.time() - t0, ln))
        n += 1
port.close()
print("=== 共 %d 行" % n)