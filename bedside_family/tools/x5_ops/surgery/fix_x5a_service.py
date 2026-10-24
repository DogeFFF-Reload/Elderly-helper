# -*- coding: utf-8 -*-
"""fix_x5a_service.py — 清 X5#A 服务打架:列清 unit、停旧启用新、真机 env、验证。"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("10.153.47.251", username="root", password="root", timeout=10)

def run(cmd, label, timeout=60):
    _, o, e = c.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print("### " + label)
    print(out if out else ("(stderr) " + err if err else "(empty)"))
    print()
    return out

# 1) 全量列出相关 unit(找昨晚那套的真名)
units = run("ls /etc/systemd/system/ | grep -iE 'arm|hand|care|x5'; "
            "systemctl list-unit-files | grep -iE 'arm|hand|care' || true",
            "所有 arm/hand 相关 unit")
# 2) 看昨晚 unit 的配置(找它的 EnvironmentFile/ExecStart 与自启链)
run("for u in $(systemctl list-unit-files | grep -iE 'arm|hand|care' | awk '{print $1}' | grep -v '^arm_service.service$'); "
    "do echo \"== $u ==\"; grep -E 'ExecStart|EnvironmentFile|Restart=' /etc/systemd/system/$u 2>/dev/null; done",
    "昨晚 unit 配置")
# 3) 停用并禁用所有非 arm_service 的旧 unit(斩防复活根)
old = [l.split()[0] for l in units.splitlines() if ".service" in l and "arm_service.service" not in l]
if old:
    names = " ".join(old)
    run("systemctl disable --now %s 2>&1 | tail -2; echo DISABLED: %s" % (names, names),
        "停用旧 unit: %s" % names)
# 4) env 恢复真机(昨晚这块板就是真手)
ENV = "MOCK_SERIAL=0\nRH56_PROTO=eb90\nHAND1_PORT=/dev/ttyUSB0\nARM_BACKEND=mock\n"
sftp = c.open_sftp()
f = sftp.open("/root/caremate/unit.env", "w")
f.write(ENV)
f.close()
sftp.close()
print("=== unit.env 写回真机模式 ===\n")
# 5) 清残留(带 [] 正戏防自杀)+ 启我的 unit
run("pkill -9 -f 'hand_[c]ontroller.arm_service'; sleep 1; "
    "ps -ef | grep arm_[s]ervice | grep -v grep || echo CLEAN", "清残留进程")
run("systemctl enable --now arm_service 2>&1 | tail -1; sleep 3; systemctl is-active arm_service",
    "启动 agent 版")
run("ss -tlnp | grep 8013 | head -1", "port 8013")
run("curl -s -m 4 http://127.0.0.1:8013/status | head -c 330", "status")
run("journalctl -u arm_service --no-pager | tail -8", "journal")
run("ls /dev/ttyUSB* 2>/dev/null || echo NO_TTYUSB", "CH340 现状")
c.close()
print("=== Done ===")