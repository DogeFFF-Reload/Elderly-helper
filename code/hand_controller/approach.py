"""Plan a short translation with the gripper orientation fixed (no SDK)."""
import math
from hand_controller.arm_kinematics import tool_position
from hand_controller import arm_safety as S


def translate_fixed(q, dx=0., dz=.06):
    """Solve the parallel J2/J3/J4 chain; preserve J1/J5/J6 and tool pitch.

    dx is radial movement in the base's current vertical plane. Joint4
    compensates shoulder/elbow motion so the gripper never twists on descent.
    """
    original = list(q)
    pitch = q[1]-q[2]-q[3]
    c, s = math.cos(q[0]), math.sin(q[0])
    p = tool_position(q)
    goal = (c*p[0]+s*p[1]+dx, p[2]+dz)
    a, b = q[1], q[2]
    def evaluate(a, b):
        target = list(original)
        target[1], target[2], target[3] = a, b, a-b-pitch
        pos = tool_position(target)
        return target, (c*pos[0]+s*pos[1], pos[2])
    for _ in range(80):
        target, pos = evaluate(a,b)
        error = [goal[i]-pos[i] for i in range(2)]
        if max(abs(x) for x in error) < 1e-5:
            S.check_limits(target, '固定夹爪方向的接近点')
            return target
        h = .0001
        _, pa = evaluate(a+h,b)
        _, pb = evaluate(a,b+h)
        A,B = (pa[0]-pos[0])/h, (pb[0]-pos[0])/h
        C,D = (pa[1]-pos[1])/h, (pb[1]-pos[1])/h
        det = A*D-B*C
        if abs(det) < 1e-8:
            break
        da,db = (D*error[0]-B*error[1])/det, (-C*error[0]+A*error[1])/det
        a += max(-.2,min(.2,da))
        b += max(-.2,min(.2,db))
    raise S.SafetyError('当前姿态无法在限位内生成接近点，请调整夹取姿态')


def generate_approach(cfg):
    if not cfg['points']['pick_med'].get('taught'):
        raise S.SafetyError('请先记录新的药盒夹取点')
    pick = cfg['points']['pick_med']['q']
    errors = []
    for clearance in (.06,.05,.04):
        try:
            pre = translate_fixed(pick, dz=clearance)
            S.check_segment(pre,pick,cfg,'药盒垂直接近')
            S.check_segment(pick,pre,cfg,'药盒垂直退出')
            orient = [pick[0], math.radians(40), math.radians(60), 0., pick[4], pick[5]]
            S.check_segment(orient,pre,cfg,'高处对准')
            return {'pre_grasp':pre, 'orient_clear':orient, 'clearance_m':clearance}
        except S.SafetyError as e:
            errors.append(str(e))
    raise S.SafetyError('无法生成至少 4 cm 的固定朝向接近点：'+errors[-1])
