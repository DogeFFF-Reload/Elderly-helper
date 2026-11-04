# -*- coding: utf-8 -*-
"""x5_deploy_b.py — 全量部署 X5#B(臂+手#1, T5AI 对端) via RNDIS(USB 快速通道).

前置: RNDIS 已通(192.168.128.10 reachable), WiFi 已配好并静态锁定 .251。
步骤:  push hand_controller 源码 + vosk-cn.zip(44MB) + vosk wheel
       -> pip 装 vosk(离线 wheel, 若失败则在线试)
       -> 解压模型到 /root/caremate/vosk-cn
       -> 装 systemd unit + env(MOCK 起步) -> enable + start
       -> 验证 8013
"""
import os, sys, time, glob
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

HOST = "10.153.47.251"
SRC_DIR = r"D:\文档\Default Project\caremate_code\code\hand_controller"
VOSK_ZIP = r"C:\Users\36197\AppData\Local\Temp\opencode\vosk-cn.zip"
REMOTE_BASE = "/root/caremate"
REMOTE_DIR = REMOTE_BASE + "/hand_controller"

# vosk wheel 本地副本(若 pip 在线失败,用这个)
WHEEL_GLOB = r"C:\Users\36197\AppData\Local\Temp\opencode\vosk*.whl"

UNIT = """[Unit]
Description=Mocen CareMate arm service (arm+hand#1, T5AI peer)
After=network.target

[Service]
WorkingDirectory=/root/caremate
ExecStart=/usr/bin/python3 -m hand_controller.arm_service
EnvironmentFile=/root/caremate/unit.env
Restart=always
RestartSec=2
User=root

[Install]
WantedBy=multi-user.target
"""

ENV = """MOCK_SERIAL=1
RH56_PROTO=mock
HAND1_PORT=/dev/ttyUSB0
ARM_BACKEND=mock
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username="root", password=os.environ.get("BOARD_PWD", "root"), timeout=10)
sftp = c.open_sftp()

def run(cmd, label, timeout=60, show=True):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    if show:
        print("### " + label)
        if out:
            print(out)
        elif err:
            print("(stderr) " + err)
        else:
            print("(empty)")
        print()
    return out, err

# 1) 目录
run("mkdir -p /root/caremate/hand_controller", "mkdir")

# 2) 推源码(SFTP, RNDIS 快)
srcs = [f for f in ("__init__.py", "arm_service.py", "poses.py",
                    "rh56f2_driver.py", "rh56f2_eb90.py", "rh56_register_map.py")
        if os.path.exists(os.path.join(SRC_DIR, f))]
print("=== SFTP push 源码 (%d 文件) ===" % len(srcs))
for f in srcs:
    sftp.put(os.path.join(SRC_DIR, f), REMOTE_DIR + "/" + f)
    print("  put %s" % f)

# 3) 推 vosk 模型 zip(44MB, USB 快车道) + vosk wheel(有则推)
print("=== SFTP push vosk-cn.zip (44MB) ...", flush=True)
t0 = time.time()
sftp.put(VOSK_ZIP, REMOTE_BASE + "/vosk-cn.zip")
print("    done in %.1fs" % (time.time() - t0), flush=True)

wheels = glob.glob(WHEEL_GLOB)
if wheels:
    print("=== SFTP push vosk wheel ===")
    for w in wheels:
        sftp.put(w, REMOTE_BASE + "/" + os.path.basename(w))
        print("  put %s" % os.path.basename(w))

# 4) 装 vosk 包:先离线 wheel,失败则在线 pip
out, err = run("cd /root/caremate && (pip3 install --no-index --find-links /root/caremate vosk 2>&1 | tail -3)", "pip vosk (offline wheel)", timeout=180)
if "Successfully" not in out and "already" not in out:
    run("pip3 install vosk 2>&1 | tail -3", "pip vosk (online try)", timeout=300)
run("python3 -c 'import vosk; print(\"vosk module OK\")'", "vosk import", timeout=30)

# 5) 解压模型
run("cd /root/caremate && rm -f vosk-cn.zip && unzip -q -o vosk-zip-temp "
    "&& rm -rf vosk-cn && mv vosk-model-small-cn-0.22 vosk-cn && echo UNZIP-OK",
    "unzip model", timeout=180)
run("du -sh /root/caremate/vosk-cn; ls /root/caremate/vosk-cn | head -5", "model verify", timeout=30)

# 6) 清理 wheel/zip 临时
run("rm -f /root/caremate/vosk-zip*", "clean temp", show=False)

# 7) systemd unit + env
sftp_open = sftp.open("/etc/systemd/system/arm_service.service", "w")
sftp_open.write(UNIT)
sftp_open.close()
sftp_open = sftp.open("/root/caremate/unit.env", "w")
sftp_open.write(ENV)
sftp_open.close()
print("=== unit + env 已写 ===")

# 8) 启动
run("systemctl daemon-reload && systemctl enable arm_service 2>&1 | tail -1 && "
    "systemctl restart arm_service && sleep 3; systemctl is-active arm_service", "enable+start service")
run("ss -tlnp | grep 8013", "port 8013")
run("curl -s -m 4 http://127.0.0.1:8013/status | head -c 300", "local /status")
run("journalctl -u arm_service --since '-1 min' --no-pager 2>/dev/null | tail -8", "journal tail")

sftp.close()
c.close()
print("=== 部署完成 ===")