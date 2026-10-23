"""supervisor：技能白名单 + 联系人白名单 + 越权拦截。

agent（无论 scripted 还是 llm）提出的每个技能请求都先过这里；
保命动作（按 SOS、拨 120、轻握）只属于状态机，agent 请求会被拦截并记一笔。
"""
import json
from pathlib import Path

SKILLS_PATH = Path(__file__).resolve().parent / "skills.json"


class Supervisor:
    def __init__(self, contacts, path=SKILLS_PATH):
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
        self.allowed = spec["agent_allowed"]
        self.fsm_only = set(spec["fsm_only"])
        self.contact_ids = {c["id"] for c in contacts}
        self.blocked = []

    def check(self, skill, contact=None, item=None):
        """返回 (ok, reason)。"""
        if skill in self.fsm_only:
            return self._block(skill, "保命动作只归状态机，agent 无权触发")
        if skill not in self.allowed:
            return self._block(skill, "技能不在白名单")
        if contact is not None and contact not in self.contact_ids:
            return self._block(skill, "联系人“%s”不在白名单" % contact)
        items = self.allowed[skill].get("items")
        if items is not None and not any(i in (item or "") for i in items):
            return self._block(skill, "“%s”不在可递物品清单" % item)
        return True, ""

    def _block(self, skill, reason):
        self.blocked.append({"skill": skill, "reason": reason})
        return False, reason
