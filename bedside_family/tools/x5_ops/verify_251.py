# -*- coding: utf-8 -*-
"""verify .251 from laptop over WiFi + T5AI chain precheck."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import socket, json

def tcp_probe(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            return True
    except OSError:
        return False

print("tcp 10.153.47.251:8013:", tcp_probe("10.153.47.251", 8013))

import urllib.request
try:
    with urllib.request.urlopen("http://10.153.47.251:8013/status", timeout=5) as r:
        data = json.loads(r.read().decode())
        print("HTTP GET /status -> 200")
        print("backend:", data.get("backend"), "| mock_serial:", data.get("mock_serial"))
        print("poses count:", len(data.get("poses", [])))
except Exception as e:
    print("HTTP FAIL:", e)