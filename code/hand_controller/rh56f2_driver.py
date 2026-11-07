"""RH56 灵巧手驱动：手册 §2.2 的因时自有 RS485 协议（包头 EB 90）。真机才要 pyserial。

  读：EB 90 | ID | 04        | 11 | 地址低 地址高 | 字节数   | 校验
  回：90 EB | ID | 字节数+3  | 11 | 地址低 地址高 | 数据…    | 校验
  写：EB 90 | ID | 字节数+3  | 12 | 地址低 地址高 | 数据…    | 校验
  回：90 EB | ID | 04        | 12 | 地址低 地址高 | 01       | 校验
  校验 = 去掉两个包头字节后其余字节累加和的低 8 位；short 低字节在前、有符号（-1 = 该指不动）。

地址和关节顺序见 rh56_register_map。安全上的几道门：
- mock（默认，MOCK_SERIAL 不为 0）只记帧不碰串口；读角度返回模拟值
- 真机模式下 rh56_register_map.VERIFIED 为 False 时只读不写
- 任何模式都只许写 WRITABLE 里的寄存器（角度 / 力度 / 速度），改 ID、波特率、存 FLASH、恢复出厂一律拒绝
- 任何模式都只许读 [0, READ_LIMIT) 里的地址：F2 读到 4032 起就卡死
- 手一次没回话就不再往它发任何帧（不自动重试）：F2 没回话时接着发，断电前都恢复不了。
  按 24V + 蓝盒的顺序断电重启后，重启 arm_service（或调 clear_silent()）才重新收发
"""
import collections
import os
import struct
import threading

from hand_controller import rh56_register_map as RM

HEAD_TX = b"\xEB\x90"
HEAD_RX = b"\x90\xEB"
CMD_READ, CMD_WRITE = 0x11, 0x12


def mock_from_env():
    """MOCK_SERIAL=0 才走真串口；不设或其他值都是 mock。"""
    return os.environ.get("MOCK_SERIAL", "1") != "0"


def checksum(body):
    """body = 包头之后的全部字节（不含校验本身）。"""
    return sum(body) & 0xFF


def frame_read(hand_id, addr, nbytes):
    body = bytes([hand_id, 0x04, CMD_READ, addr & 0xFF, addr >> 8, nbytes])
    return HEAD_TX + body + bytes([checksum(body)])


def frame_write(hand_id, addr, data):
    body = bytes([hand_id, len(data) + 3, CMD_WRITE, addr & 0xFF, addr >> 8]) + bytes(data)
    return HEAD_TX + body + bytes([checksum(body)])


def pack_shorts(values):
    return struct.pack("<%dh" % len(values), *values)


class HandError(Exception):
    pass


POWER_CYCLE = "按顺序断电重启——24V 和蓝盒 USB 都断开 → 先插蓝盒 USB → 再插 24V → 等 10 秒以上，然后重启 arm_service"


def _pyserial(port):
    import serial  # 真机才需要：pip install pyserial
    return serial.Serial(port, RM.BAUD, parity=RM.PARITY, stopbits=RM.STOPBITS, timeout=RM.TIMEOUT)


_PORTS = {}            # 串口名 → [serial, lock]：多只手同一条总线（不同 ID）时共用
_PORTS_MU = threading.Lock()


