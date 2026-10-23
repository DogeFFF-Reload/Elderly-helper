# Mocen_CareMate · 交互端（队长模块）+ 主脑与安全内核（软件模块）

按《工作报告 v2.5》§7.3 的分工，这里实现了两个模块：

- **交互端**（§2–§6）：语音链路（ASR / TTS）、三块屏、健康口述、回声风格档案、教学库、scripted 台词。
- **主脑与安全内核**（§10）：总线、状态机、supervisor、llm 超时回落、演示参数配置化、`demo_driver`、回归与合并工具。

> ⚠️ 本机没有找到团队代码仓，所以按 v2.5 文档的接口**从零重建**了一个能跑的最小总线（stand-in），让交互端可以独立开发、端到端演示。
> 合并进真仓库时：**交互端文件直接用这里的；stand-in 文件以队友的正式版为准**（见文末「合并说明」）。

## 1. 跑起来

只需 Python 3.8+，核心零依赖。

```powershell
cd code
python tests/run_all.py                  # 回归，必须 ALL PASS（当前 16 套 141 条）
python -m master_service                 # 主脑 :8000（含三块屏）
python -m demo_driver --pause            # 按 §3 主线一段一段走演示（另开终端，见 §10）
python -m hand_controller.arm_service    # 臂+双手 mock :8013（另开终端，可不开）
python -m voice.tts_player               # AI 台词 → 喇叭（另开终端）
python -m voice.asr_bridge --backend stdin   # 键盘当麦克风（另开终端）
python -m tools.preflight                # 联调自检：各节点在不在、坏了去哪修（见 §10.8）
```

| 屏 | 地址 | 给谁看 |
|---|---|---|
| 大屏 | http://127.0.0.1:8000/ui/monitor | 观众：骨架剪影、事件时间线、实时字幕、今日健康。双击全屏 |
| 家属端 | http://127.0.0.1:8000/ui/family | 评委/家属手机：紧急确认、健康日报、留言、回声授权。`?as=son` 切儿子 |
| 操作台 | http://127.0.0.1:8000/ui/console | 演示员：§3 主线按钮 + 备用指令 + 完整日志 |

多节点联调时 `python -m master_service --host 0.0.0.0`，手机连同一 Wi-Fi 打开家属端。

## 2. §7.3 交互端任务完成情况

| # | 任务 | 状态 | 在哪 |
|---|---|---|---|
| ① | 选定 ASR 并接入，识别文字走 `/mock/voice` | ✅ 选 **Vosk 离线中文**（X5/Windows 都能跑、断网可用）；桥发的就是 `{"text": ...}`，和手打完全同一入口 | `voice/asr_bridge.py` |
| ② | TTS 音频送到喇叭 | ✅ 播放器订阅总线 `say` 事件；Windows 用系统语音零安装，X5 用 espeak-ng / piper。大屏也可用浏览器播报做备用 | `voice/tts_player.py` |
| ③ | 健康口述复述确认；"指纹提取"改名"风格档案" | ✅ 先复述"我听到的是高压 138、低压 85，对吗？"，说"对"才记账；回声模块全面改用"风格档案"，只存文字特征 | `agent/health_tracker.py`、`agent/voice_profile.py` |
| ④ | 按 §3 主线打磨三块屏 | ✅ 大屏 / 家属端 / 操作台全部重做，操作台按 0:20 → 0:50 → 1:20 的顺序排按钮，带快捷键 | `ui/` |

## 3. 语音链路

```text
麦克风 → asr_bridge（Vosk）→ 文本整理 → 防自激过滤 → POST /mock/voice ─┐
                                                                    主脑
喇叭 ← tts_player ← GET /api/events（type=say）←─────────────────────┘
```

### ASR（`voice/asr_bridge.py`）

```powershell
pip install -r requirements-voice.txt
# 下载 vosk-model-small-cn-0.22（约 42MB）：https://alphacephei.com/vosk/models
# 解压到 code/voice/models/vosk-model-small-cn-0.22/
python -m voice.asr_bridge --list-devices
python -m voice.asr_bridge --device 1
```

