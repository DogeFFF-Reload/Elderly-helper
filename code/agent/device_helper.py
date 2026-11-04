"""家电教学匹配器：只从 guides.json 里找步骤，匹配不到就老实说没学过。"""
import json
from pathlib import Path

GUIDES_PATH = Path(__file__).resolve().parent / "guides.json"


class DeviceHelper:
    def __init__(self, path=GUIDES_PATH):
        self.devices = json.loads(Path(path).read_text(encoding="utf-8"))["devices"]

    def find_device(self, text):
        best = None
        for d in self.devices:
            for alias in d["aliases"]:
                if alias in text and (best is None or len(alias) > best[1]):
                    best = (d, len(alias))
        return best[0] if best else None

    def match(self, text):
        """返回 {"status": "ok"|"unknown_task"|"unknown_device", ...}。

        ok：命中设备和任务，steps 原样来自 guides.json
        unknown_task：设备学过，但这件事没学过（如"电饭煲内胆怎么拆洗"）→ 不现编
        unknown_device：设备没学过
        """
        device = self.find_device(text)
        if device is None:
            return {"status": "unknown_device", "devices": [d["name"] for d in self.devices]}
        best = None
        for task in device["tasks"]:
            score = max((len(k) for k in task["keywords"] if k in text), default=0)
            if score and (best is None or score > best[1]):
                best = (task, score)
        # 只有"内胆""拆""洗"之类明确的新动作时，宁可说没学过；泛泛的"怎么用"给第一项
        if best is None or (best[1] <= 1 and _mentions_unlisted_action(text)):
            return {"status": "unknown_task", "device": device["name"],
                    "tasks": [t["title"] for t in device["tasks"]]}
        task = best[0]
        return {"status": "ok", "device": device["name"], "task": task["title"],
                "task_id": task["id"], "steps": list(task["steps"])}

    def catalog(self):
        return [{"device": d["name"], "tasks": [t["title"] for t in d["tasks"]]} for d in self.devices]


_UNLISTED = ("拆", "洗", "修", "换", "装", "清理", "清洗", "设置", "内胆", "滤网", "坏了")


def _mentions_unlisted_action(text):
    return any(w in text for w in _UNLISTED)
