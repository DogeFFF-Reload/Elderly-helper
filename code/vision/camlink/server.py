"""CareMate 视频看护服务（像视频通话一样连手机）。规则判定疑似跌倒后经床头板通知家属 App（--alarm live，默认）；
不碰执行端（机械臂 / 手）。

    python -m vision.camlink.server [--port 8016] [--no-tunnel] [--no-browser]

电脑：浏览器打开 http://127.0.0.1:8016/ ，页面上出二维码。
手机：iPhone 相机扫码 → Safari 打开我们自己的摄像头页 → 点“开始看护”。画面走 WebRTC，
      手机和电脑在同一 Wi-Fi / 热点时直接局域网直连；隧道只传几 KB 的握手信息。
      iPhone 网页要 HTTPS 才能用摄像头，所以手机页经 cloudflared 免费隧道（https://xxx.trycloudflare.com）打开。

骨架识别在电脑浏览器里跑（MediaPipe Pose，GPU，按手机重力方向转正后再识别），
每秒约 10 次把 14 点骨架 POST 到 /api/pose，由仓库原来的 FallDetector 判断，结果显示在大屏上。

访问控制：经隧道来的请求（带 Cf-Connecting-Ip）只能打开手机页和手机那一半信令，而且要带本次随机钥匙；
其余接口（大屏、骨架、状态）只认本机 127.0.0.1。钥匙每次启动重新生成，不写盘。
"""
import argparse
import json
import mimetypes
from pathlib import Path
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser

import config
from gesture.fall_rules import FallDetector
from vision.input_quality import LABELS, InputQuality

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
VENDOR_FILES = {
    "vision_bundle.mjs": "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/vision_bundle.mjs",
    "wasm/vision_wasm_internal.js": "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/wasm/vision_wasm_internal.js",
    "wasm/vision_wasm_internal.wasm": "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.1.1/wasm/vision_wasm_internal.wasm",
    "pose_landmarker_full.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
    "pose_landmarker_heavy.task": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task",
    "qrcode.js": "https://cdn.jsdelivr.net/npm/qrcode-generator@2.0.4/dist/qrcode.js",
}
MAX_BODY = 64 * 1024
OFFER_TTL = 60.0          # 手机发出邀请后这么久电脑没接，作废
PHONE_TIMEOUT = 6.0       # 手机页这么久没来报到，算断开
CLAIM_TTL = 10.0          # 电脑页领走邀请后这么久没回应，放给别的页面
MAX_WAIT = 20.0           # 长轮询最多挂这么久（隧道每来回一次要 1–2 秒，挂着等比反复问快得多）
KP_NAMES = ("head", "neck", "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_wrist", "r_wrist",
            "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle")


def clean_kp(kp):
    """只收 14 个已知关键点、每个 [x, y, 可见度] 三个有限数；其余一律当没检测到人。"""
    if not isinstance(kp, dict):
        return None
    out = {}
    for name in KP_NAMES:
        p = kp.get(name)
        if not (isinstance(p, list) and len(p) == 3 and all(isinstance(v, (int, float)) for v in p)):
            return None
        if not all(-1.0 <= v <= 2.0 for v in p):
            return None
        out[name] = [float(p[0]), float(p[1]), float(p[2])]
    return out


