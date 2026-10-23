"""RH56F2 驱动 + 执行服务：Modbus 帧、寄存器换算、串口复用/重连、HTTP 层和主脑联调。

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
from hand_controller.rh56f2_driver import ModbusError, RH56F2, crc16, frame
from master_service import ArmClient


# ---------- 假串口 / 假工厂 ----------

def default_responder(req):
    """FC03 回 count 个 0x0001；FC06/FC16 回 8 字节"回显"帧（从站、功能码、地址、数量/值、CRC）。"""
    if len(req) < 6:
        return b""
    slave, func = req[0], req[1]
    if func == 0x03:
        count = struct.unpack(">H", req[4:6])[0]
        data = b"".join(struct.pack(">H", 1) for _ in range(count))
        body = bytes([slave, func, 2 * count]) + data
        return body + struct.pack("<H", crc16(body))
    if func in (0x06, 0x10):
        body = req[:6]
        return body + struct.pack("<H", crc16(body))
    return b""


class FakeSerial:
    """只实现驱动用到的几个串口方法；read(n) 可以少于 n，模拟超时。"""

    def __init__(self, responder=None):
        self.responder = responder or default_responder
        self.written = []
        self.closed = False
        self._pending = b""

    def write(self, data):
        data = bytes(data)
        self.written.append(data)
        self._pending = self.responder(data)
        return len(data)

    def read(self, n):
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out

    def reset_input_buffer(self):
        pass

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


def real_driver(port="FAKE1", slave=1, responder=None, factory=None):
    factory = factory or CountingFactory(lambda p, n: FakeSerial(responder))
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

    def assert_fc16(self, req, addr, values, slave=1):
        self.assertEqual(req[0], slave)
        self.assertEqual(req[1], 0x10)
        self.assertEqual(struct.unpack(">H", req[2:4])[0], addr)
        self.assertEqual(struct.unpack(">H", req[4:6])[0], len(values))
        self.assertEqual(req[6], 2 * len(values))
        self.assertEqual(struct.unpack(">%dH" % len(values), req[7:7 + 2 * len(values)]),
                         tuple(values))
        self.assertEqual(struct.unpack("<H", req[-2:])[0], crc16(req[:-2]))

    def test_frame_matches_modbus_example(self):
        self.assertEqual(frame(1, 3, bytes.fromhex("0000000a")),
                         bytes.fromhex("01 03 00 00 00 0a c5 cd"))

    def test_mock_mode_never_touches_serial(self):
        def boom(port):
            raise AssertionError("mock 模式不该调用 serial_factory")

        d = RH56F2("", 1, mock=True, serial_factory=boom)
        d.move(poses.JOINTS["finger_fist"])
        self.assertTrue(any(entry.startswith("TX ") for entry in d.log), list(d.log))
        self.assertEqual(d.last, poses.JOINTS["finger_fist"])
        self.assertEqual(d.read_registers(0x200, 2), [0, 0])

    def test_move_missing_joint_raises(self):
        d = mock_driver()
        joints = dict(poses.JOINTS["release"])
        joints.pop("pinky")
        with self.assertRaises(ModbusError):
            d.move(joints)

    def test_to_raw_range_and_midpoint(self):
        self.assertEqual(RH56F2.to_raw(-5), RM.VALUE_MIN)
        self.assertEqual(RH56F2.to_raw(150), RM.VALUE_MAX)
        midpoint = int(round(RM.VALUE_MIN + (RM.VALUE_MAX - RM.VALUE_MIN) * 50 / 100.0))
        self.assertEqual(RH56F2.to_raw(50), midpoint)

    def test_unverified_real_hand_refuses_write_but_can_read(self):
        d, factory = real_driver()
        self.assertTrue(d.connect())
        with mock.patch.object(rh56_register_map, "VERIFIED", False):
            with self.assertRaises(ModbusError) as caught:
                d.move(poses.JOINTS["release"])
            self.assertIn("VERIFIED", str(caught.exception))
            self.assertEqual(factory.serials[0].written, [], "未核对时不能往总线写动作")
            self.assertEqual(d.read_registers(RM.REG_STATUS, 2), [1, 1])

    def test_verified_move_frames(self):
        d, factory = real_driver()
        with mock.patch.object(rh56_register_map, "VERIFIED", True):
            d.move(poses.JOINTS["release"])
            ser = factory.serials[0]
            self.assertEqual(len(ser.written), 1)
            self.assert_fc16(ser.written[-1], RM.REG_TARGET_BASE,
                             [d.to_raw(poses.JOINTS["release"][j]) for j in RM.JOINT_ORDER])
            self.assertEqual(d.last, poses.JOINTS["release"])

            d.move(poses.JOINTS["release"], speed=40)
            self.assertEqual(len(ser.written), 3)
            self.assert_fc16(ser.written[1], RM.REG_SPEED_BASE,
                             [d.to_raw(40)] * len(RM.JOINT_ORDER))
            self.assert_fc16(ser.written[2], RM.REG_TARGET_BASE,
                             [d.to_raw(poses.JOINTS["release"][j]) for j in RM.JOINT_ORDER])

    def test_exception_response_reports_code(self):
        def responder(req):
            body = bytes([req[0], 0x83, 0x02])
            return body + struct.pack("<H", crc16(body))

        d, _ = real_driver(responder=responder)
        with self.assertRaises(ModbusError) as caught:
            d.read_registers(RM.REG_STATUS, 2)
        self.assertIn("异常码 2", str(caught.exception))

    def test_empty_response_reports_no_reply(self):
        d, _ = real_driver(responder=lambda req: b"")
        with self.assertRaises(ModbusError) as caught:
            d.read_registers(RM.REG_STATUS, 2)
        self.assertIn("没回应", str(caught.exception))

    def test_bad_crc_response(self):
        def responder(req):
            good = default_responder(req)
            return good[:-1] + bytes([good[-1] ^ 0xFF])

        d, _ = real_driver(responder=responder)
        with self.assertRaises(ModbusError) as caught:
            d.read_registers(RM.REG_STATUS, 2)
        self.assertIn("CRC", str(caught.exception))

    def test_wrong_slave_response(self):
        def responder(req):
            count = struct.unpack(">H", req[4:6])[0]
            body = bytes([req[0] + 1, 3, 2 * count]) + b"".join(
                struct.pack(">H", 1) for _ in range(count))
            return body + struct.pack("<H", crc16(body))

        d, _ = real_driver(slave=1, responder=responder)
        with self.assertRaises(ModbusError):
            d.read_registers(RM.REG_STATUS, 2)

    def test_write_oserror_drops_port_and_reopens(self):
        class BadWrite(FakeSerial):
            def write(self, data):
                raise OSError("线断了")

        factory = CountingFactory(lambda port, n: BadWrite() if n == 1 else FakeSerial())
        d = RH56F2("FAKE1", 1, mock=False, serial_factory=factory)
        with self.assertRaises(ModbusError):
            d.read_registers(RM.REG_STATUS, 1)
        self.assertIsNone(d.ser)
        self.assertTrue(d.error)
        self.assertNotIn("FAKE1", rh56f2_driver._PORTS)
        self.assertTrue(factory.serials[0].closed)

        self.assertEqual(d.read_registers(RM.REG_STATUS, 2), [1, 1])
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
            self.assertTrue(name in poses.JOINTS or name in poses.RHYTHMS,
                            "动作 %s 既不在 JOINTS 也不在 RHYTHMS" % name)

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
        self.url, stop = start_arm()
        self.addCleanup(stop)

    def tearDown(self):
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
        self.assertEqual(len(lines), 2)

        fresh_hands(mock_driver(1), RH56F2("", 2, mock=False))
        lines = []
        self.assertFalse(arm_service.probe(out=lines.append))
        self.assertTrue(any("读不到" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
