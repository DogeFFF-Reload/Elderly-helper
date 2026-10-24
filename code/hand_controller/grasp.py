"""Return-medicine placement wait and bounded retry for the F2 motion profile."""
import math
from hand_controller import rh56_register_map as RM
from hand_controller.rh56f2_driver import HandError


def wait_put(driver, abort, timeout=30, notify=lambda text: None):
    """Returns result dict, or None on stop. Position stall is a heuristic only."""
    for attempt, seconds in ((1, timeout), (2, 6)):
        notify('等待放入（%s 秒，第 %s 次）' % (seconds, attempt))
        if abort.wait(seconds):
            return None
        driver.set_force(300)
        if abort.is_set():
            return None
        # Keep thumb rotation neutral; close only the five flexion actuators.
        close = dict.fromkeys(RM.JOINT_ORDER, 8)
        close['thumb_rot'] = 50
        driver.move(close)
        if abort.wait(1.2):
            return None
        angles = driver.read_angles()
        values = [angles.get(j) for j in RM.JOINT_ORDER if j != 'thumb_rot']
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 100 for v in values):
            raise HandError('夹持检测读数无效，不继续收回')
        # A fully closed empty hand reaches ~8%; allow convergence tolerance.
        holding = sum(12 < v < 90 for v in values) >= 2
        result = {'holding': holding, 'attempts': attempt, 'angles': angles,
                  'method': 'position_stall_heuristic'}
        notify('疑似夹住物品' if holding else '空握')
        if holding:
            return result
        if abort.is_set():
            return None
        opened = dict.fromkeys(RM.JOINT_ORDER, 95)
        opened['thumb_rot'] = 50
        driver.move(opened)
    return result
