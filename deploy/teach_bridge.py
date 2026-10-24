"""Interactive SSH bridge for the existing gravity-assisted teaching tool.

Requires the operator to hold the arm before starting; type point names to
capture, 'where' to inspect, and 'q' to hold and finish. Keep the SSH channel
open while gravity compensation is active.
"""
import os
from pathlib import Path
import sys
import threading
import time
import paramiko

HOST = os.environ.get('CAREMATE_X5_HOST', '192.168.127.10')
ROOT = Path(__file__).resolve().parents[1]
REMOTE = '/home/sunrise/caremate_t5ai/code'


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username='root', password=os.environ['CAREMATE_SSH_PASSWORD'], timeout=8)
    s = c.open_sftp()
    source = REMOTE+'/hand_controller/arm_teach.py'
    s.get(source, str(ROOT/'deploy/arm_teach.before.py'))
    s.put(str(ROOT/'code/hand_controller/arm_teach.py'), source)
    # Keep the current, untaught business points before any operator edits.
    s.get(REMOTE+'/hand_controller/arm_showcase.json', str(ROOT/'deploy/arm_showcase.before-teach.json'))
    _, o, e = c.exec_command('systemctl stop arm_service', timeout=30)
    o.read()
    if o.channel.recv_exit_status(): raise RuntimeError(e.read().decode())
    chan = c.get_transport().open_session()
    chan.get_pty(width=160,height=40)
    chan.exec_command('cd '+REMOTE+' && PANTHERA_SCRIPTS=/home/sunrise/Panthera-HT_SDK/panthera_python/scripts /home/sunrise/panthera_venv/bin/python -u -m hand_controller.arm_teach --effector gripper --file '+REMOTE+'/hand_controller/arm_showcase.json')
    def send():
        for line in sys.stdin:
            chan.sendall(line)
    threading.Thread(target=send,daemon=True).start()
    try:
        while not chan.exit_status_ready():
            if chan.recv_ready():
                sys.stdout.write(chan.recv(16384).decode('utf8','replace')); sys.stdout.flush()
            else: time.sleep(.05)
        while chan.recv_ready(): sys.stdout.write(chan.recv(16384).decode('utf8','replace'))
        return chan.recv_exit_status()
    finally:
        c.close()


if __name__=='__main__':
    raise SystemExit(main())