- **文本整理**（`voice/text_normalize.py`）：Vosk 输出 `一 百 三十 八 比 八十 五` → `138比85`，`幺 二 零` → `120`；"一下""两杯""十分"这类单字不乱转。
- **同音纠错表** `ASR_FIXES`：`雪压→血压`、`手指草→手指操`……彩排时听到错字就往里加。
- **防自激**：喇叭里 AI 自己的台词会被麦克风听到。桥订阅 `say` 事件，播报期间丢弃识别结果，之后 15 秒内与台词高度相似的也丢；"对""确认"这类短回答不过滤。耳机或麦克风离喇叭很远时可 `--no-echo-guard`。
- **兜底**：`--backend stdin` 键盘输入，或操作台"让老人说"输入框，走的都是同一入口。

### TTS（`voice/tts_player.py`）

| 后端 | 用在哪 | 说明 |
|---|---|---|
| `sapi` | Windows 笔记本 | 零安装。**需要中文语音包**：设置 → 时间和语言 → 语音 → 添加语音 → 中文（简体）。本机目前只有英文语音，播放器会提示 |
| `espeak` | X5（Linux） | `apt install espeak-ng`，离线，音质一般 |
| `cmd` | X5 推荐 | 接 piper 离线中文（音质好），见文件头示例 |
| `print` | 调试 | 只打印 |

测喇叭：`python -m voice.tts_player --say "您好，我是 CareMate"`。积压超过 3 句会丢最旧的，保证紧急台词不被手指操口令堵住。

## 4. 健康口述（复述确认）

```text
"我刚量了血压 138比85" → "我听到的是高压 138、低压 85，对吗？"（待确认，不落盘，大屏出复述卡）
   ├ "对"                → 记账；≥135/85 标"偏高"并同步家属
   ├ "不对" / 报新数字    → 重新复述
   └ 20 秒没回应          → 丢弃不记
```

- 支持 `138比85`、`138/85`、`高压138低压85`、`收缩压…舒张压…`、`心率72`、中文数字。
- 不合理的数（低压比高压大、300 之类）会请老人重说。
- `health.json` 只存数字和时间，不存原话。日报只解读、不诊断（"建议同一时间复测，持续偏高请咨询医生"）。
- 喝水低风险，直接记。

## 5. 回声代聊 · 风格档案

授权 → 旁听 → 代聊 → 收回，每一步都在家属端可见：

- 只有家属在家属端亲手点"开启"才学习；授权前不学、不代聊。
- 档案只有**称呼、口头禅、语气词**三项文字特征，家属端原样展示学到了什么。不采集声纹，不做声音克隆。
- 代聊开口必说"让我替她说两句"，只复用她说过的原话，再附上她最近一条真实留言，**不替家属做新承诺**。
- 一键收回 = 立即从 `voice_profile.json` 删除。

## 6. 三块屏要点

- **大屏**：左侧双色骨架（左半身青、右半身橙，告警变红并发光），倒地有过渡动画；叠加层显示询问倒计时圈、SOS 横幅、手#1/手#2 举牌、120 确认窗、血压复述卡、教学卡、手指操拍数。下方字幕实时显示"老人说了什么 / CareMate 说了什么"，现场听不清也能看懂。夜间模式整屏变暗并亮 🌙。
- **家属端**：紧急卡上"我收到了，马上到"按钮做了防抖（倒计时只改文字、不重建按钮）；收到后老人那边才举"家人已收到"牌。
- **操作台**：快捷键 `1` 报血压、`2` 说对、`3` 要药、`N` 夜间、`F` 摔倒、`A` 女儿确认、`O` 绿键、`R` 重置（光标在输入框里时不生效）。

## 7. 接口（与文档一致，只替换 mock、不改接口）

