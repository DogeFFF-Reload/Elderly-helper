# Mocen_CareMate · 交互端（队长模块）

按《工作报告 v2.5》§7.3 的分工，这里实现的是**交互端**：语音链路（ASR / TTS）、三块屏、健康口述、回声风格档案、教学库、scripted 台词。

> ⚠️ 本机没有找到团队代码仓，所以按 v2.5 文档的接口**从零重建**了一个能跑的最小总线（stand-in），让交互端可以独立开发、端到端演示。
> 合并进真仓库时：**交互端文件直接用这里的；stand-in 文件以队友的正式版为准**（见文末「合并说明」）。

## 1. 跑起来

只需 Python 3.8+，核心零依赖。

```powershell
cd code
python tests/run_all.py                  # 回归，必须 ALL PASS（当前 10 套 75 条）
python -m master_service                 # 主脑 :8000（含三块屏）
python -m hand_controller.arm_service    # 臂+双手 mock :8013（另开终端，可不开）
python -m voice.tts_player               # AI 台词 → 喇叭（另开终端）
python -m voice.asr_bridge --backend stdin   # 键盘当麦克风（另开终端）
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
| 操作台 → 主脑 | `/api/night {"mode": "auto|on|off"}`、`/api/reset {"all": "0|1"}`、`/api/agent/propose` |

演示参数都在 `code/config.py`，可用 `config.local.json` 或环境变量覆盖（如 `CAREMATE_ASK_WAIT_DAY=30`）。

## 8. 合并说明（给软件 / 硬件 / 演示同学）

| 文件 | 归属 | 合并时 |
|---|---|---|
| `ui/`、`voice/`、`agent/health_tracker.py`、`agent/voice_profile.py`、`agent/guides.json`、`agent/device_helper.py`、`agent/lines.json` | **交互端（本模块）** | 用这里的 |
| `master_service.py`、`state_machine/`、`agent/intent.py`、`agent/supervisor.py`、`agent/skills.json`、`agent/llm_client.py`、`config.py` | 软件 | stand-in，以正式版为准；交互端依赖的路由见上表 |
| `hand_controller/` | 硬件 | stand-in；`poses.py` 的 `label` 会显示在大屏时间线上 |
| `vision/`、`gesture/`、`deploy/` | 演示 | 已实现（见 §11）；`master_service.py` 为猜拳加了 `/api/rps/gesture`，需软件同学合入 |

**需要软件同学拍板的三处行为**（stand-in 里已这样实现，都可改）：

1. **举牌等家属确认**：手#1"家人已收到"、手#2"女儿马上到"在家属端点"我收到了"之后才举。v2.5 §2.1 是通知后立刻举，但没回执就说"已收到"不诚实。
2. **家属不响应继续升级**（回答 §2.1【待确认】）：30 秒没人确认就通知下一位联系人，都不确认则提示老人可说"帮我打120"。时长 `family_ack_timeout` 可配置。
3. **"确认"的优先级**：120 确认窗 > 血压复述确认，避免一个"确认"被两处同时用掉。

## 9. 仍待确认

- 【硬件】喇叭和麦克风最终装在床头盒还是 X5？在床头盒的话，`tts_player --backend cmd` 把音频转给盒子，协议等硬件同学定。
- 【全队】会场噪音下 Vosk 小模型的识别率需要实测；不够用的话换 `vosk-model-cn-0.22`（大模型）或 sherpa-onnx，桥的接口不变。
- 【演示】`fall_bridge --camera` 还没在真人画面上实拍调参（见 §11.3）；大屏已能画视觉推来的实时骨架。

## 11. 视觉模块（演示）

按 §7.3 演示同学的四项任务实现：跌倒检测、猜拳识别、X5 运行环境。核心规则是纯标准库，**只有取摄像头画面和跑姿态模型需要装包**。

| # | 任务 | 状态 | 在哪 |
|---|---|---|---|
| ① | 笔记本跑通 `--camera`，依赖写进 README | ✅ 姿态/手部模型已在 mediapipe 1.1.1 官方测试图上实测（站立人像躯干 2°、旋转 90° 后 89°、张开的手判为"布"）；**摄像头取帧（cv2）还没在本机实拍过**，见 11.3 | `vision/fall_bridge.py`、`vision/pose_backends.py` |
| ② | 用真实画面调 `fall_rules`，降低误报漏报 | ✅ 规则改成看时间（见 11.2），9 个合成场景 × 多种帧率/抖动 0 误判；配套调参窗口、录制、离线评估工具 | `gesture/fall_rules.py`、`vision/preview.py`、`vision/eval_falls.py` |
| ③ | `--video` + 预录跌倒视频（Plan B） | ✅ `--video` 按真实时间播放；另有 `--keypoints`（回放录下的骨架，不需要摄像头和模型）和 `--synthetic fall`（内置合成跌倒） | `vision/fall_bridge.py`、`vision/synth.py` |
| ④ | 3 块 X5 装环境、写开机脚本 | ✅ 装环境、systemd 开机自启、桌面登录后自启（喇叭/大屏）；还没在 X5 实机上跑过 | `deploy/` |
| — | 猜拳手势识别 | ✅ 机器人先出、揭晓后才显示，输赢由总线判；摄像头不行时操作台有手动按钮 | `gesture/rps_engine.py`、`vision/rps_bridge.py` |

回归：`python tests/run_all.py` 现在 12 套 98 条（新增 `test_fall_detector`、`test_rps_engine`）。

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

### 11.5 三块 X5（§1.2 部署映射）

```bash
cp deploy/x5.env.example deploy/x5.env      # 改 MASTER_HOST / ARM_HOST / CAMERA_INDEX
bash deploy/setup_x5.sh a                    # a=视觉 b=主脑+大屏+语音 c=执行端：装 apt 包、建 .venv、跑回归自检
bash deploy/install_services.sh a            # 开机自启
bash deploy/start.sh vision                  # 手动起单个服务调试：vision|rps|master|tts|asr|arm|kiosk
```

- 不需要声卡/屏幕的（vision / master / arm）装成 systemd 服务，开机就起、挂了 2 秒自动重启；日志 `journalctl -u caremate-vision -f`。
- 要用喇叭/麦克风/屏幕的（tts / asr / 大屏浏览器 kiosk）装成桌面登录后自启，因为系统服务拿不到登录用户的音频和显示。
- mediapipe 有 Linux aarch64 轮子，X5（Ubuntu 22.04）可以直接 pip 装；X5 若连不上 Google，从笔记本拷 `code/vision/models/` 过去。
- 假设是 **USB 摄像头**；若用 MIPI 摄像头，取帧要换地平线的接口，`opencv_source` 之外的逻辑不变。
- 笔记本单机彩排：`powershell -ExecutionPolicy Bypass -File deploy\start_laptop.ps1 [-Vision camera]`，每个服务一个窗口。

### 11.6 猜拳一局怎么走

老人说"石头剪刀布" → 总线**先定好机器人出什么**（大屏、操作台都看不到）→ 约 3.5 秒后手#1 揭晓 → `rps_bridge` 在揭晓前后约 2 秒内逐帧识别、多帧投票 → `POST /api/rps/gesture` → 总线判输赢、播报。没看清就说"再来一局"，6 秒没回传就提示到展台玩。手势识别按"指尖到手腕的距离"判断手指伸直，和手怎么转、离多远、画面比例都无关。
