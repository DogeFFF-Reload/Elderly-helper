"""Small, dependency-light RH56F2 Modbus RTU driver.

The driver owns framing and register validation.  It has two deliberately
separate modes:

* ``mock=True`` (the default through ``MOCK_SERIAL=1``) records frames and
  never touches a serial port;
* ``mock=False`` requires a non-provisional map and
  ``RH56_REGMAP_CONFIRMED=1`` before opening a port.

The second guard is intentional: the handoff says the four register addresses
are still provisional.  A real serial connection without that confirmation is
an explicit error, never a best-effort write.
"""
from __future__ import annotations

import os
import struct
import threading
import time

from hand_controller import rh56_register_map as RM


class RH56Error(RuntimeError):
    """Base class for protocol and safety errors."""


class RH56SafetyError(RH56Error):
    """Raised when real writes are attempted before the map is confirmed."""


class RH56ProtocolError(RH56Error):
    """Raised when the hand returns a malformed or exception Modbus frame."""


def crc16(data: bytes) -> int:
    """Modbus RTU CRC16, returned as an integer for little-endian packing."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(slave: int, function: int, payload: bytes) -> bytes:
    body = bytes((slave, function)) + payload
    return body + struct.pack("<H", crc16(body))


def _check_crc(raw: bytes) -> None:
    if len(raw) < 4:
        raise RH56ProtocolError("Modbus response too short")
    expected = struct.unpack("<H", raw[-2:])[0]
    actual = crc16(raw[:-2])
    if expected != actual:
        raise RH56ProtocolError("Modbus CRC mismatch")


class RH56F2:
    """One RH56F2 slave on a serial Modbus RTU link."""

    def __init__(self, port="COM5", slave_id=RM.SLAVE_ID_HAND1, *, mock=None,
                 serial_obj=None, confirmed=None, timeout=0.3):
        self.port = port
        self.slave = int(slave_id)
        self.timeout = float(timeout)
        self.log = []
        self.ser = None
        self._io_lock = threading.Lock()
        self.mock = (os.environ.get("MOCK_SERIAL", "1") != "0") if mock is None else bool(mock)
        self.confirmed = RM.confirmed_from_environment(confirmed)
        self._mock_values = {}
        if self.mock:
            return
        if RM.PROVISIONAL or not self.confirmed:
            raise RH56SafetyError(
                "RH56 register map is provisional or unconfirmed; verify docs/10, "
                "set PROVISIONAL=False, and set RH56_REGMAP_CONFIRMED=1 before real writes"
            )
        if serial_obj is not None:
            self.ser = serial_obj
            return
        try:
            import serial
        except ImportError as exc:
            raise RH56Error("pyserial is required for MOCK_SERIAL=0") from exc
        self.ser = serial.Serial(
            port=port,
            baudrate=RM.BAUD,
            bytesize=RM.BYTESIZE,
            parity=RM.PARITY,
            stopbits=RM.STOPBITS,
            timeout=self.timeout,
        )

    def close(self):
        if self.ser is not None and hasattr(self.ser, "close"):
            self.ser.close()

    def _mock_response(self, function, payload):
        if function == 0x03:
            _addr, count = struct.unpack(">HH", payload)
            values = [self._mock_values.get(_addr + i, 0) for i in range(count)]
            body = bytes((self.slave, function, count * 2))
            body += struct.pack(">%dH" % count, *values)
            return body + struct.pack("<H", crc16(body))
        if function == 0x06:
            return frame(self.slave, function, payload)
        if function == 0x10:
            start, count, byte_count = struct.unpack(">HHB", payload[:5])
            values = struct.unpack(">%dH" % count, payload[5:5 + byte_count])
            for offset, value in enumerate(values):
                self._mock_values[start + offset] = value
            return frame(self.slave, function, struct.pack(">HH", start, count))
        return None

    def _transact(self, function, payload):
        request = frame(self.slave, function, payload)
        self.log.append("TX " + request.hex(" "))
        if self.mock:
            response = self._mock_response(function, payload)
            self.log.append("MOCK< slave=%d fc=%d" % (self.slave, function))
            return response
        if self.ser is None:
            raise RH56Error("serial port is not open")
        with self._io_lock:
            self.ser.write(request)
            response = self.ser.read(256)
        self.log.append("RX " + response.hex(" "))
        return response

    def _validate_header(self, response, function):
        if response is None:
            raise RH56ProtocolError("no Modbus response")
        _check_crc(response)
        if response[0] != self.slave:
            raise RH56ProtocolError("unexpected slave id %d" % response[0])
        if response[1] == (function | 0x80):
            code = response[2] if len(response) > 2 else -1
            raise RH56ProtocolError("Modbus exception 0x%02x" % code)
        if response[1] != function:
            raise RH56ProtocolError("unexpected function 0x%02x" % response[1])

    def write_register(self, address, value):
        value = int(value)
        if not 0 <= value <= 0xFFFF:
            raise ValueError("register value out of range")
        response = self._transact(0x06, struct.pack(">HH", int(address), value))
        self._validate_header(response, 0x06)
        if response[2:6] != struct.pack(">HH", int(address), value):
            raise RH56ProtocolError("FC06 echo mismatch")
        if self.mock:
            self._mock_values[int(address)] = value

    def write_registers(self, start_address, values):
        values = [int(v) for v in values]
        if not values or len(values) > 123 or any(not 0 <= v <= 0xFFFF for v in values):
            raise ValueError("invalid FC16 values")
        payload = struct.pack(">HHB", int(start_address), len(values), len(values) * 2)
        payload += struct.pack(">%dH" % len(values), *values)
        response = self._transact(0x10, payload)
        self._validate_header(response, 0x10)
        if response[2:6] != struct.pack(">HH", int(start_address), len(values)):
            raise RH56ProtocolError("FC16 echo mismatch")

    def read_register(self, address, count=1):
        count = int(count)
        if count < 1 or count > 125:
            raise ValueError("invalid FC03 count")
        response = self._transact(0x03, struct.pack(">HH", int(address), count))
        self._validate_header(response, 0x03)
        if len(response) < 3 or response[2] != count * 2 or len(response) != 5 + count * 2:
            raise RH56ProtocolError("FC03 byte count mismatch")
        return list(struct.unpack(">%dH" % count, response[3:-2]))

    @staticmethod
    def _to_raw(percent):
        percent = max(0.0, min(100.0, float(percent)))
        return int(round(RM.VALUE_MIN + (RM.VALUE_MAX - RM.VALUE_MIN) * percent / 100.0))

    def pose_raw(self, joints):
        values = [self._to_raw(joints.get(joint, RM.SCALE / 2)) for joint in RM.JOINT_ORDER]
        self.write_registers(RM.REG_TARGET_BASE, values)

    def move(self, joints, speed=50):
        """Write a complete six-joint target, then an optional speed value."""
        self.pose_raw(joints)
        if RM.REG_SPEED is not None and speed is not None:
            self.write_register(RM.REG_SPEED, self._to_raw(speed))

    def status(self):
        raw = self.read_register(RM.REG_STATUS, 2)
        return {"slave": self.slave, "mock": self.mock, "raw": raw,
                "online": True, "error": raw[1] if len(raw) > 1 else None}
