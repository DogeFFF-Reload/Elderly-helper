"""RH56 抓包桥：官方上位机 ↔ 蓝盒之间透明转发，顺手把 EB 90 帧解码记下来，找 F2 真正的寄存器地址。

为什么：F2 的地址和 V1.09 手册对不上（docs/RH56F2_实测.md §3），盲扫又会把手读死（§4）。
官方上位机知道正确地址：它显示实时角度就得发读指令，拖"设置角度"就得发写指令。把它的收发录下来，
地址就有了。本脚本自己一帧都不发，只转发。

接法（Windows）：
  1. 装 com0com（虚拟串口对，驱动要自己装），建一对：setupc install PortName=COM20 PortName=COM21
  2. 关掉 arm_service 和别的占着 COM7 的程序（一个口只能一个程序用）
  3. python -m hand_controller.rh56_sniff                  # 默认 COM21 ↔ COM7，115200
  4. 上位机连 COM20（RH56FX、115200、ID 1、RS232/485）。连上后一步一步来，每步之前在本窗口敲个备注回车：
       live   → 什么都不动，看实时数据 10 秒
       angle  → 拖一下"设置角度"（先拖到张开那头）
       speed  → 改一下速度；force → 改一下力度
  5. Ctrl+C：打印地址汇总，写 <日志>.summary.json

  python -m hand_controller.rh56_sniff --summarize rh56_sniff_xxx.jsonl     # 离线重看汇总

日志一行一个 JSON：帧（方向、原始十六进制、解码）、备注、杂字节、出错。
"""
import argparse
import json
import struct
import sys
import threading
import time

from config import utf8_console
from hand_controller import rh56_register_map as RM

HEAD_TX = b"\xEB\x90"   # 上位机 → 手
HEAD_RX = b"\x90\xEB"   # 手 → 上位机
CMD_READ, CMD_WRITE = 0x11, 0x12
IDLE_RESYNC = 0.1       # 半帧停了这么久还没收齐，就当杂字节丢掉一个重新找包头
SHOW_SHORTS = 12        # 汇总表里每行最多显示几个 short

# 已知的地址，汇总时当备注（docs/RH56F2_实测.md §3 的推测 + V1.09 手册）
HINTS = {
    1100: "实测：温度 ℃", 1112: "实测：故障码？", 1088: "实测：状态码？",
    1056: "实测：角度设置？（上电 -1）", 1068: "实测：力度设置？", 1032: "实测：上电默认力度？",
    RM.REG_ANGLE_SET: "V1.09 角度设置", RM.REG_FORCE_SET: "V1.09 力度设置", RM.REG_SPEED_SET: "V1.09 速度设置",
    RM.REG_ANGLE_ACT: "V1.09 实际角度", RM.REG_FORCE_ACT: "V1.09 实际受力", RM.REG_ERROR: "V1.09 故障",
    RM.REG_STATUS: "V1.09 状态", RM.REG_TEMP: "V1.09 温度",
}


def checksum_ok(frame):
    return sum(frame[2:-1]) & 0xFF == frame[-1]


