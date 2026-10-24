"""Deploy the reviewed single-arm setup to the already configured X5.

Credentials come from the existing SSH account via CAREMATE_SSH_PASSWORD.
Creates a recoverable backup before replacing any live source/configuration.
"""
import json
import argparse
import os
from pathlib import Path
import shlex
import time
import paramiko

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("CAREMATE_X5_HOST", "192.168.127.10")
REMOTE = "/home/sunrise/caremate_t5ai"
SDK = "/home/sunrise/Panthera-HT_SDK/panthera_python/scripts"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='Service was deliberately stopped for teaching')
    parser.add_argument('--reset-medicine', action='store_true', help='Archive and invalidate the medicine calibration as requested by the operator')
    args = parser.parse_args()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username="root", password=os.environ["CAREMATE_SSH_PASSWORD"], timeout=8)
    def run(cmd):
        _, out, err = c.exec_command(cmd, timeout=30)
        text, error = out.read().decode(), err.read().decode()
        if out.channel.recv_exit_status():
            raise RuntimeError(text + error)
        return text
    if not args.offline:
        status = json.loads(run("curl -fsS --max-time 5 http://127.0.0.1:8013/status"))
        assert not status['arm']['busy'] and not status['arm']['queued'], 'Arm must be idle'
        assert all(not h['busy'] and not h['queued'] for h in status['hands'].values()), 'Hand must be idle'
    else:
        assert run('systemctl is-active arm_service || true').strip() == 'inactive'
        # The SDK's flock still guards against an undiscovered controller.
    tag = time.strftime("%Y%m%d-%H%M%S")
    backup = "/root/caremate/handover-backups/pre-showcase-" + tag + ".tgz"
    run("tar czf " + shlex.quote(backup) + " /root/caremate/unit.env " + REMOTE +
        "/code/hand_controller " + SDK + "/Panthera_lib/Panthera.py")
    print('BACKUP', backup, flush=True)
    sftp = c.open_sftp()
    # Calibration lives on the actual robot. Preserve it across every deploy.
    config_path = ROOT/'code/hand_controller/arm_showcase.json'
    config = json.loads(config_path.read_text(encoding='utf8'))
    try:
        with sftp.open(REMOTE+'/code/hand_controller/arm_showcase.json') as f:
            previous = json.loads(f.read())
        for name, point in previous['points'].items():
            if args.reset_medicine and name in ('above_tray','pick_med','handover','pre_grasp','orient_clear'):
                continue
            if point.get('taught_at'):
                config['points'][name] = point
        config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    except FileNotFoundError:
        pass
    for name in ['arm_service.py', 'arm_backend.py', 'arm_teach.py', 'arm_safety.py', 'approach.py', 'poses.py', 'arm_showcase.json', 'showcase.html']:
        sftp.put(str(ROOT / 'code/hand_controller' / name), REMOTE + '/code/hand_controller/' + name)
    # Opening an existing /tmp lock with O_CREAT is rejected for root by Linux
    # protected_regular. Reuse that same inode and flock, without deleting the lock.
    patch = r'''from pathlib import Path
p=Path(SDK_FILE)
s=p.read_text()
old="        lock_file = open(self.INSTANCE_LOCK_PATH, 'a+')"
new="        try:\n            lock_file = open(self.INSTANCE_LOCK_PATH, 'r+')\n        except FileNotFoundError:\n            lock_file = open(self.INSTANCE_LOCK_PATH, 'a+')"
if old in s and "open(self.INSTANCE_LOCK_PATH, 'r+')" not in s:
 s=s.replace(old,new,1)
 compile(s,str(p),'exec')
 p.write_text(s)
e=Path('/root/caremate/unit.env')
updates={'ARM_BACKEND':'panthera','ARM_EFFECTOR':'gripper','HAND_COUNT':'1','HAND_LAYOUT':'standalone','PANTHERA_SCRIPTS':SDK_DIR,'ARM_SKILLS_FILE':SHOW_FILE}
lines=e.read_text().splitlines()
lines=[line for line in lines if line.split('=',1)[0] not in updates]
e.write_text('\n'.join(lines+[k+'='+v for k,v in updates.items()])+'\n')
'''
    patch = patch.replace('SDK_FILE', repr(SDK+'/Panthera_lib/Panthera.py')).replace('SDK_DIR', repr(SDK)).replace('SHOW_FILE', repr(REMOTE+'/code/hand_controller/arm_showcase.json'))
    run('/home/sunrise/panthera_venv/bin/python -c ' + shlex.quote(patch))
    run('systemctl restart arm_service')
    print('RESTARTED', flush=True)
    c.close()


if __name__ == '__main__':
    main()
