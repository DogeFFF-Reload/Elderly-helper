# CareMate Agent 交接总览(10-23 早期版·考古收录,原文照录)

> **收录说明(10-24 晚)**:这是 10-23(黑客松 Day 0)写的**早期版全项目开工卷**——"单文件喂给任何 Agent 就能开工"的原始尝试,当时系统还没有床侧/家属端两条线的分工,手驱动、语音包、家属端形态都还在悬而未决。保留原貌记录演进脉络。
> **接手新人请读现行版 `../docs/交接说明.md`**,本篇仅作历史参考。当时写下的"待复核"至今大多已定案,勘误如下:

| 当时(10-23 快照) | 现行(以交接说明.md 为准) |
|---|---|
| 机械手驱动是 RS485/Modbus 假设 ⚠待复核 | **已定:因时 EB90 串口协议**(`rh56f2_eb90.py`,真机在跑,MODBUS 只是历史兼容) |
| voice_pack 52 条(26 台词×2 语速) | **73 条×2 语速**(SAPI 16k/16bit/mono,见 `../voice_pack/README.md`) |
| 家属端 = PWA + 原生 APK 双保险 | **已定:原生 APK 唯一路线**(v1.1,下载安装点开就用;PWA 已废弃,考古见 `../family_app/legacy_webview_v1/`) |
| master_service :8000 = 主脑总台 | 演示主线=**两套独立系统**(本目录=手板线 .252);master_service 留在团队软件侧 |
| 手协议/板号未定 | **板号纪律已定**:.251=臂板(队友侧只读勿动),.252=手板(本目录),双 X5 曾因误推同板清场 |
| 开工仓库 `E:\agents\...`(无 git) | 团队 git 仓库(本仓库),床头侧全量在 `bedside_family/` |

原文照录如下(除本说明与勘误表,未删改一字):

---

# CareMate Agent 交接总览（单文件维度·喂给任何 Agent 就能开工）

> 用途：把这份文档喂给另一台电脑上的 Agent，它就能了解全项目、跑起程序、
> 并在真机日和我（主开发位）协同。文档自含上下文，不依赖外部仓库说明。

## 0. 一句话身份

**Mocen CareMate**——床边陪护机器人（黑客松 MoonStone DreamUP 上海站，官方日程 10/23–25 张江科学之门）。
六条功能线：救（跌倒感知升级）/ 递（递药递牌）/ 传（SOS）/ 陪（猜拳手指操）/ 教（技能教学）/ 回声（心里话畅聊），
外加 健康/轻握/夜间增强。六线全为演示目标，**健康线口径：家人看的档案，不是医疗器械**。

