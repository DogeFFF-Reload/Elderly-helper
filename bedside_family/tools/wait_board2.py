# -*- coding: utf-8 -*-
"""wait_board2.py — 等 .252 复活(15 分钟窗口),回来就 curl 验证服务"""
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
print("等 .252…", flush=True)
while time.time() - t0 < 900:
    if up():
        print("板子复活! 用时 %.0fs" % (time.time() - t0), flush=True)
        for p in ("/messages", "/status"):
            try:
                r = urllib.request.urlopen("http://10.153.47.252:8013" + p, timeout=4)
                print(p, "->", r.read(300).decode("utf-8", "replace"), flush=True)
            except Exception as e:
                print(p, "err", e, flush=True)
        sys.exit(0)
    time.sleep(4)
print("15 分钟仍无响应", flush=True)
sys.exit(1)