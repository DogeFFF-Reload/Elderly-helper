"""HTTP JPEG/MJPEG input; camera requests bypass system and environment proxies."""
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 256 * 1024


class CameraPageError(ValueError):
    """A camera homepage did not advertise an unambiguous live MJPEG image."""


def live_image_url(page, base):
    """Read a same-origin image reference, never execute the camera's webpage.

    Some phone apps serve HTML at / and MJPEG at an image's src. Do not guess
    endpoint names, follow archive links, or load third-party page resources.
    """
    images = []

    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag != "img":
                return
            attrs = dict(attrs)
            src = attrs.get("src", "").strip()
            if not src:
                return
            target = urljoin(base, src)
            parts, origin = urlsplit(target), urlsplit(base)
            if ((parts.scheme, parts.hostname, parts.port) != (origin.scheme, origin.hostname, origin.port)
                    or parts.username is not None or parts.password is not None or parts.fragment
                    or target == base):
                return
            label = " ".join(attrs.get(key, "") for key in ("id", "class", "alt")).lower()
            # Prefer the page's explicitly named camera image over its logo.
            live = any(word in label for word in ("video", "stream", "mjpeg", "camera", "live"))
            images.append((target, live))

    Images().feed(page)
    marked = {url for url, live in images if live}
    candidates = marked or {url for url, _live in images}
    if len(candidates) != 1:
        raise CameraPageError("摄像头首页未提供唯一的视频流地址，请在原网页画面上右键复制图片地址后重试。")
    return candidates.pop()


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
            # The homepage and its MJPEG image can be sibling paths. Scope
            # credentials to this camera origin rather than just the first path.
            passwords.add_password(None, "%s://%s/" % (parts.scheme, parts.netloc), username, password or "")
            handlers.extend((urllib.request.HTTPDigestAuthHandler(passwords),
                             urllib.request.HTTPBasicAuthHandler(passwords)))
        self.opener = urllib.request.build_opener(*handlers)

    def frames(self):
        if self.mjpeg:
            with self.opener.open(self.url, timeout=5) as response:
                content_type = response.headers.get_content_type()
                if content_type == "multipart/x-mixed-replace":
                    yield from iter_mjpeg(response)
                    return
                if content_type != "text/html":
                    raise ValueError("地址没有返回 MJPEG 视频流（收到 %s）" % content_type)
                page = response.read(MAX_PAGE_BYTES + 1)
                if len(page) > MAX_PAGE_BYTES:
                    raise CameraPageError("摄像头首页过大，请复制原网页画面中的图片地址后重试。")
                stream = live_image_url(page.decode(response.headers.get_content_charset() or "utf-8",
                                                   errors="replace"), response.geturl())
            with self.opener.open(stream, timeout=5) as response:
                if response.headers.get_content_type() != "multipart/x-mixed-replace":
                    raise CameraPageError("网页中的图片不是MJPEG直播流，请复制实际直播画面的图片地址后重试。")
                yield from iter_mjpeg(response)
        else:
            while True:
                with self.opener.open(self.url, timeout=5) as response:
                    frame = response.read(MAX_FRAME_BYTES + 1)
                if len(frame) > MAX_FRAME_BYTES:
                    raise ValueError("单帧画面超过 8 MB，请降低手机分辨率")
                yield frame
