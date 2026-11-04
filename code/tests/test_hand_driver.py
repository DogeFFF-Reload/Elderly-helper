"""RH56F2 驱动 + 执行服务：EB 90 帧、寄存器换算、串口复用/重连、HTTP 层和主脑联调。

真机路径全部用 FakeSerial，不依赖 pyserial；HTTP 层起真的 ThreadingHTTPServer，
只有"急停作废排队"那条按需求 patch arm_service._execute。
"""
import json
import struct
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import events, make_bus

from hand_controller import arm_service, poses
from hand_controller import rh56_register_map
from hand_controller import rh56_register_map as RM
from hand_controller import rh56f2_driver
from hand_controller.arm_backend import MockArm
from hand_controller.rh56f2_driver import HandError, RH56F2, checksum, frame_read, frame_write, pack_shorts
from master_service import ArmClient


# ---------- 手册原文的帧（《RH56 用户手册 V1.09》§2.2） ----------
# 期望值一律用这里的常量核对，不用被测模块的 checksum 反推。

MANUAL_READ_REQ = bytes.fromhex("eb 90 01 04 11 0a 06 0c 32")
MANUAL_WRITE_REQ = bytes.fromhex(
    "eb 90 01 0f 12 ce 05 64 00 64 00 64 00 64 00 e8 03 00 00 70")
MANUAL_READ_RESP = bytes.fromhex(
    "90 eb 01 0f 11 0a 06 64 00 64 00 64 00 64 00 e8 03 00 00 ac")
MANUAL_WRITE_RESP = bytes.fromhex("90 eb 01 04 12 ce 05 01 eb")


def sum8(body):
    """测试自己算的校验：包头之后全部字节累加和 & 0xFF。"""
    return sum(body) & 0xFF


def make_read_response(hand_id, addr, data):
    """90 EB | ID | n+3 | 11 | 地址低 地址高 | 数据 n 字节 | 校验。"""
    data = bytes(data)
    body = bytes([hand_id, len(data) + 3, 0x11, addr & 0xFF, addr >> 8]) + data
    return b"\x90\xEB" + body + bytes([sum8(body)])


def make_write_ack(hand_id, addr, status=0x01):
    """90 EB | ID | 04 | 12 | 地址低 地址高 | 01 | 校验。"""
    body = bytes([hand_id, 0x04, 0x12, addr & 0xFF, addr >> 8, status])
    return b"\x90\xEB" + body + bytes([sum8(body)])


def default_responder(req, data=None):
    """按 EB 90 协议回包；读数据来自 data={地址: bytes}，没有就全 0。"""
    if len(req) < 8:
        return b""
    hand_id, addr = req[2], req[5] | req[6] << 8
    if req[4] == 0x11:
        n = req[7] if len(req) > 7 else 0
        payload = (data or {}).get(addr, b"")
        payload = (bytes(payload) + bytes(n))[:n]
        return make_read_response(hand_id, addr, payload)
    if req[4] == 0x12:
        return make_write_ack(hand_id, addr)
    return b""


# ---------- 假串口 / 假工厂 ----------

class FakeSerial:
    """只实现驱动用到的几个串口方法；read(n) 可以少于 n，模拟超时。"""

    def __init__(self, responder=None, echo=False, data=None):
        self.responder = responder
        self.echo = echo
        self.data = dict(data or {})
        self.written = []
        self.closed = False
        self._pending = bytearray()

    def write(self, b):
        b = bytes(b)
        self.written.append(b)
        if self.echo:
            self._pending.extend(b)          # USB-RS485 转换器把自己发的字节回显一遍
        if self.responder is not None:
            self._pending.extend(self.responder(b) or b"")
        else:
            self._pending.extend(default_responder(b, self.data))
        return len(b)

    def read(self, n):
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    def reset_input_buffer(self):
        self._pending.clear()

    def close(self):
        self.closed = True


class CountingFactory:
    """记录被调用次数，并按次数造 FakeSerial。"""

    def __init__(self, make=None):
        self.make = make or (lambda port, n: FakeSerial())
        self.calls = 0
        self.ports = []
        self.serials = []

    def __call__(self, port):
        self.calls += 1
        self.ports.append(port)
        serial = self.make(port, self.calls)
        self.serials.append(serial)
        return serial


