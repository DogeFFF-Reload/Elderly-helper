# agent 离线冒烟测试(agent_tests)

PC 上(不碰板)验证语音 agent 链路两段:**LLM 网关单点质量** 与 **整链白名单收口行为**。
这两个是"要真 key"的人工冒烟,所以不在 bedside-ci 里;CI 只跑 `../../tests/` 的零依赖单测。

| 脚本 | 测什么 | 通过标准 |
|---|---|---|
| `test_agent_intent.py` | 经 arm_service 的 agent_intent:动作白名单中文名完备性(**硬断言**)+ 7 条真实糊转写的意图三元组(观察打印) | 断言全绿;糊转写("比 一个 区 开"→ok、"区 坑"→ok、超白名单→none)人工研判 |
| `test_llm_gateway.py` | 直连网关:同系统提示词下 5 条糊转写的 JSON 行为与延迟(不经服务层) | 糊转写还原正确、超白名单答 none;延迟供调 AGENT_TIMEOUT 参考 |
| `_svc_boot.py` | 共用引导:service_handboard 以 `hand_controller` 包形态导入(对齐板上部署形态);serial/vosk 缺库自动打桩 | — |

## 用法(key/网关一律环境变量,不入库不硬编码)

```powershell
$env:AGENT_API_KEY  = "sk-xxxx"        # 与板上 /root/caremate/unit.env 同钥
$env:AGENT_BASE_URL = "https://..."    # 网关地址,同板上配置(仓库不存网关地址)
$env:AGENT_MODEL    = "glm-5.3-flash"  # 可省
py tools/agent_tests/test_agent_intent.py
py tools/agent_tests/test_llm_gateway.py
```

## 为什么白名单完备性值得断言

arm_service 的安全模型=**理解开放(会说话)**、**执行封闭(词表/白名单收口)**。
LLM 只许从白名单报名字;"白名单里的每个动作必须有中文名"是提示词质量的地基——
少一个中文名,模型就该收到的动作就没了名字可叫。这条硬断言防的是"加了动作忘了加名"的静默腐化。