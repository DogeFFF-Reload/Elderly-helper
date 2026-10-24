# 床头盒固件 bedside_box

无屏床头盒(X5 侧伴生板/或 ESP32 小盒,按键+喇叭形态):给"床头盒无线留言"链路做收发终端。仓库主线方案见 `docs/29_床头盒无线留言_对接说明.md`(软件侧中继)。

## 与本仓其他固件的关系

| 板 | 仓库位置 | 角色 |
|---|---|---|
| 嘉立创 S3 床头屏 | `bedside_family/firmware_s3/` | 触摸大六键 kiosk(留言/语音/姿态) |
| **床头盒** | `bedside_family/firmware_bedside_box/`(本目录) | 无屏简易留言键+喇叭 |
| 涂鸦 T5AI 臂屏 | `firmware/bedside_t5ai/`(队友试点) + `bedside_family/firmware_t5ai_handover/` | 臂侧大屏 |
| 诊断板 | `bedside_family/firmware_diag/` | S3 面板排障 |

## 烧录

```bash
arduino-cli compile/upload --fqbn esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app -p <COM口>
```

串口日志 115200(CH340K)。烧录前改 `WIFI_SSID`/`WIFI_PASS` 库源顶部两行(仓库内密码已脱敏为 `YOUR_WIFI_PASSWORD`)。

## 对接端点(同 X5#A:8013)

- `POST /message_chunk?id=..&seq=..` → `POST /message_end?id=..&d=..`  (老人留言,16k/16bit/mono PCM)
- `GET /messages` → 拉列表(只取 `from=family` 的) → GET WAV 播放
- 长按 3s 也可作"呼叫请求"入口(预留,行为同 S3 SOS 键)

> 交接包出处:本固件与 S3 kiosk 共用播放/录音骨架(I2S0 STD,播放/录音互斥),坑位描述见 `../firmware_diag/README.md`。