class CamLink:
    """信令信箱 + 跌倒规则；线程安全，HTTP 层只做搬运。"""

    def __init__(self, rules=None, clock=time.monotonic, token=None, alarm=None):
        self.clock = clock
        self.alarm = alarm           # AlarmChannel（疑似跌倒 → 床头板 → 家属 App）；None = 只在大屏提示
        self.token = token or secrets.token_urlsafe(12)
        self.lock = threading.Condition()
        self.public_url = None
        self.session = None          # {"id", "offer", "answer", "at"}
        self.phone_seen = None
        self.phone_info = {}
        self.rules = rules
        self._fresh_rules()
        self.last_pose = None
        self.events = []
        self.frames = 0

    def _fresh_rules(self):
        self.detector = FallDetector(self.rules)
        self.quality = InputQuality(log=lambda _msg: None)
        self.t0 = self.last_t = None

    # ---------- 信令 ----------
    def post_offer(self, sdp):
        if not isinstance(sdp, str) or not 10 < len(sdp) < MAX_BODY:
            raise ValueError("bad sdp")
        with self.lock:
            sid = secrets.token_hex(6)
            self.session = {"id": sid, "offer": sdp, "answer": None, "at": self.clock()}
            self.phone_seen = self.clock()
            self.lock.notify_all()
            # 新连接从头判断，不沿用上一次的站立基准
            self._fresh_rules()
            return sid

    def _claimable(self):
        s = self.session
        now = self.clock()
        return bool(s and s["answer"] is None and now - s["at"] <= OFFER_TTL and now - s.get("claimed", -1e9) > CLAIM_TTL)

    def take_offer(self, wait=0.0):
        """同一通“来电”只交给一个电脑页面接（开着两个页面也不会抢着回应）；接的页面 10 秒没回应再放给别人。
        wait > 0 时挂着等，手机一发邀请立刻返回（长轮询）。"""
        with self.lock:
            if wait > 0:
                self.lock.wait_for(self._claimable, timeout=min(wait, MAX_WAIT))
            if self._claimable():
                s = self.session
                s["claimed"] = self.clock()
                return {"id": s["id"], "sdp": s["offer"]}
            return None

    def post_answer(self, sid, sdp):
        if not isinstance(sdp, str) or not 10 < len(sdp) < MAX_BODY:
            raise ValueError("bad sdp")
        with self.lock:
            if not self.session or self.session["id"] != sid:
                raise KeyError("no such session")
            self.session["answer"] = sdp
            self.lock.notify_all()

    def get_answer(self, sid, wait=0.0):
        """wait > 0 时挂着等电脑回应（长轮询）；会话被新的邀请顶掉就报 KeyError。"""
        with self.lock:
            self.phone_seen = self.clock()
            mine = lambda: self.session is not None and self.session["id"] == sid
            if wait > 0:
                self.lock.wait_for(lambda: not mine() or self.session["answer"] is not None, timeout=min(wait, MAX_WAIT))
                self.phone_seen = self.clock()
            if mine():
                return self.session["answer"]
            raise KeyError("no such session")

    def phone_ping(self, info):
        with self.lock:
            self.phone_seen = self.clock()
            if isinstance(info, dict):
                self.phone_info = {k: info[k] for k in ("camera", "landscape", "battery") if k in info}

    def hangup(self):
        with self.lock:
            self.session = None
            self.phone_seen = None
            self.lock.notify_all()

    # ---------- 骨架 ----------
    def pose(self, body):
        """电脑页送来一帧（已按重力转正）的 14 点骨架；返回规则状态。"""
        body = body if isinstance(body, dict) else {}
        kp = clean_kp(body.get("kp")) if body.get("kp") is not None else None
        aspect = body.get("aspect")
        with self.lock:
            now = self.clock()
            if self.t0 is None:
                self.t0 = now
            aspect = float(aspect) if isinstance(aspect, (int, float)) and 0.2 < aspect < 5 else self.detector.aspect
            self.detector.aspect = aspect
            t = now - self.t0
            if self.last_t is not None and t <= self.last_t:
                t = self.last_t + 1e-3          # 规则要求时间严格递增
            self.last_t = t
            # 和原来的实时入口一样：InputQuality 管画面间隔和状态，FallDetector 管跌倒
            self.quality.before_frame(t, aspect, self.detector)
            event = self.detector.update(t, kp)
            self.quality.after_frame(t, kp, aspect, self.detector)
            self.frames += 1
            if event:
                eid = self.alarm.fall() if self.alarm else None
                self.events.append({"at": time.strftime("%H:%M:%S"), "kind": "suspected_fall", "event_id": eid})
                del self.events[:-20]
            status = self.quality.state
            self.last_pose = {"status": status, "text": LABELS.get(status, status), "event": bool(event)}
            return dict(self.last_pose, events=len(self.events))

    def status(self):
        with self.lock:
            now = self.clock()
            phone = self.phone_seen is not None and now - self.phone_seen <= PHONE_TIMEOUT
            cam = (self.public_url + "/cam?k=" + self.token) if self.public_url else None
            events = []
            for e in self.events:
                rec = self.alarm.record(e["event_id"]) if self.alarm and e.get("event_id") else None
                events.append(dict(e, delivery=rec.get("status") if rec else None))
            return {"cam_url": cam, "tunnel": bool(self.public_url), "phone_online": phone,
                    "phone": self.phone_info if phone else {}, "pending_offer": bool(
                        self.session and self.session["answer"] is None),
                    "pose": self.last_pose, "events": events, "frames": self.frames,
                    "mode": self.alarm.mode if self.alarm else "off",
                    "alarm": self.alarm.snapshot() if self.alarm else {"mode": "off", "ready": True}}


