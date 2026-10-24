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
python tests/run_all.py                  # 回归，必须 ALL PASS（当前 19 套 167 条）
python -m master_service                 # 主脑 :8000（含三块屏）
python -m demo_driver --pause            # 按 §3 主线一段一段走演示（另开终端，见 §10）
python -m hand_controller.arm_service    # 臂+双手 mock :8013（另开终端，可不开）
python -m voice.tts_player               # AI 台词 → 喇叭（另开终端）
python -m voice.asr_bridge --backend stdin   # 键盘当麦克风（另开终端）
python -m tools.preflight                # 联调自检：各节点在不在、坏了去哪修（见 §10.9）
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
- **大屏 · 床边执行端**：右上角实时画出臂（侧视）和两只手（正视，五指各自弯曲）在做什么：伸过去按 SOS、递药盒、举牌、轻握、手指操每一拍、猜拳出拳。数据来自 `/api/state` 的 `hands`，真机做了就是真机的镜像；**执行端离线时它就是 Plan B 的“大屏动画”**，角标会显示“执行端离线 · 大屏演示”。
- **操作台**：快捷键 `1` 报血压、`2` 说对、`3` 要药、`N` 夜间、`F` 摔倒、`A` 女儿确认、`O` 绿键、`R` 重置（光标在输入框里时不生效）。`Esc` 软件急停（输入框里也生效；解除只能点按钮并确认，见 §10.11）。

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
| 节点自检 | `GET /api/nodes`（`tools/preflight` 用，见 §10.9） |
| 软件急停 | `POST /api/estop {"on": true\|false}` → 执行端 `POST :8013/stop`（见 §10.11） |
| 执行端动画 | `/api/state` 的 `hands`：`{"1": {"pose", "label", "status"}, "2": {…}}` |

演示参数都在 `code/config.py`，可用 `config.local.json`、环境变量（如 `CAREMATE_ASK_WAIT_DAY=30`）或运行中 `/api/params` 覆盖，见 §10。

## 8. 合并说明（给软件 / 硬件 / 演示同学）

| 文件 | 归属 | 合并时 |
|---|---|---|
| `ui/`、`voice/`、`agent/health_tracker.py`、`agent/voice_profile.py`、`agent/guides.json`、`agent/device_helper.py`、`agent/lines.json` | **交互端** | 用这里的 |
| `master_service.py`、`state_machine/`、`agent/intent.py`、`agent/supervisor.py`、`agent/skills.json`、`agent/llm_client.py`、`config.py`、`demo_driver.py`、`tests/`、`tools/` | **软件** | 用这里的（§10）；软件同学若另有正式版，用 `tools/merge_gate.py` 对照合并 |
| `hand_controller/` | 硬件 | RH56F2 驱动（EB 90 协议，按官方手册 V1.09）+ 执行服务（§10.13，软件代接）；`poses.py` 的 `label` 会显示在大屏时间线上 |
| `vision/`、`gesture/`、`deploy/` | 演示 | 已实现（见 §11）；猜拳的主脑一侧（`/api/rps/gesture`、判输赢）归软件，见 §10.7 |

归属的机器可读版在 `code/tools/owners.py`，`run_all` 和 `merge_gate` 都按它判断"找谁"。

**状态机里的三处行为**（软件模块沿用，每处都有回归测试盯着；软件同学接手时可复核、可改）：

1. **举牌等家属确认**：手#1"家人已收到"、手#2"女儿马上到"在家属端点"我收到了"之后才举。v2.5 §2.1 是通知后立刻举，但没回执就说"已收到"不诚实。
2. **家属不响应继续升级**（回答 §2.1【待确认】）：30 秒没人确认就通知下一位联系人，都不确认则提示老人可说"帮我打120"。时长 `family_ack_timeout` 可配置。
3. **"确认"的优先级**：120 确认窗 > 血压复述确认，避免一个"确认"被两处同时用掉。

## 9. 仍待确认

