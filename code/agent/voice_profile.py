"""回声代聊：风格档案（授权 → 旁听 → 代聊 → 收回）。

风格档案 = 家属留言里的**文字**特征：对老人的称呼、口头禅、句尾语气词。
不采集声纹、不存音频、不做声音克隆（§5.1-5、§5.3）。

规则（每条都有测试盯着）：
- 只有家属在家属端亲手点"授权"后才会旁听、学习、代聊；授权前的留言不学
- 代聊开口必说"让我替她/他说两句"，内容只用家属自己说过的原话，不编新承诺
- 一键收回 = 立即清空该联系人的风格档案并停用
"""
import json
import re
import time
from collections import Counter
from pathlib import Path

ADDRESS_TERMS = ("老妈", "妈妈", "妈", "老爸", "爸爸", "爸", "奶奶", "姥姥", "外婆", "爷爷", "姥爷", "外公")
PARTICLES = ("哈", "呀", "啦", "哦", "嘛", "呢", "噢", "哟", "～", "~")
_CLAUSE_SPLIT = re.compile(r"[，。！？!?,.\n；;…]+")
MAX_SAMPLES = 50


def extract_style(texts):
    """从若干条留言里提取风格档案（纯文字统计）。"""
    address = Counter()
    particles = Counter()
    clauses = Counter()
    for raw in texts:
        text = raw.strip()
        if not text:
            continue
        for term in ADDRESS_TERMS:
            if text.startswith(term):
                address[term] += 1
                break
        for clause in _CLAUSE_SPLIT.split(text):
            clause = clause.strip()
            if not clause:
                continue
            if clause[-1] in PARTICLES:
                particles[clause[-1]] += 1
            bare = clause.rstrip("".join(PARTICLES))
            if bare in ADDRESS_TERMS or not (3 <= len(bare) <= 14):
                continue
            clauses[bare] += 1
    repeated = [c for c, n in clauses.most_common() if n >= 2]
    caring = [c for c, _ in clauses.most_common()
              if c.startswith(("记得", "别忘", "注意", "多喝", "早点", "按时")) and c not in repeated]
    return {
        "address": address.most_common(1)[0][0] if address else None,
        "catchphrases": (repeated + caring)[:5],
        "particle": particles.most_common(1)[0][0] if particles else None,
    }


class StyleProfile:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.data = self._load()

    def _load(self):
        if self.path and self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _entry(self, contact_id):
        return self.data.get(contact_id)

    def is_authorized(self, contact_id):
        e = self._entry(contact_id)
        return bool(e and e.get("authorized"))

    # 授权：只应由家属端接口调用（总线只在 /api/echo/authorize 上暴露它）
    def authorize(self, contact_id, history=()):
        self.data[contact_id] = {"authorized": True, "since": self.clock(), "samples": []}
        for text in history:
            self.observe(contact_id, text)
        self._save()
        return self.style(contact_id)

    # 旁听：授权期间学习家属的新留言
    def observe(self, contact_id, text):
        e = self._entry(contact_id)
        if not e or not e.get("authorized") or not text.strip():
            return False
        e["samples"] = (e["samples"] + [text.strip()])[-MAX_SAMPLES:]
        e["style"] = extract_style(e["samples"])
        self._save()
        return True

    def style(self, contact_id):
        e = self._entry(contact_id)
        if not e or not e.get("authorized"):
            return None
        return e.get("style") or extract_style([])

    # 代聊：未授权返回 None
    def speak(self, contact_id, pron="她", latest=None):
        style = self.style(contact_id)
        if style is None:
            return None
        address = style["address"] or ""
        particle = style["particle"] or ""
        if style["catchphrases"]:
            # 轮换口头禅，避免每次一模一样
            idx = len(self._entry(contact_id)["samples"]) + int(self.clock()) % 7
            body = style["catchphrases"][idx % len(style["catchphrases"])] + particle
        else:
            body = "我挺想您的" + particle
        line = "让我替%s说两句：%s%s" % (pron, address + "，" if address else "", body)
        if latest:
            line += "。%s最近一条留言是：%s" % (pron, latest)
        return line

    # 一键收回：清空并停用
    def revoke(self, contact_id):
        existed = contact_id in self.data
        self.data.pop(contact_id, None)
        self._save()
        return existed

    def reset(self):
        self.data = {}
        self._save()

    def snapshot(self):
        out = {}
        for cid, e in self.data.items():
            out[cid] = {
                "authorized": bool(e.get("authorized")),
                "since": e.get("since"),
                "sample_count": len(e.get("samples", [])),
                "style": e.get("style") or extract_style([]),
            }
        return out