| 方向 | 接口 |
|---|---|
| 老人说话 → 主脑 | `POST /mock/voice {"text": "..."}` |
| 跌倒 → 主脑 | `POST /mock/fall?room=客厅`（骨架可选 `POST /api/skeleton {"keypoints": {...}, "fallen": true}`） |
| 床头盒绿键 → 主脑 | `POST /mock/button?key=ok` |
| 主脑 → 执行端 | `POST :8013/pose?name=press_sos&hand=1` |
| 主脑 → 喇叭 | `GET /api/events?since=<id>` 里 `type=="say"` 的事件 |
| 家属端 → 主脑 | `/api/family/ack`、`/api/family/message`、`/api/echo/authorize`、`/api/echo/revoke` |
| 操作台 → 主脑 | `/api/night {"mode": "auto|on|off"}`、`/api/reset {"all": "0|1"}`、`/api/agent/propose`、`GET/POST /api/params` |
| 床头盒 ↔ 主脑 | `GET /api/box?id=box1`（灯色 + 心跳，草案，见 §10） |
| 节点自检 | `GET /api/nodes`（`tools/preflight` 用，见 §10.8） |

演示参数都在 `code/config.py`，可用 `config.local.json`、环境变量（如 `CAREMATE_ASK_WAIT_DAY=30`）或运行中 `/api/params` 覆盖，见 §10。

## 8. 合并说明（给软件 / 硬件 / 演示同学）

| 文件 | 归属 | 合并时 |
|---|---|---|
| `ui/`、`voice/`、`agent/health_tracker.py`、`agent/voice_profile.py`、`agent/guides.json`、`agent/device_helper.py`、`agent/lines.json` | **交互端** | 用这里的 |
| `master_service.py`、`state_machine/`、`agent/intent.py`、`agent/supervisor.py`、`agent/skills.json`、`agent/llm_client.py`、`config.py`、`demo_driver.py`、`tests/`、`tools/` | **软件** | 用这里的（§10）；软件同学若另有正式版，用 `tools/merge_gate.py` 对照合并 |
| `hand_controller/` | 硬件 | stand-in；`poses.py` 的 `label` 会显示在大屏时间线上 |
| `vision/`、`gesture/` | 演示 | stand-in，只实现了 `--mock` |

归属的机器可读版在 `code/tools/owners.py`，`run_all` 和 `merge_gate` 都按它判断"找谁"。

**状态机里的三处行为**（软件模块沿用，每处都有回归测试盯着；软件同学接手时可复核、可改）：

1. **举牌等家属确认**：手#1"家人已收到"、手#2"女儿马上到"在家属端点"我收到了"之后才举。v2.5 §2.1 是通知后立刻举，但没回执就说"已收到"不诚实。
2. **家属不响应继续升级**（回答 §2.1【待确认】）：30 秒没人确认就通知下一位联系人，都不确认则提示老人可说"帮我打120"。时长 `family_ack_timeout` 可配置。
3. **"确认"的优先级**：120 确认窗 > 血压复述确认，避免一个"确认"被两处同时用掉。

## 9. 仍待确认

- 【硬件】喇叭和麦克风最终装在床头盒还是 X5？在床头盒的话，`tts_player --backend cmd` 把音频转给盒子，协议等硬件同学定。
- 【全队】会场噪音下 Vosk 小模型的识别率需要实测；不够用的话换 `vosk-model-cn-0.22`（大模型）或 sherpa-onnx，桥的接口不变。
- 【演示】`fall_bridge --camera/--video` 尚未实现，大屏骨架目前用预置姿态；视觉模块推关键点到 `/api/skeleton` 后大屏会直接画实时骨架。
- 【硬件 × 软件】床头盒协议是软件这边出的草案（§10.6），等硬件同学确认或改。
- 【全队】只有**两块 X5**（v2.5 §1.2 按三块写的）。建议映射见 §10.8：X5-A 视觉，X5-B 主脑 + 屏 + 语音 + 执行端。要硬件同学确认 X5-B 的 USB 口够接 RS485、麦克风、喇叭；演示同学 `deploy/` 里按 a/b/c 三块写的脚本要把 b、c 合到一块。

## 10. 软件模块（主脑与安全内核）

### 10.1 §7.3 软件任务完成情况

