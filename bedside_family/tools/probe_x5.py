# -*- coding: utf-8 -*-
"""probe_x5.py — .252 网络层探测:ICMP + TCP 22/8013/8014;顺带全子网扫描找移址"""
import socket, subprocess, sys, concurrent.futures as cf
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def tcp(ip, port, t=2.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(t)
    try:
        s.connect((ip, port)); s.close(); return True
    except Exception:
        return False

HOST = "10.153.47.252"
r = subprocess.run(["ping", "-n", "1", "-w", "2000", HOST], capture_output=True)
icmp = b"TTL=" in r.stdout
print("%s  icmp=%s  tcp22=%s  tcp8013=%s  tcp8014=%s" % (
    HOST, icmp, tcp(HOST, 22), tcp(HOST, 8013), tcp(HOST, 8014)), flush=True)

print("--- 子网扫描(在线= ICMP 或 22/8013 任一通)...")
def probe(octet):
    ip = "10.153.47.%d" % octet
    alive = False
    rp = subprocess.run(["ping", "-n", "1", "-w", "300", ip], capture_output=True)
    if b"TTL=" in rp.stdout:
        alive = True
    else:
        alive = tcp(ip, 8013, 0.8) or tcp(ip, 22, 0.8)
    return (ip, alive)

with cf.ThreadPoolExecutor(60) as ex:
    res = [x for x in ex.map(probe, range(1, 255)) if x[1]]
print("在线:", ", ".join(ip for ip, _ in res), flush=True)