# T5AI 大床头屏（涂鸦 T5AI-Board + LCD）

大屏 = 臂主导的床头遥控：四个大键 + 底部语音键，全部打到 X5-B 的 `arm_service`（:8013，`code/hand_controller/arm_service.py`）。
固件 v0.8（2026-10-25 烧录）：四个键换成 挥手 / 手指操 / 握手 / 零位待机，界面重做（深色底、渐变卡片、彩色图标 + 大字标题，
位图由 `gen_t5ai_btns.py` 生成，效果图 `preview.png`）。v0.7 起多热点轮流连（`NETS` 表），v0.6 起有 Wi-Fi 看门狗
（掉线 / 连续 5 次连不上 X5 → 断开重连）和 15 s 语音上传超时（X5 走云端大模型，见 `voice_llm.py`）。

| 键 | 请求 | arm_service 里的动作 |
|---|---|---|
| 挥手 | `POST /screen/wave` | 预设 `wave`（1.5 倍速，做完停在原处，和语音一样） |
| 手指操 | `POST /screen/finger` | 手势 `hand_exercise`（十三拍手指操，只动手） |
| 握手 | `POST /screen/shake` | 预设 `handshake`（1.5 倍速，做完停在原处） |
| 零位待机 | `POST /screen/zero` | 零位待机点：手张开，全部关节回 0°（语音说「归位 / 回零位 / 回去待着」也走这个；旧固件的 `/reset` 同样改成这个） |
| 点一下 说话 | `POST /voice_cmd`（16 kHz/16 bit/单声道裸 PCM，录 4 秒净得约 3 秒） | `voice_llm.py`：qwen3.5-omni-plus 听写 → qwen3.8-max 选预设动作 / 业务动作（「停下」本地直接急停）；没网退回 `voice_cmd.py` vosk 离线关键词 |
| 顶部状态条 | 每 2 秒 `GET /status` | 回 200 = 绿「已连接」，否则红；录音时橙 |

固件只看 HTTP 通不通：动作被拒（409/422/503）屏上不显示，看 X5 上的日志 `journalctl -u arm_service -f`。

## 文件

- `bedside_t5ai_arm/bedside_t5ai_arm.ino`：固件。现场要改的都在「现场校正区」：WiFi（Mocen_Bear）、`ARM_HOST`（10.153.47.251）、屏旋转、录音时长。
- `gen_t5ai_btns.py`：中文按钮 / 状态条位图的唯一来源（LVGL 默认字体没有汉字）。改按钮先改它，再 `python gen_t5ai_btns.py` 重新生成 `bedside_t5ai_arm/t5ai_bitmaps.h`（需要 Pillow + Windows 微软雅黑；生成的 .h 不要手改）。

## 编译 / 烧录

USB 接 UART0（CH342 的 A 口，本机现在是 COM12；日志和烧录同一个口，另一个口 COM11 没输出），烧录约 40 秒。
工具：Arduino CLI 1.3.1（`D:\Claude\tools\arduino-cli\`）+ TuyaOpen 1.2.5 / GCC Arm 10.3（装在
`C:\Users\29619\.codex\caremate-tools`，用它的 `arduino-cli.yaml`）：

```
set CLI=D:\Claude\tools\arduino-cli\arduino-cli.exe --config-file C:/Users/29619/.codex/caremate-tools/arduino-cli.yaml
%CLI% compile --fqbn tuya_open:tuya_open:TUYA_T5AI_BOARD --build-path D:/Claude/tools/build-t5ai bedside_t5ai_arm
%CLI% upload  --fqbn tuya_open:tuya_open:TUYA_T5AI_BOARD --input-dir D:/Claude/tools/build-t5ai -p COM12 bedside_t5ai_arm
```

打印 BUILD SUCCESS 就算过。串口日志 115200；`http GET /status -> -1 (transport=2)` = 连不上 X5。
回滚：10-24 修复版二进制是 Codex 交接目录里的 `firmware_t5ai_fixed_QIO.bin`。
USB 串口的 DTR/RTS 复位不了这块板（没接）；要重启就拔插 USB 或按板上复位键。

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

热点密码不入库：第一次编译前把 `bedside_t5ai_arm/wifi_secrets.example.h` 复制成 `wifi_secrets.h` 填上真密码（已在 .gitignore）。
