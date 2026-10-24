"""取物意图 → 信号（L1 固定格位）。

老人说的话先变成一个信号，主脑再按信号决定动不动臂：
    {"intent": "fetch", "item": "cup", "name": "水杯", "slot": "1", "pose": "fetch_slot1",
     "skill": "fetch_item", "source": "keyword" | "llm", "confirm": False | True, "why": "..."}

两条来源，信号格式一样：
  keyword  老人直接说出了物品（"把水杯递给我"）→ 不用确认，直接过 supervisor
  llm      老人没点名（"我渴了""看不清字"），LLM 从物品表里挑一样 → 必须老人说"对"才拿
LLM 只能从 fetch_items.json 的 id 里选，输出不了动作名、格位或坐标；回复不是合法 JSON、
挑了表外的东西、超时、断网，一律当作"没听出要拿东西"，交回原来的 scripted 流程。
"""
import json
import re
from pathlib import Path

ITEMS_PATH = Path(__file__).resolve().parent / "fetch_items.json"

SYSTEM_PROMPT = (
    "你是床边陪护机器人 CareMate 的取物意图解析器。机械臂只能从固定格位拿下面这几样东西：\n{menu}\n"
    "判断老人这句话是不是想让机器人把其中一样拿给他。只输出一行 JSON，不要别的字："
    '{{"item": 物品编号或 null, "why": "不超过十五个字的理由"}}。'
    "老人明确或明显暗示需要其中一样才填编号（比如口渴要水杯、看不清字要老花镜）；"
    "闲聊、问问题、想要表里没有的东西、拿不准，都填 null。"
)

_JSON = re.compile(r"\{.*\}", re.S)


class FetchCatalog:
    def __init__(self, path=ITEMS_PATH):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.items = {k: dict(v, id=k) for k, v in raw["items"].items()}

    def get(self, item_id):
        return self.items.get(item_id)

    def names(self):
        return [v["name"] for v in self.items.values()]

    def match(self, text):
        """文字里第一个出现的物品（按位置；同一位置取更长的叫法）。没有返回 None。"""
        best = None
        for item in self.items.values():
            for alias in item["aliases"]:
                i = text.find(alias)
                if i >= 0 and (best is None or (i, -len(alias)) < best[0]):
                    best = ((i, -len(alias)), item)
        return best[1] if best else None


def signal(item, source, why=""):
    return {"intent": "fetch", "item": item["id"], "name": item["name"], "slot": item["slot"],
            "pose": item["pose"], "skill": item["skill"], "source": source,
            "confirm": source != "keyword", "why": why}


def keyword_signal(text, catalog):
    item = catalog.match(text)
    return signal(item, "keyword") if item else None


def llm_messages(text, catalog):
    menu = "\n".join("%s = %s" % (k, v["name"]) for k, v in catalog.items.items())
    return [{"role": "system", "content": SYSTEM_PROMPT.format(menu=menu)},
            {"role": "user", "content": "老人说：" + text}]


def parse_llm(out, catalog):
    """LLM 原文 → 信号；不合规返回 (None, 原因)。"""
    m = _JSON.search(out or "")
    if not m:
        return None, "不是 JSON"
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None, "不是 JSON"
    if not isinstance(data, dict):
        return None, "不是 JSON 对象"
    item_id = data.get("item")
    if item_id in (None, "", "null"):
        return None, "没听出要拿东西"
    item = catalog.get(item_id) if isinstance(item_id, str) else None
    if item is None:
        return None, "挑了表外的东西 %r" % (item_id,)
    why = str(data.get("why") or "")[:30]
    return signal(item, "llm", why), None


def llm_signal(text, catalog, llm):
    """问 LLM；返回 (信号或 None, 原因)。llm 是 LLMClient，没开 llm 通道直接 (None, "disabled")。"""
    out, reason = llm.ask(llm_messages(text, catalog))
    if out is None:
        return None, reason
    return parse_llm(out, catalog)
