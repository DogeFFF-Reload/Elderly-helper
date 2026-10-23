"""RH56F2 framing and safety gate tests (no physical serial device required)."""
import struct
import unittest
from unittest import mock

from hand_controller import rh56_register_map as rm
from hand_controller import arm_service
from hand_controller.rh56_backend import RH56Backend
from hand_controller.rh56f2_driver import RH56F2, RH56SafetyError, crc16, frame
from hand_controller import arm_backend_vendor


class MemorySerial:
    """In-memory Modbus peer for the non-mock driver path.

    It implements the tiny pyserial surface used by RH56F2, but never opens
    or enumerates an operating-system device.
    """

    def __init__(self, slave):
        self.slave = slave
        self.registers = {}
        self.pending = b""
        self.closed = False

    def write(self, request):
        self.assert_request_crc(request)
        slave, function = request[:2]
        self.assertEqual(slave, self.slave)
        payload = request[2:-2]
        if function == 0x03:
            address, count = struct.unpack(">HH", payload)
            values = [self.registers.get(address + i, 0) for i in range(count)]
            body = bytes((slave, function, count * 2))
            body += struct.pack(">%dH" % count, *values)
            self.pending = frame(slave, function, body[2:])
        elif function == 0x06:
            address, value = struct.unpack(">HH", payload)
            self.registers[address] = value
            self.pending = frame(slave, function, payload)
        elif function == 0x10:
            address, count, byte_count = struct.unpack(">HHB", payload[:5])
            values = struct.unpack(">%dH" % count, payload[5:5 + byte_count])
            for offset, value in enumerate(values):
                self.registers[address + offset] = value
            self.pending = frame(slave, function, struct.pack(">HH", address, count))
        else:
            raise AssertionError("unexpected function %s" % function)
        return len(request)

    def read(self, _size):
        response, self.pending = self.pending, b""
        return response

    def close(self):
        self.closed = True

    @staticmethod
    def assert_request_crc(request):
        expected = struct.unpack("<H", request[-2:])[0]
        if crc16(request[:-2]) != expected:
            raise AssertionError("request CRC mismatch")

    @staticmethod
    def assertEqual(left, right):
        if left != right:
            raise AssertionError("slave mismatch: %r != %r" % (left, right))


class RH56DriverTest(unittest.TestCase):
    def test_service_mock_mode_never_enters_real_backend(self):
        """The default path stays hardware-free even if a real backend breaks."""
        with mock.patch.object(arm_service, "MOCK_SERIAL", True), \
             mock.patch.object(arm_service, "_ensure_real_backend",
                               side_effect=AssertionError("real backend touched")):
            result = arm_service._execute("fingers_5", 1)
        self.assertTrue(result["ok"])
        self.assertTrue(result["mock"])

    def test_crc_and_frame_are_modbus_little_endian(self):
        packet = frame(1, 6, struct.pack(">HH", 0x0210, 1))
        self.assertEqual(packet[:2], b"\x01\x06")
        self.assertEqual(struct.unpack("<H", packet[-2:])[0], crc16(packet[:-2]))

    def test_real_mode_requires_register_confirmation(self):
        with self.assertRaises(RH56SafetyError):
            RH56F2(mock=False, confirmed=False)

    def test_real_mode_still_requires_map_to_be_marked_final(self):
        with mock.patch.object(rm, "PROVISIONAL", True):
            with self.assertRaises(RH56SafetyError):
                RH56F2(mock=False, confirmed=True)

    def test_mock_roundtrip_writes_and_reads(self):
        hand = RH56F2(mock=True, confirmed=False)
        hand.write_register(rm.REG_STATUS, 7)
        self.assertEqual(hand.read_register(rm.REG_STATUS), [7])
        hand.write_registers(rm.REG_TARGET_BASE, [0, 1000, 500, 250, 750, 50])
        self.assertEqual(hand.read_register(rm.REG_TARGET_BASE, 6), [0, 1000, 500, 250, 750, 50])
        hand.move({name: value for name, value in zip(rm.JOINT_ORDER, [0, 20, 40, 60, 80, 100])})
        self.assertTrue(any(line.startswith("TX ") for line in hand.log))

    def test_non_mock_frame_path_uses_in_memory_peer_only(self):
        wire = MemorySerial(slave=1)
        wire.registers[rm.REG_STATUS] = 0x1234
        wire.registers[rm.REG_STATUS + 1] = 0
        with mock.patch.object(rm, "PROVISIONAL", False):
            hand = RH56F2(mock=False, serial_obj=wire, confirmed=True)
            self.assertEqual(hand.status()["raw"], [0x1234, 0])
            hand.write_register(rm.REG_CONTROL, 1)
            hand.write_registers(rm.REG_TARGET_BASE, [0, 1000, 500, 250, 750, 50])
            self.assertEqual(hand.read_register(rm.REG_TARGET_BASE, 6),
                             [0, 1000, 500, 250, 750, 50])
            hand.move({name: value for name, value in zip(
                rm.JOINT_ORDER, [0, 20, 40, 60, 80, 100])})
            self.assertEqual(wire.registers[rm.REG_SPEED], 500)
            hand.close()
        self.assertTrue(wire.closed)

    def test_mock_backend_maps_master_actions_to_hand_poses(self):
        backend = RH56Backend(RH56F2(mock=True), RH56F2(mock=True))
        result = backend.execute("rps_rock", 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["did"], "pose:rock")
        self.assertTrue(backend.execute("press_sos", 1)["ok"] is False)

    def test_master_gentle_hold_alias_runs_on_hand_two(self):
        hand1, hand2 = RH56F2(mock=True), RH56F2(mock=True)
        backend = RH56Backend(hand1, hand2)
        with mock.patch("hand_controller.rh56_backend.time.sleep"):
            result = backend.execute("gentle_hold", 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["did"], "seq:gentle_hold")
        self.assertEqual(result["hand"], 2)
        self.assertEqual(hand1.read_register(rm.REG_TARGET_BASE, 6), [0, 0, 0, 0, 0, 0])
        self.assertEqual(hand2.read_register(rm.REG_TARGET_BASE, 6)[1:5], [950, 950, 950, 950])

    def test_panthera_slot_fails_closed_without_sdk(self):
        with mock.patch.object(arm_backend_vendor, "BRAND_SDK_IMPORTED", False):
            result = arm_backend_vendor.execute("press_sos")
        self.assertFalse(result["ok"])
        self.assertIn("SDK", result["error"])


if __name__ == "__main__":
    unittest.main()