- 【硬件】喇叭和麦克风最终装在床头盒还是 X5？在床头盒的话，`tts_player --backend cmd` 把音频转给盒子，协议等硬件同学定。
- 【全队】会场噪音下 Vosk 小模型的识别率需要实测；不够用的话换 `vosk-model-cn-0.22`（大模型）或 sherpa-onnx，桥的接口不变。
- 【演示】`fall_bridge --camera` 还没在真人画面上实拍调参（见 §11.3）；大屏已能画视觉推来的实时骨架。
- 【硬件 × 软件】床头盒协议是软件这边出的草案（§10.6），等硬件同学确认或改。
- 【全队】只有**两块 X5**（v2.5 §1.2 按三块写的）。建议映射见 §10.9：X5-A 视觉，X5-B 主脑 + 屏 + 语音 + 执行端。要硬件同学确认 X5-B 的 USB 口够接 RS485、麦克风、喇叭；演示同学 `deploy/` 里的脚本已改成两块的版本（`a` / `b`，执行端并进 `b`，见 §11.5）。

## 10. 软件模块（主脑与安全内核）

### 10.1 §7.3 软件任务完成情况

| # | 任务 | 状态 | 在哪 |
|---|---|---|---|
| ① | `llm` 超时自动回落 `scripted` | ✅ 四道闸：台词白名单、整句超时、断网冷却、内容检查。LLM 在单独的台词线程里生成，不占总线锁 | `agent/llm_client.py`、`master_service.SpeechQueue` |
| ② | 演示参数配置化：等待时长、夜间窗口、时间覆盖开关 | ✅ 全是配置项，启动时校验；彩排时可用 `/api/params` 现场调 | `config.py` |
| ③ | `demo_driver` 加健康步与手指操 | ✅ §3 主线（血压 → 递药 → 夜间摔倒）+ 手指操，每步检查结果、计时 | `demo_driver.py` |
| ④ | 维护 `run_all`，负责合并 | ✅ `run_all` 每套标负责人；`merge_gate` 做归属检查 + 试合并 + 回归 | `tests/run_all.py`、`tools/` |

另外顺手修了一处安全内核漏洞（§10.4），并起草了床头盒协议（§10.6）。Day1 联调前又补了执行端回执与断链保护（§10.8）、联调自检（§10.9）。

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

### 10.7 猜拳接口（视觉 → 主脑）

视觉只负责认出老人出了什么，**输赢由主脑判**（`master_service.rps_judge`，确定性代码）：

- 老人说"石头剪刀布" → 主脑先随机定好机器人出什么（`/api/state` 的 `rps.robot` 揭晓前为 `null`）→ 发 `rps` 事件（`phase=start`、`reveal_in` 秒后揭晓）→ 到点手#1 摆出 `rps_rock / rps_scissors / rps_paper`
- 视觉回传：`POST /api/rps/gesture {"round": n, "gesture": "rock" | "scissors" | "paper" | null}`；`null` = 没看清，也会揭晓并请老人再来一局
- 揭晓后 `rps_timeout` 秒没回传 → 提示到展台玩；跌倒事件直接取消这一局
- 猜拳登记为用到臂的技能 `rock_paper_scissors`（`uses: ["arm"]`），保命链进行中被资源锁挂起
- `rps_reveal_delay`（默认 3.5 秒）、`rps_timeout`（默认 6 秒）进了 `LIMITS`，可经 `/api/params` 调
- 顺带改了别人的文件：`lines.json`（队长，6 句猜拳台词）、`poses.py`（硬件，3 个出拳动作）、两块屏的猜拳卡片和兜底按钮（队长）

### 10.8 执行端链路（保命动作告警 + 断链保护）

原来总线把动作发给 :8013 就不管了：执行端没开、USB-RS485 松了，时间线上照样写"臂+手#1 按下 SOS 键"，演示员看不出来。现在：

