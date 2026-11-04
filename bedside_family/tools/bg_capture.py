# -*- coding: utf-8 -*-
"""bg_capture.py — COM8 后台监听(默认 1800 秒,可传参),USB 瞬断自动重连"""
import sys, time, traceback
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import serial

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 1800
t0 = time.time()
n = 0
port = None
while time.time() - t0 < secs:
    if port is None or not port.is_open:
        try:
            port = serial.Serial("COM8", 115200, timeout=1, rtscts=False, dsrdtr=False)
            port.setDTR(False); port.setRTS(False)
            print("== COM8 已连接(已收 %d 行,elapsed %.0fs)" % (n, time.time() - t0), flush=True)
        except Exception as e:
            port = None
            time.sleep(2)
            continue
    try:
        ln = port.readline()
    except Exception as e:
        print("== 端口异常,5s 后重连: %s" % e, flush=True)
        try:
            port.close()
        except Exception:
            pass
        port = None
        time.sleep(5)
        continue
    s = ln.decode("utf-8", "replace").strip()
    if s:
        print("%.1fs| %s" % (time.time() - t0, s), flush=True)
        n += 1
if port:
    try:
        port.close()
    except Exception:
        pass
print("=== 共 %d 行,监听结束" % n)