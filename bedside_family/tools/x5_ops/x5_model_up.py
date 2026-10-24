# -*- coding: utf-8 -*-
"""x5_model_up.py — push vosk cn model from laptop, unzip, restart service, verify."""
import sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

HOST, USER, PASS = "10.153.47.251", "root", "root"
LOCAL_ZIP = r"C:\Users\36197\AppData\Local\Temp\opencode\vosk-cn.zip"

CMDS = [
    ("kill wget + clean", "pkill -f 'vosk-model-small-cn' 2>/dev/null; sleep 1; "
                          "rm -f /root/caremate/vosk-cn.zip; echo CLEANED"),
    ("unzip to place",    "cd /root/caremate && unzip -q -o vosk-zip-temp && "
                          "rm -rf vosk-cn && mv vosk-model-small-cn-0.22 vosk-cn && echo UNZIP-OK"),
    ("verify model dir",  "ls /root/caremate/vosk-cn/ | head -6; du -sh /root/caremate/vosk-cn"),
    ("restart service",   "systemctl restart caremate-arm && sleep 1 && systemctl is-active caremate-arm"),
    ("probe voice(no audio)", "curl -s -m 5 -X POST http://127.0.0.1:8013/voice_cmd --data-binary ''; echo"),
    ("probe rps x3",      "for i in 1 2 3; do curl -s -m 8 -X POST http://127.0.0.1:8013/pose/rps_random; echo; done"),
    ("status recent",     "curl -s -m 5 http://127.0.0.1:8013/status | python3 -m json.tool | tail -30"),
]

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15,
            allow_agent=False, look_for_keys=False)

def run(tag, cmd, to=120):
    print("\n--- [%s] $ %s" % (tag, cmd[:90]))
    _in, out, _err = ssh.exec_command(cmd, timeout=to)
    print(out.read().decode("utf-8", errors="replace"), end="")

run("kill wget + clean", CMDS[0][1])

print("\n--- [SFTP] push vosk-cn.zip (42MB) ...")
t0 = time.time()
sftp = ssh.open_sftp()
sftp.put(LOCAL_ZIP, "/root/caremate/vosk-zip-temp")
sftp.close()
print("push done in %.1fs" % (time.time() - t0))

run("unzip to place",   CMDS[1][1], to=180)
run("verify model dir", CMDS[2][1])
run("restart service",  CMDS[3][1], to=60)
time.sleep(1)
run("probe voice(no audio)", CMDS[4][1])
run("probe rps x3",     CMDS[5][1], to=60)
run("status recent",    CMDS[6][1])
ssh.close()
print("\n=== done ===")