class RH56F2:
    def __init__(self, port="", hand_id=RM.SLAVE_ID_HAND1, mock=None, serial_factory=None):
        self.port = port
        self.slave = hand_id
        self.mock = mock_from_env() if mock is None else mock
        self.factory = serial_factory or _pyserial
        self.ser = None
        self.lock = None
        self.error = None
        self.silent = None                        # 手没回过话：记下原因，之后一帧都不发，等断电重启
        self.last = None                          # 最近一次下发的开合度
        self.sim_angles = [RM.VALUE_MAX] * 6      # mock 下的"实际角度"：开机张开，move 后跟着变
        self.log = collections.deque(maxlen=50)   # 最近的收发帧，排错用

    # ---------- 串口 ----------

    def connect(self):
        """真机：打开串口，失败只记在 error 里不抛（USB 后插也能重试）；mock 总是可用。"""
        if self.mock or self.ser is not None:
            return True
        if not self.port:
            self.error = "没配串口（环境变量 HAND<n>_PORT，如 COM7 或 /dev/ttyUSB0）"
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

    # ---------- 收发 ----------

    def clear_silent(self):
        """手已经按顺序断电重启过：允许重新收发。"""
        self.silent = None

    def _transact(self, req, expect_len):
        if self.silent:
            raise HandError(self.silent)
        self.log.append("TX " + req.hex(" "))
        if not self.connect():
            raise HandError(self.error)
        try:
            with self.lock:
                self.ser.reset_input_buffer()
                self.ser.write(req)
                resp = self.ser.read(expect_len)
                if resp[:2] == HEAD_TX:       # 有的 USB-RS485 转换器会把自己发的字节回显一遍：读完回显再取回包
                    resp = (resp + self.ser.read(len(req) + expect_len - len(resp)))[len(req):]
        except Exception as e:  # noqa: BLE001 串口层出错 = 线断了
            self._drop("串口 %s 出错：%s" % (self.port, e))
            raise HandError(self.error)
        self.log.append("RX " + resp.hex(" "))
        return resp

    def _check(self, resp, cmd, addr, expect_len):
        if not resp:
            self.silent = ("手 %d 没回应，已停止再发指令（F2 没回话时接着发会一直卡住）：%s。"
                           "还不回就查 24V、A/B 线、手的 ID、波特率，以及型号是不是 RS485 版（E2）" % (self.slave, POWER_CYCLE))
            raise HandError(self.silent)
        if len(resp) < expect_len:
            raise HandError("手 %d 回包不完整（%d/%d 字节）：%s" % (self.slave, len(resp), expect_len, resp.hex(" ")))
        resp = resp[:expect_len]
        if resp[:2] != HEAD_RX:
            raise HandError("回包包头不是 90 EB：%s（协议不对？）" % resp.hex(" "))
        if checksum(resp[2:-1]) != resp[-1]:
            raise HandError("回包校验和不对：%s" % resp.hex(" "))
        if resp[2] != self.slave or resp[4] != cmd or resp[5] | resp[6] << 8 != addr:
            raise HandError("回包对不上（ID / 命令 / 地址）：%s" % resp.hex(" "))
        return resp

    def read(self, addr, nbytes):
        """读 nbytes 个字节，返回数据部分。"""
        if addr < 0 or nbytes <= 0 or addr + nbytes > RM.READ_LIMIT:
            raise HandError("不许读地址 %d 起 %d 字节：只许读 0~%d（F2 读到 %d 起会卡死，要断电重启）"
                            % (addr, nbytes, RM.READ_LIMIT - 1, RM.READ_LIMIT))
        req = frame_read(self.slave, addr, nbytes)
        if self.mock:
            self.log.append("TX " + req.hex(" "))
            return self._sim_read(addr, nbytes)
        expect = 8 + nbytes
        resp = self._check(self._transact(req, expect), CMD_READ, addr, expect)
        if resp[3] != nbytes + 3:
            raise HandError("回包长度字段不对：%s" % resp.hex(" "))
        return resp[7:7 + nbytes]

    def write(self, addr, data):
        if not any(lo <= addr and addr + len(data) <= hi for lo, hi in RM.WRITABLE):
            raise HandError("地址 %d 起 %d 字节不在允许写的范围（只许写角度 / 力度 / 速度）" % (addr, len(data)))
        if not self.mock and not RM.VERIFIED:
            raise HandError("寄存器表还没确认（rh56_register_map.VERIFIED = False）：真机只读，不发动作")
        req = frame_write(self.slave, addr, data)
        if self.mock:
            self.log.append("TX " + req.hex(" "))
            if addr == RM.REG_ANGLE_SET:
                self.sim_angles = [v if v >= 0 else old for v, old in
                                   zip(struct.unpack("<%dh" % (len(data) // 2), data), self.sim_angles)]
            return
        resp = self._check(self._transact(req, 9), CMD_WRITE, addr, 9)
        if resp[3] != 0x04 or resp[7] != 0x01:
            raise HandError("写入没被确认：%s" % resp.hex(" "))

    def _sim_read(self, addr, nbytes):
        if addr == RM.REG_ANGLE_ACT and nbytes == 12:
            return pack_shorts(self.sim_angles)
        return bytes(nbytes)

    def read_shorts(self, addr, count):
        return list(struct.unpack("<%dh" % count, self.read(addr, 2 * count)))

    def write_shorts(self, addr, values):
        self.write(addr, pack_shorts(values))

    # ---------- 业务层 ----------

    @staticmethod
    def to_raw(pct):
        """开合度 0（握紧）~100（张开）→ 角度 0~1000。"""
        pct = max(0.0, min(100.0, float(pct)))
        return int(round(RM.VALUE_MIN + (RM.VALUE_MAX - RM.VALUE_MIN) * pct / 100.0))

    def move(self, joints, speed=None):
        """joints = {关节: 开合度}，6 个关节必须给全（少一个就是动作表写漏了，宁可报错不乱猜）。"""
        missing = [j for j in RM.JOINT_ORDER if j not in joints]
        if missing:
            raise HandError("动作缺关节：%s" % "、".join(missing))
        if speed is not None:
            self.write_shorts(RM.REG_SPEED_SET, [self.to_raw(speed)] * 6)
        self.write_shorts(RM.REG_ANGLE_SET, [self.to_raw(joints[j]) for j in RM.JOINT_ORDER])
        self.last = dict(joints)

    def set_force(self, grams):
        """每根手指的力控阈值（g，0~1000）：受力到这个值手指就停。"""
        g = int(max(0, min(RM.FORCE_MAX, grams)))
        self.write_shorts(RM.REG_FORCE_SET, [g] * 6)

    def hold(self):
        """六个角度设置值写 -1：手册说"该指无反应，保持当前实际位置"。急停时用。"""
        self.write_shorts(RM.REG_ANGLE_SET, [-1] * 6)

    def read_angles(self):
        """读 ANGLE_ACT，返回 {关节: 开合度 0~100}；读回 -1（0xFFFF，没有值）的关节给 None。只读，探测就用它。"""
        raw = self.read_shorts(RM.REG_ANGLE_ACT, 6)
        return {j: None if v < 0 else round(v * 100.0 / RM.VALUE_MAX, 1) for j, v in zip(RM.JOINT_ORDER, raw)}

    def status(self):
        return {"port": self.port, "id": self.slave, "mock": self.mock,
                "connected": self.mock or self.ser is not None, "error": self.silent or self.error,
                "silent": bool(self.silent), "last": self.last}
