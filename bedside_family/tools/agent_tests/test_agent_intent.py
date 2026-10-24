# -*- coding: utf-8 -*-
"""test_agent_intent.py — PC 侧冒烟:agent_intent 三元组 + 动作白名单中文名完备性。

两层:
  ① 硬断言(测试型):_VOICE_ACTIONS 里每个动作都必须有 _ACTION_ZH 中文名——
     LLM 提示词靠中文名让模型分清"拉勾=pinch_4"这类歧义,缺名=提示词露怯;
  ② 观察型:一组真实糊转写喂给 agent_intent,打印 三元组+回话,人工研判
     (LLM 语义有随机性,不硬断言;糊转写应被还原为正确动作)。

用法(key/网关一律环境变量,**不入库不硬编码**):
    set AGENT_API_KEY=sk-xxxx        # 与板上 unit.env 同钥
    set AGENT_BASE_URL=https://...   # 网关地址(同板上配置)
    py tools/agent_tests/test_agent_intent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if not os.environ.get("AGENT_API_KEY"):
    raise SystemExit("缺 AGENT_API_KEY 环境变量——本脚本直连 LLM 网关,key 不入库不硬编码")
if not os.environ.get("AGENT_BASE_URL"):
    raise SystemExit("缺 AGENT_BASE_URL 环境变量——网关地址同板上 unit.env 配置,不烙进仓库")

from _svc_boot import import_service

svc = import_service()
missing = [a for a in svc._VOICE_ACTIONS if a not in svc._ACTION_ZH]
print("动作数=%d  缺中文名=%s" % (len(svc._VOICE_ACTIONS), missing or "无"))
assert not missing, "白名单动作缺中文名(LLM 提示词会露怯): %s" % missing

for text in ["比 一个 区 开",       # 真实糊转写:"比个ok"
             "区 坑",              # 真实糊转写第二句(最难)
             "拉个勾",
             "从一数到五",
             "谢谢",
             "帮我拿个杯子过来",    # 超白名单 → 应 none(回话保留)
             "给我来个波浪卷指"]:
    try:
        act, reply, ok = svc.agent_intent(text)
        print("%-14s | ok=%-5s | %-12s | %s" % (text, ok, act or "(none)", (reply or "")[:40]))
    except Exception as e:
        print("%-14s | FAIL %r" % (text, e))

print("== 白名单完备性 PASS;意图行为请对照上表人工研判(糊转写应还原为正确动作) ==")