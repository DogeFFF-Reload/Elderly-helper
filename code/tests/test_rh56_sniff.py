"""RH56 抓包桥：EB 90 帧切分 / 重同步、解码、地址汇总、两个方向透明转发、离线重算汇总。全用假串口，不碰硬件。"""
import json
import os
import queue
import tempfile
import threading
import time
import unittest

from hand_controller import rh56_register_map as RM
from hand_controller import rh56_sniff as sniff
from hand_controller.rh56f2_driver import frame_read, frame_write, pack_shorts

# 手册 §2.2 原文的帧
READ_REQ = bytes.fromhex("eb 90 01 04 11 0a 06 0c 32")
READ_RESP = bytes.fromhex("90 eb 01 0f 11 0a 06 64 00 64 00 64 00 64 00 e8 03 00 00 ac")
WRITE_REQ = bytes.fromhex("eb 90 01 0f 12 ce 05 64 00 64 00 64 00 64 00 e8 03 00 00 70")
WRITE_ACK = bytes.fromhex("90 eb 01 04 12 ce 05 01 eb")


def read_resp(hand_id, addr, data):
    body = bytes([hand_id, len(data) + 3, 0x11, addr & 0xFF, addr >> 8]) + bytes(data)
    return b"\x90\xEB" + body + bytes([sum(body) & 0xFF])


def write_ack(hand_id, addr):
    body = bytes([hand_id, 0x04, 0x12, addr & 0xFF, addr >> 8, 0x01])
    return b"\x90\xEB" + body + bytes([sum(body) & 0xFF])


def frames_of(items):
    return [b for kind, b in items if kind == "frame"]


class ParserTest(unittest.TestCase):
    def test_frames_split_byte_by_byte(self):
        p = sniff.FrameParser()
        got = []
        for b in READ_REQ + WRITE_REQ:
            got += p.feed(bytes([b]), 0.0)
        self.assertEqual(frames_of(got), [READ_REQ, WRITE_REQ])
        self.assertEqual([k for k, _ in got], ["frame", "frame"], "整帧切碎了喂也不该有杂字节")

    def test_junk_before_and_between_frames(self):
        p = sniff.FrameParser()
        got = p.feed(b"\x00\x11" + READ_RESP + b"\x55" + WRITE_ACK, 0.0)
        self.assertEqual(frames_of(got), [READ_RESP, WRITE_ACK])
        self.assertEqual([b for k, b in got if k == "junk"], [b"\x00\x11", b"\x55"])

    def test_bad_checksum_resyncs_on_next_header(self):
        bad = READ_REQ[:-1] + bytes([READ_REQ[-1] ^ 0xFF])
        p = sniff.FrameParser()
        self.assertEqual(frames_of(p.feed(bad + WRITE_REQ, 0.0)), [WRITE_REQ])

    def test_stalled_half_frame_is_dropped_after_idle(self):
        # 杂字节里刚好有个"包头"，长度字段说后面还有 200 字节：等不到就丢掉，后面的真帧照样认
        p = sniff.FrameParser()
        self.assertEqual(p.feed(b"\xEB\x90\x01\xC8\x11", 0.0), [])
        self.assertEqual(p.poll(0.05), [], "还没到重同步时间")
        p.feed(READ_REQ, 0.06)
        got = p.poll(0.06 + sniff.IDLE_RESYNC + 0.01)
        self.assertEqual(frames_of(got), [READ_REQ])

    def test_header_split_across_reads_is_kept(self):
        p = sniff.FrameParser()
        self.assertEqual(p.feed(b"\x42" + READ_REQ[:1], 0.0), [("junk", b"\x42")])
        self.assertEqual(frames_of(p.feed(READ_REQ[1:], 0.0)), [READ_REQ])


class DecodeTest(unittest.TestCase):
    def test_manual_frames(self):
        d = sniff.decode(READ_REQ, "app")
        self.assertEqual((d["kind"], d["id"], d["addr"], d["nbytes"]), ("read_req", 1, 1546, 12))
        d = sniff.decode(READ_RESP, "hand")
        self.assertEqual((d["kind"], d["addr"], d["shorts"]), ("read_resp", 1546, [100, 100, 100, 100, 1000, 0]))
        d = sniff.decode(WRITE_REQ, "app")
        self.assertEqual((d["kind"], d["addr"], d["shorts"]), ("write", 1486, [100, 100, 100, 100, 1000, 0]))
        d = sniff.decode(WRITE_ACK, "hand")
        self.assertEqual((d["kind"], d["addr"], d["status"]), ("write_ack", 1486, 1))

    def test_negative_shorts(self):
        d = sniff.decode(frame_write(1, 1056, pack_shorts([-1] * 6)), "app")
        self.assertEqual(d["shorts"], [-1] * 6)

    def test_echo_on_hand_side_is_not_counted(self):
        d = sniff.decode(READ_REQ, "hand")
        self.assertEqual(d["kind"], "echo")
        s = sniff.Summary()
        s.add(d, 0)
        self.assertEqual((s.reads, s.echo), ({}, 1))


