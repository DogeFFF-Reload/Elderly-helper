# -*- coding: utf-8 -*-
"""board_wifi.py — 床头板(X5)换 WiFi 一条命令(经 USB 网卡 192.168.128.10)

用法:
  py board_wifi.py --check                 # 看现在连的网 + IP + 服务状态
  py board_wifi.py --ssid X --pass Y       # 删旧配置 → 连新网 → 显示新 IP + 服务状态
烧完先对时:  py board_sync_clock.py        (板无 RTC,断电就丢)

板子 USB 数据线必须插电脑(会出现 Remote NDIS 网卡)。
"""
import argparse, shlex, sys
import paramiko

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST, USER, PW = "192.168.128.10", "root", "root"

def run(c, cmd, t=60):
    _, o, e = c.exec_command(cmd, timeout=t)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    return out, err

def conn():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=10)
    return c

def check(c):
    cmd = (
        "iw dev wlan0 link 2>/dev/null | head -4; echo ====IP====; "
        "ip -4 addr show wlan0 | grep inet; echo ====GW====; "
        "ip route | head -3; echo ====SVC====; "
        "systemctl is-active arm_service; "
        "curl -s -m 3 http://localhost:8013/status | head -c 100; echo; echo ====DATE====; "
        "date '+%F %T'"
    )
    o, _ = run(c, cmd)
    print(o)
    print("(时间若不对,跑: py board_sync_clock.py)")

def switch(c, ssid, pwd):
    q_s, q_p = shlex.quote(ssid), shlex.quote(pwd)
    print(f"== 删旧配置(如有同名)→ 连 {ssid} …")
    cmd = (
        f"nmcli con delete id {q_s} 2>/dev/null; "
        f"nmcli dev wifi connect {q_s} password {q_p} "
        "&& nmcli con mod $(nmcli -g NAME,DEVICE connection show --active | grep wlan0 | head -1 | cut -d: -f1) "
        "802-11-wireless.powersave 2 ipv4.method auto 2>/dev/null; "
        "sleep 2; "
        "ip -4 addr show wlan0 | grep inet; "
        "ip route | head -2; "
        "curl -s -m 4 http://localhost:8013/status >/dev/null && echo 服务OK || echo 服务检查失败"
    )
    o, e = run(c, cmd, t=90)
    if o:
        print(o)
    if e:
        print("err:", e[:300])
    print("== 记得把新 IP 告诉队友;屏幕/其他板重新烧成同一 WiFi")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssid")
    ap.add_argument("--pass", dest="password")
    ap.add_argument("--psk")             # 只改已存配置的密码,不动当前连接(等 AP 侧换密码后自动重连)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    try:
        c = conn()
    except Exception as ex:
        print(f"连不上板子 USB({HOST}): {type(ex).__name__} —— 检查 USB 数据线插电脑")
        raise SystemExit(1)
    try:
        if a.check:
            check(c)
        elif a.psk:
            o, e = run(c, f"nmcli con mod Mocen_Bear 802-11-wireless-security.psk {shlex.quote(a.psk)}; "
                          "nmcli -s con show Mocen_Bear | grep -E 'psk|interface-name'")
            print(o or e)
            print("(当前连接保持不断;AP 侧改密码踢人后,板子用新 psk 自动重连)")
        elif a.ssid and a.password:
            switch(c, a.ssid, a.password)
        else:
            print("用 --check / --psk / --ssid X --pass Y")
    finally:
        c.close()

if __name__ == "__main__":
    main()