- **保命动作没做成，马上告警**：按 SOS、轻握、两块举牌，任一没做成，操作台日志出一条红色"保命动作没做成：…；执行端连不上。通知家属走软件通道，照常送达；现场可切 Plan B"。大屏不显示（type=system）。
- **其他动作**（递药、手指操）失败只在刚开始失败时记一条，13 拍手指操不会刷屏。
- **在线探测**：总线每 2 秒 `GET :8013/status`，开演前就知道执行端在不在（操作台"执行端"灯），掉线、恢复各记一条。
- **分清"连不上"和"被拒绝"**：执行端回了 4xx/5xx（多半是动作名在 `poses.py` 里没有）算在线但拒绝，日志里带执行端给的原因。
- **断链保护（§5.2 的总线一侧）**：排队超过 `arm_stale`（5 秒）还没发出去的动作直接丢，链路恢复时臂不会补做一串过期动作。**不重发**：超时的请求可能已经到了执行端，重发 press_sos 等于按两下。
- `arm_timeout`（1.5 秒）、`arm_stale` 是配置项，也能经 `/api/params` 现场调。

**和硬件同学的约定**：全队只运行仓库里的 `code/hand_controller/arm_service.py`，不要把 kit 自带的第二份服务放到 8013。主脑使用 `POST /pose?name=<规范动作>&hand=<1|2>`；为了让 kit 的验收命令也能打到同一个服务，执行端还兼容 `/pose/<动作>`、`/press_sos`、`/handover_med`、`/reset`，并把 `rock`、`comfort_rhythm`、`thumbs_up`、`handover_med` 映射到仓库规范动作。

双手的真实后端是 `rh56f2_driver.py`（**EB 90 协议**，2026-10-24 整合时从 Modbus 版换过来：这只 RH56F2 实测只回 EB 90、Modbus 完全不回，见 §10.13 和 `docs/RH56F2_实测.md`）。真实写入只认 `rh56_register_map.VERIFIED`：F2 的角度设置地址确认前保持 False，执行端对动作回 503，只读探测照常。`PROVISIONAL`（= not VERIFIED）和 `RH56_REGMAP_CONFIRMED` 仍保留给 `tools/hardware_preflight` 报告用。Panthera-HT 仍通过 `arm_backend_vendor.py` 接入（`ARM_BACKEND=vendor_sdk`），SDK 未安装或动作映射未填时会诚实返回失败。

无论是真手还是 mock，`POST /pose` 都应快速回执；动作执行不能阻塞 HTTP 请求。做不了的动作返回错误，在 JSON 的 `error` 里写原因，总线原样显示；`GET /status` 保持可用，总线靠它探测在线。

双手真机环境安装 `pip install -r code/requirements-hardware.txt`。默认仍是安全的 mock：`MOCK_SERIAL=1`。上真手的步骤见 §10.13（先只读 `--probe`，F2 地址确认后再改 `VERIFIED`）；只接通串口或只安装 pyserial 不代表动作已经验证。Panthera 相关资料见 `docs/22_真实控制资料卡.md`，SDK 到手前保持 `ARM_BACKEND=mock` 或让 Panthera 动作明确失败。

上真机前可以先运行只读检查：`cd code && python -m tools.hardware_preflight --arm-url http://127.0.0.1:8013`。它只扫描可见串口并读取执行端 `/status`，不会发姿态或写寄存器；现场要强制检查 real 条件时加 `--mode real --hand1-port <COMx> --hand2-port <COMx> --require-real`。寄存器表仍标记为占位时，该命令会明确阻塞 real 模式。

按优先级接续现场工作见 `docs/25_现场执行清单_优先级.md`；其中包含真手低风险顺序、Panthera SDK 四项资料、手机跌倒三连验收和可直接填写的现场回执。

不在现场时运行 `powershell -ExecutionPolicy Bypass -File deploy/offline_smoke.ps1`。它强制 `MOCK_SERIAL=1`，只跑回归、编译和假时钟演示，完全不会扫描或连接硬件。

