"""台词双通道：scripted（默认，零网络）/ llm（OpenAI 兼容接口）。

LLM 只管"怎么说"：输入台词键 + 参数，输出一句话；不返回任何动作。
llm 通道的四道闸（§1.3 建议：超时自动回落 scripted，保证现场断网也不卡）：
  1. 白名单：只有 LLM_KEYS 里的台词交给 LLM 润色；保命链、120、健康复述、教学步骤
     这类台词永远走 scripted。新加的台词键默认不进白名单。
  2. 超时：单句超过 llm_timeout 秒就用 scripted（整句计时，不是 socket 计时）。
  3. 冷却：网络/接口出错一次后 llm_cooldown 秒内直接 scripted，断网时不再每句干等。
  4. 内容检查：可以省略，不能编——不许出现 scripted 版没有的数字、联系人称呼，
     不许冒出"120/拨打/求助"这类 scripted 版没有的承诺；不许英文和符号（TTS 会念出来）。不过关就用 scripted。
"""
import json
import queue
import re
import threading
import time
import urllib.request
from pathlib import Path

LINES_PATH = Path(__file__).resolve().parent / "lines.json"

LLM_KEYS = frozenset({
    "cancel_ok", "call_family", "deliver_start", "deliver_unknown",
    "exercise_start", "exercise_done", "exercise_stop", "rps_booth",
    "message_sent", "message_empty", "safe_report", "echo_off",
    "water_saved", "elder_ok_idle", "chitchat",
})

CLAIM_WORDS = ("120", "拨打", "急救", "救护车", "报警", "求助", "医生", "吃药", "诊断")
_NUM = re.compile(r"\d+")
_BAD = re.compile(r"[A-Za-z]{2,}|[/\\*#<>\[\]{}|`~@$^_=+]")

SYSTEM_PROMPT = (
    "你是床边陪护机器人 CareMate 的说话润色器，听的人是独居老人。"
    "把给你的参考台词改写成更自然、温暖的一句中文口语。"
    "规则：只输出这一句话；不超过 {max_len} 个字；参考台词里的数字和称呼原样保留，"
    "不要新增数字或别的称呼；不要承诺或声称做了参考台词里没有的事；"
    "不要英文、符号、表情和引号以外的标点花样。"
)


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


class LLMClient:
    def __init__(self, mode="scripted", lines_path=LINES_PATH, base_url="", api_key="", model="",
                 timeout=3.0, cooldown=60.0, keep_words=(), transport=None, now=time.monotonic):
        self.mode = mode
        self.lines = {k: v for k, v in json.loads(Path(lines_path).read_text(encoding="utf-8")).items()
                      if not k.startswith("_")}
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.cooldown = cooldown
        self.keep_words = tuple(keep_words)
        self.transport = transport  # 测试注入；None = 走 HTTP
        self.now = now
        self.cooldown_until = 0.0
        self.stats = {"llm": 0, "fallback": 0, "last_error": None}

    @classmethod
    def from_config(cls, cfg, transport=None):
        return cls(cfg["llm_mode"], base_url=cfg["llm_base_url"], api_key=cfg["llm_api_key"],
                   model=cfg["llm_model"], timeout=cfg["llm_timeout"], cooldown=cfg["llm_cooldown"],
                   keep_words=[c["name"] for c in cfg["contacts"]], transport=transport)

    @property
    def enabled(self):
        """llm 模式且接口配全（或注入了 transport）才算开着。"""
        return self.mode == "llm" and (self.transport is not None or bool(self.base_url and self.model))

    def scripted(self, key, **params):
        template = self.lines.get(key)
        if template is None:
            return ""
        return template.format_map(_SafeDict(params))

    say = scripted  # 兼容旧调用：say() 永远是 scripted，不碰网络

    def render(self, key, params, abort=lambda: False):
        """返回 (text, info)。info["source"] 是 llm / scripted；回落时 info["reason"] 说明原因，
        info["tripped"] 为 True 表示这次失败刚进入冷却（总线据此只记一条事件，不刷屏）。"""
        text = self.scripted(key, **params)
        info = {"source": "scripted", "reason": None, "tripped": False}
        if not self.enabled or key not in LLM_KEYS or not text:
            return text, info
        if self.now() < self.cooldown_until:
            return self._fallback(text, info, "cooldown")
        try:
            out = self._call(self._messages(text, params), abort)
        except _Aborted:
            return self._fallback(text, info, "queue_busy")
        except Exception as e:  # noqa: BLE001 网络、超时、接口格式，一律回落
            self.cooldown_until = self.now() + self.cooldown
            self.stats["last_error"] = "%s: %s" % (type(e).__name__, e)
            info["tripped"] = True
            return self._fallback(text, info, "timeout" if isinstance(e, TimeoutError) else "error")
        problem = self.check(out, text)
        if problem:
            return self._fallback(text, info, "rejected: " + problem)
        self.stats["llm"] += 1
        info["source"] = "llm"
        return out, info

    def check(self, out, scripted):
        """内容检查：返回问题描述，没问题返回 None。"""
        if not out:
            return "空回复"
        if len(out) > max(60, 2 * len(scripted)):
            return "太长"
        if _BAD.search(out):
            return "含英文或符号"
        extra = set(_NUM.findall(out)) - set(_NUM.findall(scripted))
        if extra:
            return "多出了数字 %s" % "、".join(sorted(extra))
        for w in self.keep_words:
            if w in out and w not in scripted:
                return "多出了称呼“%s”" % w
        for w in CLAIM_WORDS:
            if w in out and w not in scripted:
                return "多出了“%s”" % w
        return None

    def status(self):
        left = self.cooldown_until - self.now()
        return dict(self.stats, mode=self.mode, enabled=self.enabled,
                    cooldown_left=round(left, 1) if left > 0 else 0)

    # ---------- 内部 ----------

    def _fallback(self, text, info, reason):
        self.stats["fallback"] += 1
        info["reason"] = reason
        return text, info

    def _messages(self, text, params):
        user = "参考台词：" + text
        if params.get("text"):
            user += "\n老人刚才说：" + params["text"]
        return [{"role": "system", "content": SYSTEM_PROMPT.format(max_len=max(30, len(text) + 10))},
                {"role": "user", "content": user}]

    def _call(self, messages, abort):
        """在子线程里调 transport，整句计时；abort() 为真（后面有台词在排队）就不等了。"""
        box = queue.Queue(maxsize=1)

        def run():
            try:
                box.put((True, (self.transport or self._http_chat)(messages, self.timeout)))
            except Exception as e:  # noqa: BLE001
                box.put((False, e))

        threading.Thread(target=run, daemon=True).start()
        deadline = self.now() + self.timeout
        while True:
            try:
                ok, value = box.get(timeout=0.02)
                break
            except queue.Empty:
                if abort():
                    raise _Aborted()
                if self.now() >= deadline:
                    raise TimeoutError("超过 %g 秒" % self.timeout)
        if not ok:
            raise value
        text = str(value or "").strip()
        return text.splitlines()[0].strip().strip("“”\"") if text else ""

    def _http_chat(self, messages, timeout):
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0.6,
                           "max_tokens": 120}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


class _Aborted(Exception):
    pass