| # | 任务 | 状态 | 在哪 |
|---|---|---|---|
| ① | `llm` 超时自动回落 `scripted` | ✅ 四道闸：台词白名单、整句超时、断网冷却、内容检查。LLM 在单独的台词线程里生成，不占总线锁 | `agent/llm_client.py`、`master_service.SpeechQueue` |
| ② | 演示参数配置化：等待时长、夜间窗口、时间覆盖开关 | ✅ 全是配置项，启动时校验；彩排时可用 `/api/params` 现场调 | `config.py` |
| ③ | `demo_driver` 加健康步与手指操 | ✅ §3 主线（血压 → 递药 → 夜间摔倒）+ 手指操，每步检查结果、计时 | `demo_driver.py` |
| ④ | 维护 `run_all`，负责合并 | ✅ `run_all` 每套标负责人；`merge_gate` 做归属检查 + 试合并 + 回归 | `tests/run_all.py`、`tools/` |

另外顺手修了一处安全内核漏洞（§10.4），并起草了床头盒协议（§10.6）。Day1 联调前又补了执行端回执与断链保护（§10.7）、联调自检（§10.8）。

### 10.2 llm 通道

```json
// code/config.local.json（已在 .gitignore 里）
{ "llm_mode": "llm", "llm_base_url": "https://<主办方地址>/v1", "llm_model": "<模型名>", "llm_timeout": 3 }
```

API key 只走环境变量：`$env:CAREMATE_LLM_API_KEY = "..."`。接口按 OpenAI 兼容格式（`POST {base}/chat/completions`）写，主办方给的接口不兼容的话，只需要改 `LLMClient._http_chat`。

- **白名单**：只有 `LLM_KEYS` 里的台词（递药、留言、报平安、闲聊……）交给 LLM 润色。保命链、120、健康复述、教学步骤**永远 scripted**；新加的台词键默认不进白名单。
- **超时**：单句超过 `llm_timeout`（3 秒）就用 scripted 版。计时按整句算，不按 socket 算，服务器一个字一个字慢慢回也会被掐断。
- **冷却**：网络或接口出错一次，之后 `llm_cooldown`（60 秒）内直接走 scripted，断网时不会每句都干等 3 秒。
- **内容检查**：可以省略，不能编。不许出现 scripted 版没有的数字（包括"两片""三次"这类中文数量）、联系人称呼或别名（"小芳""小军"），也不许冒出"120 / 拨打 / 求助 / 医生 / 吃药"这类承诺；不许英文和符号（TTS 会照念）。不过关就用 scripted。
- **台词线程不会死**：生成台词时出任何意外都退回 scripted 版，线程继续跑，后面的 SOS 台词照常播。
- **不占锁**：llm 模式下台词在单独线程里生成，状态机 tick、跌倒处理都不用等它。后面有台词在排队（比如跌倒询问）就不等 LLM，当前这句直接用 scripted，播放顺序不乱。
- 回落时操作台日志会出现"LLM 超时 → 回落 scripted"（大屏不显示）；`/api/state` 的 `llm` 字段里有计数和最近一次错误。

### 10.3 演示参数（§2.2）

可调项：`ask_wait_day/night`、`dial_confirm_window`、`family_ack_timeout`、`health_confirm_window`、`night_start_hour/end_hour`、`exercise_beat_seconds`、`llm_timeout/cooldown`。

