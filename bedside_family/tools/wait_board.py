# -*- coding: utf-8 -*-
"""wait_board.py — 等 .252 复活:每 5 秒探测一次 /messages,通了立刻推 app 并验证"""
import sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import urllib.request

def up():
    try:
        urllib.request.urlopen("http://10.153.47.252:8013/messages", timeout=3)
        return True
    except Exception:
        return False

t0 = time.time()
print("等待 .252 复活…", flush=True)
while time.time() - t0 < 300:
    if up():
        print("板子回来了!用时 %.0fs" % (time.time() - t0), flush=True)
        sys.exit(0)
    time.sleep(5)
print("300 秒内没等到,板子仍未上线", flush=True)
sys.exit(1)