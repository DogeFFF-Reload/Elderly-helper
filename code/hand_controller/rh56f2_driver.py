"""RH56F2 灵巧手驱动：最小 Modbus RTU（FC03 读 / FC06 写一个 / FC16 写多个）。真机才要 pyserial。

来源 caremate_hand2_kit 的同名文件，改了这几处：
- 去掉 move() 里"先往目标位置首寄存器写 0"的占位：真机上那一下会让拇指先握到底
- 校验回包（从站号、功能码、CRC、异常码），读寄存器真的解析出数值
- 两只手可以共用一个串口（同一条总线、不同从站号），串口加锁
- mock / 真机由构造参数决定（默认看环境变量 MOCK_SERIAL），测试可以注入假串口
- 寄存器表没核对（rh56_register_map.VERIFIED = False）时，真机只读，写动作直接拒绝

开合度约定：0 = 全握，100 = 全开（和 poses.JOINTS 一致）。
"""
import collections
import os
import struct
import threading

from hand_controller import rh56_register_map as RM


def mock_from_env():
    """MOCK_SERIAL=0 才走真串口；不设或其他值都是 mock（只记帧、不发）。"""
    return os.environ.get("MOCK_SERIAL", "1") != "0"


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(slave, func, payload):
    body = bytes([slave, func]) + payload
    return body + struct.pack("<H", crc16(body))


class ModbusError(Exception):
    pass


def _pyserial(port):
    import serial  # 真机才需要：pip install pyserial
    return serial.Serial(port, RM.BAUD, parity=RM.PARITY, stopbits=RM.STOPBITS, timeout=RM.TIMEOUT)


_PORTS = {}            # 串口名 → [serial, lock]：两只手同一条总线时共用
_PORTS_MU = threading.Lock()


class RH56F2:
    def __init__(self, port="", slave_id=RM.SLAVE_ID_HAND1, mock=None, serial_factory=None):
        self.port = port
        self.slave = slave_id
        self.mock = mock_from_env() if mock is None else mock
        self.factory = serial_factory or _pyserial
        self.ser = None
        self.lock = None
        self.error = None
        self.last = None                          # 最近一次下发的开合度
        self.log = collections.deque(maxlen=50)   # 最近的收发帧，排错用

    # ---------- 串口 ----------

    def connect(self):
        """真机：打开串口，失败只记在 error 里不抛（USB 后插也能重试）；mock 总是可用。"""
        if self.mock or self.ser is not None:
            return True
        if not self.port:
            self.error = "没配串口（环境变量 HAND<n>_PORT，如 COM6 或 /dev/ttyUSB0）"
            return False
        try:
            with _PORTS_MU:
                if self.port not in _PORTS:
                    _PORTS[self.port] = [self.factory(self.port), threading.Lock()]
                self.ser, self.lock = _PORTS[self.port]
        except Exception as e:  # noqa: BLE001 驱动没装、口号不对、被占用
            self.error = "串口 %s 打不开：%s" % (self.port, e)
            return False
        self.error = None
        return True

    def _drop(self, why):
        """串口坏了（USB 拔了）：关掉、从共用表里去掉，下次 connect 重开。"""
        with _PORTS_MU:
            entry = _PORTS.pop(self.port, None)
        if entry:
            try:
                entry[0].close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = self.lock = None
        self.error = why

    # ---------- Modbus ----------

    def _transact(self, func, payload, expect_len):
        req = frame(self.slave, func, payload)
        self.log.append("TX " + req.hex(" "))
        if self.mock:
            return None
        if not self.connect():
            raise ModbusError(self.error)
        try:
            with self.lock:
                self.ser.reset_input_buffer()
                self.ser.write(req)
                resp = self.ser.read(expect_len)
        except Exception as e:  # noqa: BLE001 串口层出错 = 线断了
            self._drop("串口 %s 出错：%s" % (self.port, e))
            raise ModbusError(self.error)
        self.log.append("RX " + resp.hex(" "))
        return self._check(func, resp, expect_len)

    def _check(self, func, resp, expect_len):
        crc_ok = lambda r: crc16(r[:-2]) == struct.unpack("<H", r[-2:])[0]  # noqa: E731
        if len(resp) == 5 and resp[1] == func | 0x80 and crc_ok(resp):
            raise ModbusError("从站 %d 回异常码 %d（地址或功能码不对？对照手册）" % (self.slave, resp[2]))
        if len(resp) < expect_len:
            raise ModbusError("从站 %d 没回应（收到 %d/%d 字节）：查 24V、A/B 线、从站号、波特率" % (
                self.slave, len(resp), expect_len))
        if not crc_ok(resp):
            raise ModbusError("从站 %d 回包 CRC 不对：线上有干扰或波特率不对" % self.slave)
        if resp[0] != self.slave or resp[1] != func:
            raise ModbusError("回包不是从站 %d 功能码 %d 的：%s" % (self.slave, func, resp.hex(" ")))
        return resp

    def read_registers(self, addr, count=1):
        resp = self._transact(0x03, struct.pack(">HH", addr, count), 5 + 2 * count)
        if resp is None:
            return [0] * count
        return list(struct.unpack(">%dH" % count, resp[3:3 + 2 * count]))

    def write_register(self, addr, value):
        self._guard_write()
        self._transact(0x06, struct.pack(">HH", addr, value), 8)

    def write_registers(self, addr, values):
        self._guard_write()
        payload = struct.pack(">HHB", addr, len(values), 2 * len(values)) + struct.pack(">%dH" % len(values), *values)
        self._transact(0x10, payload, 8)

    def _guard_write(self):
        if not self.mock and not RM.VERIFIED:
            raise ModbusError("寄存器表还没按手册核对（rh56_register_map.VERIFIED = False）：真机只读，不发动作")

    # ---------- 业务层 ----------

    @staticmethod
    def to_raw(pct):
        pct = max(0.0, min(100.0, float(pct)))
        return int(round(RM.VALUE_MIN + (RM.VALUE_MAX - RM.VALUE_MIN) * pct / 100.0))

    def move(self, joints, speed=None):
        """joints = {关节: 开合度}，6 个关节必须给全（少一个就是动作表写漏了，宁可报错不乱猜）。"""
        missing = [j for j in RM.JOINT_ORDER if j not in joints]
        if missing:
            raise ModbusError("动作缺关节：%s" % "、".join(missing))
        if speed is not None:
            self.write_registers(RM.REG_SPEED_BASE, [self.to_raw(speed)] * len(RM.JOINT_ORDER))
        self.write_registers(RM.REG_TARGET_BASE, [self.to_raw(joints[j]) for j in RM.JOINT_ORDER])
        self.last = dict(joints)

    def set_force(self, pct):
        """力度上限（0~100），每个手指一样。"""
        self.write_registers(RM.REG_FORCE_BASE, [self.to_raw(pct)] * len(RM.JOINT_ORDER))

    def read_status(self):
        return self.read_registers(RM.REG_STATUS, 2)

    def status(self):
        return {"port": self.port, "id": self.slave, "mock": self.mock,
                "connected": self.mock or self.ser is not None, "error": self.error, "last": self.last}