Windows 笔记本可以用 `powershell -ExecutionPolicy Bypass -File deploy/start_hand.ps1 -Mode mock` 起仓库服务；寄存器核对完成后再用 `-Mode real -ConfirmRegisterMap -Hand1Port COM5 -Hand2Port COM6`。`deploy/hand_demo.ps1` 只发张开、挥手、点赞三条手姿态，便于先验收双手，不会绕过主脑直接触发 SOS。

### 10.9 联调自检（`tools/preflight`）

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
| 时区 | 本机 UTC 偏移和 expected_utc_offset（默认 +8）比 | ⚠️（X5 设 Asia/Shanghai；夜间段用操作台开关） |

- 没有 ❌ 退出码 0，有 ❌ 为 1，连不上主脑为 2，可以写进开机脚本。数据来自 `GET /api/nodes`，自检本身不触发任何动作。
- 语音工具订阅事件时带上 `client=脚本名`（`voice/bus_client.py` 改了一行），自检靠它认出喇叭、麦克风那头；没更新的旧版显示为"未标名"。

**部署按两块 X5**（建议，等硬件同学确认）：

| 节点 | 跑什么 | 接什么 |
|---|---|---|
| X5-A | 视觉：`fall_bridge --camera`、猜拳 `rps_bridge` | 摄像头 |
| X5-B | 主脑 :8000 + 三块屏 + `tts_player` / `asr_bridge` + 执行端 `arm_service` :8013 | 显示器、喇叭、麦克风、USB-RS485（双手） |

执行端和主脑放同一块：保命链"主脑 → 执行端"走本机回环，不经过 Wi-Fi，`arm_url` 用默认的 `http://127.0.0.1:8013`。视觉跨板走 Wi-Fi/网线，断了还有操作台"触发摔倒"兜底。

### 10.11 软件急停（§5.2 的软件一侧）

物理急停按钮归硬件同学，一按断电；软件急停是它的补充：演示员在操作台按 `Esc` 或点“🛑 急停”，**臂和双手立刻不再接收任何动作**。

- 总线：`POST /api/estop {"on": true}`。队列里没发出去的动作全部丢弃（不补做），并尽力通知执行端 `POST :8013/stop`。
- **保命链照常走软件通道**：急停中摔倒，照样询问 → SOS → 通知家属，只是臂不按 SOS 键，操作台日志出红色“急停中，保命动作未执行”。
- 用到臂的技能（递药、手指操、猜拳）被资源锁挂起，老人听到“机械臂先停一停……”；进行中的手指操、猜拳当场中止。
- **重置演示不解除急停**；解除只能点“解除急停”并确认现场安全（键盘不给解除，防误触）。解除后不补做急停期间的动作。
- 自检（`tools/preflight`）在急停未解除时报 ❌；大屏“床边执行端”变灰、显示“急停中”。
- **和硬件同学的约定**：真执行端实现 `POST /stop`：立刻停止当前运动、保持姿态，回 200；之后的 `/pose` 照常接收（总线在急停期间不会发）。

### 10.12 时区自检

夜间模式和健康日报的“今天”按**本机时区**算。X5 装好 Ubuntu 默认 UTC，比北京时间慢 8 小时，晚上 8 点它以为是中午。自检会比对本机 UTC 偏移和 `expected_utc_offset`（默认 +8），不一致报 ⚠️：X5 上 `sudo timedatectl set-timezone Asia/Shanghai` 后重启主脑；演示的夜间段本来就用操作台开关，不依赖钟点。回归测试的假时钟也改成按本机时区取“上午 10 点”，在任何时区的机器上都能跑过。

**演示机一律默认日间**：笔记本 `code/config.local.json` 写 `{"night_override": "off"}`，X5 在 `deploy/x5.env` 里 `export CAREMATE_NIGHT_OVERRIDE=off`（模板已带）。夜里那段由操作台“夜间模式”开关或 `demo_driver` 打开，重置后回到日间。`demo_driver` 的早上 / 中午两段也会先强制日间。

### 10.13 执行端接真手（RH56F2，按因时官方手册 V1.09）

