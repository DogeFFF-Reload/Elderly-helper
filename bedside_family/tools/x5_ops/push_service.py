# -*- coding: utf-8 -*-
"""push_service.py — paramiko 推 arm_service.py 到 .252 并重启验证"""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

HOST, USER, PW = "10.153.47.252", "root", "root"
LOCAL = r"D:\文档\Default Project\caremate_code\code\hand_controller\arm_service.py"
REMOTE = "/root/caremate/hand_controller/arm_service.py"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, timeout=8)

def run(cmd, label=""):
    _, o, e = c.exec_command(cmd, timeout=25)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    print("## %s\n$ %s\n%s%s" % (label, cmd, out, ("\n[err] " + err) if err else ""))

sftp = c.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.close()
print("## 上传完成")
run("python3 -c \"import sys; sys.path.insert(0,'/root/caremate'); import hand_controller.arm_service; print('remote import OK')\"", "远端语法自检")
run("systemctl restart arm_service && sleep 2 && systemctl is-active arm_service", "重启服务")
run("ss -ltnp | grep 8013", "端口确认")
run("curl -s http://127.0.0.1:8013/messages | head -c 260", "留言列表")
run("journalctl -u arm_service -n 5 --no-pager | tail -5", "服务日志尾部")
c.close()