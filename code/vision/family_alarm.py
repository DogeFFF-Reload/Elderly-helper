"""Bedside v5.7 alarm contract. No device access, redirects or system proxies.

`off` records locally; `dry` sends a server-side dry request; only `live`
creates an alarm. A rule event has one id, reused for all delivery attempts.
"""
import copy
from http.client import HTTPConnection, HTTPSConnection
import json
import math
import queue
import threading
import time
from urllib.parse import urlsplit
import uuid

DEFAULT_FAMILY = "http://10.153.47.252:8013"
MODES = ("off", "dry", "live")
MAX_RESPONSE = 64 * 1024


def base_url(value):
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in ("", "/", "/fall_alarm")):
        raise ValueError("床头/主脑地址须为 http(s)://IP:端口，不含账号、查询参数或其他路径")
    try:
        parts.port
    except ValueError:
        raise ValueError("服务端口无效") from None
    return "%s://%s" % (parts.scheme, parts.netloc)


def request_json(method, url, payload=None, timeout=3, max_bytes=MAX_RESPONSE):
    """Explicit connections neither follow redirects nor use proxy environment vars."""
    parts = urlsplit(url)
    connection = (HTTPSConnection if parts.scheme == "https" else HTTPConnection)(
        parts.hostname, parts.port, timeout=timeout)
    body = None if payload is None else json.dumps(
        payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request(method, path, body=body,
                           headers={"Content-Type": "application/json; charset=utf-8"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("服务返回非200状态")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("服务回执过大")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("服务回执必须是JSON对象")
        return result
    finally:
        connection.close()


def new_event_id():
    return "vision-" + uuid.uuid4().hex


class AlarmClient:
    def __init__(self, family=DEFAULT_FAMILY, *, mode="off", src="cam-A", timeout=3,
                 attempts=3, backoff=(1, 2), transport=request_json, sleep=time.sleep):
        if mode not in MODES:
            raise ValueError("报警模式须为 off、dry 或 live")
        if not isinstance(src, str) or not src.strip() or len(src) > 40:
            raise ValueError("来源标签须为1至40个字符")
        if not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("请求超时须大于0且不超过10秒")
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("每轮发送次数须为1至3次")
        if len(backoff) < attempts - 1 or any(
                not math.isfinite(v) or not 0 <= v <= 10 for v in backoff):
            raise ValueError("重试间隔无效")
        self.base, self.mode, self.src = base_url(family), mode, src
        self.timeout, self.attempts, self.backoff = timeout, attempts, backoff
        self.transport, self.sleep = transport, sleep

    def payload(self, event_id, *, dry=None):
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 64:
            raise ValueError("事件号须为1至64个字符")
        body = {"kind": "fall", "src": self.src, "event_id": event_id}
        if dry is True or (dry is None and self.mode == "dry"):
            body["dry"] = True
        # The rule detector has no calibrated probability. Never invent `conf`.
        return body

    def _receipt(self, response, body):
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError("床头未明确接受报警")
        dry = body.get("dry") is True
        fields = response.get("echo") if dry else response
        if not isinstance(fields, dict) or any(
                fields.get(k) != body[k] for k in ("kind", "src", "event_id")):
            raise ValueError("床头回执与本次事件不匹配")
        if dry:
            if response.get("dry") is not True:
                raise ValueError("床头未确认无扰模式")
            return {"status": "dry_accepted", "bedside_stored": False,
                    "phone_received": None}
        if (response.get("dry") is True or not isinstance(response.get("id"), str)
                or not response["id"] or type(response.get("dedup")) is not bool):
            raise ValueError("缺少报警编号或去重回执")
        return {"status": "bedside_accepted", "bedside_stored": True,
                "alarm_id": response["id"], "dedup": response["dedup"],
                "phone_received": None}

    def preflight(self):
        """Check v5.7+ before sending even a dry POST to an older receiver."""
        if self.mode == "off":
            return {"status": "disabled"}
        health = self.transport("GET", self.base + "/fall_alarm", timeout=self.timeout)
        if not isinstance(health, dict):
            raise ValueError("床头健康回执必须是JSON对象")
        try:
            version = tuple(int(n) for n in str(health.get("ver", "")).split(".")[:2])
        except (ValueError, AttributeError):
            version = ()
        if health.get("ok") is not True or len(version) != 2 or version < (5, 7):
            raise ValueError("床头须运行支持event_id和dry的v5.7或更新版本")
        body = self.payload("check-" + uuid.uuid4().hex, dry=True)
        response = self.transport("POST", self.base + "/fall_alarm", body, timeout=self.timeout)
        return dict(self._receipt(response, body), version=health["ver"])

    def deliver(self, event_id):
        body = self.payload(event_id)
        if self.mode == "off":
            return {"event_id": event_id, "status": "disabled", "attempts": 0,
                    "bedside_stored": False, "phone_received": None}
        error = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.transport("POST", self.base + "/fall_alarm", body,
                                          timeout=self.timeout)
                return dict(self._receipt(response, body), event_id=event_id, attempts=attempt)
            except Exception as exc:
                # Exceptions can include credential-bearing URLs. Record type only.
                error = type(exc).__name__
                if attempt < self.attempts:
                    self.sleep(self.backoff[attempt - 1])
        return {"event_id": event_id, "status": "failed", "attempts": self.attempts,
                "error_type": error, "bedside_stored": None, "phone_received": None}


class AlarmPoster:
    """A bounded background sender. Frames/coordinates never go to this endpoint."""
    accepts_photo = False

    def __init__(self, client, *, log=print, on_change=None, queue_size=32):
        self.client, self.log, self.on_change = client, log, on_change
        self.events, self.records = queue.Queue(maxsize=queue_size), []
        self.lock = threading.RLock()
        self.closed = False
        self.worker = threading.Thread(target=self._worker, name="family-alarm", daemon=True)
        self.worker.start()

    def skeleton(self, _kp, _fallen):
        pass

    def _changed(self, record):
        if self.on_change:
            self.on_change(copy.deepcopy(record))

    def fall(self, room):
        with self.lock:
            if self.closed:
                raise RuntimeError("报警发送器已关闭")
            record = {"event_id": new_event_id(), "room": room, "status": "queued", "attempts": 0}
            self.records.append(record)
            self._changed(record)  # Journal the id BEFORE starting the first request.
            try:
                self.events.put_nowait(record)
            except queue.Full:
                record.update(status="failed", error_type="queue_full")
                self._changed(record)
                self.log("[报警失败] 发送队列已满；事件号 %s" % record["event_id"])
            return record["event_id"]

    def _worker(self):
        while True:
            try:
                record = self.events.get(timeout=.05)
            except queue.Empty:
                if self.closed:
                    return
                continue
            try:
                result = self.client.deliver(record["event_id"])
                with self.lock:
                    record.update(result)
                    self._changed(record)
                label = {"disabled": "仅本地记录，未发送", "dry_accepted": "无扰格式检查通过，未建警报",
                         "bedside_accepted": "床头已收下报警；手机是否收到待现场确认",
                         "failed": "发送失败或回执未确认；送达情况未知"}[result["status"]]
                self.log("[报警] %s | %s | 尝试%d次" % (label, record["event_id"], result["attempts"]))
            except Exception as exc:
                with self.lock:
                    record.update(status="failed", error_type=type(exc).__name__)
                self.log("[报警失败] 发送/记录异常；保留事件号 %s" % record["event_id"])
            finally:
                self.events.task_done()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.records)

    def close(self, timeout=20):
        with self.lock:
            self.closed = True
        self.worker.join(timeout=timeout)
        return not self.worker.is_alive()