驱动按《因时机器人仿人五指灵巧手--RH56用户手册V1.09cn》§2.2 的 **EB 90 自有协议**重写（kit 里的 Modbus 版地址全是占位、和手册对不上）。**执行端接口没变**（`POST /pose?name=&hand=`、`/stop`、`/status`），主脑一行没改。
**不要**用 kit 里的 `arm_service.py` / `START_HAND.bat` / `GO_HAND.ps1`：接口是 `/pose/<名字>` 一套（主脑发的动作全 404），而且一找到 COM 口就往占位地址 `0x0100` 写动作。这只手**不需要烧录**，出厂自带固件。

| 文件 | 干什么 |
|---|---|
| `hand_controller/rh56f2_driver.py` | EB 90 协议：读 `EB 90 ID 04 11 地址低 地址高 字节数 校验`，写 `EB 90 ID 字节数+3 12 地址低 地址高 数据 校验`，校验 = 去掉包头后累加和低 8 位。回包逐项校验（包头 `90 EB`、ID、命令、地址、长度、校验和）；兼容会回显的 USB-RS485 转换器；两只手可共用一个口；串口断了自动重开。帧格式和手册 4 个原文例子逐字节一致（测试盯着） |
| `hand_controller/rh56_register_map.py` | 手册地址：`ANGLE_SET` 1486、`FORCE_SET` 1498（g）、`SPEED_SET` 1522、`ANGLE_ACT` 1546、`FORCE_ACT` 1582、`ERROR` 1606、`STATUS` 1612、`TEMP` 1618。关节顺序 **小拇指、无名指、中指、食指、拇指弯曲、拇指旋转**；角度 1000 = 张开、0 = 握紧、-1 = 不动。**只许写角度 / 力度 / 速度**，改 ID、波特率、`SAVE`（写 FLASH）、`RESET_PARA`（恢复出厂）一律拒绝。`VERIFIED = False` 时真机只读 |
| `hand_controller/poses.py` | `JOINTS`：仓库每个动作名 → 六个关节开合度（0 握紧 / 100 张开，驱动 ×10 成角度）；`RHYTHMS`：轻握安抚的一握一松节拍 |
| `hand_controller/arm_service.py` | 收到就回 200，每只手一个线程；新动作或 `/stop` 立刻打断节拍、急停前排着的作废；真手 `/stop` 还会把角度设置值写 -1（手册：手指停在当前位置） |

环境变量：`MOCK_SERIAL=0` 才碰串口（默认 mock）；`HAND1_PORT`/`HAND1_ID`、`HAND2_PORT`/`HAND2_ID`。**手出厂 ID 都是 1**：两只手各接各的串口时设 `HAND2_ID=1`；要接同一条总线，先用官方上位机把手#2 改成 2（程序不写 ID）。真机要 `pip install pyserial`。

**第一次上真手**（Windows 用 `set`，X5 用 `export`）：

1. 看手上型号：`RH56F2-E2…` 是 RS485 版，走这套驱动；`E4…` 是 CAN FD 版，帧格式完全不同（因时《CAN增补协议V0.0.2cn》：1 Mbps、29 位扩展帧，ID 里编手号 / 寄存器地址 / 读写位），要另写。
2. 插转换器，设备管理器里看 COM 口（本组笔记本上蓝盒是 COM7，COM3~6 是蓝牙）。X5 上是 `/dev/ttyUSB*`，`setup_x5.sh b` 已装 pyserial 并把用户加进 `dialout` 组，**重新登录一次**。
3. 上 24V 后跑只读探测：`MOCK_SERIAL=0 HAND2_PORT=COM7 HAND2_ID=1 python -m hand_controller.arm_service --probe`。它只发一条读 `ANGLE_ACT` 的指令，读到 6 个 0~100 的开合度 = 线、ID、波特率、协议都对。**读不到先别往下走**。
4. 读通了再把 `VERIFIED` 改成 `True`；起服务，先发最安全的张开：`POST /pose?name=release&hand=2`，再试 `gentle_hold`、`/stop`。`JOINTS` 里标 `:示教:` 的数对着实物调。

