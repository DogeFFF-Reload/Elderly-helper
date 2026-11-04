# 位图与提示音生成器(tools)

每个生成器对应一个 `*_bitmaps.h` / `voice_prompts.h`,**生成物都已入库**——克隆即得,不必现场装依赖跑生成。本目录是源头:**改按钮/图标/台词先改生成器再重新生成,不要手改 .h**。

## 清单

| 脚本 | 生成物 | 用在哪 |
|---|---|---|
| `gen_szp_btns.py` | `../firmware_s3/btn_bitmaps.h`(1.4MB) | 嘉立创 S3 六键 kiosk |
| `gen_t5ai_btns.py` | `../firmware_t5ai_handover/bedside_t5ai_arm/t5ai_bitmaps.h`(1.6MB) | 涂鸦 T5AI 四键 |
| `gen_voice_prompts.py` + `tts1.ps1` | `../firmware_s3/voice_prompts.h`(2.2MB) | S3 三条内置提示音(SAPI 16k/mono) |
| `gen_tts.py` + `gen_pack.ps1` | `../voice_pack/wav*` | 73 条板端播报包 |
| `gen_app_icon.py` | `../family_app/android/res/drawable/ic_launcher.png` | APK 图标 |
| `trim_wav.py` | (处理) | TTS 尾部静音裁剪(≥0.25s 收口) |

## 依赖与用法

- `gen_szp_btns.py`:Pillow,`FONT=C:\Windows\Fonts\simhei.ttf`(黑体;没有可换 `msyh.ttc`),`SWAP_BYTES` 现场校色开关(屏上红蓝颠倒时改 1);
- `gen_t5ai_btns.py`:同 PIL 流程,T5AI 480x480 圆形 UI 语义;
- `gen_voice_prompts.py`:Windows SAPI(`tts1.ps1` 直调 COM),Rate=0,直出 16k/16bit/mono 不需 ffmpeg,合成后过 `trim_wav.py` 去 0.25s 尾静音;
- 生成器头部 `OUT` 路径指向固件目录,跑完即得新 .h。

## 纪律

1. 按钮/图标变更:**先改生成器 → 跑生成 → 连 `README` 截图提交**,防手上误画偏移;
2. `*_bitmaps.h`/`voice_prompts.h` 带有 "Auto generated" 头与生成参数注释(SWAP_BYTES=N),评审时先核对这行;
3. PIL 依赖只在**重新生成**时需要;烧固件不需要。