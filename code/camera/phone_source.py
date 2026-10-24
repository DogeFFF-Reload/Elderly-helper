"""HTTP JPEG/MJPEG input; camera requests bypass system and environment proxies."""
import urllib.request
from urllib.parse import urlsplit

MAX_FRAME_BYTES = 8 * 1024 * 1024


def iter_mjpeg(response):
    """Read JPEGs even when multipart headers omit Content-Length.

    read1() avoids waiting for a full buffer on a live HTTP response. Marker
    parsing also handles MIME boundaries/markers split across socket reads.
    """
    buffer = bytearray()
    read = getattr(response, "read1", response.read)
    while True:
        chunk = read(64 * 1024)
        if not chunk:
            raise ConnectionError("视频流已断开，请保持手机 App 在前台")
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                # Preserve a possible first byte of the next JPEG marker.
                del buffer[:-1]
                break
            if start:
                del buffer[:start]
            end = buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(buffer) > MAX_FRAME_BYTES:
                    raise ValueError("单帧画面超过 8 MB，请降低手机分辨率")
                break
            end += 2
            if end > MAX_FRAME_BYTES:
                raise ValueError("单帧画面超过 8 MB，请降低手机分辨率")
            frame = bytes(buffer[:end])
            del buffer[:end]
            yield frame


class PhoneFrameSource:
    def __init__(self, url, *, mjpeg=False, username=None, password=None):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("摄像头地址必须是完整的 http:// 或 https:// 地址")
        if parts.username is not None or parts.password is not None:
            raise ValueError("请用 --username / --password 提供账号，不要放进网址")
        if password is not None and username is None:
            raise ValueError("设置密码时也需要 --username")
        self.url = url
        self.mjpeg = mjpeg
        handlers = [urllib.request.ProxyHandler({})]
        if username is not None:
            passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            passwords.add_password(None, url, username, password or "")
            handlers.append(urllib.request.HTTPBasicAuthHandler(passwords))
        self.opener = urllib.request.build_opener(*handlers)

    def frames(self):
        if self.mjpeg:
            with self.opener.open(self.url, timeout=5) as response:
                content_type = response.headers.get_content_type()
                if content_type != "multipart/x-mixed-replace":
                    raise ValueError("地址没有返回 MJPEG 视频流（收到 %s）" % content_type)
                yield from iter_mjpeg(response)
        else:
            while True:
                with self.opener.open(self.url, timeout=5) as response:
                    frame = response.read(MAX_FRAME_BYTES + 1)
                if len(frame) > MAX_FRAME_BYTES:
                    raise ValueError("单帧画面超过 8 MB，请降低手机分辨率")
                yield frame