**安全**：轻握节拍最紧只到开合度 50（`GENTLE_MIN_OPEN`，测试盯着），`FORCE_SET` 300 g（手指受力到了就停），最长 60 秒；`/stop` 打断节拍、作废排队、真手写 -1 停住。手册里没有专门的急停寄存器，**拔 24V 是最后一道**。

**实测（2026-10-23，详见 `docs/RH56F2_实测.md`）**：EB 90 协议经蓝盒（COM7）能和手通信（ID 1、115200），但 **F2 的寄存器地址和 V1.09 不一样**——V1.09 的 1486~1617 在 F2 上全是 0xFF，实际角度地址还没找到，所以 `VERIFIED` 仍是 False。**读 4032 及以上的地址会把手卡死**，恢复要 24V 和蓝盒都断电、先插蓝盒再插 24V、等 10 秒。臂（Panthera-HT）的 SDK 还没接，臂的部分只打印。

### 10.10 回归与合并

- 联调这批新增 2 套：`test_arm_link`（回执、告警、断链保护、探测，外加真 `arm_service` 端到端）、`test_preflight`（`/api/nodes` 和自检判定）。
- 新增 4 套：`test_llm_fallback`（回落、冷却、内容检查、不占锁、紧急插队）、`test_config_params`、`test_demo_driver`、`test_merge_gate`。另外 `test_agent_supervisor` 补了资源锁，`test_bus_http` 补了 `/api/params` 和 `/api/box`。
- 测试只读 `config.py` 默认值，不受本机 `config.local.json` 和环境变量影响（`config.load(use_local=False)`）。
- `python tests/run_all.py fsm help` 可以只跑几套；结果表里每套标了负责人，还会列出负责人还没提交的"缺"的套。

合并流程（§7.3 协作规则 3）：

1. 每人在 `角色/主题` 分支上开发：`captain/…`、`demo/…`、`hardware/…`、`software/…`
2. 软件跑 `python tools/merge_gate.py demo/vision`：列出改动归谁；别人的分支改了 `master_service.py` 直接拦下，动了其他人的模块会提醒先打招呼；在临时 worktree 里试合并进 `main`，并在合并结果上跑回归
3. 全部通过后，按它打印的命令人工合并
4. Day2 12:00 前打 tag 冻结：`git tag demo-final`

软件模块碰到别人的文件：`agent/lines.json`（队长）加了一句 `skill_busy` 台词，供资源锁挂起时口播；联调这批改了 `voice/bus_client.py`（队长）一行，订阅事件时报上脚本名。

## 11. 视觉模块（演示）

按 §7.3 演示同学的四项任务实现：跌倒检测、猜拳识别、X5 运行环境。核心规则是纯标准库，**只有取摄像头画面和跑姿态模型需要装包**。

| # | 任务 | 状态 | 在哪 |
|---|---|---|---|
| ① | 笔记本跑通 `--camera`，依赖写进 README | ✅ 姿态/手部模型已在 mediapipe 1.1.1 官方测试图上实测（站立人像躯干 2°、旋转 90° 后 89°、张开的手判为"布"）；**摄像头取帧（cv2）还没在本机实拍过**，见 11.3 | `vision/fall_bridge.py`、`vision/pose_backends.py` |
| ② | 用真实画面调 `fall_rules`，降低误报漏报 | ✅ 规则改成看时间（见 11.2），9 个合成场景 × 多种帧率/抖动 0 误判；配套调参窗口、录制、离线评估工具 | `gesture/fall_rules.py`、`vision/preview.py`、`vision/eval_falls.py` |
| ③ | `--video` + 预录跌倒视频（Plan B） | ✅ `--video` 按真实时间播放；另有 `--keypoints`（回放录下的骨架，不需要摄像头和模型）和 `--synthetic fall`（内置合成跌倒） | `vision/fall_bridge.py`、`vision/synth.py` |
| ④ | 2 块 X5 装环境、写开机脚本 | ✅ 装环境、systemd 开机自启、桌面登录后自启（喇叭/大屏）；还没在 X5 实机上跑过 | `deploy/` |
| — | 猜拳手势识别 | ✅ 视觉只认手势（分类 + 多帧投票）并回传；一局流程和判输赢在主脑，由软件分支 `software/rps-hook` 提供 | `gesture/rps_engine.py`、`vision/rps_bridge.py` |

