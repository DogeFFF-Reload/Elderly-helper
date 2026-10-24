import unittest
from hand_controller.grasp import wait_put
from hand_controller.rh56f2_driver import HandError


class Stop:
    def __init__(self, at=None):
        self.waits = []
        self.at = at
        self.stopped = False

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = len(self.waits) == self.at
        return self.stopped

    def is_set(self):
        return self.stopped


class Hand:
    def __init__(self, readings):
        self.readings = iter(readings)
        self.moves = []
        self.forces = []

    def set_force(self, value):
        self.forces.append(value)

    def move(self, joints):
        self.moves.append(joints)

    def read_angles(self):
        return next(self.readings)


def angles(value):
    return dict(pinky=value, ring=value, middle=value, index=value, thumb=value, thumb_rot=50)


class GraspTest(unittest.TestCase):
    def test_object_stops_two_flexion_fingers(self):
        h, stop = Hand([angles(40)]), Stop()
        self.assertTrue(wait_put(h, stop)['holding'])
        self.assertEqual(stop.waits, [30, 1.2])
        self.assertEqual(h.forces, [300])
        self.assertEqual(h.moves[0]['thumb_rot'], 50)

    def test_empty_reopens_then_retries_once(self):
        h, stop = Hand([angles(8), angles(35)]), Stop()
        r = wait_put(h, stop)
        self.assertEqual((r['holding'], r['attempts']), (True, 2))
        self.assertEqual(stop.waits, [30, 1.2, 6, 1.2])
        self.assertEqual([p['pinky'] for p in h.moves], [8, 95, 8])

    def test_second_empty_leaves_hand_open(self):
        h = Hand([angles(8.2), angles(8)])
        r = wait_put(h, Stop())
        self.assertEqual((r['holding'], r['attempts']), (False, 2))
        self.assertEqual(h.moves[-1]['pinky'], 95)

    def test_thumb_rotation_does_not_count_as_object(self):
        h = Hand([angles(8), angles(8)])
        self.assertFalse(wait_put(h, Stop())['holding'])

    def test_stop_during_wait_does_not_close(self):
        h = Hand([])
        self.assertIsNone(wait_put(h, Stop(at=1)))
        self.assertEqual(h.moves, [])

    def test_stop_during_settle_prevents_retry(self):
        h = Hand([])
        self.assertIsNone(wait_put(h, Stop(at=2)))
        self.assertEqual(len(h.moves), 1)

    def test_invalid_readback_is_error(self):
        for value in (None, float('nan'), -1, 101):
            with self.subTest(value=value), self.assertRaises(HandError):
                wait_put(Hand([angles(value)]), Stop())


if __name__ == '__main__':
    unittest.main()
