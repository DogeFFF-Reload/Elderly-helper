"""床头盒中继（跑在 X5 上）：S3 屏录的老人留言 → 先存盘 → 无线转发给主脑 → 家属端；家属的语音留言 → X5 喇叭。

无线怎么走：S3 屏、X5、跑主脑的笔记本连同一个 Wi-Fi（现场热点 / 自带的随身路由器），全程 HTTP：

  S3 屏 ──Wi-Fi──▶ X5 本中继 :8014 ──Wi-Fi──▶ 主脑 :8000 ──▶ 家属端网页 /ui/family（不用点，自动响提示、自动播放）
  家属留言 ──▶ 主脑 ──▶ 本中继每 2 秒来拉一次 ──▶ X5 喇叭放给老人（文字留言由 voice/tts_player 念）

先存盘再转发：网络断了留言也不丢，连上后按顺序补发；主脑明确拒收（格式不对之类）的不再重试，记在 index.json 里。

给 S3 固件用的接口：
  POST /messages[?to=all|daughter|son]   正文 = 录好的音频：WAV，或 16 kHz/16 bit/单声道裸 PCM
                                          （Content-Type: application/octet-stream）。to 默认 all（发给所有家属）
                                          → {"ok": true, "id": "msg_20261024_193005", "duration": 23.5}
  POST /record/start | /record/stop       S3 屏没有麦克风时用：X5 用自己的 USB 麦克风录（arecord），stop 后同样存盘转发
  GET  /status                            {"recording", "pending" 待转发条数, "master" 主脑连得上吗, "unheard" 家属新留言条数}
  POST /play                              放最早一条没听过的家属语音留言（S3 屏上的“听留言”键）
家属手机直连兜底（和《家属端需求单》第 3 节一致，同一个热点就能用）：
  GET  /messages                          老人留言列表，新的在前
  GET  /messages/<id>.wav                 音频
  GET  /messages/inbox                    手机浏览器打开的收件箱网页

运行（X5，在 code/ 下）：
  python -m elder_link.relay --master http://<笔记本 IP>:8000 [--port 8014] [--dir ~/caremate_msgs] [--box-id hand-A]
"""
import argparse
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RATE, WIDTH, CHANNELS = 16000, 2, 1
MAX_SECONDS = 60.0
MAX_BYTES = 4 * 1024 * 1024
FORWARD_EVERY = 2.0
POLL_EVERY = 2.0
ANNOUNCE_WAIT = 4.0        # 主脑先念“女儿发来语音留言，我放给您听”，等这句念完再放
PLAY_FRESH = 60.0          # 中继刚启动时，只放 60 秒内的播放请求，不把以前的旧请求翻出来再放


def pcm_to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(WIDTH)
        w.setframerate(RATE)
        w.writeframes(pcm[: len(pcm) - len(pcm) % WIDTH])
    return buf.getvalue()


def wav_info(data):
    """合法 WAV → 时长（秒）；否则 None。"""
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def play_file(path):
    """放一段音频，放完才返回。X5：aplay；装了 ffmpeg 的用 ffplay；Windows 联调用 winsound；都没有就只打印。"""
    path = str(path)
    if path.endswith(".wav") and shutil.which("aplay"):
        cmd = ["aplay", "-q", path]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    elif sys.platform == "win32" and path.endswith(".wav"):
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
        return True
    else:
        print("[relay] 没有播放器（装 alsa-utils 的 aplay），跳过播放：%s" % path, flush=True)
        return False
    return subprocess.run(cmd, check=False).returncode == 0


