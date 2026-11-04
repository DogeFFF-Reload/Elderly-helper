"""Read-only latest-frame monitor. No device, alarm or upstream HTTP client here.

The detector hands off one replaceable frame to a JPEG worker. Each browser
reads the same in-memory JPEG; slow viewers cannot queue camera observations.
"""
from collections import deque
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit


def encode_frame(frame, kp, detector):
    import cv2
    from vision.preview import Preview
    if frame is not None:
        h, w = frame.shape[:2]
        if max(h, w) > 960:
            scale = 960 / max(h, w)
            frame = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))
    image = Preview(detector.rules).render(frame, kp, detector, diagnostics=False)
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return data.tobytes()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        # Disconnecting a viewer must not print its request or interrupt vision.
        pass


class WebMonitor:
    def __init__(self, *, host="127.0.0.1", port=8015, mode="off", input_kind="phone_mjpeg",
                 stale_after=3.0, fps=5, encoder=encode_frame, clock=time.monotonic):
        if host not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("监看绑定地址须为127.0.0.1或0.0.0.0")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("监看端口无效")
        if mode not in ("off", "dry", "live"):
            raise ValueError("监看模式无效")
        if not math.isfinite(stale_after) or not .5 <= stale_after <= 30:
            raise ValueError("画面过期阈值须为0.5至30秒")
        if not math.isfinite(fps) or not 0 < fps <= 10:
            raise ValueError("监看帧率须大于0且不超过10")
        self.mode, self.input_kind = mode, input_kind
        self.stale_after, self.interval = stale_after, 1 / fps
        self.encoder, self.clock = encoder, clock
        self.condition = threading.Condition()
        self.closed = False
        self.packet = self.jpeg = None
        self.frame_at = self.jpeg_at = None
        self.sequence = self.jpeg_sequence = 0
        self.next_encode = -math.inf
        self.times = deque(maxlen=120)
        self.input_state = "waiting"
        self.run_status = "starting"
        self.input_message = None
        self.alarm_count = 0
        self.latest_alarm = None
        self.preflight = None
        self.render_error = None
        self.served_frames = 0
        self.page = (Path(__file__).with_name("monitor.html")).read_bytes()
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def log_message(self, *_args):
                pass

            def reply(self, code, data, content_type):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                 "style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; "
                                 "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/":
                    self.reply(200, monitor.page, "text/html; charset=utf-8")
                elif path == "/api/status":
                    self.reply(200, json.dumps(monitor.snapshot(), ensure_ascii=False,
                                              allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")
                elif path == "/frame.jpg":
                    data = monitor.frame()
                    self.reply(200 if data else 503, data or b"No fresh frame", "image/jpeg" if data else "text/plain")
                else:
                    self.reply(404, b"Not found", "text/plain")

            def do_POST(self):
                self.close_connection = True
                self.reply(405, b"Read-only monitor", "text/plain")

        self.server = _Server((host, port), Handler)
        self.address = self.server.server_address
        self.worker = threading.Thread(target=self._render, name="monitor-jpeg", daemon=True)
        self.http = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.1),
                                     name="monitor-http", daemon=True)
        self.worker.start()
        self.http.start()

    def update(self, *, run_status=None, alarm=None, preflight=None, input_message=None):
        with self.condition:
            if run_status is not None:
                self.run_status = run_status
            if input_message is not None:
                # The caller supplies a fixed, credential-free diagnostic.
                from camera.phone_calibrate import CAMERA_ERROR_MESSAGES
                self.input_message = input_message if input_message in CAMERA_ERROR_MESSAGES.values() else None
            if alarm is not None:
                # All lifecycle updates for one event keep the same id.
                if not self.latest_alarm or self.latest_alarm["event_id"] != alarm["event_id"]:
                    if self.latest_alarm and alarm.get("status") != "queued":
                        return  # An older event's receipt must not replace a newer queued event.
                    self.alarm_count += 1
                self.latest_alarm = copy.deepcopy(alarm)
            if preflight is not None:
                self.preflight = preflight.get("status")

    def show(self, frame, kp, detector):
        now = self.clock()
        with self.condition:
            if self.closed:
                return True
            self.frame_at = now
            self.sequence += 1
            self.times.append(now)
            self.input_state = getattr(detector, "input_status", "waiting")
            self.run_status = "running"
            if now < self.next_encode:
                return True
            self.next_encode = now + self.interval
            # Copy mutable values before the detector advances to the next frame.
            state = SimpleNamespace(**{name: copy.deepcopy(getattr(detector, name)) for name in (
                "rules", "state", "last", "standing_hip", "standing_len")}, input_status=self.input_state)
            self.packet = (self.sequence, now, frame.copy() if frame is not None else None,
                           copy.deepcopy(kp), state)
            self.condition.notify()
        return True

    def _render(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.packet is not None)
                if self.closed:
                    return
                sequence, received, frame, kp, state = self.packet
                self.packet = None
            try:
                data = self.encoder(frame, kp, state)
                if not isinstance(data, bytes) or not data or len(data) > 2 * 1024 * 1024:
                    raise ValueError("Invalid JPEG size")
                with self.condition:
                    self.jpeg, self.jpeg_at, self.jpeg_sequence = data, received, sequence
                    self.render_error = None
            except Exception as exc:
                with self.condition:
                    self.render_error = type(exc).__name__
                    self.jpeg = None

    def snapshot(self):
        with self.condition:
            now = self.clock()
            age = None if self.frame_at is None else max(0, now - self.frame_at)
            jpeg_age = None if self.jpeg_at is None else max(0, now - self.jpeg_at)
            active = not self.closed and self.run_status in ("starting", "running")
            stale = not active or age is None or age > self.stale_after
            span = self.times[-1] - self.times[0] if len(self.times) > 1 else 0
            fps = (len(self.times) - 1) / span if span else 0
            return {"mode": self.mode, "input_kind": self.input_kind, "run_status": self.run_status,
                    "input_message": self.input_message,
                    "input_state": "input_error" if stale and age is not None else self.input_state,
                    "stale": stale, "frame_age_s": None if age is None else round(age, 2),
                    "frame_ready": bool(self.jpeg) and not stale and jpeg_age <= self.stale_after,
                    "sequence": self.sequence, "jpeg_sequence": self.jpeg_sequence,
                    "processing_fps": 0 if stale else round(fps, 1), "render_error": self.render_error,
                    "alarm_count": self.alarm_count, "latest_alarm": copy.deepcopy(self.latest_alarm),
                    "preflight": self.preflight, "phone_received": None,
                    "frames_saved": False}

    def frame(self):
        with self.condition:
            if not self.snapshot()["frame_ready"]:
                return None
            self.served_frames += 1
            return self.jpeg

    def close(self):
        with self.condition:
            self.closed = True
            self.packet = self.jpeg = None
            self.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.http.join(timeout=2)
        self.worker.join(timeout=2)


class MonitorPreview:
    """One detector feeds both optional views; closing a browser stops no service."""
    def __init__(self, monitor, preview=None):
        self.monitor, self.preview = monitor, preview

    def show(self, frame, kp, detector):
        self.monitor.show(frame, kp, detector)
        return self.preview.show(frame, kp, detector) if self.preview else True

    def close(self):
        if self.preview:
            self.preview.close()