回归：`python tests/run_all.py` 合并 main 后 16 套 132 条（视觉新增 `test_fall_detector`，`test_rps_engine` 改为只测视觉一侧；`test_fall_detector` 的负责人登记在 `software/rps-hook` 里）。本分支只改视觉文件，`master_service.py` 等软件文件一行没动。

### 11.1 跑起来

```powershell
# 依赖（只装 mediapipe：它自带 opencv-contrib-python，别再另装 opencv-python）
pip install -r code/requirements-vision.txt
cd code
python -m vision.pose_backends --download     # 下载姿态 + 手部模型（约 13MB）到 code/vision/models/
python -m vision.pose_backends --check        # 自检：加载两个模型各跑一帧

python -m vision.fall_bridge --camera --preview            # 实拍 + 本机调参窗口
python -m vision.fall_bridge --video falls/fall_01.mp4     # Plan B：预录视频
python -m vision.fall_bridge --keypoints falls/fall_01.jsonl   # Plan B：回放骨架（不需要模型）
python -m vision.fall_bridge --synthetic fall              # 兜底：内置合成跌倒，什么都不用装
python -m vision.rps_bridge --camera                       # 猜拳（展台）；--stdin 键盘代替摄像头
```

发给总线的格式和 mock 一模一样：倒地时 `POST /mock/fall?room=…`，平时约 5 次/秒 `POST /api/skeleton`，大屏直接画实时骨架。画面只在内存里处理，不落盘不外发。

### 11.2 跌倒规则：看"过程"，不看单帧

单帧只看得出"身体横着"，分不清摔倒、弯腰捡东西、上床躺下。现在的判定：

- **快速跌倒**：1 秒内髋部下降 ≥ 0.5 个躯干长、躯干变横（站着或坐着摔下来都算）→ 2 秒内没站起来 → 报警。看不到人也算"没站起来"（摔在床后被挡住）。
- **缓慢滑倒**：横躺在低处（比站立时低）、不在床区，持续 12 秒 → 报警。
- **不报**：踉跄后马上站起、坐下、弯腰（髋部不降）、在床区里躺着。
- 报警后要站稳 2 秒才重新布防，一次摔倒只报一次。

合成场景实测：从开始摔到报警 2.3 秒左右（落地后约 1.7 秒），之后主脑再按日 5 秒 / 夜 3 秒询问。所有阈值在 `DEFAULT_RULES`，命令行可改：`--set lying_angle=50`、`--bed 0.55,0.40,0.98,0.75`（床区，归一化坐标）、`--rules my_rules.json`。

### 11.3 实拍调参流程（演示同学要做的）

1. `--camera --preview`：窗口里看骨架、躯干角度、髋部下降、规则状态；按 `q` 退出。
2. 录素材（只存坐标，不存画面）：`--camera --record-keypoints recordings/fall_01.jsonl`。文件名带 `fall` 的是跌倒，其余（`sit_`、`bend_`、`bed_` …）是日常动作。建议各录 3 段以上，摔的时候垫好垫子。
3. 批量评估：`python -m vision.eval_falls recordings/`，输出每段是否漏报 / 误报和报警时间；改参数再跑：`--set confirm_hold=1.5`。
4. 定下来的参数写进 `rules.json`，开机脚本里 `VISION_ARGS="--rules rules.json"`。
5. 录好的 `recordings/` 可以提交进仓库，以后谁改规则都先跑一遍 eval。

### 11.4 Plan B 顺序

现场跌倒判定不稳时，从上往下换：`--camera` → `--video 预录视频` → `--keypoints 预录骨架` → `--synthetic fall` → 操作台"触发摔倒"按钮。后三种不依赖摄像头和光照。