- 仓库：`E:\agents\opencode\opencode_proj\dabashou\`（无 git，主开发机工作副本）
- 门槛判定：`python tests/run_all.py` 必须 **ALL PASS（9 套 82 项）**；彩排 `python tests/e2e_rehearsal.py` 全绿（**33 项**）才准开演
- 今日状态（10/24）：软件面 82+33 双绿全 lock；硬件面开箱接线中，见 §1

## 1. 硬件现场快照（已到 / 未到 / 身份判定）

| 件 | 状态 | 关键事实 |
|---|---|---|
| **机械臂 Panthera-HT**（高擎动力） | ✅ 已到 | 六轴+**夹爪=第7电机**（`gripper_open/close`）；官方 Python SDK（含中文指南 hightorque.cn/Panthera-HT_Hub/guide.html）；QQ群 1035440629；**SDK wheel 仅 Linux**；供电 220V→24V 官方砖（铭牌待拍）；**掉电=臂体跌落** 官方警告 |
| **机械手 ×1**（黑手本体） | ✅ 已到 | 一缆三件套：**深蓝色透明转接盒 = USB→CANFD（丝印 COM-USB-CANFD-01，端子拧线 + DIP 四拨码）**——CAN 方言，**不是 RS485**；银色航空插头长缆（电源+通信合一）；DB9 母头=同套备胎线；**电源砖 = Fuyuang FY2403000：24V/3A/72W**（不是预估的 5A→首电限力） |
| **机械手 ×2** | ⏳ 未领 | 领货时照 §1 第一只清点逐件对照 |
| **主板 RDK X5**（D-Robotics 旭日X5） | ✅ 1台已通电验证 | **左 Type-C=电源（LED亮），右 Type-C=数据口（RNDIS 网卡，板 IP 192.168.128.10）**；RJ45 网口=192.168.127.10（静态）；串口兜底=micro-USB CH340@115200；SSH `root`/`root`；**盒内无 IPEX 天线**；出厂 Ubuntu+TROS |
| **床头盒**（立创实战派 T5AI，涂鸦） | ✅ 已点亮 | 无电池=纯线供；屏+喇叭+麦克风+触摸齐装；刷机等立创例程/涂鸦授权码 |
| **显示器** | ⏳ 未领 | 要顺带 HDMI 线和转接头（X5 HDMI 直插床头大屏） |
| **借线清单**（未齐） | 🔴 | USB-A↔C 数据线（X5 部署/刷机）、HDMI、床头盒 1.5m 供电线、micro-USB |

**上电铁律**：24V 不与 5V 共插排；手不与 X5 共插排；任何异常先拔 24V/墙插；手沟通未确认前**允许通电但只读不写**（不喂动作指令）；臂通电后臂下永不站人。

## 2. 软件地图（每侧谁是干什么的）

```
code/
├─ master_service.py        主脑+HTTP 总台（默认 :8000，stdlib 零依赖）
│   ├─ _mouth_loop          装置的嘴（Win=SAPI Huihui；Linux=X5 走 voice_pack wav+aplay）
│   ├─ /ui/monitor|family|console    三大页面（大屏/家属/老人操作台）
│   ├─ /api/vm/send|GET /api/vm/<file>, /api/vm/...   留言簿原声（隐私红线：只存本机）
│   ├─ /api/privacy          隐私仪表盘（监听音频 0 字节 + 留言原声仅本机）
│   ├─ /api/dyn/*            动态提醒闹钟（开演前必 POST /api/dyn/clear）
│   └─ /mock/*               测试注入点（mock/fall 等，彩排用）
├─ hand_controller/          手的控制中枢（人在笔记本或 X5 都能跑）
│   ├─ arm_service.py       HTTP 服务 :8013  ← **日常操作入口**
│   ├─ poses.py             **动作真源词表**（猜拳/点赞/捏卡…，LLM 只许报名字）
│   ├─ rh56f2_driver.py     手驱动（MOCK_SERIAL 开关；RS485 假设⚠待CAN复核）
│   └─ rh56_register_map.py 寄存器表（**待厂家核对**，docs/10 驱动笔记）
├─ deploy/rdk_deploy.ps1     一键把 master 迁去 X5（scp+systemd 自启）
├─ ui/                      monitor/family/console 三端 + APK/PWA 资产
├─ voice_pack/              52 个离线 WAV（26 台词×2 语速）
├─ battery APKS             family_apk/CareMate_family.apk（已签名 VERIFIED）
└─ tests/                   run_all.py（82 项）+ e2e_rehearsal.py（33 项）
```

### 2.1 手（机械手）操作面

**启动**（真手）：

```cmd
set MOCK_SERIAL=0
set HAND1_PORT=COM5      ← CANDFD 蓝盒枚举出的 COM 号（CP210x 多见）
python -m hand_controller.arm_service
```

**API**（谁都这么喊）：

| 动作 | 命令 |
|---|---|
| 张开 | `POST http://127.0.0.1:8013/pose/fingers_5` |
| 石头/剪刀/布 | `/pose/rock` `/pose/scissors` `/pose/paper` |
| 点赞 | `/pose/thumbs_up` |
| 捏卡/松手 | `/pose/hold_card` `/pose/release` |
| 演示序列 | `/pose/wave` 等（在 poses.py SEQUENCES） |

**词表铁律**：LLM/状态机只许引用 `POSE_VOCAB` 的名字，越权即收不进。

**未决问题（⚠ 按 CANFD 复核协议层）**：
- 转接盒 = USB→CANFD；**`rh56f2_driver` 底层为 RS485/Modbus 假设**，也许要换血为 CAN 帧（若 CY 打开 `hightorque_robot` SDK 或厂家文档）；
- **高层不动**：poses 词表 / arm_service HTTP API / 演示剧本触发全保留；
- 定案前：**只通电只读，不喂动作**（CAN 帧乱发=无害，但别瞎写）。

### 2.2 臂（Panthera-HT）操作面

**官方 SDK**：`https://github.com/HighTorque-Robotics/Panthera-HT_SDK`（MIT）+ 简版示例：

```python
from Panthera_lib import Panthera
robot = Panthera("../robot_param/Leader.yaml")
robot.gripper_open() / gripper_close()      # 夹爪=第7电机，限位 0.0~2.0 rad
```

**约束**：
- wheel **仅 Linux**（motor_whl/linux_x86_64）→ 臂的控制台跑 X5/Linux，不跑笔记本 Windows
- 官方警告：掉电时手臂会垂落（扶好）
- 24V 大电流（官方推荐可调 24V supply，15A 规格随机发砖待铭牌确认）
- 我方 arm_service `ARM_BACKEND`：`mock`（今日）/ `vendor_sdk` / `waypoint_seq`——路线已预留，真机日接 SDK 就靠这三个名字摆档（"递物/举牌/SOS按压/猜拳" 逐条映射）

### 2.3 主板（X5）操作面

```cmd
# 笔记本（任何 Windows 机）一键部署（SSH 手输 root/root）
powershell -NoProfile -File code\deploy\rdk_deploy.ps1
# 板上 systemd 服务 caremate（开机自启 + TTS_OUT=1）
```

**策略**：板上只跑 `master_service`；`arm_service`（手，真机日）也在板上/笔记本二选一；
臂（Linux SDK）= **X5 旁路另开 Linux shell**（统领，不抢 HTTP 面相）。
**任务台词出口**）：Linux 分支已写进 `_mouth_loop`——X5 上用 voice_pack 脚本逐句 aplay，**台词库名字与包内台词一字不差才出声，对不上就闭嘴**（不瞎拼）。

### 2.4 显示面（显示器侧操作极少）

- 三页面 URI：` http://<X5_IP>:8000/ui/monitor` 大屏 / `/ui/family` 家属 / `/ui/console` 老人台
- 家属手机端：**双保险** = PWA 添加到主屏幕 + 原生 APK（零第三方依赖，已签名 VERIFIED）
- 女儿页面中的红点未读计数 = 家属页 title 徽章 + 留言簿红点

### 2.5 语音链（照常通透明合成）

| 场景 | 管理 |
|---|---|
| 笔记本真机日 | `start_demo_loud.bat`（SAPI Huihui 零网络） |
| X5（无 SAPI） | Linux 分支自动走 voice_pack 逐句 aplay，同 `_mouth_loop`；台词不入包=只上报事件不出声 |
| 真手语音收音 | 未通——收音链路待评跟随（厂家 ASR/手机传递/手机麦克风浏览器语音识别三选一后定） |

## 3. 命令速查（真机日照抄）

```cmd
:: 软件门槛
python tests/run_all.py

:: 双绿验证后开演
python tests/e2e_rehearsal.py
:: 开演前必做（清彩排挂的提醒/原声）
POST /api/dyn/clear

:: 三窗口（真机日点位）
python -m master_service                                        :: :8000
python -m hand_controller.arm_service                             :: :8013（真机日 MOCK_SERIAL=0）
tail -f state_machine/events.jsonl                              :: 事件流（可选）
```

## 4. 未决问题与待办（给接任 Agent 的 mission queue）

1. **手协议复核（最高优先）**：CANFD 转 CAN 帧 vs 官方文档核对 → driver 只动 `rh56f2_driver/RM`，**高层调用不变**。
2. **X5 部署**：需一根 USB-A↔C 数据线到手（路由 RNDIS 通道）→ `deploy/rdk_deploy.ps1`。
3. **24V 首电 SOP**：通电→以 `arm_service` MOCK_SERIAL=0 跑只读（get_state 类）→ 稳 10 分钟无异味/无抽搐 → 首条动作 `fingers_5`（全开，安全位）。
4. **24V/3A 抽查机制**：若 grip force 满握突然掉电/重启 → 换赛方 24V/5A 砖，先 test 低负载一节不变。
5. **显示器到货日** = 总装日（4–6h 有效工时，臂 SDK 确认+接线定标一遍 loop）。

## 5. 验收定义（唯一上台阶口径）

**连续 3 遍完整 3 分钟演示流程，零救场，82+33 双绿 → 冻结（追加功能一律拒绝）**。
剩余时间全给心态/背稿/彩排，不给代码。

## 6. 你接任 Agent 的第一步（笔记本场景）

```cmd
cd /d <解压的 code 路径>
pip install pyserial
python -m hand_controller.arm_service           :: 模拟模式当天/mock 全绿即入职
powershell -c "Invoke-RestMethod -Method POST http://127.0.0.1:8013/pose/fingers_5"   :: 模拟链路验通
```

之后按 §4 mission queue 推进。**任何异常先拔 24V，代码再修。**