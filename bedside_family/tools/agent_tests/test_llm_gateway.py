# -*- coding: utf-8 -*-
"""test_llm_gateway.py — 用真实糊转写直测网关意图解析质量 + 延迟(不经 arm_service)。

与 test_agent_intent.py 的分工:
  本脚本=网关本身的单点冒烟(转写→JSON 动作,量延迟);
  test_agent_intent.py=带上 arm_service 白名单收口后的整链行为。

用法(key/网关一律环境变量,**不入库**):
    set AGENT_API_KEY=sk-xxxx
    set AGENT_BASE_URL=https://...
    py tools/agent_tests/test_llm_gateway.py
"""
import json
import os
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

KEY = os.environ.get("AGENT_API_KEY", "")
BASE = os.environ.get("AGENT_BASE_URL", "")
MODEL = os.environ.get("AGENT_MODEL", "glm-5.3-flash")
if not KEY or not BASE:
    raise SystemExit("缺 AGENT_API_KEY / AGENT_BASE_URL——key 与网关地址都不入库,走环境变量")

names = ("fingers_1, fingers_2, fingers_3, fingers_4, fingers_5, ok, scissors, rock, paper, "
         "wave, thumbs_up, nod_like, pinch_1, pinch_2, pinch_3, pinch_4, curl_pk, curl_pk_rg, "
         "curl_mid, curl_all, cup, hold_card, fist_soft, grip_pillow, clench_open, count_five, "
         "hand_exercise, comfort_rhythm, release, reset, rps_random, handover_med, return_med, press_sos")
sys_prompt = (
    "你是养老陪伴机械手的语音理解器,用户多为老人短句。"
    "从固定动作表选一个,只输出JSON{\"action\":\"名字或none\",\"reply\":\"可选的一句暖心回话\",\"object\":null}。"
    "动作表:" + names + "。"
    "注意:转写来自中文语音识别,音近字很乱(ok 可能被写成 欧开/偶K/区开/圈开),按读音大胆还原真实意图。"
    "不是指挥手做动作/要取放物品/听不懂 → action=none。只输出JSON。")

tests = [
    "比 一个 区 开",     # 真实糊转写:"比个ok"
    "区 坑",             # 真实糊转写(第二句,最难)
    "从一数到五",
    "拉个勾",
    "帮我拿个杯子过来",  # 超白名单 → 应 none
]

for heard in tests:
    t0 = time.time()
    body = json.dumps({"model": MODEL, "temperature": 0,
                       "messages": [{"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": heard}]}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + KEY,
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        txt = json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
        print("%s | %.1fs | %s" % (heard, time.time() - t0, txt[:160]))
    except Exception as e:
        print("%s | FAIL %r" % (heard, e))