# 嘉立创 S3 床头屏固件 bedside_s3_arm_kiosk

老人床头触摸屏一体固件(ESP32-S3):六键布局 + 双向原声留言 + 语音命令,全部本地资源编译期烧入(无 SD 卡依赖)。

## 文件构成(三个都要,克隆即编译)

| 文件 | 大小 | 内容 |
|---|---|---|
| `bedside_s3_arm_kiosk.ino` | 41KB | 主程序(~2300 行):屏驱/触摸/双 I2S/留言/语音/HTTP |
| `btn_bitmaps.h` | 1.4MB | 六键图标位图(RGB565 数组,**生成物,入库即源码**) |
| `voice_prompts.h` | 2.2MB | 提示音 WAV(SAPI 16k/16bit/mono,C 数组):还没有留言/已发送/开始录提示 |

提示音可用 `../tools/gen_voice_prompts.py` 重新生成;按键位图无生成脚本入库,以本文件为准。

## 固件能力(对照床头屏六键)

| 键 | 行为 | 调对端 |
|---|---|---|
| 招手 / 比 ok / 拉勾 | 触发手板姿态 | `POST /pose/<name>` |
| 留言 | 按住录 16k/16bit/mono → 分块上传 → 落盘 | `POST /message_chunk?id=..&seq=..` + `POST /message_end?id=..&d=..` |
| 听留言 | 只播**家属的 fam_* 回话**,原声不转文字 | `GET /messages` → 挑 `from=family` → 拉 WAV 播 |
| 语音命令 | 采 4s PCM → Vosk 本地识别(词表 25 姿态+7 序列) | `POST /voice_cmd` |

留言/播放共用 I2S0,**永远不会同时录放**(板上已互斥)。

## 对端

X5#A 手板服务 `../service_handboard/arm_service.py`(v5.7,:8013)。接口契约见 `../README.md` §3。

## 烧录(Arduino IDE / arduino-cli)

| 项 | 值 | 说明 |
|---|---|---|
| 开发板 | ESP32S3 Dev Module | 嘉立创 ESP32-S3 |
| Partition | **huge_app** | **必选**:位图+提示音占 3.6MB,默认分区放不下 |
| PSRAM | 按板子实配(我们用 OPI 8MB) | |
| Upload Speed | 921600 | |
| USB CDC On Boot | Enabled | 串口日志 |
| 数据口 | 现场 COM7(烧录兼日志) | 另一 USB 口 COM8 做纯日志抓取(`../tools/bg_capture.py`) |

**烧前必改**:库源顶部 `WIFI_SSID` / `WIFI_PASS`——密码已脱敏为 `YOUR_WIFI_PASSWORD`,改回现场值再编译。

## 现场注意

- S3 与床头服务(`10.153.47.252:8013`)必须同一路由/热点。
- **热点客户端 >8 台会踢不活跃设备,S3 最安静、最先被踢**——演示日热点 ≤5 台或上独立路由器。
- 日志抓取:`py tools/bg_capture.py`(插上数据线后先确认 COM 号,插拔会变)。