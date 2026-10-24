"""RH56F2 真机驱动 —— EB90 串口协议 + F2 实测寄存器图（2026-10 真机破译版）。

帧格式同 V1.09 手册：EB 90 ID LEN CMD ADDR_L ADDR_H DATA.. CK（CK=sum(byte[2]..末数据)&0xFF）

F2 实测核心差异（相对旧 DFX 手册，均经真机目击/轨迹采样验证）：
    运动目标组 REG_POS_SET 0x040A 组写 12B：6 short，0=全开 2000=全握
              （实测写 2000 被拒收，驱动统一封顶 1995）
    实际位置组 REG_POS_ACT 0x0422 组读 12B：同量纲，伺服收敛误差 ±4（0.2% 满量程）
    极性（真机目击锁定）：ACT 高 = 握紧（~1650 ≈ 八成握），ACT 低 = 张开
    速度：未写速度参数时全程约 0.5s/840 单位，演示手感良好，默认不碰速度组
    ⚠ 部分地址的【单寄存器(2字节)读写是别名寄存器】（例：单读 0x0410 返回的是
      另一量纲的值），因此本驱动只用组写/组读，不做单寄存器操作。
历史上会"收下但不动"的影子面（勿再使用）：
    0x05C2 旧 POS_SET / 0x05CE ANGLE_SET —— 写入 ACK、可读回、不触发运动。

对外接口与旧驱动同形：move / open_hand / close_hand / status / pose_raw，
arm_service 在 RH56_PROTO=EB90 时换用本驱动，其余代码零改动。

保持位语义：姿态只给部分关节时，其余关节保持"当前目标值"（组读 0x040A 得到），
不使用 -1（F2 真实运动组对 -1 行为未验证，避免风险）。
"""
from __future__ import annotations
import os
import struct
import time

MOCK = os.environ.get("MOCK_SERIAL", "1") != "0"

REG_POS_SET = 0x040A     # 组写 12B：六个自由度执行器位置目标，0=全开 2000=全握
REG_POS_ACT = 0x0422     # 组读 12B：实际位置，同量纲
REG_FORCE_SET = 0x05DA   # 组写 12B：力控阈值（克）

RAW_MAX = 1995           # 2000 整值实测拒收，封顶

# poses 关节名 → 运动组下标（真机验证：小指 0 / 无名 1 / 中指 2 / 食指 3 / 拇弯 4 / 拇转 5）
JOINT_INDEX = {"pinky": 0, "ring": 1, "middle": 2,
               "index": 3, "thumb": 4, "thumb_rot": 5}
EB90_ORDER = ["pinky", "ring", "middle", "index", "thumb", "thumb_rot"]


def _ck(body: bytes) -> int:
    return sum(body) & 0xFF