class SummaryTest(unittest.TestCase):
    def feed(self, s, frame, side, t=0.0):
        s.add(sniff.decode(frame, side), t)

    def test_reads_track_range_and_flag_live_values(self):
        s = sniff.Summary()
        for t, angles in enumerate(([1572, 1600, 1650, 1700, 1764, 1600], [1500, 1600, 1650, 1700, 1764, 1600])):
            self.feed(s, frame_read(1, 1546, 12), "app", t)
            self.feed(s, read_resp(1, 1546, pack_shorts(angles)), "hand", t)
        self.feed(s, frame_read(1, 1100, 12), "app")
        self.feed(s, read_resp(1, 1100, pack_shorts([36, 36, 34, 34, 30, 34])), "hand")
        self.feed(s, frame_read(1, 2000, 2), "app")          # 没回包
        r = s.reads[(1, 1546, 12)]
        self.assertEqual((r["requests"], r["responses"]), (2, 2))
        self.assertEqual(r["min"][0], 1500)
        self.assertEqual(r["max"][0], 1572)
        self.assertIn("像实时量", s.read_hint(1546, 12, r))
        self.assertIn("温度", s.read_hint(1100, 12, s.reads[(1, 1100, 12)]))
        self.assertNotIn("在变", s.read_hint(1100, 12, s.reads[(1, 1100, 12)]))
        self.assertIn("没回包", s.read_hint(2000, 2, s.reads[(1, 2000, 2)]))
        text = "\n".join(s.lines())
        self.assertIn("地址 1546", text)
        json.dumps(s.to_json(), ensure_ascii=False)        # 能写成 summary.json

    def test_reads_past_limit_are_flagged(self):
        s = sniff.Summary()
        self.feed(s, frame_read(1, RM.READ_LIMIT, 64), "app")
        self.assertIn("READ_LIMIT", s.read_hint(RM.READ_LIMIT, 64, s.reads[(1, RM.READ_LIMIT, 64)]))

    def test_writes_remember_the_note_before_them(self):
        s = sniff.Summary()
        s.add({"kind": "note", "text": "angle"}, 0)
        self.feed(s, frame_write(1, 1056, pack_shorts([1000] * 6)), "app")
        self.feed(s, frame_write(1, 1056, pack_shorts([500] * 6)), "app")
        s.add({"kind": "note", "text": "speed"}, 1)
        self.feed(s, frame_write(1, 1056, pack_shorts([500] * 6)), "app")
        w = s.writes[(1, 1056, 12)]
        self.assertEqual((w["count"], w["after_note"]), (3, "angle"))
        self.assertEqual(w["values"], [[1000] * 6, [500] * 6])


# ---------- 两个方向的转发：假串口 ----------

class FakePort:
    """read(n) 最多等 10 ms，没数据回 b""（像 timeout=0.01 的 pyserial）；write 记下来，或交给 on_write。"""

    def __init__(self, on_write=None):
        self.incoming = queue.Queue()
        self.written = bytearray()
        self.on_write = on_write
        self.mu = threading.Lock()

    @property
    def in_waiting(self):
        return self.incoming.qsize()

    def push(self, data):
        for b in data:
            self.incoming.put(bytes([b]))

    def read(self, n):
        out = b""
        try:
            out += self.incoming.get(timeout=0.01)
            while len(out) < n:
                out += self.incoming.get_nowait()
        except queue.Empty:
            pass
        return out

    def write(self, data):
        with self.mu:
            self.written.extend(data)
        if self.on_write:
            self.on_write(bytes(data))
        return len(data)


class FakeHand(FakePort):
    """手：收齐一帧请求就按 EB 90 协议回包（读回 data 里的值，没有就全 0）；可选把请求回显一遍。"""

    def __init__(self, data=None, echo=False):
        super().__init__(on_write=self._got)
        self.data = data or {}
        self.echo = echo
        self.parser = sniff.FrameParser()

    def _got(self, data):
        for frame in frames_of(self.parser.feed(data, time.monotonic())):
            if self.echo:
                self.push(frame)
            addr = frame[5] | frame[6] << 8
            if frame[4] == 0x11:
                n = frame[7]
                self.push(read_resp(frame[2], addr, (self.data.get(addr, b"") + bytes(n))[:n]))
            elif frame[4] == 0x12:
                self.push(write_ack(frame[2], addr))


def wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def settle(port, quiet=0.15, timeout=3.0):
    """等 port 收到的字节不再增加（回显和回包可能分两次转过来）。"""
    end = time.monotonic() + timeout
    last, since = len(port.written), time.monotonic()
    while time.monotonic() < end:
        time.sleep(0.01)
        if len(port.written) != last:
            last, since = len(port.written), time.monotonic()
        elif time.monotonic() - since >= quiet:
            return


class BridgeTest(unittest.TestCase):
    def run_bridge(self, hand, script, log=None):
        app = FakePort()
        lines = []
        rec = sniff.Recorder(log, out=lines.append)
        stop = threading.Event()
        threads = sniff.bridge(app, hand, rec, stop)
        for item in script:
            if isinstance(item, str):
                rec.note(item)
                continue
            n = len(app.written)
            app.push(item)
            self.assertTrue(wait_until(lambda: len(app.written) > n), "手的回包要转回上位机")
            settle(app)
        stop.set()
        for t in threads:
            t.join(2.0)
        return app, rec, lines

    def test_forwards_both_ways_verbatim_and_summarizes(self):
        hand = FakeHand(data={1546: pack_shorts([1572, 1600, 1650, 1700, 1764, 1600])})
        req1, req2 = frame_read(1, 1546, 12), frame_write(1, 1056, pack_shorts([1000] * 6))
        app, rec, lines = self.run_bridge(hand, ["live", req1, "angle", req2])
        self.assertEqual(bytes(hand.written), req1 + req2, "上位机发的原样到手")
        self.assertEqual(bytes(app.written), read_resp(1, 1546, pack_shorts([1572, 1600, 1650, 1700, 1764, 1600]))
                         + write_ack(1, 1056), "手回的原样到上位机")
        s = rec.summary
        self.assertEqual(s.reads[(1, 1546, 12)]["responses"], 1)
        self.assertEqual(s.writes[(1, 1056, 12)]["after_note"], "angle")
        self.assertTrue(any("新的读地址" in l and "1546" in l for l in lines), lines)
        self.assertTrue(any("写：" in l and "1056" in l for l in lines), lines)

    def test_adapter_echo_is_forwarded_but_not_counted(self):
        hand = FakeHand(echo=True)
        req = frame_read(1, 1100, 12)
        app, rec, _ = self.run_bridge(hand, [req])
        self.assertEqual(bytes(app.written), req + read_resp(1, 1100, bytes(12)))
        self.assertEqual(bytes(app.written[:len(req)]), req, "回显也照样转给上位机，和直连一样")
        self.assertEqual(rec.summary.reads[(1, 1100, 12)]["requests"], 1)
        self.assertEqual(rec.summary.echo, 1)

    def test_bridge_never_sends_its_own_frames(self):
        hand = FakeHand()
        app, _, _ = self.run_bridge(hand, ["live", "angle"])
        self.assertEqual(bytes(hand.written), b"")
        self.assertEqual(bytes(app.written), b"")

    def test_log_roundtrip_and_offline_summarize(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.remove, path)
        with open(path, "w", encoding="utf-8") as log:
            self.run_bridge(FakeHand(), ["angle", frame_write(1, 1056, pack_shorts([800] * 6)), frame_read(1, 1100, 12)],
                            log=log)
        out = []
        s = sniff.summarize(path, out=out.append)
        self.assertEqual(s.writes[(1, 1056, 12)]["after_note"], "angle")
        self.assertEqual(s.reads[(1, 1100, 12)]["responses"], 1)
        self.assertTrue(any("地址 1056" in l for l in out), out)

    def test_port_error_stops_cleanly(self):
        class Broken(FakePort):
            def read(self, n):
                raise OSError("USB 拔了")

        lines = []
        rec = sniff.Recorder(None, out=lines.append)
        stop = threading.Event()
        threads = sniff.bridge(Broken(), FakeHand(), rec, stop)
        self.assertTrue(stop.wait(2.0))
        for t in threads:
            t.join(2.0)
        self.assertTrue(any("USB 拔了" in l for l in lines), lines)


if __name__ == "__main__":
    unittest.main()
