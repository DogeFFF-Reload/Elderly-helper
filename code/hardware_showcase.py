"""Run a finite, measured exhibition over the public execution API.

python -m hardware_showcase --server http://192.168.127.10:8013
Add --run warmup|arm|hand|gripper with an operator guarding the hardware.
"""
import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server', default='http://192.168.127.10:8013')
    p.add_argument('--run', choices=('warmup','arm','hand','gripper','deliver','return','sos','sign'))
    p.add_argument('--report', default='hardware_acceptance.json')
    p.add_argument('--hand-tolerance', type=float, default=10,
                   help='Allowed percentage-point position error under the 300g force limit')
    a = p.parse_args()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {'started':time.strftime('%Y-%m-%d %H:%M:%S'), 'server':a.server, 'checks':[]}
    def req(path, post=False):
        request = urllib.request.Request(a.server.rstrip('/')+path, data=b'{}' if post else None)
        try:
            with op.open(request, timeout=10) as r: result=json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.load(e).get('error',str(e))) from e
        if not result.get('ok'): raise RuntimeError(str(result))
        return result
    def status(): return req('/status')
    def action(pose, part, timeout=120):
        before=status(); worker=before['arm'] if part=='arm' else before['hands']['1']
        if worker['busy'] or worker['queued']: raise RuntimeError(part+' is busy')
        record={'pose':pose,'before':before,'samples':[]}; report['checks'].append(record)
        record['accepted']=req('/pose?name='+pose+'&hand=1',True)
        print('START',pose,flush=True)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            after=status(); now=after['arm'] if part=='arm' else after['hands']['1']
            sample={'elapsed':round(timeout-(deadline-time.monotonic()),2),'state':now}
            record['samples'].append(sample)
            if now['fail_seq']>worker['fail_seq']: raise RuntimeError(now['last_error'])
            if part=='arm' and now['aborted']>worker['aborted']: raise RuntimeError('Arm action interrupted')
            if not now['busy'] and not now['queued'] and now['done']>worker['done']:
                record['completed']=True
                if part=='hand':
                    # Worker completion means command sent. Verify actual angles separately.
                    settle=time.monotonic()+6
                    while True:
                        measured=req('/hand_positions?hand=1')['angles']
                        target=now['last']
                        errors=[abs(measured[j]-target[j]) for j in target if measured.get(j) is not None]
                        record['measured']=measured
                        record['max_error_pct']=round(max(errors),2) if len(errors)==6 else None
                        if len(errors)==6 and max(errors)<=a.hand_tolerance: break
                        if time.monotonic()>settle:
                            raise RuntimeError('Hand '+pose+' position error '+str(record['max_error_pct'])+'%; measured='+str(measured))
                        time.sleep(.25)
                print('PASS',pose, 'angles='+str(now.get('joints_deg') or record.get('measured')),flush=True)
                return
            time.sleep(.35)
        raise TimeoutError(pose)
    code=0
    try:
        report['initial']=status()
        print(json.dumps({'hardware':report['initial'].get('hardware'),'arm':report['initial']['arm']['backend'],
                          'effector':report['initial']['arm']['effector']},ensure_ascii=False),flush=True)
        if a.run:
            assert report['initial']['arm']['effector']=='gripper', 'Requires separate arm gripper'
            if a.run=='warmup': action('arm_warmup','arm')
            if a.run=='arm': action('arm_showcase','arm')
            if a.run=='gripper': action('gripper_cycle','arm')
            for option, pose in [('deliver','deliver_medicine'),('return','return_medicine'),
                                 ('sos','press_sos'),('sign','sign_family_received')]:
                if a.run == option:
                    action(pose, 'arm', 180)
            if a.run=='hand':
                for pose in ['release','wave','thumbs_up','ok','rps_rock','rps_scissors','rps_paper','count_five','hand_exercise','clench_open','sign_coming','release']:
                    action(pose,'hand',30)
                    time.sleep(.8)
        report['final']=status()
    except (Exception, KeyboardInterrupt) as e:
        code=1; report['error']=str(e)
        if a.run:
            try: report['stop']=req('/stop',True)
            except Exception as stop_error: report['stop_error']=str(stop_error)
        print('STOPPED:',e,flush=True)
    finally:
        Path(a.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print('REPORT',a.report,flush=True)
    return code


if __name__=='__main__':
    raise SystemExit(main())
