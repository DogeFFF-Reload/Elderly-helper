# -*- coding: utf-8 -*-
"""set_wifi.py — 场地定网络后一条命令:改屏固件 WiFi 参数 → 编译 → (可选)烧录

用法:
  py set_wifi.py --ssid 场地WiFi名 --pass 密码        # 改参数+编译
  py set_wifi.py --ssid ... --pass ... --burn         # 改+编译+自动找串口烧录
  py set_wifi.py --compile-only                       # 只编译(参数已在正本里)

真密码只进 master 编译正本(不在公开仓库)。
"""
import argparse, os, re, subprocess, sys, time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

INO = r"D:\文档\Default Project\caremate_code\code\bedside_s3_arm_kiosk\bedside_s3_arm_kiosk.ino"
CLI = r"D:\arduino-cli\arduino-cli.exe"
FQBN = "esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app"
BUILD = r"C:\Users\36197\AppData\Local\Temp\opencode\build_v57"

def cstr(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')

def patch(ssid=None, pwd=None):
    src = open(INO, encoding="utf-8").read()
    if ssid:
        src = re.sub(r'(const char\*\s*WIFI_SSID\s*=\s*)"[^"]*"',
                     lambda m: m.group(1) + '"' + cstr(ssid) + '"', src, count=1)
    if pwd:
        src = re.sub(r'(const char\*\s*WIFI_PASS\s*=\s*)"[^"]*"',
                     lambda m: m.group(1) + '"' + cstr(pwd) + '"', src, count=1)
    open(INO, "w", encoding="utf-8").write(src)
    if ssid or pwd:
        print("固件校正区已更新:" + (f"SSID={ssid} " if ssid else "")
              + ("PASS=<已写入,%d位>" % len(pwd) if pwd else ""))

def find_port():
    try:
        from serial.tools import list_ports
    except ImportError:
        print("需要 pyserial: py -m pip install pyserial")
        return None
    cands = []
    for p in list_ports.comports():
        d = (p.description or "") + (p.hwid or "")
        if "CH340" in d or "CH341" in d:
            cands.insert(0, p.device)          # CH340K 优先
        elif "303A" in d.upper():
            cands.append(p.device)             # 原生 USB-CDC
    if not cands:
        print("没找到串口(屏幕 USB 线插电脑了吗?)")
        return None
    return cands[0]

def compile_fw():
    print("== 编译中…")
    r = subprocess.run([CLI, "compile", "--fqbn", FQBN, "--build-path", BUILD,
                        os.path.dirname(INO)], capture_output=True, text=True, errors="replace")
    sys.stdout.write(r.stdout[-600:]); sys.stdout.write(r.stderr[-600:])
    if r.returncode != 0:
        raise SystemExit("编译失败")
    print("== 编译 OK")

def burn(port=None):
    p = port or find_port()
    if not p:
        raise SystemExit(1)
    print("== 烧录 →", p)
    r = subprocess.run([CLI, "upload", "--fqbn", FQBN, "-p", p, "--baud", "921600",
                        "--input-dir", BUILD], capture_output=True, text=True, errors="replace")
    sys.stdout.write(r.stdout[-800:]); sys.stdout.write(r.stderr[-800:])
    if r.returncode != 0:
        print("烧录失败:查数据线;再不行按住屏幕 BOOT 键重插 USB 后重试")
        raise SystemExit(1)
    print("== 烧录完成!接着开串口看 [boot] v5.7 和 [wifi]/[net] 日志")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssid")
    ap.add_argument("--pass", dest="password")
    ap.add_argument("--burn", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--compile-only", action="store_true")
    a = ap.parse_args()

    if a.compile_only:
        compile_fw()
        return

    if a.ssid or a.password:
        patch(a.ssid, a.password)
        compile_fw()
        time.sleep(1)
    elif not os.path.isfile(os.path.join(BUILD, "bedside_s3_arm_kiosk.ino.bin")):
        print("build 目录没有现成固件,先编译一遍:")
        compile_fw()

    if a.burn:
        burn(a.port)
    else:
        print("(编译产物就绪;插上屏幕后加 --burn 开始烧)")

if __name__ == "__main__":
    main()