- 覆盖顺序：`config.py` 默认值 < `config.local.json` < 环境变量 `CAREMATE_<大写键名>` < 运行中 `/api/params`（重启后失效）。
- **时间覆盖开关**：`"night_override": "on"` 开机即夜间模式，重置后也保持；操作台的夜间按钮照常可用。
- 启动时整体校验：数值越界、夜间小时不在 0–23、联系人 id 重复，都会直接报中文错误退出；`config.local.json` 里拼错的键会给出提示，不会被悄悄忽略。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/params                       # 当前值 + 合法范围
$b = [Text.Encoding]::UTF8.GetBytes('{"ask_wait_day": 8}')
Invoke-RestMethod http://127.0.0.1:8000/api/params -Method Post -Body $b -ContentType 'application/json'
```

一批里只要有一项不合法，整批都不生效。API key、联系人、执行端地址不能在运行中改。

### 10.4 保命链资源锁（安全内核修补）

原 stand-in 里，SOS 期间老人说"把药递给我"，或者等家属时说"做个手指操"，臂和手#1 会离开 SOS 键，或者放下"家人已收到"的牌。现在的规则：

- 老人的**每个**请求都先过 supervisor（以前只有递药过）。
- 询问 / SOS / 等家属期间，臂归状态机：用到臂的技能（递药、手指操）会被挂起，并口播"我先顾着您这边的安全……"；不用臂的请求（叫儿子、留言、教学）照常放行。
- 保命链结束（老人说没事、按绿键、重置）后自动恢复。技能占用哪些资源，写在 `skills.json` 的 `uses` 里。

### 10.5 demo_driver（§3 主线）

```powershell
python -m demo_driver --list            # health / medicine / night_fall（主线）+ exercise
python -m demo_driver --pause           # 演示当天：每段前按回车，跟着主讲的节奏走
python -m demo_driver --all             # 彩排：主线 + 手指操，每段打印"用时 / §3 预算"
python -m demo_driver --server http://<X5-B>:8000   # Day1 联调冒烟
python -m demo_driver --local           # 不连服务，假时钟几秒跑完
```

只走公开接口（和操作台、ASR 桥、视觉桥同一入口），每一步都检查结果（复述了没、记账了没、臂动了没、家属端收到没），任何一步没过退出码就不是 0。手指操这段如果前面救援场景还没结束，会先重置，因为资源锁不允许保命链进行中做操。

### 10.6 床头盒协议（草案，等硬件同学确认）

| 方向 | 接口 | 说明 |
|---|---|---|
| 盒 → 主脑 | `GET /api/box?id=box1`，每 0.5 秒一次 | 返回 `{"led", "pattern", "night", "remaining", "dial_pending", "state"}`；这次轮询同时当心跳，3 秒没来就算离线（`/api/state` 的 `box.online`） |
| 盒 → 主脑 | `POST /mock/button?key=ok` | 绿键"我没事"，和现在一样 |
| 灯 | 空闲 绿·呼吸 / 询问 黄·闪 / SOS 红·闪 / 家属已确认 蓝·常亮 | 固件只管照着亮，判断逻辑全在主脑；`night` 为真时建议调暗 |

喇叭放哪还没定（见 §9）。选 HTTP 轮询是因为 ESP32-S3 连 Wi-Fi 后最好写，也不用在主脑上再开一个长连接服务。

### 10.7 执行端链路（保命动作告警 + 断链保护）

原来总线把动作发给 :8013 就不管了：执行端没开、USB-RS485 松了，时间线上照样写"臂+手#1 按下 SOS 键"，演示员看不出来。现在：

- **保命动作没做成，马上告警**：按 SOS、轻握、两块举牌，任一没做成，操作台日志出一条红色"保命动作没做成：…；执行端连不上。通知家属走软件通道，照常送达；现场可切 Plan B"。大屏不显示（type=system）。
- **其他动作**（递药、手指操）失败只在刚开始失败时记一条，13 拍手指操不会刷屏。
- **在线探测**：总线每 2 秒 `GET :8013/status`，开演前就知道执行端在不在（操作台"执行端"灯），掉线、恢复各记一条。
- **分清"连不上"和"被拒绝"**：执行端回了 4xx/5xx（多半是动作名在 `poses.py` 里没有）算在线但拒绝，日志里带执行端给的原因。
- **断链保护（§5.2 的总线一侧）**：排队超过 `arm_stale`（5 秒）还没发出去的动作直接丢，链路恢复时臂不会补做一串过期动作。**不重发**：超时的请求可能已经到了执行端，重发 press_sos 等于按两下。
- `arm_timeout`（1.5 秒）、`arm_stale` 是配置项，也能经 `/api/params` 现场调。

**和硬件同学的约定**：`POST /pose` 收到就回 200，动作在执行端自己做，别等做完才回，否则超过 `arm_timeout` 会被当成离线；做不了的动作回 4xx，在 JSON 的 `error` 里写原因，总线原样显示；`GET /status` 保持可用，总线靠它探测在线。现在的 mock 已经是这样。

### 10.8 联调自检（`tools/preflight`）

Day1 联调最耗时间的是找"哪一台没起来"。一条命令看全：

```powershell
python -m tools.preflight                                     # 本机主脑
python -m tools.preflight --server http://192.168.1.20:8000   # 在任意一台机器上查 X5-B
python -m tools.preflight --wait 60                           # 等到没有 ❌ 或超时（开机后 / 开演前）
```

```text
CareMate 联调自检 · http://127.0.0.1:8000
  ✅ 状态机    空闲
  ❌ 执行端    连不上 http://127.0.0.1:8013
               → X5-B 上 bash deploy/start.sh arm（笔记本：python -m hand_controller.arm_service）；…
  ✅ 喇叭      tts_player 在接收台词
  ⚠️ 视觉      还没收到过骨架
               → X5-A 上 bash deploy/start.sh vision；Plan B：fall_bridge --video 预录视频，或操作台“触发摔倒”
  …
