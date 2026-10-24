"""F2 motion profile integration with simulated wire replies; no hardware."""
import os
import sys
import unittest
from pathlib import Path

os.environ['RH56_PROFILE'] = 'f2_motion'
os.environ['RH56_REGMAP_CONFIRMED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_controller import rh56_register_map as RM, rh56f2_driver as D


class Wire:
    def __init__(self):
        self.frames = []
        self.pending = b''
        self.bad_ack = False
        self.bad_force = False

    def reset_input_buffer(self):
        self.pending = b''

    def write(self, request):
        self.frames.append(request)
        if request[4] == 0x11:
            data = D.pack_shorts([80, 100, 200, 400, 600, 1000])
            body = bytes([1, 15, 0x11]) + request[5:7] + data
        else:
            ack = 1 if self.bad_ack or (self.bad_force and request[5:7] == b'\xda\x05') else len(request[7:-1])
            body = bytes([1, 4, 0x12]) + request[5:7] + bytes([ack])
        self.pending = b'\x90\xeb' + body + bytes([sum(body) & 255])

    def read(self, n):
        data, self.pending = self.pending[:n], self.pending[n:]
        return data


class F2Test(unittest.TestCase):
    def setUp(self):
        D._PORTS.clear()
        self.wire = Wire()
        self.driver = D.RH56F2('test', 1, mock=False, serial_factory=lambda _: self.wire)

    def test_addresses_and_limits(self):
        self.assertEqual((RM.REG_ANGLE_SET, RM.REG_ANGLE_ACT, RM.FORCE_MAX), (0x40a, 0x422, 500))
        self.assertEqual([D.RH56F2.to_raw(x) for x in [0, 50, 100, -20, 120]], [1995, 1000, 0, 1995, 0])

    def test_mock_starts_open(self):
        self.assertEqual(set(D.RH56F2(mock=True).read_angles().values()), {100.0})

    def test_force_precedes_motion(self):
        self.driver.move(dict.fromkeys(RM.JOINT_ORDER, 95))
        self.assertEqual([r[5:7] for r in self.wire.frames], [b'\xda\x05', b'\x0a\x04'])
        self.assertEqual(self.wire.frames[0][7:-1], D.pack_shorts([300] * 6))
        self.assertEqual(self.wire.frames[1][7:-1], D.pack_shorts([100] * 6))

    def test_force_ack_failure_prevents_motion(self):
        self.wire.bad_force = True
        with self.assertRaises(D.HandError):
            self.driver.move(dict.fromkeys(RM.JOINT_ORDER, 95))
        self.assertEqual(len(self.wire.frames), 1)

    def test_corrupt_ack_rejected(self):
        self.wire.bad_ack = True
        with self.assertRaises(D.HandError):
            self.driver.set_force(300)

    def test_force_cap(self):
        self.driver.set_force(999)
        self.assertEqual(self.wire.frames[-1][7:-1], D.pack_shorts([500] * 6))

    def test_hold_uses_actual_instead_of_unverified_minus_one(self):
        self.driver.hold()
        self.assertEqual(self.wire.frames[-1][7:-1], D.pack_shorts([80, 100, 200, 400, 600, 1000]))

    def test_read_polarity(self):
        self.assertEqual(self.driver.read_angles()['pinky'], 96)
        self.assertEqual(self.driver.read_angles()['thumb_rot'], 50)

    def test_unverified_speed_address_rejected(self):
        with self.assertRaises(D.HandError):
            self.driver.write_shorts(RM.REG_SPEED_SET, [100] * 6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