def wait_of(query):
    try:
        return max(0.0, min(float((query.get("wait") or ["0"])[0]), MAX_WAIT))
    except ValueError:
        return 0.0


def make_handler(link):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CareMateCamLink"

        def log_message(self, *_args):
            pass

        def public(self):
            # cloudflared 转发的请求都带这个头；本机直连没有
            return bool(self.headers.get("Cf-Connecting-Ip"))

        def keyed(self, query):
            return secrets.compare_digest((query.get("k") or [""])[0], link.token)

        def reply(self, code, data=b"", ctype="application/json; charset=utf-8", cache=False):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def drain(self):
            """先把请求正文读完再回复：不读完就回 404 / 403，Windows 上客户端会看到连接被中止。"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                self.close_connection = True
                self._raw = None
                return
            self._raw = self.rfile.read(n) if n else b""

        def body(self):
            if not self._raw:
                raise ValueError("body size")
            return json.loads(self._raw.decode("utf-8"))

        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            path = url.path
            if path == "/cam":
                if not self.keyed(q):
                    return self.reply(403, "链接已失效：请在电脑上重新扫码。", "text/plain; charset=utf-8")
                return self.reply(200, (HERE / "phone.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/sig/answer":
                if not self.keyed(q):
                    return self.reply(403, {"error": "key"})
                try:
                    sdp = link.get_answer((q.get("id") or [""])[0], wait=wait_of(q))
                except KeyError:
                    return self.reply(404, {"error": "session"})
                return self.reply(200, {"sdp": sdp})
            if self.public():
                return self.reply(404, b"Not found", "text/plain")
            # ---- 以下只给本机 ----
            if path == "/":
                return self.reply(200, (HERE / "monitor.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self.reply(200, link.status())
            if path == "/api/sig/offer":
                return self.reply(200, link.take_offer(wait=wait_of(q)) or {})
            if path.startswith("/vendor/"):
                rel = path[len("/vendor/"):]
                if rel not in VENDOR_FILES:
                    return self.reply(404, b"Not found", "text/plain")
                f = VENDOR / rel
                if not f.is_file():
                    return self.reply(404, b"Missing vendor file; run --fetch", "text/plain")
                ctype = {"mjs": "text/javascript", "js": "text/javascript", "wasm": "application/wasm",
                         "task": "application/octet-stream"}.get(f.suffix.lstrip("."), mimetypes.guess_type(f.name)[0])
                return self.reply(200, f.read_bytes(), ctype, cache=True)
            return self.reply(404, b"Not found", "text/plain")

        def do_POST(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            self.drain()
            try:
                if url.path in ("/api/sig/offer", "/api/phone/ping", "/api/phone/bye"):
                    if not self.keyed(q):
                        return self.reply(403, {"error": "key"})
                    data = {} if url.path == "/api/phone/bye" else self.body()   # sendBeacon 可能不带正文
                    if url.path == "/api/sig/offer":
                        return self.reply(200, {"id": link.post_offer(data.get("sdp"))})
                    if url.path == "/api/phone/ping":
                        link.phone_ping(data)
                        return self.reply(200, {"ok": True})
                    link.hangup()
                    return self.reply(200, {"ok": True})
                if self.public():
                    return self.reply(404, {"error": "not found"})
                data = self.body() if url.path != "/api/alarm/test" else {}
                if url.path == "/api/sig/answer":
                    link.post_answer(data.get("id"), data.get("sdp"))
                    return self.reply(200, {"ok": True})
                if url.path == "/api/pose":
                    return self.reply(200, link.pose(data))
                if url.path == "/api/alarm/test":
                    if not link.alarm:
                        return self.reply(409, {"error": "报警通道未开启"})
                    try:
                        return self.reply(200, link.alarm.send_test())
                    except RuntimeError as exc:
                        return self.reply(409, {"error": str(exc)})
                return self.reply(404, {"error": "not found"})
            except KeyError:
                return self.reply(404, {"error": "session"})
            except (ValueError, json.JSONDecodeError):
                return self.reply(400, {"error": "bad request"})

    return Handler


def start_tunnel(port, link, log=print):
    """后台起 cloudflared 免费隧道，拿到 https 地址后写进 link.public_url。"""
    from tools.public_link import URL_RE, find_cloudflared
    try:
        exe = find_cloudflared(None)
    except SystemExit as exc:
        log("[隧道] " + str(exc))
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", "http://127.0.0.1:%d" % port],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def read():
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m and not link.public_url:
                link.public_url = m.group(0)
                log("[隧道] 手机页地址已就绪（二维码在电脑页面上）")
    threading.Thread(target=read, daemon=True, name="cloudflared").start()
    return proc


def fetch_vendor(log=print):
    import urllib.request
    for rel, url in VENDOR_FILES.items():
        dest = VENDOR / rel
        if dest.is_file():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        log("下载 " + rel)
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)


def main(argv=None):
    config.utf8_console()
    ap = argparse.ArgumentParser(description="CareMate 视频看护：手机扫码连摄像头，电脑识别骨架（仅观察）")
    ap.add_argument("--port", type=int, default=8016)
    ap.add_argument("--no-tunnel", action="store_true", help="不起 cloudflared（只做本机调试）")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--fetch", action="store_true", help="下载网页版 MediaPipe 和骨架模型到 vendor/ 后退出")
    ap.add_argument("--alarm", choices=("off", "dry", "live"), default="live",
                    help="疑似跌倒怎么办：live=发到家属 App（默认）；dry=只做格式检查不响铃；off=只在大屏提示")
    ap.add_argument("--family", default="auto", help="床头板地址 http://IP:8013；auto=自己找（默认）")
    ap.add_argument("--room", default="卧室", help="报警里写的房间名")
    args = ap.parse_args(argv)
    if args.fetch:
        fetch_vendor()
        return 0
    missing = [rel for rel in VENDOR_FILES if not (VENDOR / rel).is_file()]
    if missing:
        print("缺少 %s，先运行：python -m vision.camlink.server --fetch" % ", ".join(missing))
        return 2
    from vision.camlink.alarm import AlarmChannel
    alarm = AlarmChannel(args.alarm, args.family, room=args.room)
    alarm.start()
    link = CamLink(alarm=alarm)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(link))
    server.daemon_threads = True
    tunnel = None if args.no_tunnel else start_tunnel(args.port, link)
    page = "http://127.0.0.1:%d/" % args.port
    print("CareMate 视频看护 · " + {"live": "疑似跌倒会通知家属 App", "dry": "报警只做格式检查（不响铃）",
                                      "off": "仅观察，不发报警"}[args.alarm])
    print("[电脑页面] " + page + "（二维码出现后，用 iPhone 相机扫一扫）")
    print("结束请按 Ctrl+C。")
    if not args.no_browser:
        try:
            webbrowser.open(page)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n已结束。")
    finally:
        server.server_close()
        alarm.close()
        if tunnel:
            tunnel.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