还不能开演：先修 执行端
```

| 项目 | 怎么判断 | 不在时 |
|---|---|---|
| 执行端 | 总线探测 `:8013/status` | ❌（Plan B：手走 mock + 大屏动画） |
| 喇叭 | `tts_player` 在拉台词事件 | ❌（兜底：大屏"大屏播报"） |
| 麦克风 | `asr_bridge` 在拉事件，或听到过老人说话 | ⚠️（兜底：操作台输入框） |
| 视觉 | 3 秒内收到过骨架 | ⚠️（Plan B：预录视频、操作台"触发摔倒"） |
| 床头盒 | 3 秒内有心跳 | ⚠️（兜底：操作台绿键） |
| LLM | 模式、最近一次错误 | ⚠️（会自动回落 scripted） |
| 状态机、演示参数 | 不在空闲；参数和默认值不同 | ⚠️（彩排时改了忘了改回来） |

- 没有 ❌ 退出码 0，有 ❌ 为 1，连不上主脑为 2，可以写进开机脚本。数据来自 `GET /api/nodes`，自检本身不触发任何动作。
- 语音工具订阅事件时带上 `client=脚本名`（`voice/bus_client.py` 改了一行），自检靠它认出喇叭、麦克风那头；没更新的旧版显示为"未标名"。

**部署按两块 X5**（建议，等硬件同学确认）：

| 节点 | 跑什么 | 接什么 |
|---|---|---|
| X5-A | 视觉：`fall_bridge --camera`、猜拳 `rps_bridge` | 摄像头 |
| X5-B | 主脑 :8000 + 三块屏 + `tts_player` / `asr_bridge` + 执行端 `arm_service` :8013 | 显示器、喇叭、麦克风、USB-RS485（双手） |

执行端和主脑放同一块：保命链"主脑 → 执行端"走本机回环，不经过 Wi-Fi，`arm_url` 用默认的 `http://127.0.0.1:8013`。视觉跨板走 Wi-Fi/网线，断了还有操作台"触发摔倒"兜底。

### 10.9 回归与合并

- 联调这批新增 2 套：`test_arm_link`（回执、告警、断链保护、探测，外加真 `arm_service` 端到端）、`test_preflight`（`/api/nodes` 和自检判定）。
- 新增 4 套：`test_llm_fallback`（回落、冷却、内容检查、不占锁、紧急插队）、`test_config_params`、`test_demo_driver`、`test_merge_gate`。另外 `test_agent_supervisor` 补了资源锁，`test_bus_http` 补了 `/api/params` 和 `/api/box`。
- 测试只读 `config.py` 默认值，不受本机 `config.local.json` 和环境变量影响（`config.load(use_local=False)`）。
- `python tests/run_all.py fsm help` 可以只跑几套；结果表里每套标了负责人，还会列出"缺"的套（如 `test_rps_engine` 等演示同学提交）。

合并流程（§7.3 协作规则 3）：

1. 每人在 `角色/主题` 分支上开发：`captain/…`、`demo/…`、`hardware/…`、`software/…`
2. 软件跑 `python tools/merge_gate.py demo/vision`：列出改动归谁；别人的分支改了 `master_service.py` 直接拦下，动了其他人的模块会提醒先打招呼；在临时 worktree 里试合并进 `main`，并在合并结果上跑回归
3. 全部通过后，按它打印的命令人工合并
4. Day2 12:00 前打 tag 冻结：`git tag demo-final`

软件模块碰到别人的文件：`agent/lines.json`（队长）加了一句 `skill_busy` 台词，供资源锁挂起时口播；联调这批改了 `voice/bus_client.py`（队长）一行，订阅事件时报上脚本名。