def shorts(data):
    """低字节在前的有符号 short；奇数字节就不拆。"""
    if not data or len(data) % 2:
        return None
    return list(struct.unpack("<%dh" % (len(data) // 2), data))


def decode(frame, port_side):
    """一帧完整、校验对的帧 → dict。port_side = 这帧是从哪个口读到的（"app" / "hand"）。"""
    src = "app" if frame[:2] == HEAD_TX else "hand"
    length, cmd = frame[3], frame[4]
    d = {"src": src, "id": frame[2], "cmd": cmd, "raw": frame.hex(" ")}
    if src != port_side:
        # 手那一侧读到 EB 90：USB-RS485 转换器把上位机发的回显了一遍；照样转发，但不算数
        d["kind"] = "echo" if src == "app" else "other"
        return d
    if length < 3:
        d["kind"] = "other"
        return d
    d["addr"] = frame[5] | frame[6] << 8
    payload = frame[7:-1]
    if src == "app" and cmd == CMD_READ and len(payload) == 1:
        d.update(kind="read_req", nbytes=payload[0])
    elif src == "hand" and cmd == CMD_READ:
        d.update(kind="read_resp", nbytes=len(payload), data=payload.hex(" "), shorts=shorts(payload))
    elif src == "app" and cmd == CMD_WRITE:
        d.update(kind="write", nbytes=len(payload), data=payload.hex(" "), shorts=shorts(payload))
    elif src == "hand" and cmd == CMD_WRITE and len(payload) == 1:
        d.update(kind="write_ack", status=payload[0])
    else:
        d.update(kind="other", data=payload.hex(" "))
    return d


class FrameParser:
    """一个方向的字节流 → 帧。找包头、按长度字段切、对校验；对不上就丢一个字节重新找包头。"""

    def __init__(self):
        self.buf = bytearray()
        self.last_byte = 0.0

    def feed(self, data, now):
        self.buf.extend(data)
        self.last_byte = now
        return self._parse()

    def poll(self, now):
        """没有新字节时调：半帧卡太久（杂字节里刚好有个像包头的），丢一个字节重新找。"""
        out = []
        while self.buf and now - self.last_byte > IDLE_RESYNC:
            out.append(("junk", bytes(self.buf[:1])))
            del self.buf[:1]
            out.extend(self._parse())
        return _merge_junk(out)

    def flush(self):
        rest, self.buf = bytes(self.buf), bytearray()
        return [("junk", rest)] if rest else []

    def _parse(self):
        out = []
        while True:
            starts = [i for i in (self.buf.find(HEAD_TX), self.buf.find(HEAD_RX)) if i >= 0]
            if not starts:
                keep = 1 if self.buf[-1:] in (b"\xEB", b"\x90") else 0   # 包头可能被切在两次 read 之间
                if len(self.buf) > keep:
                    out.append(("junk", bytes(self.buf[:len(self.buf) - keep])))
                    del self.buf[:len(self.buf) - keep]
                break
            i = min(starts)
            if i:
                out.append(("junk", bytes(self.buf[:i])))
                del self.buf[:i]
            if len(self.buf) < 4:
                break
            total = 5 + self.buf[3]      # 包头 2 + ID 1 + 长度 1 + 长度字段那么多字节 + 校验 1
            if self.buf[3] == 0:
                out.append(("junk", bytes(self.buf[:1])))
                del self.buf[:1]
                continue
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            if checksum_ok(frame):
                out.append(("frame", frame))
                del self.buf[:total]
            else:
                out.append(("junk", bytes(self.buf[:1])))
                del self.buf[:1]
        return _merge_junk(out)


def _merge_junk(items):
    out = []
    for kind, b in items:
        if kind == "junk" and out and out[-1][0] == "junk":
            out[-1] = ("junk", out[-1][1] + b)
        else:
            out.append((kind, b))
    return out


class Summary:
    """按 (ID, 地址, 字节数) 汇总读写：读了几次、回了几次、值在不在变；写了什么、写之前最近的备注是什么。"""

    def __init__(self):
        self.reads = {}
        self.writes = {}
        self.other = 0
        self.echo = 0
        self.junk_bytes = 0
        self.note = None

    def add(self, d, t):
        kind = d.get("kind")
        if kind == "note":
            self.note = d["text"]
        elif kind == "junk":
            self.junk_bytes += len(bytes.fromhex(d["raw"]))
        elif kind == "echo":
            self.echo += 1
        elif kind in ("read_req", "read_resp"):
            r = self.reads.setdefault((d["id"], d["addr"], d["nbytes"]), {
                "requests": 0, "responses": 0, "first_t": t, "last": None, "min": None, "max": None})
            if kind == "read_req":
                r["requests"] += 1
                return
            r["responses"] += 1
            r["last"] = d["data"]
            v = d.get("shorts")
            if v is not None:
                r["min"] = v if r["min"] is None else [min(a, b) for a, b in zip(r["min"], v)]
                r["max"] = v if r["max"] is None else [max(a, b) for a, b in zip(r["max"], v)]
        elif kind == "write":
            w = self.writes.setdefault((d["id"], d["addr"], d["nbytes"]), {
                "count": 0, "first_t": t, "after_note": self.note, "values": []})
            w["count"] += 1
            v = d.get("shorts") if d.get("shorts") is not None else d["data"]
            if v not in w["values"] and len(w["values"]) < 20:
                w["values"].append(v)
        elif kind == "other":
            self.other += 1

    @staticmethod
    def read_hint(addr, nbytes, r):
        notes = [HINTS[addr]] if addr in HINTS else []
        if addr + nbytes > RM.READ_LIMIT:
            notes.append("⚠ 超过 READ_LIMIT：上位机读这里手没卡死？和实测矛盾，记下来")
        if r["min"] is not None and r["min"] != r["max"]:
            span = (min(r["min"]), max(r["max"]))
            notes.append("在变 %d~%d" % span + ("：像实时量（实际角度 / 受力？）" if -1 <= span[0] and span[1] <= 2000 else ""))
        if r["requests"] > r["responses"]:
            notes.append("%d 次没回包" % (r["requests"] - r["responses"]))
        return "；".join(notes)

    def to_json(self):
        def key(k):
            return {"id": k[0], "addr": k[1], "nbytes": k[2]}
        return {
            "reads": [dict(key(k), hint=self.read_hint(k[1], k[2], r), **r) for k, r in sorted(self.reads.items())],
            "writes": [dict(key(k), hint=HINTS.get(k[1], ""), **w) for k, w in sorted(self.writes.items())],
            "other_frames": self.other, "echo_frames": self.echo, "junk_bytes": self.junk_bytes,
        }

    def lines(self):
        out = ["", "==== 读（上位机 → 手）：按地址 ===="]
        if not self.reads:
            out.append("  （一条都没有：上位机连上了吗？连的是 COM20 吗？）")
        for (hid, addr, n), r in sorted(self.reads.items()):
            v = shorts(bytes.fromhex(r["last"])) if r["last"] else None
            shown = "—" if r["last"] is None else (str(v[:SHOW_SHORTS]) + ("…" if len(v) > SHOW_SHORTS else "")
                                                    if v is not None else r["last"])
            out.append("  ID %d  地址 %4d  %2d 字节  读 %4d 次 / 回 %4d  最近 %s  %s"
                       % (hid, addr, n, r["requests"], r["responses"], shown, self.read_hint(addr, n, r)))
        out.append("==== 写（上位机 → 手）：按地址 ====")
        if not self.writes:
            out.append("  （没有：拖一下上位机的'设置角度'试试）")
        for (hid, addr, n), w in sorted(self.writes.items()):
            out.append("  ID %d  地址 %4d  %2d 字节  写 %3d 次  第一次在备注「%s」之后  %s  值 %s"
                       % (hid, addr, n, w["count"], w["after_note"] or "—", HINTS.get(addr, ""), w["values"][:5]))
        out.append("其他帧 %d、回显 %d、杂字节 %d" % (self.other, self.echo, self.junk_bytes))
        return out


class Recorder:
    """收解码结果：写 JSONL、更新汇总、在屏幕上提示（新出现的读地址、每一次写、备注、出错）。"""

    def __init__(self, log, out=print, verbose=False):
        self.log, self.out, self.verbose = log, out, verbose
        self.summary = Summary()
        self.t0 = time.monotonic()
        self.mu = threading.Lock()
        self.seen = set()

    def _put(self, entry):
        entry = dict(entry, t=round(time.monotonic() - self.t0, 3))
        with self.mu:
            if self.log:
                self.log.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self.log.flush()
            self.summary.add(entry, entry["t"])
            self._show(entry)

    def _show(self, e):
        kind = e.get("kind")
        if self.verbose:
            self.out("%8.3f %-9s %s" % (e["t"], kind, e.get("raw") or e.get("text") or e.get("error")))
            return
        if kind in ("read_req", "write"):
            k = (kind, e["id"], e["addr"], e["nbytes"])
            if kind == "read_req" and k in self.seen:
                return
            self.seen.add(k)
            what = "新的读地址" if kind == "read_req" else "写"
            extra = "" if kind == "read_req" else "  值 %s" % (e.get("shorts") or e.get("data"))
            self.out("%8.3f %s：ID %d 地址 %d 起 %d 字节%s  %s" % (e["t"], what, e["id"], e["addr"], e["nbytes"],
                                                            extra, HINTS.get(e["addr"], "")))
        elif kind == "write_ack" and e.get("status") != 1:
            self.out("%8.3f 手拒绝了写入：地址 %d 状态 %s" % (e["t"], e["addr"], e["status"]))
        elif kind == "other":
            self.out("%8.3f 不认识的帧：%s" % (e["t"], e["raw"]))
        elif kind == "note":
            self.out("%8.3f ---- 备注：%s" % (e["t"], e["text"]))
        elif kind == "error":
            self.out("%8.3f ❌ %s" % (e["t"], e["error"]))

    def frame(self, frame, port_side):
        self._put(decode(frame, port_side))

    def junk(self, data, port_side):
        self._put({"kind": "junk", "side": port_side, "raw": data.hex(" ")})

    def note(self, text):
        if text:
            self._put({"kind": "note", "text": text})

    def error(self, text):
        self._put({"kind": "error", "error": text})


def pump(src, dst, side, rec, stop):
    """从 src 读到的字节原样写给 dst（先转发、再解码，别让上位机等），同时按帧解码记下来。"""
    parser = FrameParser()
    while not stop.is_set():
        try:
            data = src.read(getattr(src, "in_waiting", 0) or 1)
            if data:
                dst.write(data)
        except Exception as e:  # noqa: BLE001 拔了 USB、虚拟口被关
            rec.error("%s 一侧串口出错，停止转发：%s" % ("上位机" if side == "app" else "手", e))
            stop.set()
            break
        now = time.monotonic()
        items = parser.feed(data, now) if data else parser.poll(now)
        for kind, b in items:
            rec.frame(b, side) if kind == "frame" else rec.junk(b, side)
    for _, b in parser.flush():
        rec.junk(b, side)


def bridge(app, hand, rec, stop):
    """起两个方向的转发线程，返回线程（stop.set() 后自己退出）。"""
    threads = [threading.Thread(target=pump, args=(app, hand, "app", rec, stop), daemon=True),
               threading.Thread(target=pump, args=(hand, app, "hand", rec, stop), daemon=True)]
    for t in threads:
        t.start()
    return threads


def open_port(port, baud, hint):
    try:
        import serial
    except ImportError:
        raise SystemExit("没装 pyserial：pip install -r requirements-hardware.txt")
    try:
        return serial.Serial(port, baud, parity=RM.PARITY, stopbits=RM.STOPBITS, timeout=0.01)
    except Exception as e:  # noqa: BLE001
        raise SystemExit("串口 %s 打不开：%s\n%s" % (port, e, hint))


def summarize(path, out=print):
    """离线：读一份日志，重算汇总。"""
    s = Summary()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                e = json.loads(line)
                s.add(e, e.get("t", 0))
    for line in s.lines():
        out(line)
    return s


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(description="RH56 抓包桥：上位机 ↔ 蓝盒透明转发并解码 EB 90 帧（自己不发任何帧）")
    ap.add_argument("--app", default="COM21", help="com0com 虚拟口里给本脚本用的那一头（上位机连另一头，如 COM20）")
    ap.add_argument("--hand", default="COM7", help="蓝盒的串口")
    ap.add_argument("--baud", type=int, default=RM.BAUD)
    ap.add_argument("--log", help="日志文件（JSONL），默认 rh56_sniff_<时间>.jsonl")
    ap.add_argument("--verbose", action="store_true", help="每一帧都打印（上位机轮询很密，平时别开）")
    ap.add_argument("--summarize", metavar="LOG", help="不接串口，只把已有日志重新汇总")
    args = ap.parse_args(argv)
    if args.summarize:
        summarize(args.summarize)
        return 0

    path = args.log or time.strftime("rh56_sniff_%Y%m%d_%H%M%S.jsonl")
    app = open_port(args.app, args.baud, "com0com 装了吗？虚拟口对是 COM20↔COM21 吗？（setupc list 看）")
    hand = open_port(args.hand, args.baud, "蓝盒插着吗？arm_service / 上位机是不是正占着它？")
    stop = threading.Event()
    with open(path, "w", encoding="utf-8") as log:
        rec = Recorder(log, verbose=args.verbose)
        log.write(json.dumps({"kind": "start", "app": args.app, "hand": args.hand, "baud": args.baud,
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False) + "\n")
        print("转发中：%s（上位机那头）↔ %s（蓝盒），%d 8N1。日志 %s" % (args.app, args.hand, args.baud, path))
        print("每做一步之前敲个备注回车（live / angle / speed / force）；Ctrl+C 结束并汇总。")
        threads = bridge(app, hand, rec, stop)
        try:
            while not stop.is_set():
                try:
                    rec.note(input().strip())
                except EOFError:          # 没有键盘输入（比如后台跑）：就只转发，等 Ctrl+C
                    while not stop.wait(0.5):
                        pass
        except KeyboardInterrupt:
            pass
        stop.set()
        for t in threads:
            t.join(1.0)
        app.close()
        hand.close()
        summary = rec.summary
    for line in summary.lines():
        print(line)
    with open(path + ".summary.json", "w", encoding="utf-8") as f:
        json.dump(summary.to_json(), f, ensure_ascii=False, indent=1)
    print("汇总已写 %s.summary.json" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
