import os
# -*- coding: utf-8 -*-
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("10.153.47.251", username="root", password=os.environ.get("BOARD_PWD", "root"), timeout=15,
            allow_agent=False, look_for_keys=False)

sftp = ssh.open_sftp()
sftp.put(r"D:\文档\Default Project\caremate_code\code\hand_controller\arm_service.py",
         "/root/caremate/hand_controller/arm_service.py")
sftp.put(r"C:\Users\36197\AppData\Local\Temp\opencode\test_gwdz.wav", "/tmp/t3.wav")
sftp.put(r"C:\Users\36197\AppData\Local\Temp\opencode\test_nwcz.wav", "/tmp/t4.wav")
sftp.close()
print("pushed service + 2 wavs")

_run = ("systemctl restart caremate-arm && sleep 2 && curl -s -m 30 -X POST "
        "http://127.0.0.1:8013/voice_cmd --data-binary @/tmp/t3.wav; echo; sleep 1; "
        "curl -s -m 30 -X POST http://127.0.0.1:8013/voice_cmd --data-binary @/tmp/t4.wav; echo")
_i, out, _e = ssh.exec_command(_run, timeout=120)
print(out.read().decode("utf-8", errors="replace"))
ssh.close()