def real_driver(port="FAKE1", slave=1, responder=None, factory=None, data=None, echo=False):
    factory = factory or CountingFactory(lambda p, n: FakeSerial(responder, echo=echo, data=data))
    return RH56F2(port, slave, mock=False, serial_factory=factory), factory


def mock_driver(slave=1):
    return RH56F2("", slave, mock=True, serial_factory=lambda port: (_ for _ in ()).throw(
        AssertionError("mock 模式不该开串口")))


def wait_until(predicate, timeout, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ---------- 驱动 ----------

class DriverTest(unittest.TestCase):
    def setUp(self):
        rh56f2_driver._PORTS.clear()

    def tearDown(self):
        rh56f2_driver._PORTS.clear()

    def assert_last_frame(self, ser, hand_id, addr, data):
        self.assertEqual(ser.written[-1], frame_write(hand_id, addr, data))

    # ---- 手册 golden ----

    def test_manual_golden_frames(self):
        self.assertEqual(frame_read(1, 1546, 12), MANUAL_READ_REQ)
        self.assertEqual(
            frame_write(1, 1486, pack_shorts([100, 100, 100, 100, 1000, 0])),
            MANUAL_WRITE_REQ)

        # 测试辅助函数本身也要和手册原文对得上
        self.assertEqual(
            make_read_response(1, 1546, pack_shorts([100, 100, 100, 100, 1000, 0])),
            MANUAL_READ_RESP)
        self.assertEqual(make_write_ack(1, 1486), MANUAL_WRITE_RESP)

        self.assertEqual(checksum(MANUAL_READ_RESP[2:-1]), MANUAL_READ_RESP[-1])
        self.assertEqual(checksum(MANUAL_WRITE_RESP[2:-1]), MANUAL_WRITE_RESP[-1])

    def test_manual_read_response_parses_angles(self):
        ser = FakeSerial(responder=lambda req: MANUAL_READ_RESP)
        d = RH56F2("FAKE1", 1, mock=False, serial_factory=lambda port: ser)
        self.assertEqual(d.read_angles(), {
            "pinky": 10.0, "ring": 10.0, "middle": 10.0,
            "index": 10.0, "thumb": 100.0, "thumb_rot": 0.0,
        })
        self.assertEqual(ser.written, [MANUAL_READ_REQ])

    # ---- mock ----

    def test_mock_never_touches_serial_and_tracks_angles(self):
        def boom(port):
            raise AssertionError("mock 模式不该调用 serial_factory")

        d = RH56F2("", 1, mock=True, serial_factory=boom)
        self.assertEqual(d.read_angles(), {j: 100.0 for j in RM.JOINT_ORDER})

        joints = poses.JOINTS["finger_fist"]
        d.move(joints)
        self.assertTrue(any(entry.startswith("TX ") for entry in d.log), list(d.log))
        self.assertEqual(d.last, joints)
        expected = {j: round(RH56F2.to_raw(joints[j]) * 100.0 / RM.VALUE_MAX, 1)
                    for j in RM.JOINT_ORDER}
        self.assertEqual(d.read_angles(), expected)

        before = d.read_angles()
        d.hold()
        self.assertEqual(d.read_angles(), before, "-1 表示保持，mock 角度不能变")

    # ---- 换算 ----

    def test_move_missing_joint_raises(self):
        d = mock_driver()
        joints = dict(poses.JOINTS["release"])
        joints.pop("pinky")
        with self.assertRaises(HandError):
            d.move(joints)

    def test_to_raw_range_and_midpoint(self):
        self.assertEqual(RH56F2.to_raw(-5), RM.VALUE_MIN)
        self.assertEqual(RH56F2.to_raw(150), RM.VALUE_MAX)
        self.assertEqual(RH56F2.to_raw(50), 500)

    # ---- VERIFIED 门 ----

    def test_unverified_real_hand_refuses_write_but_can_read(self):
        d, factory = real_driver()
        self.assertTrue(d.connect())
        with mock.patch.object(rh56_register_map, "VERIFIED", False):
            with self.assertRaises(HandError) as caught:
                d.move(poses.JOINTS["release"])
            self.assertIn("VERIFIED", str(caught.exception))
            self.assertEqual(factory.serials[0].written, [], "未核对时不能往总线写动作")
            self.assertEqual(len(d.read_angles()), len(RM.JOINT_ORDER))

    # ---- 真机写帧 ----

    def test_verified_move_frames(self):
        d, factory = real_driver()
        release = poses.JOINTS["release"]
        angle_frame = frame_write(
            1, RM.REG_ANGLE_SET,
            pack_shorts([RH56F2.to_raw(release[j]) for j in RM.JOINT_ORDER]))
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            d.move(release)
            ser = factory.serials[0]
            self.assertEqual(len(ser.written), 1)
            self.assertEqual(ser.written[-1], angle_frame)
            self.assertEqual(d.last, release)

            d.move(release, speed=40)
            self.assertEqual(len(ser.written), 3)
            speed_frame = frame_write(1, RM.REG_SPEED_SET, pack_shorts([400] * 6))
            self.assertEqual(ser.written[1], speed_frame)
            self.assertEqual(ser.written[2], angle_frame)

    def test_verified_set_force_frames(self):
        d, factory = real_driver()
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            d.set_force(300)
            ser = factory.serials[0]
            self.assert_last_frame(ser, 1, RM.REG_FORCE_SET, pack_shorts([300] * 6))

            d.set_force(5000)
            self.assertEqual(len(ser.written), 2)
            self.assert_last_frame(ser, 1, RM.REG_FORCE_SET, pack_shorts([1000] * 6))

    def test_verified_hold_frame(self):
        d, factory = real_driver()
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            d.hold()
        ser = factory.serials[0]
        self.assertEqual(len(ser.written), 1)
        self.assert_last_frame(ser, 1, RM.REG_ANGLE_SET, pack_shorts([-1] * 6))
        self.assertEqual(ser.written[-1][7:-1], b"\xFF" * 12)

    # ---- 写白名单 ----

    BAD_WRITES = (
        (1000, b"\x02"),     # HAND_ID
        (1005, b"\x01"),     # SAVE
        (1006, b"\x01"),     # RESET_PARA
        (1486, bytes(14)),   # 越过 ANGLE_SET 末尾
    )

    def test_write_whitelist_mock(self):
        d = mock_driver()
        for addr, data in self.BAD_WRITES:
            with self.subTest(addr=addr):
                with self.assertRaises(HandError):
                    d.write(addr, data)

    def test_write_whitelist_real_verified(self):
        d, factory = real_driver()
        self.assertTrue(d.connect())
        ser = factory.serials[0]
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            for addr, data in self.BAD_WRITES:
                with self.subTest(addr=addr):
                    with self.assertRaises(HandError):
                        d.write(addr, data)
        self.assertEqual(ser.written, [], "被白名单拒绝的写不能发到总线")

    # ---- 回包异常 ----

    def test_read_empty_response_reports_no_reply(self):
        d, _ = real_driver(responder=lambda req: b"")
        with self.assertRaises(HandError) as caught:
            d.read_angles()
        self.assertIn("没回应", str(caught.exception))

    def test_read_short_response_reports_incomplete(self):
        d, _ = real_driver(responder=lambda req: MANUAL_READ_RESP[:10])
        with self.assertRaises(HandError) as caught:
            d.read_angles()
        self.assertIn("不完整", str(caught.exception))

    def test_read_bad_header_reports_header(self):
        bad = b"\x00\x00" + MANUAL_READ_RESP[2:]
        d, _ = real_driver(responder=lambda req: bad)
        with self.assertRaises(HandError) as caught:
            d.read_angles()
        self.assertIn("包头", str(caught.exception))

    def test_read_bad_checksum_reports_checksum(self):
        bad = MANUAL_READ_RESP[:-1] + bytes([MANUAL_READ_RESP[-1] ^ 0xFF])
        d, _ = real_driver(responder=lambda req: bad)
        with self.assertRaises(HandError) as caught:
            d.read_angles()
        self.assertIn("校验和", str(caught.exception))

    def test_read_wrong_id_reports_mismatch(self):
        wrong_id = make_read_response(2, RM.REG_ANGLE_ACT, pack_shorts([100] * 6))
        d, _ = real_driver(slave=1, responder=lambda req: wrong_id)
        with self.assertRaises(HandError) as caught:
            d.read_angles()
        self.assertIn("对不上", str(caught.exception))

    def test_write_unconfirmed_reports_not_confirmed(self):
        def responder(req):
            if req[4] == 0x12:
                return make_write_ack(req[2], req[5] | req[6] << 8, status=0x00)
            return default_responder(req)

        d, _ = real_driver(responder=responder)
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            with self.assertRaises(HandError) as caught:
                d.move(poses.JOINTS["release"])
        self.assertIn("没被确认", str(caught.exception))

    # ---- 回显 ----

    def test_echo_read_angles(self):
        raw = [100, 200, 300, 400, 500, 600]
        d, factory = real_driver(echo=True, data={RM.REG_ANGLE_ACT: pack_shorts(raw)})
        expected = {j: round(v * 100.0 / RM.VALUE_MAX, 1)
                    for j, v in zip(RM.JOINT_ORDER, raw)}
        self.assertEqual(d.read_angles(), expected)
        self.assertEqual(factory.serials[0].written, [MANUAL_READ_REQ])

    # ---- 串口掉线 / 复用 / 连接 ----

    def test_write_oserror_drops_port_and_reopens(self):
        class BadWrite(FakeSerial):
            def write(self, data):
                raise OSError("线断了")

        factory = CountingFactory(lambda port, n: BadWrite() if n == 1 else FakeSerial())
        d = RH56F2("FAKE1", 1, mock=False, serial_factory=factory)
        with self.assertRaises(HandError):
            d.read_angles()
        self.assertIsNone(d.ser)
        self.assertTrue(d.error)
        self.assertNotIn("FAKE1", rh56f2_driver._PORTS)
        self.assertTrue(factory.serials[0].closed)

        self.assertEqual(len(d.read_angles()), len(RM.JOINT_ORDER))
        self.assertEqual(factory.calls, 2, "串口掉了之后下一次读要重新打开")

    def test_two_drivers_share_one_port(self):
        factory = CountingFactory()
        d1 = RH56F2("FAKE1", 1, mock=False, serial_factory=factory)
        d2 = RH56F2("FAKE1", 2, mock=False, serial_factory=factory)
        self.assertTrue(d1.connect())
        self.assertTrue(d2.connect())
        self.assertEqual(factory.calls, 1)
        self.assertIs(d1.ser, d2.ser)

    def test_connect_errors(self):
        d = RH56F2("", 1, mock=False, serial_factory=lambda port: FakeSerial())
        self.assertFalse(d.connect())
        self.assertIn("HAND", d.error)

        def boom(port):
            raise OSError("打不开")

        d2 = RH56F2("FAKE2", 1, mock=False, serial_factory=boom)
        self.assertFalse(d2.connect())
        self.assertIn("FAKE2", d2.error)


# ---------- 动作表 ----------

class PoseTableTest(unittest.TestCase):
    def test_every_pose_has_joints_or_rhythm(self):
        for name in poses.POSES:
            self.assertTrue(name in poses.JOINTS or name in poses.RHYTHMS or name in poses.SEQUENCES,
                            "动作 %s 不在 JOINTS / RHYTHMS / SEQUENCES 里" % name)

    def test_sequence_steps_are_complete_and_in_range(self):
        # kit 词表的挥手、点赞、握张、数一到五：每一步六个关节齐全、0..100、停顿 0~2 秒
        for name, steps in poses.SEQUENCES.items():
            self.assertTrue(steps, name)
            for joints, hold in steps:
                self.assertTrue(0 < hold <= 2, (name, hold))
                for joint in RM.JOINT_ORDER:
                    self.assertTrue(0 <= joints[joint] <= 100, (name, joint, joints[joint]))

    def test_joint_sets_are_complete_and_in_range(self):
        for name, joints in poses.JOINTS.items():
            for joint in RM.JOINT_ORDER:
                self.assertIn(joint, joints, "%s 缺关节 %s" % (name, joint))
                self.assertTrue(0 <= joints[joint] <= 100,
                                "%s.%s = %s 超出 0..100" % (name, joint, joints[joint]))

    def test_gentle_hold_does_not_close_all_the_way(self):
        rhythm = poses.RHYTHMS["gentle_hold"]
        self.assertLessEqual(rhythm["max_seconds"], 120)
        for step_no, (joints, _hold) in enumerate(rhythm["steps"]):
            for joint in RM.JOINT_ORDER:
                self.assertIn(joint, joints)
                self.assertGreaterEqual(joints[joint], poses.GENTLE_MIN_OPEN,
                                        "gentle_hold 第 %d 步 %s 握得太紧" % (step_no, joint))

    def test_gentle_hold_force_in_grams(self):
        force = poses.RHYTHMS["gentle_hold"]["force"]
        self.assertGreaterEqual(force, 1)
        self.assertLessEqual(force, 500)


# ---------- 执行服务 ----------

def start_arm():
    """照 test_arm_link.start_arm，但不 patch _execute。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), arm_service.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
    return "http://127.0.0.1:%d" % server.server_address[1], stop


def http_post(url, path):
    req = urllib.request.Request(url + path, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def http_get(url, path):
    try:
        with urllib.request.urlopen(url + path, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def fresh_hands(d1, d2):
    arm_service._HANDS.clear()
    arm_service._HANDS.update({1: arm_service.HandWorker(1, d1), 2: arm_service.HandWorker(2, d2)})


class ServiceTest(unittest.TestCase):
    def setUp(self):
        rh56f2_driver._PORTS.clear()
        arm_service._HANDS.clear()
        arm_service.HISTORY.clear()
        arm_service.use_arm(arm_service.ArmWorker(MockArm(time_scale=0)))
        self.url, stop = start_arm()
        self.addCleanup(stop)

    def tearDown(self):
        arm_service.arm().stop()
        arm_service.arm().wait_idle(1.0)
        current = list(arm_service._HANDS.values())
        for w in current:
            w.stop()
        wait_until(lambda: all(not w.status()["busy"] for w in current), timeout=1.0)
        arm_service._HANDS.clear()
        arm_service.HISTORY.clear()
        rh56f2_driver._PORTS.clear()

    def test_pose_finger_fist(self):
        d1, d2 = mock_driver(1), mock_driver(2)
        fresh_hands(d1, d2)
        code, body = http_post(self.url, "/pose?name=finger_fist&hand=1")
        self.assertEqual(code, 200, body)
        arm_service.hands()[1].drain()
        self.assertEqual(d1.last, poses.JOINTS["finger_fist"])
        self.assertEqual(arm_service.HISTORY[-1]["pose"], "finger_fist")

    def test_gentle_hold_interrupted_by_release(self):
        d1, d2 = mock_driver(1), mock_driver(2)
        fresh_hands(d1, d2)
        started = time.monotonic()
        code, body = http_post(self.url, "/pose?name=gentle_hold&hand=2")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 200, body)
        self.assertLess(elapsed, 0.5, "gentle_hold 的 POST 必须马上回 200")

        self.assertTrue(wait_until(lambda: arm_service.hands()[2].status()["busy"], timeout=0.3),
                        "gentle_hold 没开始跑")
        code, body = http_post(self.url, "/pose?name=release&hand=2")
        self.assertEqual(code, 200, body)

        def done():
            st = arm_service.hands()[2].status()
            return not st["busy"] and st["queued"] == 0 and d2.last == poses.JOINTS["release"]

        self.assertTrue(wait_until(done, timeout=2.0),
                        "release 没在 2 秒内打断节拍：%s" % (arm_service.hands()[2].status(),))

    def test_stop_discards_queued_actions(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_execute(pose, hand):
            calls.append((pose, hand))
            if len(calls) == 1:
                started.set()
                release.wait(2.0)

        with mock.patch.object(arm_service, "_execute", fake_execute):
            w = arm_service.hands()[1]
            w.submit("finger_fist")
            try:
                self.assertTrue(started.wait(1.0), "第一条动作没开始")
                w.submit("finger_open")
                w.stop()
            finally:
                release.set()
            w.drain()
        self.assertEqual(calls, [("finger_fist", 1)])

    def test_bad_pose_or_hand_rejected(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        for path in ("/pose?name=fly&hand=1",
                     "/pose?name=release&hand=3",
                     "/pose?name=release&hand=abc"):
            code, body = http_post(self.url, path)
            self.assertEqual(code, 400, (path, body))
            self.assertFalse(body["ok"])

    def test_unverified_real_hand_returns_503(self):
        factory = CountingFactory()
        fresh_hands(mock_driver(1),
                    RH56F2("FAKE1", 2, mock=False, serial_factory=factory))
        with mock.patch.object(rh56_register_map, "VERIFIED", False):
            code, body = http_post(self.url, "/pose?name=release&hand=2")
        self.assertEqual(code, 503, body)
        self.assertIn("VERIFIED", body["error"])
        self.assertFalse([h for h in arm_service.HISTORY if h["pose"] == "release"])

    def test_real_hand_without_port_returns_503(self):
        fresh_hands(mock_driver(1), RH56F2("", 2, mock=False))
        code, body = http_post(self.url, "/pose?name=release&hand=2")
        self.assertEqual(code, 503, body)
        self.assertIn("没配串口", body["error"])

    def test_status_shape(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        code, body = http_get(self.url, "/status")
        self.assertEqual(code, 200, body)
        self.assertIn("verified", body)
        self.assertEqual(set(body["hands"]), {"1", "2"})
        for hand in ("1", "2"):
            for field in ("port", "id", "mock", "busy", "done"):
                self.assertIn(field, body["hands"][hand], "手#%s 状态缺 %s" % (hand, field))

    def test_stop_endpoint(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        code, body = http_post(self.url, "/stop")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["stopped"])
        self.assertEqual(arm_service.HISTORY[-1]["pose"], "stop")

    def test_stop_interrupts_gentle_hold(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        code, _ = http_post(self.url, "/pose?name=gentle_hold&hand=2")
        self.assertEqual(code, 200)
        self.assertTrue(wait_until(lambda: arm_service.hands()[2].status()["busy"], timeout=0.3))
        code, body = http_post(self.url, "/stop")
        self.assertEqual(code, 200, body)
        self.assertTrue(wait_until(lambda: not arm_service.hands()[2].status()["busy"], timeout=1.0),
                        "急停后手#2 还在忙")

    def test_end_to_end_with_real_arm_service(self):
        d1, d2 = mock_driver(1), mock_driver(2)
        fresh_hands(d1, d2)
        bus, clock, _ = make_bus(arm=ArmClient(self.url), arm_url=self.url)
        bus.fall("卧室")
        clock.advance(6, bus)
        bus.family_ack("daughter")
        bus.arm.drain()
        self.assertTrue(arm_service.arm().wait_idle(3.0), arm_service.arm().status())
        for w in arm_service.hands().values():
            w.drain()

        def idle():
            return all(not w.status()["busy"] and w.status()["queued"] == 0
                       for w in arm_service.hands().values())

        self.assertTrue(wait_until(idle, timeout=3.0),
                        {h: w.status() for h, w in arm_service.hands().items()})
        self.assertEqual(d1.last, poses.JOINTS["sign_family_received"])
        self.assertEqual(d2.last, poses.JOINTS["sign_coming"],
                         "轻握节拍应被举牌打断，最后停在 sign_coming")
        self.assertFalse([e for e in events(bus, "system") if e["level"] == "danger"])

    def test_probe(self):
        fresh_hands(mock_driver(1), mock_driver(2))
        lines = []
        self.assertTrue(arm_service.probe(out=lines.append))
        self.assertEqual(len(lines), 3)
        for line in lines[:2]:
            self.assertIn("实际角度", line)
        self.assertIn("关节角", lines[2])

        fresh_hands(mock_driver(1), RH56F2("", 2, mock=False))
        lines = []
        self.assertFalse(arm_service.probe(out=lines.append))
        self.assertEqual(len(lines), 3)
        self.assertTrue(any("读不到" in line for line in lines), lines)

    # ---- 急停写真机 ----

    def test_stop_sends_hold_to_verified_real_hand(self):
        factory = CountingFactory()
        fresh_hands(mock_driver(1),
                    RH56F2("FAKE1", 2, mock=False, serial_factory=factory))
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            code, body = http_post(self.url, "/stop")
        self.assertEqual(code, 200, body)
        ser = factory.serials[0]
        self.assertEqual(ser.written[-1],
                         frame_write(2, RM.REG_ANGLE_SET, pack_shorts([-1] * 6)))

    def test_stop_does_not_write_to_unverified_real_hand(self):
        factory = CountingFactory()
        d2 = RH56F2("FAKE1", 2, mock=False, serial_factory=factory)
        fresh_hands(mock_driver(1), d2)
        self.assertTrue(d2.connect())
        ser = factory.serials[0]
        with mock.patch.object(rh56_register_map, "VERIFIED", False):
            code, body = http_post(self.url, "/stop")
        self.assertEqual(code, 200, body)
        self.assertEqual(ser.written, [], "未核对寄存器表时急停也不能写总线")


if __name__ == "__main__":
    unittest.main()


class ReviewFixTest(unittest.TestCase):
    """代码审查修的几处：写帧回显、-1 角度、后台失败上报、急停叫臂停。"""

    def setUp(self):
        rh56f2_driver._PORTS.clear()
        arm_service._HANDS.clear()
        arm_service.HISTORY.clear()

    def tearDown(self):
        arm_service._HANDS.clear()
        arm_service.HISTORY.clear()
        rh56f2_driver._PORTS.clear()

    def test_write_survives_echoing_adapter(self):
        # 写请求 20 字节、写回包 9 字节：回显的转换器下也要能拿到回包
        d, factory = real_driver(echo=True)
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            d.move(poses.JOINTS["release"])
        self.assertEqual(d.last, poses.JOINTS["release"])

    def test_invalid_angle_reads_as_none(self):
        d, _ = real_driver(data={rh56_register_map.REG_ANGLE_ACT: pack_shorts([-1, 500, -1, 1000, 0, -1])})
        angles = d.read_angles()
        self.assertIsNone(angles[rh56_register_map.JOINT_ORDER[0]])
        self.assertEqual(angles[rh56_register_map.JOINT_ORDER[1]], 50.0)
        self.assertEqual(angles[rh56_register_map.JOINT_ORDER[3]], 100.0)

    def test_background_failure_is_counted_in_status(self):
        w = arm_service.HandWorker(1, mock_driver(1))
        with mock.patch.object(arm_service, "_execute", side_effect=RuntimeError("串口断了")):
            w.submit("press_sos")
            w.drain()
        st = w.status()
        self.assertEqual((st["fail_seq"], st["fail_pose"]), (1, "press_sos"))
        self.assertIn("串口断了", st["last_error"])

    def test_master_reports_background_failure_once(self):
        from master_service import ArmClient
        arm = ArmClient("http://127.0.0.1:9")      # 不探测、不发动作，只喂 /status 的回包
        got = []
        arm.on_result = lambda *a: got.append(a)
        body = lambda seq: json.dumps({"mock": False, "hands": {"1": {"fail_seq": seq, "fail_pose": "press_sos",
                                                                       "last_error": "press_sos：串口断了"}}}).encode()
        arm._note_status(body(3))                   # 第一次见：只记基线，不补报旧账
        arm._note_status(body(3))
        self.assertEqual(got, [])
        arm._note_status(body(4))
        self.assertEqual(got, [("press_sos", 1, False, "failed", "press_sos：串口断了")])
        self.assertIs(arm.status()["mock"], False)

    def test_bus_alerts_on_reported_failure(self):
        bus, clock, arm = make_bus()
        bus.fall("卧室")
        clock.advance(6, bus)
        bus._arm_result("press_sos", 1, False, "failed", "press_sos：串口断了")
        danger = [e["text"] for e in events(bus, "system") if e["level"] == "danger"]
        self.assertTrue(any("做到一半出错" in t for t in danger), danger)
        self.assertEqual(bus.snapshot()["hands"]["1"]["status"], "failed")

    def test_arm_background_failure_is_reported_as_hand1(self):
        # 臂线程做到一半出错（SDK 断了）：/status 的 arm 带 fail_seq，主脑按手#1 补报；被打断、急停不算出错
        a = arm_service.ArmWorker(MockArm(time_scale=0))
        with mock.patch.object(a, "_run_skill", side_effect=RuntimeError("SDK 断了")):
            self.assertEqual(a.request("deliver_medicine"), (200, ""))
            self.assertTrue(a.wait_idle(3.0))
        st = a.status()
        self.assertEqual((st["fail_seq"], st["fail_pose"]), (1, "deliver_medicine"))
        from master_service import ArmClient
        client = ArmClient("http://127.0.0.1:9")
        got = []
        client.on_result = lambda *x: got.append(x)
        body = lambda seq: json.dumps({"mock": True, "hands": {}, "arm": {
            "fail_seq": seq, "fail_pose": "deliver_medicine", "last_error": "deliver_medicine：SDK 断了"}}).encode()
        client._note_status(body(1))
        client._note_status(body(2))
        self.assertEqual(got, [("deliver_medicine", 1, False, "failed", "deliver_medicine：SDK 断了")])