class RH56F2:
    """EB90 协议灵巧手。port 形如 COM6；slave_id = Hands_ID（默认 1）。"""

    def __init__(self, port: str = "COM6", slave_id: int = 1):
        self.port = port
        self.slave = slave_id
        self.ser = None
        self.log: list[str] = []
        self.force = int(os.environ.get("RH56_FORCE", "500"))   # 克
        self.last_ack: bytes = b""
        self._last_raw: list[int] | None = None   # 最近写进运动组的 6 个目标
        if not MOCK:
            self._open()
            self._sync()

    def _open(self):
        import serial
        self.ser = serial.Serial(self.port, 115200, parity="N",
                                 stopbits=1, bytesize=8, timeout=0.05)

    def _reconnect(self):
        """USB 瞬断/句柄失效后自救：关闭旧句柄→重开→重学目标位。"""
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        time.sleep(0.4)
        for i in range(3):
            try:
                self._open()
                self.log.append("port_reconnect_ok")
                self._sync(force_only=True)
                return
            except Exception as e:
                self.log.append("reopen_fail[%d]: %s" % (i, e))
                time.sleep(0.8)

    # ---------- 底层 ----------
    def _send_frame(self, frame: bytes, min_len: int, window: float = 0.3) -> bytes:
        """发帧并收集应答；写失败 → 自愈重连后重试一次。返回已收字节。"""
        buf = b""
        for attempt in (0, 1):
            try:
                self.ser.write(frame)
                t0 = time.perf_counter()
                buf = b""
                while time.perf_counter() - t0 < window:
                    if self.ser.in_waiting:
                        buf += self.ser.read(self.ser.in_waiting)
                        if len(buf) >= min_len:
                            break
                    else:
                        time.sleep(0.002)
                break
            except Exception as e:
                self.log.append("port_err[%d]: %r" % (attempt, e))
                self._reconnect()
        return buf

    def _tx(self, cmd: int, addr: int, data: bytes) -> bytes:
        """组写。应答 9 字节：90 EB ID 04 12 AL AH [写入字节数] CK。"""
        b = bytes([self.slave, len(data) + 3, cmd,
                   addr & 0xFF, (addr >> 8) & 0xFF]) + data
        frame = b"\xEB\x90" + b + bytes([_ck(b)])
        self.log.append("TX " + frame.hex(" "))
        if MOCK or self.ser is None:
            self.last_ack = b"\x90\xEB" + bytes([self.slave, 4, cmd,
                                                 addr & 0xFF, (addr >> 8) & 0xFF,
                                                 len(data), 0])
            return self.last_ack
        buf = self._send_frame(frame, 9)
        self.log.append("RX " + buf.hex(" ") if buf else "RX (空)")
        self.last_ack = buf
        return buf

    def _rx_read(self, addr: int, n: int) -> list[int] | None:
        """组读 n 字节（带帧校验：长度/cmd/地址回显/和校验），失败重试一次。"""
        if MOCK:
            return [500] * (n // 2)
        c = bytes([self.slave, 0x04, 0x11, addr & 0xFF, (addr >> 8) & 0xFF, n])
        frame = b"\xEB\x90" + c + bytes([_ck(c)])
        self.log.append("TXR " + frame.hex(" "))
        for _ in range(2):
            buf = self._send_frame(frame, 6 + n)
            j = 0
            while True:
                i = buf.find(b"\x90\xEB", j)
                if i < 0:
                    break
                end = i + 7 + n
                if (end <= len(buf)
                        and buf[i + 3] == n + 3
                        and buf[i + 4] == 0x11
                        and buf[i + 5] == (addr & 0xFF)
                        and buf[i + 6] == ((addr >> 8) & 0xFF)
                        and _ck(buf[i + 2:end]) == buf[end]):
                    vals = list(struct.unpack("<%dh" % (n // 2), buf[i + 7:i + 7 + n]))
                    self.log.append("RXR " + buf[i:end + 1].hex(" "))
                    return vals
                j = i + 1
            time.sleep(0.03)
        self.log.append("read_fail @0x%04X n=%d" % (addr, n))
        return None

    def _sync(self, force_only: bool = False):
        """启动/重连：写入力控阈值；组读当前运动目标作为保持基线。"""
        fc = struct.pack("<6H", *([self.force] * 6))
        self._tx(0x12, REG_FORCE_SET, fc)
        if force_only:
            return
        sets = self._rx_read(REG_POS_SET, 12)
        if sets and all(-1 <= v <= 2000 for v in sets):
            self._last_raw = [max(0, min(RAW_MAX, v)) for v in sets]
        else:
            self._last_raw = None
            self.log.append("sync_no_sets")

    @staticmethod
    def _ack_ok(buf: bytes, addr: int, nbytes: int = 1) -> bool:
        # 写 ACK: 90 EB ID 04 12 AL AH [写入字节数] CK
        return (len(buf) >= 9 and buf[0] == 0x90 and buf[1] == 0xEB
                and buf[4] == 0x12 and buf[5] == (addr & 0xFF)
                and buf[7] == nbytes)

    # ---------- 数据换算 ----------
    @staticmethod
    def _to_raw(pct: float) -> int:
        """开合度 0~100（0=全握 100=全开）→ 执行器位置 0~1995（0=全开 2000=全握）。"""
        pct = max(0.0, min(100.0, float(pct)))
        raw = int(round((100.0 - pct) * 20))
        return max(0, min(RAW_MAX, raw))

    @staticmethod
    def _from_raw(raw: int) -> float:
        raw = max(0, min(2000, raw))
        return round(100 - raw / 20.0, 1)

    # ---------- 业务层（与旧驱动同形） ----------
    def move(self, joints: dict[str, float], speed: float = 50):
        """按关节名写运动组；未出现的关节保持当前目标。"""
        self.log.append("[%d] move %s" % (self.slave, joints))
        raw = self._last_raw
        if raw is None:
            cur = self._rx_read(REG_POS_SET, 12)
            raw = [max(0, min(RAW_MAX, v)) for v in cur] if cur else [RAW_MAX // 2] * 6
        else:
            raw = list(raw)
        for name, pct in joints.items():
            idx = JOINT_INDEX.get(name)
            if idx is None or pct is None or pct < 0:
                continue          # 未知关节 / 负值 → 保持
            raw[idx] = self._to_raw(pct)
        self._tx(0x12, REG_POS_SET, struct.pack("<6h", *raw))
        self._last_raw = raw

    def pose_raw(self, joints: dict[str, float]):
        self.move(joints)

    def open_hand(self, speed: float = 60):
        self.move({j: 95 for j in EB90_ORDER}, speed)

    def close_hand(self, speed: float = 60):
        self.move({j: 8 for j in EB90_ORDER}, speed)

    def status(self) -> dict:
        act = None
        if not MOCK and self.ser is not None:
            act = self._rx_read(REG_POS_ACT, 12)
        if act and all(isinstance(v, int) and 0 <= v <= 2000 for v in act):
            ok = self._ack_ok(self.last_ack, REG_POS_SET, 12) if self.last_ack else None
            pct = {name: self._from_raw(v) for name, v in zip(EB90_ORDER, act)}
            return {"slave": self.slave, "mock": MOCK, "proto": "EB90",
                    "last_ack_ok": ok,
                    "last_ack": (self.last_ack.hex(" ") if self.last_ack else ""),
                    "act_raw": act, "act_pct": pct}
        ok = self._ack_ok(self.last_ack, REG_POS_SET, 12) if self.last_ack else None
        return {"slave": self.slave, "mock": MOCK, "proto": "EB90",
                "last_ack_ok": ok,
                "last_ack": (self.last_ack.hex(" ") if self.last_ack else "")}