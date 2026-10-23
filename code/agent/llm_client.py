"""台词双通道：scripted（默认，零网络）/ llm。

LLM 只管"怎么说"：输入台词键 + 参数，输出一句话；不返回任何动作。
llm 通道的超时回落属于主脑模块（§7.3 软件 ①），这里先留接口，未配置时一律走 scripted。
"""
import json
from pathlib import Path

LINES_PATH = Path(__file__).resolve().parent / "lines.json"


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


class LLMClient:
    def __init__(self, mode="scripted", lines_path=LINES_PATH):
        self.mode = mode
        self.lines = {k: v for k, v in json.loads(Path(lines_path).read_text(encoding="utf-8")).items()
                      if not k.startswith("_")}

    def say(self, key, **params):
        template = self.lines.get(key)
        if template is None:
            return ""
        return template.format_map(_SafeDict(params))
