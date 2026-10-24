"""RH56F2 驱动 —— 最小 Modbus RTU（FC03/FC06/FC16，纯 Python 无依赖）。

MOCK_SERIAL=1（默认）  : 所有写操作只打印日志，读操作返回模拟值 → 笔记本全流程可跑
MOCK_SERIAL=0          : 走真实 COM 口（现场核对 register_map 后启用）

% 开合度 0~100  →  原始寄存器值：value = SCALE? 见 register_map.py
"""
from __future__ import annotations
import os
import struct
import time

from hand_controller import rh56_register_map as RM

MOCK = os.environ.get("MOCK_SERIAL", "1") != "0"


# ---------- CRC16 (Modbus) ----------

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _frame(slave: int, func: int, payload: bytes) -> bytes:
    body = bytes([slave, func]) + payload
    c = crc16(body)
    return body + struct.pack("<H", c)


# ---------- Driver ----------

class RH56F2:
    def __init__(self, port: str = "COM5", slave_id: int = RM.SLAVE_ID_HAND1):
        self.port = port
        self.slave = slave_id
        self.ser = None
        self.log: list[str] = []
        if not MOCK:
            import serial  # pyserial；现场才需要
            self.ser = serial.Serial(port, RM.BAUD, parity=RM.PARITY,
                                     stopbits=RM.STOPBITS, timeout=0.3)

    # ---- 底层 ----

    def _transact(self, func: int, payload: bytes) -> bytes | None:
        req = _frame(self.slave, func, payload)
        self.log.append("TX " + req.hex(" "))
        if MOCK:
            self.log.append(f"MOCK< (slave={self.slave} fc={func})")
            return bytes(8)
        self.ser.write(req)
        time.sleep(0.05)
        resp = self.ser.read(64)
        self.log.append("RX " + resp.hex(" "))
        return resp

    def write_register(self, addr: int, value: int):
        self._transact(0x06, struct.pack(">HH", addr, value))

    def write_registers(self, start_addr: int, values: list[int]):
        payload = struct.pack(">HHB", start_addr, len(values), len(values) * 2)
        payload += struct.pack(">%dH" % len(values), *values)
        self._transact(0x10, payload)

    def read_register(self, addr: int, count: int = 1) -> list[int]:
        self._transact(0x03, struct.pack(">HH", addr, count))
        if MOCK:
            return [0] * count
        # 现场解析：resp = addr(1)+func(1)+bytecount(1)+data+crc(2)
        return []

    # ---- 业务层（全系统唯一入口） ----

    def _to_raw(self, pct: float) -> int:
        pct = max(0.0, min(100.0, pct))
        return int(RM.VALUE_MIN + (RM.VALUE_MAX - RM.VALUE_MIN) * pct / 100.0)

    def pose_raw(self, joints: dict[str, float]):
        """按 JOINT_ORDER 批量下发 6 关节。"""
        vals = []
        for j in RM.JOINT_ORDER:
            v = joints.get(j, RM.SCALE / 2)
            vals.append(self._to_raw(v))
        self.write_registers(RM.REG_TARGET_BASE, vals)

    def move(self, joints: dict[str, float], speed: float = 50):
        self.log.append(f"[{self.slave}] move {joints} speed={speed}")
        self.write_register(RM.REG_TARGET_BASE, 0)   # 占位；速度按手册再写
        self.pose_raw(joints)

    def open_hand(self, speed: float = 60):   self.move(
        {j: 95 for j in RM.JOINT_ORDER}, speed)

    def close_hand(self, speed: float = 60):  self.move(
        {j: 8 for j in RM.JOINT_ORDER}, speed)

    def status(self) -> dict:
        raw = self.read_register(RM.REG_STATUS, 2)
        return {"slave": self.slave, "mock": MOCK, "raw": raw}
