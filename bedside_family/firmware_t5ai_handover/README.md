# 涂鸦 T5AI 床头臂屏固件(交接版)bedside_t5ai_arm

> **注意**:这是 10-24 交接给硬件队友的**原始版本**;队友在此仓库 `firmware/bedside_t5ai/` 已继续演进(语音键 grammar、闲聊过滤、还药键等)。要看最新版去那边;本目录保留交接时的源头快照(配对 `t5ai_bitmaps.h` 1.6MB)。

## 是什么

涂鸦 T5AI 大屏(连 X5#B 臂板 .251)的固件:四键(递药盒|还药 / 按SOS|归位)+ 底部"点一下说话"语音键。每 2s 轮询 X5 `/status` 变状态条;四个按钮 POST `/pose`/序列;语音键录 4s 16k/16bit/mono PCM 裸流 POST `/voice_cmd`。

## 交接说明(原文要点,详见 `../docs/交接说明.md`)

- **拓扑定版**:两屏互不相干——T5AI=臂主导控 .251;嘉立创 S3=手板控 .252。T5AI 烤的 IP 已定 .251,零重烧。
- **烧录**:`arduino-cli compile/upload --fqbn tuya_open:tuya_open:TUYA_T5AI_BOARD -p COM9`(编译 exit 1 + BUILD SUCCESS 也算过;烧录约 35s)。
- **位图生成**:`../../tools/gen_t5ai_btns.py` → 生成 `t5ai_bitmaps.h`(**勿手改**,要改按钮先改生成器)。

## 踩坑(浓缩自血泪)

- **T5AI(TuyaOpen/LVGL 线程模型)**:`lv_vendor_disp_lock` 非递归,LVGL 回调里绝不能再拿(死锁)=回调只置标志/切图,HTTP 全在 loop() 单线程串行;
- **PCM 裸流 POST 必须用底层 `http_client_request_t`**(.body_length 自填),封装版 strlen 会截断二进制;
- **Audio 库大缓冲必须 `tal_psram_malloc`**;板载 mic 16k/16bit/mono 与 vosk 同规格零转码;
- **X5 半死时 T5AI 的 POST 会阻塞 loop 约 30s**(屏不卡,因为线程独立)。

## 与 X5#A(.252)的关系

T5AI 本屏只连 .251 臂板;**.252 手板禁区勿动**(板号纪律见 `../docs/交接说明.md` §九-7)。