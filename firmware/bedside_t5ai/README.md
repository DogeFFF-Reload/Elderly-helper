# T5AI 大床头屏（涂鸦 T5AI-Board + LCD）

大屏 = 臂主导的床头遥控：四个大键 + 底部语音键，全部打到 X5-B 的 `arm_service`（:8013，`code/hand_controller/arm_service.py`）。
固件 v0.5，2026-10-24 由队友交接（交接包 `caremate_handover.zip`），屏上实测横屏、按键、在线灯都正常。

| 键 | 请求 | arm_service 里的动作 |
|---|---|---|
| 递药盒 | `POST /handover_med` | `deliver_medicine`（臂） |
| 还药 | `POST /return_med` | `return_medicine`（臂：张开手等 10 秒放进来，送回托盘） |
| 按SOS | `POST /press_sos` | `press_sos`（臂，保命动作） |
| 归位 | `POST /reset` | `home`（臂） |
| 点一下 说话 | `POST /voice_cmd`（16 kHz/16 bit/单声道裸 PCM，录 4 秒净得约 3 秒） | `voice_cmd.py` vosk 离线识别 → 关键词 → 上面同一条路（手#1） |
| 顶部状态条 | 每 2 秒 `GET /status` | 回 200 = 绿「已连接」，否则红；录音时橙 |

固件只看 HTTP 通不通：动作被拒（409/422/503）屏上不显示，看 X5 上的日志 `journalctl -u arm_service -f`。

## 文件

- `bedside_t5ai_arm/bedside_t5ai_arm.ino`：固件。现场要改的都在「现场校正区」：WiFi（Mocen_Bear）、`ARM_HOST`（10.153.47.251）、屏旋转、录音时长。
- `gen_t5ai_btns.py`：中文按钮 / 状态条位图的唯一来源（LVGL 默认字体没有汉字）。改按钮先改它，再 `python gen_t5ai_btns.py` 重新生成 `bedside_t5ai_arm/t5ai_bitmaps.h`（需要 Pillow + Windows 微软雅黑；生成的 .h 不要手改）。

## 编译 / 烧录

USB 接 UART0（CH342 的 A 口，本机 COM9），约 35 秒：

```
D:\arduino-cli\arduino-cli.exe compile --fqbn tuya_open:tuya_open:TUYA_T5AI_BOARD bedside_t5ai_arm
D:\arduino-cli\arduino-cli.exe upload  --fqbn tuya_open:tuya_open:TUYA_T5AI_BOARD -p COM9 bedside_t5ai_arm
```

compile 退出码 1 但打印 BUILD SUCCESS 也算过。串口日志：COM9，115200（B 口 COM10 是乱码）；`http POST /xxx -> -1` = X5 不可达。

## 踩过的坑（别再踩）

- LVGL 回调跑在 LVGL 线程里，`lv_vendor_disp_lock` 不可重入：回调里只改 UI、挂标志，HTTP 全在 `loop()` 里串行发。
- PCM 裸流不能用 HTTPClient 封装（内部 `strlen` 量正文长度，遇 0x00 截断），用底层 `http_client_request`，`body_length` 自填。
- Audio 大缓冲要 `tal_psram_malloc`；板载 mic 16k/16bit/mono 与 vosk 同规格，不用转码。
- X5 半死时 POST 会卡住 `loop()` 约 30 秒（屏不冻，LVGL 独立线程）。

## X5 那边

- `arm_service` 以 root 的 systemd 服务 `arm_service.service` 跑（`--host 0.0.0.0`），代码在 `~sunrise/caremate_t5ai`（`~/Elderly-helper` 的 git worktree，分支 hardware/t5ai-screen），
  环境在 `/root/caremate/unit.env`；臂 `ARM_BACKEND=mock` 直到点位示教完。kit 版的服务 / 环境留着 `.kit.bak`，代码还在 `/root/caremate/hand_controller`。
- vosk：系统 python3 `pip install vosk`（0.3.45），模型 `vosk-model-small-cn-0.22` 解压在 `/root/caremate/vosk-cn`（`VOSK_MODEL` 可改）。
  小模型词表里没有「递药」「还药」「按铃」，grammar 里切成「递 药」；闲聊会被硬凑成关键词，所以 [unk] 占说话时长一半以上不算指令。
  2026-10-24 用 edge-tts 两个声音实测：13 条指令全对、2 条闲聊都没动，每条约 0.4 秒。
- 安全：应急停 = 拔 24V；手的上电顺序 USB → 20 秒 → 24V。