### 11.5 两块 X5（部署映射见 §10.9）

| 板 | 角色参数 | 开机自启 | 接什么 |
|---|---|---|---|
| X5-A | `a` | `vision`（猜拳 `rps` 在展台手动起） | USB 摄像头 |
| X5-B | `b` | `master` :8000、`arm` :8013、`tts`、`kiosk`（大屏），`WITH_ASR=1` 时加 `asr` | 显示器、喇叭、麦克风、USB-RS485（双手） |

```bash
cp deploy/x5.env.example deploy/x5.env      # 两块都改 MASTER_HOST（X5-B 的地址）；X5-A 再看 CAMERA_INDEX
bash deploy/setup_x5.sh a                    # a=视觉 b=主脑+大屏+语音+执行端：装 apt 包、建 .venv、跑回归自检
bash deploy/install_services.sh a            # 开机自启
bash deploy/start.sh vision                  # 手动起单个服务调试：vision|rps|master|tts|asr|arm|kiosk
```

- 执行端和主脑在同一块 X5-B，`ARM_HOST` 保持 `127.0.0.1`：保命链"主脑 → 执行端"走本机回环，不经过 Wi-Fi。旧的 `c` 参数还认，会提示后按 `b` 装。
- 不需要声卡/屏幕的（vision / master / arm）装成 systemd 服务，开机就起、挂了 2 秒自动重启；日志 `journalctl -u caremate-vision -f`。
- 要用喇叭/麦克风/屏幕的（tts / asr / 大屏浏览器 kiosk）装成桌面登录后自启，因为系统服务拿不到登录用户的音频和显示。
- mediapipe 有 Linux aarch64 轮子，X5（Ubuntu 22.04）可以直接 pip 装；X5 若连不上 Google，从笔记本拷 `code/vision/models/` 过去。
- 假设是 **USB 摄像头**；若用 MIPI 摄像头，取帧要换地平线的接口，`opencv_source` 之外的逻辑不变。
- 笔记本单机彩排：`powershell -ExecutionPolicy Bypass -File deploy\start_laptop.ps1 [-Vision camera]`，每个服务一个窗口。

### 11.6 猜拳一局怎么走

视觉这边只做一件事：在揭晓前后约 2 秒内逐帧识别老人的手势、多帧投票，回传 `POST /api/rps/gesture {"round": n, "gesture": "rock"|"scissors"|"paper"|null}`。一局怎么走、机器人出什么、谁赢，都在主脑一侧（`software/rps-hook`，README §10.7）。手势识别按"指尖到手腕的距离"判断手指伸直，和手怎么转、离多远、画面比例都无关。

## 12. 手机摄像头保底哨兵

当 X5 的 MIPI 摄像头不可用时，可以用安卓手机运行 IP Webcam，把手机当作临时视觉输入。哨兵只负责从 `/shot.jpg` 取帧、计算动作能量并向主脑发送 `POST /mock/fall`；它不直接调用机械臂，跌倒后的询问、升级和家属通知仍由主脑状态机负责。

```powershell
pip install -r code/requirements-phonecam.txt
cd code
python -m camera.phone_fall_watch --phone http://<手机IP>:8080 --arm 0
python -m camera.phone_fall_watch --phone http://<手机IP>:8080 --master http://127.0.0.1:8000 --thresh 38 --cooldown 30 --arm 1 --room 客厅
```

`--arm 0` 用于现场校准，`--arm 1` 才会真正上报；`--conn` 控制连续超阈值帧数。手机和主脑必须处于同一网络，手机应保持插电并关闭省电策略。X5 部署可在 `deploy/x5.env` 设置 `PHONE_URL` 和 `PHONE_ARGS`，然后运行 `bash deploy/start.sh phone`；执行 `bash deploy/install_services.sh a` 时若已配置 `PHONE_URL`，会在 X5-A 上安装手机哨兵 systemd 服务。完整交接步骤见 `docs/21_交接_手机摄像头组.md`。
