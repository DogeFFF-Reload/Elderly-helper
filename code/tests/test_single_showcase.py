"""Exercise the real exhibition topology with mock drivers only."""
import os
import copy
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from hand_controller import arm_safety as safety, arm_service as service
from hand_controller.arm_teach import teaching_guard
from hand_controller.arm_backend import MockArm, PantheraArm, ArmError
from hand_controller.poses import JOINTS, SEQUENCES
from hand_controller.rh56f2_driver import RH56F2

S_deg = safety.deg
SHOW = os.path.join(os.path.dirname(service.__file__), "arm_showcase.json")


class ShowcaseTest(unittest.TestCase):
    def test_approach_keeps_gripper_orientation_through_descent(self):
        from hand_controller.approach import generate_approach
        from hand_controller.arm_kinematics import frames, tool_position
        cfg=safety.load_config('gripper',SHOW)
        cfg['points']['pick_med']['taught']=True
        plan=generate_approach(cfg)
        pick,pre=cfg['points']['pick_med']['q'],plan['pre_grasp']
        self.assertGreaterEqual(tool_position(pre)[2]-tool_position(pick)[2],.039)
        wanted=frames(pick)[-1][1]
        for k in range(21):
            q=[a+(b-a)*k/20 for a,b in zip(pre,pick)]
            actual=frames(q)[-1][1]
            self.assertLess(max(abs(actual[i][j]-wanted[i][j]) for i in range(3) for j in range(3)),1e-6)
            self.assertAlmostEqual(q[5],pick[5])

    def test_rotating_gripper_on_final_descent_is_rejected(self):
        cfg=safety.load_config('gripper',SHOW)
        cfg['points']['pre_grasp']['q'][5]+=.1
        with self.assertRaises(safety.SafetyError):
            safety.check_skill('deliver_medicine',[0.]*6,cfg)

    def test_capture_preserves_servo_command_and_measured_sag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(has_gripper=True, q=[0,.35,.35,0,0,0])
            backend.last_command = [0,.37,.39,0,0,0]
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            with patch.object(service, 'TEACH_MODE', True):
                code, result = worker.teach('capture', point='handover')
            self.assertEqual(code, 200)
            self.assertEqual(result['saved']['q'], backend.last_command)
            self.assertEqual(result['saved']['measured_q'], backend.q)

    def test_empty_gripper_cannot_be_reported_as_holding(self):
        for value in (0, .003, 1.6, None, float('nan')):
            with self.assertRaises(ArmError):
                service.verify_gripper({'gripper_position': value})
        result = service.verify_gripper({'gripper_position': .4})
        self.assertTrue(result['holding'])
        self.assertEqual(result['method'], 'position_stall_heuristic')

    def test_empty_pickup_stops_before_lifting_or_handover(self):
        import json
        class EmptyGripper(MockArm):
            mock = False
            def status(self):
                return dict(super().status(), gripper_position=0.)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            with open(SHOW, encoding='utf8') as f:
                cfg = json.load(f)
            for point in cfg['points'].values():
                if isinstance(point, dict): point['taught'] = True
            with open(path, 'w', encoding='utf8') as f:
                json.dump(cfg, f)
            backend = EmptyGripper(has_gripper=True)
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            self.assertEqual(worker.request('deliver_medicine')[0], 200)
            self.assertTrue(worker.wait_idle(3))
            self.assertEqual(worker.fail_seq, 1)
            self.assertEqual(worker.done, 0)
            self.assertEqual(len(backend.moves), 3, 'must not lift after detecting an empty gripper')
            self.assertEqual(backend.holds, 1)

    def test_jog_is_bounded_and_capture_preserves_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            worker = service.ArmWorker(MockArm(has_gripper=True), effector='gripper', skills_path=path)
            self.assertEqual(worker.teach('jog', joint=1, delta=1)[0], 423)
            with patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('jog', joint=1, delta=31)[0], 400)
                self.assertEqual(worker.teach('jog', joint=1, delta=5)[0], 200)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.teach('capture', point='rest')[0], 400)
                code, result = worker.teach('capture', point='handover')
                self.assertEqual(code, 200)
                self.assertTrue(result['saved']['taught'])
                self.assertTrue(safety.load_config('gripper', path)['points']['handover']['taught'])
                self.assertIsNone(worker.at)
                worker.backend.q[0] = 2.09
                self.assertEqual(worker.teach('jog', joint=1, delta=5)[0], 422)

    def test_custom_speed_is_capped_by_safety_config(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(has_gripper=True, q=[0, .35, .35, 0, 0, 0])
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            with patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('jog', joint=1, delta=20, speed=2)[0], 200)
                self.assertTrue(worker.wait_idle(3))
                self.assertAlmostEqual(backend.moves[-1][1], math.radians(20) / math.radians(2), places=3)
                self.assertEqual(worker.teach('joint', joint=1, target=60, speed=500)[0], 200)   # 再走 40°
                self.assertTrue(worker.wait_idle(3))
                vmax = worker.max_vel
                self.assertAlmostEqual(backend.moves[-1][1], math.radians(40) / vmax, places=3)
                self.assertEqual(worker.teach('vertical', delta=-.11)[0], 400)
                self.assertEqual(worker.teach('jog', joint=1, delta=1, speed=-3)[0], 400)

    def test_slider_moves_one_joint_to_absolute_target(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(has_gripper=True, q=[0, .35, .35, 0, 0, 0])
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            with patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('joint', joint=1, target=60)[0], 200)   # beyond the 30° jog cap
                self.assertTrue(worker.wait_idle(3))
                self.assertAlmostEqual(backend.q[0], math.radians(60), places=4)
                self.assertEqual(backend.q[1:], [.35, .35, 0, 0, 0])
                self.assertTrue(worker.teach('joint', joint=1, target=60)[1].get('unchanged'))
                self.assertEqual(worker.teach('joint', joint=1, target=121)[0], 422)
                self.assertEqual(worker.teach('joint', joint=4, target=90)[0], 422)
                self.assertEqual(worker.teach('joint', joint=7, target=0)[0], 400)
                self.assertEqual(worker.teach('joint', joint=1)[0], 400)
            self.assertEqual(worker.status()['teach_limits']['joints'][0], [-120, 120])

    def test_recording_merges_jogs_and_replays_in_order(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(has_gripper=True, q=[0, .35, .35, 0, 0, 0])
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            self.assertEqual(worker.recordings_path, os.path.join(tmp, 'arm_recordings.json'))
            with patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('rec_save', name='x')[0], 409)
                self.assertEqual(worker.teach('rec_start')[0], 200)
                self.assertEqual(worker.teach('rec_save', name='x')[0], 422)
                for args in ({'joint': 1, 'delta': 5}, {'joint': 1, 'delta': 5}, {'joint': 2, 'delta': 3},
                             {'joint': 1, 'delta': -5}):
                    self.assertEqual(worker.teach('jog', speed=4, **args)[0], 200)
                    self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.teach('grip', close=True)[0], 200)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.status()['recording'], 4)
                code, body = worker.teach('rec_save', name='演示')
                self.assertEqual((code, body['steps']), (200, 5))
                self.assertIsNone(worker.recording)
            steps = worker.recordings()['演示']['steps']
            self.assertEqual([s.get('via') or s['grip'] for s in steps], ['start', 'jog', 'jog', 'jog', 'close'])
            self.assertAlmostEqual(steps[1]['move'][0], math.radians(10), places=3)
            end = list(backend.last_command)
            # replay works outside teach mode, from somewhere else, and ends where the recording ended
            backend.q = [0, .3, .3, 0, 0, 0]
            backend.last_command = list(backend.q)
            backend.moves.clear(); backend.grips.clear()
            code, body = worker.teach('replay', name='演示', scale=2)
            self.assertEqual(code, 200, body)
            self.assertTrue(worker.wait_idle(3))
            self.assertEqual(worker.done, 6)
            self.assertEqual(len(backend.moves), 4)
            self.assertEqual([round(x, 4) for x in backend.moves[-1][0]], [round(x, 4) for x in end])
            self.assertEqual(backend.grips, [True])
            self.assertEqual(worker.teach('replay', name='没有')[0], 404)
            self.assertEqual(worker.teach('replay', name='演示', scale=5)[0], 422)
            # a hand-edited recording that leaves the safe envelope is refused before moving
            data = worker.recordings()
            data['bad'] = {'steps': [{'move': [math.radians(150), .35, .35, 0, 0, 0], 'vel': .1}]}
            worker._save_recordings(data)
            self.assertEqual(worker.teach('replay', name='bad')[0], 422)
            self.assertEqual(worker.teach('rec_delete', name='bad')[0], 200)
            self.assertNotIn('bad', worker.recordings())

    def test_t5ai_key_replays_bound_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(has_gripper=True, q=[0, .35, .35, 0, 0, 0])
            worker = service.ArmWorker(backend, effector='gripper', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'TEACH_MODE', True):
                worker.teach('rec_start')
                worker.teach('jog', joint=1, delta=10)
                self.assertTrue(worker.wait_idle(3))
                worker.teach('grip', close=True)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.teach('rec_save', name='递药演示')[0], 200)
            self.assertEqual(worker.bind('press_sos', '递药演示')[0], 400)     # SOS 不开放绑定
            self.assertEqual(worker.bind('deliver_medicine', '没有')[0], 404)
            self.assertEqual(worker.bind('deliver_medicine', '递药演示')[0], 200)
            self.assertEqual(worker.bindings(), {'deliver_medicine': '递药演示'})
            backend.q, backend.last_command = [0, .35, .35, 0, 0, 0], None
            backend.moves.clear(); backend.grips.clear()
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'arm', return_value=worker):
                with patch.object(service, 'TEACH_MODE', True):   # 示教时大屏按键不动作
                    self.assertEqual(service.dispatch('deliver_medicine', '1', alias_of='/handover_med')[0], 423)
                code, body = service.dispatch('deliver_medicine', '1', alias_of='/handover_med')
                self.assertEqual((code, body.get('replay')), (200, '递药演示'), body)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.current, 'teach_replay')
                self.assertEqual(backend.grips, [True])
                self.assertEqual(service.HISTORY[-1].get('replay'), '递药演示')
                # 删掉录制 → 绑定自动解除，按键恢复原业务动作（未示教的真机会被拒，mock 照常受理）
                with patch.object(service, 'TEACH_MODE', True):
                    self.assertEqual(worker.teach('rec_delete', name='递药演示')[1]['unbound'], ['deliver_medicine'])
                self.assertEqual(worker.bindings(), {})
                code, body = service.dispatch('deliver_medicine', '1', alias_of='/handover_med')
                self.assertNotIn('replay', body)

    def test_pose_library_script_with_mounted_hand(self):
        import math
        from hand_controller.arm_kinematics import tool_position
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, .6, .6, -.3, 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('grip', close=True)[0], 422)          # 没有夹爪了
                self.assertEqual(worker.teach('hand', point='grasp')[0], 200)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(hand.driver.last['index'], 15)
                self.assertEqual(worker.teach('pose_save', name='待机')[0], 200)
                worker.teach('joint', joint=1, target=30); self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.teach('pose_save', name='取物点')[0], 200)
                code, body = worker.teach('pose_above', name='取物点', delta=.05)
                self.assertEqual(code, 200, body)
                up = body['pose_saved']
                self.assertEqual(up, '取物点·上方5cm')
                lib = worker.library()
                dz = tool_position(lib['poses'][up]['q'], .22)[2] - tool_position(lib['poses']['取物点']['q'], .22)[2]
                self.assertAlmostEqual(dz, .05, places=3)
                worker.teach('joint', joint=1, target=-30); self.assertTrue(worker.wait_idle(3))
                self.assertEqual(worker.teach('pose_save', name='递给老人')[0], 200)
                self.assertEqual(worker.teach('pose_save', name='')[0], 422)
                steps = [{'hand': 'release'}, {'pose': '待机'}, {'pose': up}, {'pose': '取物点', 'speed': 4},
                         {'hand': 'grasp'}, {'wait': .5}, {'pose': up, 'speed': 4}, {'pose': '递给老人'},
                         {'wait': 2}, {'pose': '待机'}, {'hand': 'relax'}]
                with self.assertRaises(ValueError):
                    worker.save_script('坏', [{'hand': 'fly'}])
                name, missing = worker.save_script('递物', steps + [{'pose': '还没示教'}])
                self.assertEqual(missing, ['还没示教'])
                self.assertEqual(worker.teach('replay', name='递物')[0], 422)        # 引用了没示教的姿态
                worker.save_script('递物', steps)
                lib = worker.library()
                backend.moves.clear()
                code, body = worker.teach('replay', name='递物', start=3)            # 从第 4 步开始
                self.assertEqual(code, 200, body)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(len(backend.moves), 4)
                self.assertEqual([round(x, 4) for x in backend.moves[-1][0]], lib['poses']['待机']['q'])
                self.assertAlmostEqual(backend.moves[0][1], max(.6, max(abs(a - b) for a, b in zip(
                    lib['poses']['递给老人']['q'], lib['poses']['取物点']['q'])) / math.radians(4)), places=2)
                pts = worker.cfg['points']
                self.assertTrue(pts['_rec_4'].get('settle'))     # 抓取点后面是手部动作：停稳
                self.assertTrue(pts['_rec_7'].get('blend'))       # 抬起后直接去递送点：连贯通过
                self.assertTrue(pts['_rec_8'].get('settle'))      # 递送点后面是停留
                self.assertEqual(hand.driver.last['thumb_rot'], JOINTS['home']['thumb_rot'])
                # 重新示教一个姿态，脚本跟着变；删姿态会报出还在引用它的脚本
                code, body = worker.delete_pose('递给老人')
                self.assertEqual(body['used_by'], ['递物'])
                # 实时录制里的手部动作和姿态前往也按名字记
                worker.teach('rec_start')
                worker.teach('pose_go', name='待机', speed=5); self.assertTrue(worker.wait_idle(3))
                worker.teach('hand', point='pinch'); self.assertTrue(worker.wait_idle(3))
                nogap = lambda st: {k: v for k, v in st.items() if k != 'gap'}
                self.assertEqual([nogap(st) for st in worker.recording[1:]], [{'pose': '待机', 'speed': 5.0}, {'hand': 'pinch'}])

    def test_park_lifts_first_when_hand_is_on_the_table(self):
        import json
        import math
        from hand_controller.arm_kinematics import body_points
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(SHOW, encoding='utf8') as f:
            raw = json.load(f)
        raw['floor_z'] = .03            # 现场已去掉桌面检查；这里打开它，测"先抬起"的逻辑
        table = os.path.join(tmp, 'skills.json')
        with open(table, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        cfg = safety.load_config('hand1', table)
        rest = cfg['points']['rest']['q']
        # 2026-10-24 现场：手朝下贴着桌面，模型里末端已低于桌面；直走会被 check_segment 拒绝、臂转阻尼下沉
        q = [math.radians(v) for v in (19.3, 83.9, 33.2, -34.5, -20.1, 91.0)]
        with self.assertRaises(safety.SafetyError):
            safety.check_segment(q, rest, cfg)
        route = service.park_route(q, rest, cfg)
        self.assertTrue(route[0][0].startswith('垂直抬起'), route)
        self.assertEqual(route[-1][1], rest)
        lifted = route[0][1]
        z0, z1 = (body_points(x, cfg['tool_length'])['tool'][2] for x in (q, lifted))
        self.assertGreater(z1, z0)
        for a, b in zip([q] + [w for _, w in route], [w for _, w in route]):
            if a is q:
                safety.check_escape(a, b, cfg)
            else:
                safety.check_segment(a, b, cfg)
        # 真正走一遍：MockArm 按路线逐段走到 rest
        backend = MockArm(q=list(q))
        worker = service.ArmWorker(backend, effector='hand1', skills_path=table)
        with patch.object(service, 'hands', return_value={1: service.HandWorker(1, RH56F2("", 1, mock=True))}):
            self.assertTrue(service.park(worker, out=lambda s: None))
        self.assertEqual([m[0] for m in backend.moves], [w for _, w in route])
        self.assertEqual(backend.q, rest)

    def test_escape_never_digs_deeper(self):
        import math
        cfg = dict(safety.load_config('hand1', SHOW), floor_z=.03)
        q = [math.radians(v) for v in (19.3, 83.9, 33.2, -34.5, -20.1, 91.0)]
        lower = list(q); lower[1] += math.radians(10)       # 肩再往前压：手更低
        with self.assertRaises(safety.SafetyError):
            safety.check_escape(q, lower, cfg)
        # 起点合规时不走脱困规则：路上碰桌面照样不行
        ok = [0, math.radians(40), math.radians(60), 0, 0, 0]
        cfg2 = dict(cfg, keep_out=[{'name': '挡板', 'min': [-1, -1, -1], 'max': [1, 1, 2]}])
        with self.assertRaises(safety.SafetyError):
            safety.plan_route(ok, cfg['points']['rest']['q'], cfg2)

    def test_follow_mode_moves_several_joints_while_dragging(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, .35, .35, 0, 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            with patch.object(service, 'SERVO_IDLE', .3), patch.object(service, 'TEACH_MODE', True), \
                    patch.object(service, 'hands', return_value={1: service.HandWorker(1, RH56F2("", 1, mock=True))}):
                worker.teach('rec_start')
                code, body = worker.teach('servo', targets={1: 10}, speed=20)
                self.assertEqual((code, body['servo']), (200, 'start'), body)
                for d in (15, 20, 25):                  # 拖动中不断更新目标，不会 409
                    time.sleep(.06)
                    code, body = worker.teach('servo', targets={1: d}, speed=20)
                    self.assertEqual((code, body['servo']), (200, 'update'), body)
                code, body = worker.teach('servo', targets={2: 30, 3: 40}, speed=20)   # 另两个关节一起走
                self.assertEqual(code, 200, body)
                self.assertEqual(worker.teach('jog', joint=1, delta=1)[0], 409)        # 跟随中别的示教要等
                self.assertEqual(worker.teach('servo', targets={1: 130})[0], 422)      # 底座 ±120°
                self.assertEqual(worker.teach('servo', targets={4: 200})[0], 422)      # 关节限位
                self.assertTrue(worker.wait_idle(5))
                self.assertEqual(worker.current, 'teach_servo')
                self.assertEqual(S_deg(backend.q), [25.0, 30.0, 40.0, 0.0, 0.0, 0.0])
                self.assertGreaterEqual(len(backend.moves), 3)
                vel = math.radians(20)
                self.assertTrue(all(m[1] >= service.SERVO_MIN_SECONDS for m in backend.moves))
                self.assertEqual(worker.recording[-1]['via'], 'drag')
                self.assertEqual(S_deg(worker.recording[-1]['move']), [25.0, 30.0, 40.0, 0.0, 0.0, 0.0])
                self.assertAlmostEqual(worker.recording[-1]['vel'], round(vel, 4))
                # 跟随结束后再拖：开一个新的跟随
                self.assertEqual(worker.teach('servo', targets={1: 0})[1]['servo'], 'start')
                self.assertTrue(worker.wait_idle(5))
                # 急停打断跟随：停住保持
                worker.teach('servo', targets={1: 30})
                time.sleep(.1)
                self.assertEqual(worker.running, 'teach_servo')
                worker.stop()
                self.assertTrue(worker.wait_idle(3))
                self.assertGreaterEqual(backend.holds, 1)

    def test_teaching_speed_cap_is_the_sdk_velocity_limit(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            worker = service.ArmWorker(MockArm(has_gripper=True), effector='gripper', skills_path=path)
            worker._setup()
            self.assertEqual(worker.status()['teach_limits']['max_speed'], 57.3)   # Follower.yaml velocity_limits 1.0 rad/s
            self.assertAlmostEqual(worker._teach_vel(45), math.radians(45))
            self.assertAlmostEqual(worker._teach_vel(200), 1.0)
            # 真臂按 SDK 报的限幅；没报（没连上）退回 30°/s
            arm = PantheraArm()
            self.assertIsNone(arm.vel_limit)
            arm.robot = SimpleNamespace(velocity_limits=[1.0, .8, 1.0, 1.0, 1.0, 1.0])
            self.assertEqual(arm.vel_limit, .8)
            w2 = service.ArmWorker(PantheraArm(), effector='gripper', skills_path=path)
            self.assertAlmostEqual(w2.max_vel, service.TEACH_MAX_VEL)
            self.assertLessEqual(worker.cfg['speed']['max_joint_vel'], .22)          # 业务动作不受影响

    def test_presets_keep_base_and_return_home_at_every_base_angle(self):
        import math
        from hand_controller.arm_presets import PRESETS
        R = math.radians
        starts = {'rest': [0, 0, 0, 0, 0, 0], 'chest': [0, R(30), R(60), R(-30), 0, 0],
                  'raised': [0, R(60), R(90), R(-20), R(10), R(45)]}
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}),                 patch.object(service.HandWorker, 'pause', lambda self, seconds: False):   # 手势不真等
            for j1 in range(-120, 121, 15):
                for sname, s in starts.items():
                    q0 = [R(j1)] + s[1:]
                    backend = MockArm(q=list(q0))
                    worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
                    worker._setup()
                    for key, preset in PRESETS.items():
                        backend.q, backend.last_command = list(q0), list(q0)
                        backend.moves.clear()
                        code, body = worker.teach('preset', name=key)
                        self.assertEqual(code, 200, (key, j1, sname, body))
                        self.assertTrue(worker.wait_idle(5))
                        self.assertEqual(worker.done and worker.last_error, None, (key, j1, sname, worker.last_error))
                        for target, *_ in backend.moves:
                            self.assertAlmostEqual(target[0], q0[0], places=6, msg=(key, j1))   # 底座不动
                            self.assertAlmostEqual(target[4], q0[4], places=6, msg=(key, j1))   # 腕部左右不动
                        self.assertEqual([round(x, 6) for x in backend.q], [round(x, 6) for x in q0], (key, sname))
            self.assertEqual(worker.fail_seq, 0)

    def test_preset_speed_scale(self):
        import math
        backend = MockArm(q=[0, math.radians(30), math.radians(60), math.radians(-30), 0, 0])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        worker._setup()
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}):
            slow = worker._build_replay([{'preset': 'handshake'}], 1, first_cap=False)
            fast = worker._build_replay([{'preset': 'handshake'}], 2, first_cap=False)
            waits = lambda sk: [a for k, a in sk if k == 'wait']
            self.assertIn(3.0, waits(slow))
            self.assertIn(1.5, waits(fast))                        # 停顿跟着倍速缩短
            worker._build_replay([{'preset': 'wave'}], 3, first_cap=False)
            vels = [p['vel'] for k, p in worker.cfg['points'].items() if k.startswith('_rec_')]
            self.assertLessEqual(max(vels), worker.max_vel + 1e-9)  # 3× 也不超过 SDK 速度限幅
            self.assertAlmostEqual(max(vels), worker.max_vel)       # 30°/s × 3 = 90°/s → 夹到 57.3°/s
            first = worker.cfg['points']['_rec_1_1']['vel']
            self.assertGreater(first, worker.cfg['speed']['near_person_vel'])   # 按钮做预设：第一段不按位置未知限速
            capped = worker._build_replay([{'preset': 'wave'}], 3)              # 脚本里：第一段照旧限速
            self.assertAlmostEqual(worker.cfg['points']['_rec_1_1']['vel'], worker.cfg['speed']['near_person_vel'])
            with self.assertRaises(safety.SafetyError):
                worker._build_replay([{'preset': 'wave'}], 3.5)
            backend.last_command = list(backend.q)
            self.assertEqual(worker.teach('preset', name='beckon', scale=2.5)[0], 200)
            self.assertTrue(worker.wait_idle(5))
            self.assertIsNone(worker.last_error)

    def test_preset_inside_script_and_gestures_run_with_arm(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, math.radians(30), math.radians(60), math.radians(-30), 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True),                     patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
                worker.teach('joint', joint=1, target=40); self.assertTrue(worker.wait_idle(3))
                worker.teach('pose_save', name='面向老人')
                with self.assertRaises(ValueError):
                    worker.save_script('坏', [{'preset': 'fly'}])
                worker.save_script('迎宾', [{'pose': '面向老人'}, {'preset': 'wave'}, {'wait': 1}, {'preset': 'handshake'}])
                backend.moves.clear()
                self.assertEqual(worker.teach('replay', name='迎宾')[0], 200)
                self.assertTrue(worker.wait_idle(5))
                self.assertIsNone(worker.last_error)
                self.assertTrue(all(abs(m[0][0] - math.radians(40)) < 1e-3 for m in backend.moves))   # 姿态存 4 位小数
                self.assertEqual(hand.current, 'wave')      # 挥手手势在臂摆腕时同时做了
                pts = worker.cfg['points']
                self.assertTrue(pts['_rec_2_1'].get('blend'))   # 举手后接着摆腕：手势同时做，不停顿
                # 夹爪末端不能做需要手的预设
                g = service.ArmWorker(MockArm(has_gripper=True), effector='gripper', skills_path=path)
                self.assertEqual(g.teach('preset', name='wave')[0], 422)
                self.assertEqual(g.teach('preset', name='nope')[0], 404)
            self.assertIn('wave', worker.library()['presets'])

    def test_teaching_stops_before_motor_layer_base_trip(self):
        import math
        teaching_guard([math.radians(119), 0, 0, 0, 0, 0])
        for angle in (120, 149, -120, -149):
            with self.assertRaises(safety.SafetyError):
                teaching_guard([math.radians(angle), 0, 0, 0, 0, 0])

    def test_gripper_reports_measured_position_and_torque(self):
        backend = PantheraArm()
        backend.robot = SimpleNamespace(has_gripper=True,
            get_current_pos_gripper=lambda: 1.57, get_current_torque_gripper=lambda: .02)
        st = backend.status()
        self.assertEqual(st['gripper_position'], 1.57)
        self.assertEqual(st['gripper_torque'], .02)

    def test_drag_pose_lock_and_derived_above_pose(self):
        import math
        from hand_controller.arm_kinematics import tool_position
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, .6, .9, -.3, 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            with patch.object(service, 'TEACH_MODE', True):
                worker.teach('pose_save', name='取物点')
                code, body = worker.teach('pose_above', name='取物点', delta=.06)
                self.assertEqual(code, 200, body)
            up = body['pose_saved']
            self.assertEqual(worker.poses()[up]['derived'], {'from': '取物点', 'dz': .06})
            # 3D 里把取物点往前拖：上方点跟着走，仍在它正上方 6 cm；真臂不动
            q = safety.deg(worker.poses()['取物点']['q'])
            q[1] += 5; q[2] += 3
            code, body = worker.set_pose('取物点', q)
            self.assertEqual((code, body['followers']), (200, [up]), body)
            self.assertEqual(backend.moves, [])
            p, u = (worker.poses()[n]['q'] for n in ('取物点', up))
            self.assertAlmostEqual(tool_position(u, .22)[2] - tool_position(p, .22)[2], .06, places=3)
            # 锁住上方点：不再跟着走，也不能拖、不能覆盖、不能删
            self.assertEqual(worker.lock_pose(up, True)[0], 200)
            before = worker.poses()[up]['q']
            q[1] += 3
            self.assertEqual(worker.set_pose('取物点', q)[1]['followers'], [])
            self.assertEqual(worker.poses()[up]['q'], before)
            self.assertEqual(worker.set_pose(up, q)[0], 422)
            with patch.object(service, 'TEACH_MODE', True):
                self.assertEqual(worker.teach('pose_save', name=up)[0], 422)
            self.assertEqual(worker.delete_pose(up)[0], 423)
            self.assertEqual(worker.lock_pose(up, False)[0], 200)
            self.assertEqual(worker.delete_pose(up)[0], 200)
            # 越界的拖动被拒绝
            bad = list(q); bad[3] = 120
            self.assertEqual(worker.set_pose('取物点', bad)[0], 422)
            self.assertEqual(worker.set_pose('没有', q)[0], 404)

    def test_pose_routes_and_staged_fist(self):
        import math
        from hand_controller.arm_kinematics import tool_position
        R = math.radians
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, R(30), R(60), R(-30), 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True):
                # 握紧2：分段握拳，最后拇指压上来
                self.assertEqual(worker.teach('hand', point='fist_wrap')[0], 200)
                self.assertTrue(worker.wait_idle(5))
                self.assertEqual((hand.driver.last['index'], hand.driver.last['thumb'], hand.driver.last['thumb_rot']), (3, 20, 75))
                self.assertIn('fist_wrap', worker.library()['hand_presets'])
                worker.teach('pose_save', name='待机')
                worker.teach('joint', joint=1, target=40); self.assertTrue(worker.wait_idle(3))
                worker.teach('joint', joint=2, target=50); self.assertTrue(worker.wait_idle(3))
                worker.teach('pose_save', name='取物点')
                worker.teach('pose_go', name='待机'); self.assertTrue(worker.wait_idle(3))
                home, pick = (worker.poses()[n]['q'] for n in ('待机', '取物点'))
                # 先抬起：原地上 6 cm → 目标正上方 → 落下
                self.assertEqual(worker.set_route('取物点', 'lift', 6)[0], 200)
                backend.moves.clear()
                self.assertEqual(worker.teach('pose_go', name='取物点')[0], 200)
                self.assertTrue(worker.wait_idle(3))
                m = [x[0] for x in backend.moves]
                self.assertEqual(len(m), 3)
                z = lambda q: tool_position(q, .22)[2]
                self.assertAlmostEqual(z(m[0]) - z(home), .06, places=3)
                self.assertAlmostEqual(z(m[1]) - z(pick), .06, places=3)
                self.assertEqual([round(x, 4) for x in m[2]], pick)
                # 经过「待机」
                self.assertEqual(worker.set_route('取物点', 'via', via=['待机'])[0], 200)
                backend.moves.clear()
                worker.teach('pose_go', name='取物点'); self.assertTrue(worker.wait_idle(3))
                self.assertEqual([[round(x, 4) for x in mv[0]] for mv in backend.moves], [home, pick])
                # 脚本里的「移动到姿态」也按路径走
                worker.set_route('取物点', 'lift', 4)
                worker.save_script('取', [{'pose': '待机'}, {'pose': '取物点', 'speed': 10}])
                backend.moves.clear()
                self.assertEqual(worker.teach('replay', name='取')[0], 200)
                self.assertTrue(worker.wait_idle(3))
                self.assertEqual(len(backend.moves), 4)       # 待机 + 抬起 + 正上方 + 落下
                self.assertIn('_rec_2', worker.cfg['points'])  # 最后一段仍按步号命名，页面能高亮
                # 录制里记的是姿态名，回放时按路径重算
                worker.teach('rec_start')
                worker.teach('pose_go', name='待机'); self.assertTrue(worker.wait_idle(3))
                self.assertEqual({k: v for k, v in worker.recording[-1].items() if k != 'gap'}, {'pose': '待机'})
                # 错误输入
                self.assertEqual(worker.set_route('取物点', 'via', via=['取物点'])[0], 400)
                self.assertEqual(worker.set_route('取物点', 'via', via=['没有'])[0], 400)
                self.assertEqual(worker.set_route('取物点', 'lift', 20)[0], 400)
                self.assertEqual(worker.set_route('取物点', 'fly')[0], 400)
                self.assertEqual(worker.set_route('取物点', 'direct')[0], 200)
                self.assertNotIn('route', worker.poses()['取物点'])

    def test_record_my_actions_into_script(self):
        import math
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[0, math.radians(30), math.radians(60), math.radians(-30), 0, 0])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True), \
                    patch.object(service, 'SERVO_IDLE', .2), \
                    patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
                worker.teach('pose_save', name='待机')
                worker.save_script('别的脚本', [{'wait': .1}])
                self.assertEqual(worker.teach('rec_take')[0], 409)
                self.assertEqual(worker.teach('rec_start')[0], 200)
                worker.teach('jog', joint=1, delta=10); self.assertTrue(worker.wait_idle(3))
                time.sleep(1.0)                                      # 停一下：应记成 gap
                self.assertEqual(worker.teach('preset', name='nod')[0], 200); self.assertTrue(worker.wait_idle(5))
                worker.teach('hand', point='fist_wrap'); self.assertTrue(worker.wait_idle(5))
                worker.teach('pose_go', name='待机', speed=12); self.assertTrue(worker.wait_idle(3))
                worker.teach('servo', targets={2: 40}); self.assertTrue(worker.wait_idle(5))
                worker.teach('replay', name='别的脚本'); self.assertTrue(worker.wait_idle(3))   # 跑脚本不录
                code, body = worker.teach('rec_take')
                self.assertEqual(code, 200, body)
                self.assertIsNone(worker.recording)
            kinds = [next(k for k in ('preset', 'hand', 'pose', 'move') if k in st) for st in body['taken']]
            self.assertEqual(kinds, ['move', 'move', 'preset', 'hand', 'pose', 'move'])
            taken = body['taken']
            self.assertEqual(taken[0]['via'], 'start')
            self.assertEqual(taken[2], {'preset': 'nod', 'gap': taken[2]['gap']})
            self.assertGreaterEqual(taken[2]['gap'], .9)
            self.assertEqual({k: taken[4][k] for k in ('pose', 'speed')}, {'pose': '待机', 'speed': 12.0})
            self.assertEqual(taken[5]['via'], 'drag')
            # 页面把它们接进脚本保存（gap 变成等待步骤）；保存时多余字段被丢掉
            steps = []
            for st in taken:
                if st.get('gap', 0) >= .8:
                    steps.append({'wait': st['gap']})
                steps.append({k: v for k, v in st.items() if k != 'gap'})
            name, missing = worker.save_script('我的动作', steps)
            self.assertEqual(missing, [])
            saved = worker.recordings()['我的动作']['steps']
            self.assertTrue(all('gap' not in st for st in saved))
            self.assertIn({'preset': 'nod'}, saved)

    def test_wrap_grasp_thumb_first_then_fingers(self):
        seq = service.HAND_PRESETS['wrap_grasp'][1]
        self.assertEqual(service.HAND_PRESETS['wrap_grasp'][0], '握紧3')
        # 先拇指：转到对掌位、只微弯，四指还张着
        self.assertEqual(seq[1][0]['thumb_rot'], 85)
        self.assertGreaterEqual(seq[1][0]['index'], 90)
        self.assertGreaterEqual(seq[1][0]['thumb'], 60)
        # 再四指一路弯下来包住；拇指不握到底（夹住物体，不是握拳）
        fingers = [shape['index'] for shape, _ in seq]
        self.assertEqual(fingers, sorted(fingers, reverse=True))
        self.assertGreaterEqual(seq[-1][0]['thumb'], 30)
        self.assertLessEqual(service.HAND_PRESETS['wrap_grasp'][2], 400)
        backend = MockArm()
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True):
            self.assertEqual(worker.teach('hand', point='wrap_grasp')[0], 200)
            self.assertTrue(worker.wait_idle(5))
        self.assertEqual(hand.driver.last, seq[-1][0])

    def test_limit_margin_slowdown_and_watchdog(self):
        import math
        R = math.radians
        lo, hi = service.teach_limits()
        self.assertAlmostEqual(math.degrees(hi[1]), 165, places=0)     # J2 170 → 165
        self.assertEqual(lo[1], 0.0)                                    # 收拢那一侧不留余量（rest 在 0°）
        self.assertAlmostEqual(math.degrees(hi[3]), 71, places=0)      # J4 ±76 → ±71
        self.assertAlmostEqual(math.degrees(hi[0]), 120, places=0)
        # 靠近限位：限速 15°/s
        self.assertAlmostEqual(service.near_limit_vel([0, R(100), 0, 0, 0, 0], [0, R(160), 0, 0, 0, 0], 1.0),
                               service.NEAR_LIMIT_VEL)
        self.assertEqual(service.near_limit_vel([0, R(60), 0, 0, 0, 0], [0, R(100), 0, 0, 0, 0], 1.0), 1.0)
        backend = MockArm(q=[0, R(160), R(100), 0, 0, 0])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        with patch.object(service, 'TEACH_MODE', True), patch.object(service, 'SERVO_IDLE', .2):
            self.assertEqual(worker.teach('servo', targets={2: 168})[0], 422)     # 余量外
            self.assertEqual(worker.teach('joint', joint=2, target=168)[0], 422)
            # 已经在余量外的关节可以往里走；别的关节照常能动
            backend.q[1] = R(168)
            backend.last_command = list(backend.q)
            self.assertEqual(worker.teach('servo', targets={1: 10})[0], 200)
            self.assertTrue(worker.wait_idle(3))
            self.assertEqual(worker.teach('servo', targets={2: 166})[0], 200)
            self.assertTrue(worker.wait_idle(3))
            self.assertEqual(worker.status()['teach_limits']['joints'][1], [0.0, 165.0])
        # 看门狗：跟随时实际角度往外冲到离软件限位 1° 以内 → 停住保持
        class Overshoot(MockArm):
            def joints(self):               # 带载冲过头：到了目标还每读一次往外多走 1.5°
                if self.last_command and self.q[1] < R(172) and not self.holds:
                    self.q[1] += R(1.5)
                return list(self.q)
        b2 = Overshoot(q=[0, R(150), R(100), 0, 0, 0])
        w2 = service.ArmWorker(b2, effector='hand1', skills_path=SHOW)
        with patch.object(service, 'TEACH_MODE', True), patch.object(service, 'SERVO_IDLE', .5):
            self.assertEqual(w2.teach('servo', targets={2: 165})[0], 200)
            self.assertTrue(w2.wait_idle(3))
        self.assertIn('往限位冲），已提前停住', w2.last_error)
        self.assertEqual(b2.holds, 1)

    def test_preset_can_stay_at_its_last_pose(self):
        import math
        R = math.radians
        q0 = [R(20), R(30), R(60), R(-30), 0, 0]
        backend = MockArm(q=list(q0))
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True),                 patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
            worker.teach('rec_start')
            self.assertEqual(worker.teach('preset', name='point', back=False)[0], 200)
            self.assertTrue(worker.wait_idle(5))
            # 停在「指向前方」的姿态：J1、J5 还是出发时的，J2~J4 是预设的
            self.assertEqual([round(math.degrees(x), 1) for x in backend.q], [20.0, 60.0, 95.0, -35.0, 0.0, 0.0])
            self.assertEqual({k: v for k, v in worker.recording[-1].items() if k != 'gap'}, {'preset': 'point', 'back': False})
            backend.moves.clear()
            self.assertEqual(worker.teach('preset', name='point')[0], 200)      # 默认照旧回出发姿态
            self.assertTrue(worker.wait_idle(5))
            self.assertEqual([round(math.degrees(x), 1) for x in backend.q], [20.0, 60.0, 95.0, -35.0, 0.0, 0.0])
            self.assertEqual(len(backend.moves), 2)
        self.assertEqual(service.ArmWorker._clean_steps([{'preset': 'wave', 'back': False}, {'preset': 'wave', 'back': True}]),
                         [{'preset': 'wave', 'back': False}, {'preset': 'wave'}])

    def test_orientation_lock_keeps_thumb_up(self):
        import math
        from hand_controller.arm_kinematics import frames
        R = math.radians
        D = lambda q: [round(math.degrees(x), 1) for x in q]

        def hand_axes(q):     # 手朝向（link6 的 x）和拇指方向（link6 的 z），世界坐标
            rot = frames(q)[5][1]
            return [rot[i][0] for i in range(3)], [rot[i][2] for i in range(3)]

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm(q=[R(20), R(50), R(90), R(-10), R(15), R(40)])
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True), \
                    patch.object(service, 'SERVO_IDLE', .2), \
                    patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
                worker.teach('rec_start')
                code, body = worker.teach('orient', close=True)            # 开：先摆正
                self.assertEqual((code, body['orient_lock']), (200, True), body)
                self.assertTrue(worker.wait_idle(8))
                self.assertEqual(D(backend.q), [20.0, 50.0, 90.0, -40.0, 0.0, 0.0])
                fwd, thumb = hand_axes(backend.q)
                self.assertAlmostEqual(fwd[2], 0, places=6)                  # 手水平朝前
                self.assertAlmostEqual(thumb[2], 1, places=6)                # 拇指竖直朝上
                # 肩、肘怎么动，J4 跟着补，手的朝向不变
                for targets in ({3: 120}, {2: 70}, {2: 30, 3: 60}):
                    self.assertEqual(worker.teach('servo', targets=targets)[0], 200)
                    self.assertTrue(worker.wait_idle(8))
                    fwd, thumb = hand_axes(backend.q)
                    self.assertAlmostEqual(fwd[2], 0, places=6)
                    self.assertAlmostEqual(thumb[2], 1, places=6)
                worker.teach('joint', joint=3, target=100); self.assertTrue(worker.wait_idle(8))
                self.assertAlmostEqual(hand_axes(backend.q)[1][2], 1, places=6)
                # J4~J6 不能单独动；页面发来的 J4 被忽略
                self.assertEqual(worker.teach('jog', joint=5, delta=5)[0], 423)
                worker.teach('servo', targets={2: 40, 4: 30}); self.assertTrue(worker.wait_idle(8))
                self.assertAlmostEqual(math.degrees(backend.q[3]), 40 - 100, places=3)
                # 前往姿态也按锁住的手腕走
                worker.save_pose('斜着', [R(-30), R(60), R(80), R(20), R(10), R(30)])
                worker.teach('pose_go', name='斜着'); self.assertTrue(worker.wait_idle(8))
                self.assertEqual(D(backend.q), [-30.0, 60.0, 80.0, -20.0, 0.0, 0.0])
                # 关
                self.assertEqual(worker.teach('orient', close=False)[1]['orient_lock'], False)
                self.assertEqual(worker.teach('jog', joint=5, delta=5)[0], 200)
                self.assertTrue(worker.wait_idle(8))
                code, body = worker.teach('rec_take')
            kinds = [next(k for k in ('orient', 'pose', 'move') if k in st) for st in body['taken']]
            self.assertEqual(kinds, ['move', 'orient', 'move', 'move', 'move', 'move', 'move', 'pose', 'orient', 'move'])
            self.assertEqual([st['orient'] for st in body['taken'] if 'orient' in st], [True, False])
            # 回放：「开」之后的姿态按锁住的手腕；预设动作里的手腕不受锁影响
            worker.save_script('锁着走', [{'pose': '斜着'}, {'orient': True}, {'pose': '斜着'}, {'preset': 'wave'}])
            with patch.object(service, 'hands', return_value={1: hand}), \
                    patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
                skill = worker._build_replay(worker.recordings()['锁着走']['steps'], 1)
            pts = worker.cfg['points']
            self.assertEqual(D(pts['_rec_1']['q'])[3:], [20.0, 10.0, 30.0])        # 锁之前：照姿态原样
            self.assertEqual(D(pts['_rec_3']['q'])[3:], [-20.0, 0.0, 0.0])         # 锁之后：J4 = J2 − J3，J5 = J6 = 0
            self.assertEqual(D(pts['_rec_4_1']['q'])[5], 90.0)                      # 挥手的 J6 = 90 不被锁掉
            self.assertEqual(service.ArmWorker._clean_steps([{'orient': 1}]), [{'orient': True}])

    def test_restart_plan_and_request(self):
        import math
        import signal as sig
        R = math.radians

        class RealLike(MockArm):     # restart_plan 跳过模拟臂；当成真臂算
            mock = False
        backend = RealLike(q=[R(20), R(80), R(100), R(-20), 0, 0])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        with patch.object(service, 'arm', return_value=worker):
            plan = service.restart_plan()
            self.assertTrue(plan['moves'])
            self.assertEqual(plan['route'], ['回 rest'])
            self.assertGreater(plan['seconds'], 5)
            self.assertGreater(plan['lowest_cm'], 5)
            backend.q = [0.0] * 6
            self.assertEqual(service.restart_plan(), {'moves': False, 'why': '已经在 rest'})
            backend.tripped = '电机层越界锁定'
            plan = service.restart_plan()
            self.assertFalse(plan['moves'])
            self.assertIn('锁定', plan['why'])
            backend.tripped = None
            backend.q = [R(20), R(80), R(100), R(-20), 0, 0]
            with patch.object(service, 'park_route', side_effect=safety.SafetyError('找不到安全的回 rest 路线')):
                plan = service.restart_plan()
            self.assertTrue(plan['sinks'])
            self.assertIn('扶住臂', plan['why'])
        killed = []
        with patch.object(service.os, 'kill', lambda pid, s: killed.append(s)):
            service.request_restart(delay=0.01)
            time.sleep(.2)
        self.assertEqual(killed, [sig.SIGTERM])        # 和 systemctl restart 同一条退出路

    def test_gravity_overshoot_stopped_early_with_full_torque(self):
        import math
        R = math.radians
        self.assertEqual(service.teach_torque([0, R(60), R(90), 0, 0, 0], [0, R(100), R(90), 0, 0, 0]), .6)
        self.assertEqual(service.teach_torque([0, R(140), R(90), 0, 0, 0], [0, R(150), R(90), 0, 0, 0]), 1.0)

        class Gravity(MockArm):
            """到了目标后，被重力拖着按 21°/s 继续往外走（10-25 02:31 现场）；hold 之后停住。"""
            def __init__(self, **kw):
                super().__init__(**kw)
                self.t_reach = None
                self.peak = 0.0

            def command(self, q, seconds, torque_scale=1.0):
                super().command(q, seconds, torque_scale)
                self.t_reach = time.monotonic() if q[1] > R(150) else None

            def joints(self):
                if self.t_reach is not None and not self.holds:
                    self.q[1] = self.last_command[1] + R(21) * (time.monotonic() - self.t_reach)
                self.peak = max(self.peak, self.q[1])
                return list(self.q)

        backend = Gravity(q=[0, R(140), R(100), 0, 0, 0])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        with patch.object(service, 'TEACH_MODE', True), patch.object(service, 'SERVO_IDLE', 1.0):
            self.assertEqual(worker.teach('servo', targets={2: 165}, speed=30)[0], 200)
            self.assertTrue(worker.wait_idle(5))
        self.assertIn('已提前停住', worker.last_error)
        self.assertEqual(backend.holds, 1)
        self.assertLess(math.degrees(backend.peak), 168)          # 远在 170° 软件限位、175° 电机层锁定之前
        self.assertEqual(backend.moves[0][2], 1.0)                 # 靠近上限：满力矩
        # 正常快速拖到 155°（没冲过头）不会被提前停
        b2 = MockArm(q=[0, R(100), R(100), 0, 0, 0])
        w2 = service.ArmWorker(b2, effector='hand1', skills_path=SHOW)
        with patch.object(service, 'TEACH_MODE', True), patch.object(service, 'SERVO_IDLE', .3):
            for d in (120, 140, 155):
                self.assertEqual(w2.teach('servo', targets={2: d}, speed=57)[0], 200)
                time.sleep(.08)
            self.assertTrue(w2.wait_idle(5))
        self.assertIsNone(w2.last_error)
        self.assertAlmostEqual(math.degrees(b2.q[1]), 155, places=3)

    def test_per_finger_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            backend = MockArm()
            worker = service.ArmWorker(backend, effector='hand1', skills_path=path)
            hand = service.HandWorker(1, RH56F2("", 1, mock=True))
            with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'arm', return_value=worker), \
                    patch.object(service, 'TEACH_MODE', True):
                code, body = service.set_fingers(1, {'index': 20, 'thumb_rot': 85}, 250)
                self.assertEqual(code, 200, body)
                hand.drain()
                self.assertEqual((hand.driver.last['index'], hand.driver.last['thumb_rot']), (20, 85))
                self.assertEqual(hand.driver.last['pinky'], service.JOINTS['release']['pinky'])   # 没给的保持
                service.set_fingers(1, {'pinky': 10}, 250); hand.drain()
                self.assertEqual((hand.driver.last['pinky'], hand.driver.last['index']), (10, 20))
                for bad in ({'nose': 5}, {'index': 120}, {'index': -1}):
                    self.assertEqual(service.set_fingers(1, bad, 300)[0], 400)
                self.assertEqual(service.set_fingers(1, {'index': 5}, 5000)[0], 400)      # 力控超范围
                self.assertEqual(service.set_fingers(2, {'index': 5}, 300)[0], 400)       # 没有手#2
                # 录制：连续拖手指只记最后的手型
                worker.teach('rec_start')
                for v in (60, 40, 25):
                    service.set_fingers(1, {'middle': v}, 300)
                hand.drain()
                fingers = [st for st in worker.recording if 'fingers' in st]
                self.assertEqual(len(fingers), 1)
                self.assertEqual(fingers[0]['fingers']['middle'], 25)
                # 脚本里的手型步骤
                shape = dict(service.JOINTS['release'], index=30, thumb=40)
                worker.save_script('捏', [{'fingers': shape, 'force': 200}])
                with self.assertRaises(ValueError):
                    worker.save_script('坏', [{'fingers': {'index': 30}}])               # 要给全六个关节
                self.assertEqual(worker.teach('replay', name='捏')[0], 200)
                self.assertTrue(worker.wait_idle(5))
                self.assertEqual(hand.driver.last, shape)
                # 臂正在用手：拒绝
                with patch.object(worker, 'holds_hand1', return_value=True):
                    self.assertEqual(service.set_fingers(1, {'index': 5}, 300)[0], 409)

    def test_zero_standby_button(self):
        import math
        R = math.radians
        backend = MockArm(q=[R(30), R(60), R(90), R(-20), R(10), R(40)])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'TEACH_MODE', True):
            worker.teach('rec_start')
            self.assertEqual(worker.teach('start')[0], 200)
            self.assertTrue(worker.wait_idle(5))
            self.assertEqual(backend.q, worker.cfg['points']['rest']['q'])          # 全部关节 0°
            self.assertEqual(hand.driver.last, service.JOINTS['release'])            # 手张开
            steps = [{k: v for k, v in st.items() if k != 'gap'} for st in worker.recording[1:]]
            self.assertEqual(steps[0], {'grip': 'open'})                              # 回放时换成灵巧手张开
            self.assertEqual(steps[1]['move'], [0.0] * 6)
            code, body = worker.teach('rec_take')
            worker.save_script('回零位', body['taken'])                              # 录下来的步骤能存能回放
            backend.q = [R(10), R(30), R(40), 0, 0, 0]
            self.assertEqual(worker.teach('replay', name='回零位')[0], 200)
            self.assertTrue(worker.wait_idle(5))
            self.assertEqual(backend.q, [0.0] * 6)

    def test_script_voice_phrase_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'skills.json')
            shutil.copyfile(SHOW, path)
            worker = service.ArmWorker(MockArm(), effector='hand1', skills_path=path)
            steps = [{'pose': '水杯'}, {'hand': 'wrap_grasp'}, {'pose': '老人'}, {'wait': 2}, {'hand': 'release'},
                     {'preset': 'wave'}]
            worker.save_script('递物演示', steps, voice='递水、拿东西给老人')
            worker.save_script('递物演示', steps)                                   # 不带说法再存：保留原来的
            self.assertEqual(worker.recordings()['递物演示']['voice'], '递水、拿东西给老人')
            worker.save_script('递物演示', steps, voice='')                          # 清空
            self.assertNotIn('voice', worker.recordings()['递物演示'])
            worker.save_script('递物演示', steps, voice='递水')
            with patch.object(service, 'arm', return_value=worker):
                desc = service.voice_scripts()['递物演示']
            self.assertEqual(desc, '说法：递水；步骤：去「水杯」、握紧3、去「老人」、张开、挥手打招呼')

    def test_t5ai_v08_keys_and_voice_home(self):
        import math
        R = math.radians
        backend = MockArm(q=[R(20), R(40), R(70), R(-20), 0, 0])
        worker = service.ArmWorker(backend, effector='hand1', skills_path=SHOW)
        hand = service.HandWorker(1, RH56F2("", 1, mock=True))
        with patch.object(service, 'hands', return_value={1: hand}), patch.object(service, 'arm', return_value=worker),                 patch.object(service, 'TEACH_MODE', False),                 patch.object(service.HandWorker, 'pause', lambda self, seconds: False):
            # 零位待机：示教模式外也能用；手张开，全部关节回 0°
            self.assertEqual(service.SCREEN_KEYS['zero']()[0], 200)
            self.assertTrue(worker.wait_idle(5))
            self.assertEqual(backend.q, [0.0] * 6)
            self.assertEqual(hand.driver.last, service.JOINTS['release'])
            # 挥手 / 握手：预设，1.5 倍速，做完停在原处
            code, body = service.SCREEN_KEYS['wave']()
            self.assertEqual(code, 200, body)
            self.assertTrue(worker.wait_idle(5))
            self.assertNotEqual(backend.q, [0.0] * 6)                     # 没回出发姿态
            self.assertEqual(service.SCREEN_KEYS['shake']()[0], 200)
            self.assertTrue(worker.wait_idle(8))
            # 手指操：只动手
            moves = len(backend.moves)
            self.assertEqual(service.SCREEN_KEYS['finger']()[0], 200)
            hand.drain()
            self.assertEqual(hand.current, 'hand_exercise')
            self.assertEqual(len(backend.moves), moves)
            # 语音「归位」→ 零位待机点
            code, body = service.voice_dispatch('home', '1', '/voice_cmd', 'voice:归位')
            self.assertEqual(code, 200, body)
            self.assertTrue(worker.wait_idle(8))
            self.assertEqual(backend.q, [0.0] * 6)
            self.assertEqual(service.voice_dispatch('wave', '1')[0], 200)   # 其他动作照常走 dispatch

    def test_table_check_removed_on_site(self):
        import math
        cfg = safety.load_config('hand1', SHOW)
        self.assertIsNone(cfg['floor_z'])
        self.assertEqual(cfg['keep_out'], [])
        low = [math.radians(v) for v in (-60, 30, 0, 0, 0, 0)]          # 手伸到底座平面以下
        safety.check_pose(low, cfg)                                        # 不再因为桌面拒绝
        with self.assertRaises(safety.SafetyError):
            safety.check_pose(low, dict(cfg, floor_z=.03))                 # 打开检查仍然有效
        with self.assertRaises(safety.SafetyError):
            safety.check_limits([math.radians(150), 0, 0, 0, 0, 0])        # 限位照旧

    def test_full_sweep_preserves_all_existing_safety_checks(self):
        cfg = safety.load_config("gripper", SHOW)
        for name in ("arm_warmup", "arm_showcase", "gripper_cycle"):
            safety.check_skill(name, [0.] * 6, cfg, require_taught=True)
        self.assertLessEqual(cfg["speed"]["max_joint_vel"], .22)
        self.assertGreaterEqual(len(safety.skill_moves("arm_showcase", cfg)), 10)
        cfg = copy.deepcopy(cfg)
        cfg['points']['pick_med']['taught'] = False
        with self.assertRaises(safety.SafetyError):
            safety.check_skill("deliver_medicine", [0.] * 6, cfg, require_taught=True)

    def test_standalone_gripper_never_moves_rh56(self):
        backend = MockArm(has_gripper=True)
        worker = service.ArmWorker(backend, effector="gripper", skills_path=SHOW)
        with patch.object(service, "hands", side_effect=AssertionError("independent hand touched")):
            self.assertEqual(worker.request("arm_showcase")[0], 200)
            self.assertTrue(worker.wait_idle(3))
            self.assertEqual(worker.done, 1)
            self.assertEqual(worker.request("gripper_cycle")[0], 200)
            self.assertTrue(worker.wait_idle(3))
        self.assertEqual(backend.grips, [False, True, False])

    def test_single_hand_rhythm_uses_connected_hand(self):
        w = service.HandWorker(1, RH56F2("", 1, mock=True))
        a = service.ArmWorker(MockArm(has_gripper=True), effector="gripper", skills_path=SHOW)
        with patch.object(service, "HAND_COUNT", 1), patch.object(service, "HAND_LAYOUT", "standalone"), \
             patch.object(service, "hands", return_value={1:w}), patch.object(service, "arm", return_value=a), \
             patch.object(w, "submit") as submit:
            code, body = service.dispatch("gentle_hold", "2")
            self.assertEqual((code, body["hand"]), (200, 1))
            submit.assert_called_once_with("gentle_hold")
            self.assertEqual(service.dispatch("release", "2")[0], 400)

    def test_thirteen_beats_and_complete_finger_positions(self):
        self.assertEqual(len(SEQUENCES["hand_exercise"]), 13)
        for positions, _ in SEQUENCES["hand_exercise"]:
            self.assertEqual(set(positions), set(JOINTS["release"]))


if __name__ == "__main__":
    unittest.main()