class Recorder:
    """S3 屏没有麦克风时，用 X5 的 USB 麦克风录（arecord 输出裸 PCM）。"""

    def __init__(self):
        self.proc = None
        self.buf = bytearray()
        self.started = None
        self.reader = None

    @property
    def active(self):
        return self.proc is not None

    def start(self):
        if self.proc:
            return
        exe = shutil.which("arecord")
        if not exe:
            raise RuntimeError("X5 上没有 arecord（apt install alsa-utils），或者让 S3 屏自己录好再 POST /messages")
        self.buf = bytearray()
        self.proc = subprocess.Popen([exe, "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                                     stdout=subprocess.PIPE)
        self.started = time.time()

        def pump(proc):
            while True:
                chunk = proc.stdout.read(3200)
                if not chunk:
                    break
                self.buf.extend(chunk)
                if len(self.buf) >= MAX_SECONDS * RATE * WIDTH:
                    break
        self.reader = threading.Thread(target=pump, args=(self.proc,), daemon=True)
        self.reader.start()

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return b""
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.reader.join(timeout=2)
        return bytes(self.buf[: int(MAX_SECONDS * RATE * WIDTH)])


class Relay:
    def __init__(self, store, master, box_id="hand-A", to="all", utc_offset=8, player=play_file, clock=time.time):
        self.store = Path(store)
        self.store.mkdir(parents=True, exist_ok=True)
        self.master = master.rstrip("/")
        self.box_id, self.to, self.utc_offset = box_id, to, utc_offset
        self.player, self.clock = player, clock
        self.lock = threading.RLock()
        self.index_path = self.store / "index.json"
        self.items = self._load()
        self.recorder = Recorder()
        self.master_ok = False
        self.state = None            # 最近一次从主脑拉到的快照
        self.since = 0
        self.play_seq = None         # 已经处理过的播放请求序号；None = 还没拉过（启动时不放旧请求）
        self.unheard = []            # 家属发给老人、还没听的语音留言
        self.play_lock = threading.Lock()
        self.retry_at = 0.0
        self.backoff = FORWARD_EVERY

    # ---------- 存盘 ----------

    def _load(self):
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _wall(self, ts):
        return time.gmtime(ts + self.utc_offset * 3600)

    def add(self, data, ctype="", to=None):
        """收下一条老人留言（WAV 或裸 PCM）→ 存盘、排进转发队列。返回条目或抛 ValueError。"""
        if not data:
            raise ValueError("录音是空的")
        if len(data) > MAX_BYTES:
            raise ValueError("录音太大")
        secs = wav_info(data)
        if secs is None:
            if "wav" in (ctype or "").lower():
                raise ValueError("不是合法的 WAV")
            data = pcm_to_wav(data)   # 裸 PCM：按 16k/16bit/单声道包成 WAV
            secs = wav_info(data)
        if secs < 0.5:
            raise ValueError("说话时间太短")
        with self.lock:
            now = self.clock()
            base = time.strftime("msg_%Y%m%d_%H%M%S", self._wall(now))
            mid, n = base, 1
            while (self.store / (mid + ".wav")).exists():
                n += 1
                mid = "%s_%d" % (base, n)
            (self.store / (mid + ".wav")).write_bytes(data)
            item = {"id": mid, "time": time.strftime("%Y-%m-%d %H:%M:%S", self._wall(now)), "ts": now,
                    "duration": round(min(secs, MAX_SECONDS), 1), "to": to or self.to,
                    "forwarded": False, "voice_id": None, "error": None}
            self.items.append(item)
            self._save()
            self.retry_at = 0.0   # 新留言立刻试着发
            self.backoff = FORWARD_EVERY
        print("[relay] 收到老人留言 %s（%.1f 秒）→ 排队转发给 %s" % (mid, item["duration"], item["to"]), flush=True)
        return item

    # ---------- 上行：转发给主脑 ----------

    def forward_once(self):
        """把还没转发的按顺序发给主脑；网络不通就等下一轮（逐步放慢到 30 秒一次）。返回这轮发成功的条数。"""
        if self.clock() < self.retry_at:
            return 0
        sent = 0
        with self.lock:
            pending = [i for i in self.items if not i["forwarded"] and not i["error"]]
        for item in pending:
            q = urllib.parse.urlencode({"from": "elder", "to": item["to"], "dur": item["duration"],
                                        "elder": self.box_id})
            data = (self.store / (item["id"] + ".wav")).read_bytes()
            req = urllib.request.Request("%s/api/voice_msg?%s" % (self.master, q), data=data, method="POST",
                                         headers={"Content-Type": "audio/wav"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    res = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                res = {"ok": False, "error": "HTTP %d" % e.code}
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.master_ok = False
                self.retry_at = self.clock() + self.backoff
                self.backoff = min(self.backoff * 2, 30.0)
                print("[relay] 主脑连不上（%s），%g 秒后重试；留言已存盘不会丢" % (e, self.backoff), flush=True)
                return sent
            self.master_ok = True
            self.backoff = FORWARD_EVERY
            with self.lock:
                if res.get("ok"):
                    item.update(forwarded=True, voice_id=res.get("voice_id"))
                    sent += 1
                    print("[relay] 已转发 %s → 家属端（主脑语音 #%s）" % (item["id"], res.get("voice_id")), flush=True)
                else:
                    item["error"] = res.get("error") or "主脑拒收"
                    print("[relay] 主脑拒收 %s：%s（不再重试）" % (item["id"], item["error"]), flush=True)
                self._save()
        return sent

    # ---------- 下行：家属留言放给老人 ----------

    def poll_once(self):
        """拉一次主脑快照（顺带报到 client=elder-box），有发给床头盒的播放请求就放。"""
        url = "%s/api/poll?since=%d&client=elder-box" % (self.master, self.since)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            self.master_ok = False
            return None
        self.master_ok = True
        self.since = data.get("last_id", self.since)
        st = self.state = data["state"]
        self.unheard = [m for m in st.get("messages", []) if m.get("kind") == "voice"
                        and m.get("to") == "elder" and not m.get("heard")]
        req = (st.get("comms") or {}).get("play")
        if req is None:
            return None
        if self.play_seq is None:   # 刚启动：之前的请求不补放，太新的（刚收到）照放
            self.play_seq = req["seq"] - (1 if st["now"] - req.get("ts", 0) < PLAY_FRESH else 0)
        if req["seq"] <= self.play_seq:
            return None
        self.play_seq = req["seq"]
        if req.get("target") != "box":
            return None
        threading.Thread(target=self.play_voice, args=(req["id"], req.get("auto")), daemon=True).start()
        return req["id"]

    def play_voice(self, voice_id, auto=False):
        with self.play_lock:   # 一条一条放，不叠在一起
            if auto:
                time.sleep(ANNOUNCE_WAIT)
            try:
                with urllib.request.urlopen("%s/api/voice_msg?id=%s" % (self.master, voice_id), timeout=10) as r:
                    data, ctype = r.read(), r.headers.get("Content-Type", "")
            except (urllib.error.URLError, OSError) as e:
                print("[relay] 取家属留言 #%s 失败：%s" % (voice_id, e), flush=True)
                return False
            suffix = ".wav" if "wav" in ctype else ".webm" if "webm" in ctype else ".audio"
            fd, path = tempfile.mkstemp(prefix="family_%s_" % voice_id, suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                print("[relay] 放家属留言 #%s" % voice_id, flush=True)
                ok = self.player(path)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if ok:
                body = json.dumps({"id": voice_id, "party": "elder"}).encode("utf-8")
                req = urllib.request.Request(self.master + "/api/voice_msg/heard", data=body, method="POST",
                                             headers={"Content-Type": "application/json; charset=utf-8"})
                try:
                    urllib.request.urlopen(req, timeout=5).close()
                except (urllib.error.URLError, OSError):
                    pass
            return ok

    def play_next(self):
        """S3 屏“听留言”键：最早一条没听过的；都听过就放最近一条。"""
        msgs = [m for m in ((self.state or {}).get("messages") or []) if m.get("kind") == "voice" and m.get("to") == "elder"]
        if not msgs:
            return None
        m = self.unheard[0] if self.unheard else msgs[-1]
        threading.Thread(target=self.play_voice, args=(m["voice_id"],), daemon=True).start()
        return m["voice_id"]

    # ---------- 输出 ----------

    def status(self):
        with self.lock:
            pending = sum(1 for i in self.items if not i["forwarded"] and not i["error"])
        return {"ok": True, "box": self.box_id, "recording": self.recorder.active, "pending": pending,
                "master": self.master_ok, "unheard": len(self.unheard)}

    def listing(self):
        with self.lock:
            return [{k: i[k] for k in ("id", "time", "duration", "to", "forwarded", "error")}
                    for i in reversed(self.items)]

    def run_background(self, stop):
        def loop(fn, every):
            while not stop.is_set():
                try:
                    fn()
                except Exception as e:   # 后台线程不能死：记一笔，下一轮再来
                    print("[relay] %s 出错：%r" % (fn.__name__, e), flush=True)
                stop.wait(every)
        for fn, every in ((self.forward_once, FORWARD_EVERY), (self.poll_once, POLL_EVERY)):
            threading.Thread(target=loop, args=(fn, every), daemon=True).start()


INBOX_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>老人留言</title>
<style>
body{margin:0;background:#f4f1ec;color:#1f2328;font:16px/1.5 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:22px;margin:4px 0 2px}p.sub{color:#6b7280;margin:0 0 18px;font-size:14px}
.item{background:#fff;border-radius:16px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.meta{display:flex;justify-content:space-between;color:#6b7280;font-size:13px;margin-bottom:8px}
.meta b{color:#1f2328;font-size:15px}audio{width:100%%}
.tag{font-size:12px;padding:1px 8px;border-radius:99px;background:#e7f6ee;color:#0f8a5f}
.tag.wait{background:#fff3df;color:#b26b00}.empty{color:#9ca3af;text-align:center;padding:40px 0}
</style></head><body><main><h1>老人留言</h1><p class="sub">床头盒 %(box)s · 新的在上面 · 每 10 秒自动刷新</p>%(rows)s</main>
<script>setTimeout(()=>location.reload(),10000)</script></body></html>"""


def inbox_html(relay):
    rows = []
    for i in relay.listing():
        tag = '<span class="tag">已送到家属端</span>' if i["forwarded"] else \
              '<span class="tag wait">%s</span>' % html.escape(i["error"] or "等网络，稍后自动补发")
        rows.append('<div class="item"><div class="meta"><b>%s</b><span>%.0f 秒 %s</span></div>'
                    '<audio controls preload="none" src="/messages/%s.wav"></audio></div>'
                    % (html.escape(i["time"]), i["duration"], tag, urllib.parse.quote(i["id"])))
    return INBOX_PAGE % {"box": html.escape(relay.box_id),
                         "rows": "".join(rows) or '<div class="empty">还没有留言</div>'}


def make_handler(relay):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/status":
                return self._json(relay.status())
            if path == "/messages":
                return self._json({"ok": True, "messages": relay.listing()})
            if path in ("/messages/inbox", "/"):
                return self._send(200, inbox_html(relay).encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/messages/") and path.endswith(".wav"):
                mid = urllib.parse.unquote(path[len("/messages/"):-4])
                f = relay.store / (mid + ".wav")
                if "/" in mid or "\\" in mid or ".." in mid or not f.is_file():
                    return self._json({"ok": False, "error": "没有这条留言"}, 404)
                return self._send(200, f.read_bytes(), "audio/wav")
            self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path.rstrip("/")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BYTES:
                return self._json({"ok": False, "error": "录音太大"}, 413)
            body = self.rfile.read(n) if n else b""
            try:
                if path == "/messages":
                    item = relay.add(body, self.headers.get("Content-Type", ""), q.get("to"))
                    return self._json({"ok": True, "id": item["id"], "duration": item["duration"]})
                if path == "/record/start":
                    relay.recorder.start()
                    return self._json({"ok": True, "recording": True})
                if path == "/record/stop":
                    item = relay.add(relay.recorder.stop(), "application/octet-stream", q.get("to"))
                    return self._json({"ok": True, "id": item["id"], "duration": item["duration"]})
                if path == "/play":
                    vid = relay.play_next()
                    return self._json({"ok": vid is not None, "voice_id": vid} if vid else
                                      {"ok": False, "error": "没有家属留言"})
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 422)
            except RuntimeError as e:
                return self._json({"ok": False, "error": str(e)}, 503)
            self._json({"ok": False, "error": "not found"}, 404)

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description="床头盒中继：S3 屏留言 ⇄ 主脑 ⇄ 家属端")
    ap.add_argument("--master", default=os.environ.get("CAREMATE_MASTER", "http://127.0.0.1:8000"),
                    help="主脑地址，如 http://192.168.1.20:8000（也可设环境变量 CAREMATE_MASTER）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8014)
    ap.add_argument("--dir", default=os.path.expanduser("~/caremate_msgs"), help="留言存盘目录")
    ap.add_argument("--box-id", default="hand-A")
    ap.add_argument("--to", default="all", help="老人留言默认发给谁：all / 联系人 id")
    ap.add_argument("--utc-offset", type=float, default=8, help="文件名里的时间按哪个时区（默认北京时间）")
    args = ap.parse_args()
    relay = Relay(args.dir, args.master, args.box_id, args.to, args.utc_offset)
    stop = threading.Event()
    relay.run_background(stop)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(relay))
    server.daemon_threads = True
    print("[relay] 床头盒中继 %s 监听 :%d，主脑 %s，存盘 %s" % (args.box_id, args.port, args.master, args.dir), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
