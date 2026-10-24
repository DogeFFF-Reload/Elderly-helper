"""视频看护 → 床头板 → 家属 App 的跌倒报警通道。

发送沿用仓库原来的 vision.family_alarm（v5.7 合约：POST /fall_alarm {kind, src, event_id}，
同一事件号重试去重；dry=true 只核对格式不响铃）。家属 App 轮询床头板的 /alarms 后全屏警铃。

床头板地址会随热点换网段而变（docs/36），所以和家属 App v1.5 一样自己找：
  先试上次找到的地址 → 不通就扫本机所在各 /24 网段的 8013 端口，用 GET /fall_alarm（只读健康检查）验身。
每 60 秒做一次无扰检查（dry，不建警报、不响铃）；不通就重新找板子。

模式：off = 只在大屏提示；dry = 只做格式检查；live = 规则判定疑似跌倒后真的发到家属 App。
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import threading
import time

import config
from vision.family_alarm import AlarmClient, AlarmPoster, MODES, request_json

PORT = 8013
SAVED = config.CODE_DIR / "data" / "camlink" / "board.json"
RECHECK_S = 60.0
RETRY_S = 15.0


def local_ipv4s():
    """本机各网卡的 IPv4（不含回环和 169.254 自动地址）。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:   # 默认路由那块网卡（不真的发包）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def is_board(base, transport=request_json, timeout=1.5):
    """GET /fall_alarm 只读健康检查：床头服务且版本 ≥ 5.7 才算。"""
    try:
        health = transport("GET", base + "/fall_alarm", timeout=timeout)
        version = tuple(int(n) for n in str(health.get("ver", "")).split(".")[:2])
        return health.get("ok") is True and len(version) == 2 and version >= (5, 7)
    except Exception:
        return False


def port_open(ip, port=PORT, timeout=0.35):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_boards(ips=None, check=is_board, opener=port_open):
    """扫本机所在各 /24 网段的 8013，返回第一个验身通过的 http://IP:8013。"""
    hosts = []
    for ip in ips if ips is not None else local_ipv4s():
        prefix = ip.rsplit(".", 1)[0]
        hosts += ["%s.%d" % (prefix, n) for n in range(1, 255) if "%s.%d" % (prefix, n) != ip]
    with ThreadPoolExecutor(max_workers=64) as pool:
        open_hosts = [h for h, ok in zip(hosts, pool.map(opener, hosts)) if ok]
    for host in open_hosts:
        base = "http://%s:%d" % (host, PORT)
        if check(base):
            return base
    return None


def find_board(explicit=None, saved=SAVED, check=is_board, scan=scan_boards):
    if explicit and explicit != "auto":
        return explicit.rstrip("/") if check(explicit.rstrip("/")) else None
    try:
        last = json.loads(Path(saved).read_text(encoding="utf-8")).get("base")
    except (OSError, ValueError, AttributeError):
        last = None
    if last and check(last):
        return last
    base = scan()
    if base:
        try:
            Path(saved).parent.mkdir(parents=True, exist_ok=True)
            Path(saved).write_text(json.dumps({"base": base}) + "\n", encoding="utf-8")
        except OSError:
            pass
    return base


class AlarmChannel:
    def __init__(self, mode="live", family="auto", *, src="camlink", room="现场", log=print,
                 finder=find_board, client_factory=AlarmClient, poster_factory=AlarmPoster,
                 clock=time.monotonic):
        if mode not in MODES:
            raise ValueError("报警模式须为 off、dry 或 live")
        self.mode, self.family, self.src, self.room, self.log = mode, family, src, room, log
        self.finder, self.client_factory, self.poster_factory, self.clock = finder, client_factory, poster_factory, clock
        self.lock = threading.RLock()
        self.client = self.poster = None
        self.records = {}
        self.state = {"mode": mode, "base": None, "ready": mode == "off", "version": None,
                      "error": None, "checked": None, "searching": False}
        self.stopping = threading.Event()
        self.thread = None

    # ---------- 板子 ----------
    def connect_once(self):
        """找板子 + 无扰检查一次；返回是否就绪。"""
        if self.mode == "off":
            return True
        with self.lock:
            self.state["searching"] = self.state["base"] is None
        base = self.state["base"] if self.state["base"] and self.client else None
        try:
            if base is None:
                base = self.finder(self.family)
                if not base:
                    raise LookupError("没找到床头板（8013）")
                client = self.client_factory(base, mode=self.mode, src=self.src)
                with self.lock:
                    old = self.poster
                    self.client = client
                    self.poster = self.poster_factory(client, log=self.log, on_change=self._changed)
                    self.state["base"] = base
                if old:
                    old.close(timeout=1)
            pre = self.client.preflight()          # dry：只核对格式，不建警报、不响铃
            with self.lock:
                self.state.update(ready=True, version=pre.get("version"), error=None, checked=time.strftime("%H:%M:%S"),
                                  searching=False)
            return True
        except Exception as exc:
            with self.lock:
                self.state.update(ready=False, error=str(exc) if isinstance(exc, (LookupError, ValueError)) else type(exc).__name__,
                                  base=None, searching=False)
            return False

    def start(self):
        if self.mode == "off" or self.thread:
            return

        def loop():
            while not self.stopping.is_set():
                ok = self.connect_once()
                self.stopping.wait(RECHECK_S if ok else RETRY_S)
        self.thread = threading.Thread(target=loop, daemon=True, name="camlink-alarm")
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.poster:
            self.poster.close(timeout=5)

    # ---------- 事件 ----------
    def _changed(self, record):
        with self.lock:
            self.records[record["event_id"]] = record

    def fall(self):
        """规则判定一次疑似跌倒：live 发到家属 App；返回事件号（off / 没板子时只记本地）。"""
        with self.lock:
            if self.mode == "off":
                return None
            if self.poster is None:
                rec = {"event_id": "local-%d" % int(time.time() * 1000), "status": "failed", "error_type": "no_board",
                       "attempts": 0}
                self.records[rec["event_id"]] = rec
                return rec["event_id"]
            poster = self.poster
        return poster.fall(self.room)

    def send_test(self):
        """手动测试：给家属 App 发一条真实报警（来源 camlink-test）。只在 live 模式、板子就绪时可用。"""
        with self.lock:
            base, ready = self.state["base"], self.state["ready"]
        if self.mode != "live" or not base or not ready:
            raise RuntimeError("报警通道未就绪")
        client = self.client_factory(base, mode="live", src=self.src + "-test")
        from vision.family_alarm import new_event_id
        result = client.deliver(new_event_id())
        with self.lock:
            self.records[result["event_id"]] = dict(result, test=True)
        return result

    def record(self, event_id):
        with self.lock:
            r = self.records.get(event_id)
            return dict(r) if r else None

    def snapshot(self):
        with self.lock:
            return dict(self.state)
