# Mocen CareMate · 床头屏侧 + 家属端模块(嘉立创屏 + 手板 + 家属 App + 跌倒警报推送)

本目录是团队仓库之外的另一半系统——**床头侧(老人端)** 与 **家属手机端** 的完整实现。
与本仓库其他模块的关系,一句话:**视觉模块(§11)负责"发现跌倒",本目录负责"警报立刻响到家属手机上"**,同时床头屏、留言互通、家属语音回话也都从这里完成。

> 贡献说明:本目录所有代码独立可跑,不依赖、也不修改仓库中其他同学的任何文件(`code/hand_controller/arm_service.py` 是另一种用途的执行端服务,**两者并存、路径不同、端口都是 8013 但部署在不同的 X5 上**,见下文"部署拓扑")。若团队日后想统一为一套执行端,服务里的留言/警报三组接口(§3)可平移进 `code/hand_controller/arm_service.py`,接口契约见 对接卡。

---

## 1. 部署拓扑(与主仓库 §10.9 两块 X5 的映射)

| 设备 | IP | 跑什么 | 来源 |
|---|---|---|---|
| X5#A(手板) | 10.153.47.252 | `bedside_family/service_handboard/arm_service.py`(v5.7,systemd `arm_service`,:8013) | **本目录** |
| 嘉立创 S3 床头屏 | DHCP | `bedside_family/firmware_s3/bedside_s3_arm_kiosk.ino`(v5.4 烧录件) | **本目录** |
| 家属手机 | DHCP | `bedside_family/family_app/`(APK v1.1,桌面安装) | **本目录** |
| X5(主脑/视觉,本仓库) | 10.153.47.251 | 本仓库 `master_service` / `vision` | 本仓库 |

手机必须与床头同一路由/热点(现场限制;上公网中转后解开,见 `docs/留言接入队友App_对接卡.md` 的 A/B 方案)。

## 2. 跑起来的完整闭环(全部真机验证过)

1. 老人在床头屏按「留言」→ 说话(≤60s,16k/16bit/mono WAV)→ 上传 X5#A 存为 `msg_*`
2. 家属手机 App「听 TA 说」→ 拉列表 → 播放**原声**(不转文字)
3. 家属 App「我来说」→ 按住说话 → 松手上传 → 存为 `fam_*`
4. 老人床头屏按「听留言」→ **只播家属的 `fam_*` 回话**原声
5. (跌倒)检测端 POST `/fall_alarm` → 家属 App **3 秒内**全屏红色警报+警铃+震动(app 关着由常驻守护服务 10 秒内弹系统级高优先级通知)
6. 床头屏六键:招手/比ok/拉勾/留言/听留言/语音命令(语音走 X5#A 上的 Vosk 本地识别,不限词表)

## 3. 接口契约(`X5#A:8013`)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/message_chunk?id=..?seq=..` | 老人留言 PCM 分块 |
| POST | `/message_end?id=..&d=秒` | 结束落盘 + 自动 PUSH 家属端 |
| GET | `/messages` / `/messages/<id>.wav` | 留言列表(新在前) / 原声 WAV |
| POST | `/family_message` | 家属回话 WAV(裸 body,octet-stream) |
| **POST** | **`/fall_alarm`** | **跌倒上报;body 可选 `{"kind":"fall","src":"cam-A","conf":0.9,"event_id":"唯一事件号"}`,空 body 也收;同 event_id 重试自动去重只响一次(v5.7)** |
| GET | `/alarms` | 警报列表(新在前,50 条,含 acked) |
| POST | `/alarms/ack` | `{"id":"al_..."}` 确认 |
| POST | `/fall_alarm` `{"dry":true}` | 无扰自检:只回执不落账,手机不响(v5.7) |
| GET | `/fall_alarm` | 检测端健康检查:`{"ok":true,"ver":"5.7",...}`,无副作用 |
| POST | `/pose/<name>` | 手部姿态(wave/ok/thumbs_up/...) |
| POST | `/voice_cmd` | S3 语音 PCM 4s → 本地 Vosk → 动作 |

## 4. 视觉模块 → 家属警报(一行接入 + 桥接器两种姿势)

**姿势 A——检测端直发(一行)**:在你们的跌倒判定处加:

```python
urllib.request.urlopen(urllib.request.Request(
    "http://10.153.47.252:8013/fall_alarm",
    data=b'{"kind":"fall","src":"cam-A"}',
    method="POST"), timeout=3)
```

**姿势 B——走主脑不动视觉代码(零侵入)**:

```
python bridges/fall_to_family.py --master http://<主脑IP>:8000 --family http://10.153.47.252:8013
```

它订阅主脑事件流(`GET /api/events?since=`),看到 fall/fall_hint 类事件就转发 `/fall_alarm`。视觉、主脑、操作台都不用改一行,警报通道独立于大屏流程。

**姿势 C——操作台"触发摔倒"演示**:直接 `curl -X POST http://10.153.47.252:8013/fall_alarm -d '{}'`,家属手机立刻响;不响手机的验证用 `curl -X POST .../fall_alarm -d '{"dry":true}'`(v5.7,只回执不落账)。

## 5. 目录结构

```
bedside_family/
├── firmware_s3/bedside_s3_arm_kiosk.ino   # 嘉立创 S3 床头屏固件 v5.4(六键+留言+语音,COM7 烧录/COM8 日志)
│   ├── btn_bitmaps.h                      #   六键图标位图(1.4MB 生成物,入库即源码,缺了编译不过)
│   └── voice_prompts.h                    #   提示音 WAV 数组(2.2MB SAPI 16k/mono)
├── firmware_t5ai_handover/bedside_t5ai_arm/   # 涂鸦 T5AI 床头臂屏固件(交接队友的原始版;队友已在仓库 firmware/bedside_t5ai 继续演进)
├── firmware_bedside_box/bedside_box.ino   #床头盒(无屏留言中继/播音)固件
├── firmware_diag/                         # 嘉立创 S3 面板诊断板配置(ESP_Panel 板级 conf 7 件,排屏/触摸故障用)
├── service_handboard/                     # X5#A 手板执行端服务包(可运行)
│   ├── arm_service.py                     #   v5.7:留言+家属推送/回话+跌倒警报(event_id 去重/dry 无扰测)+姿态+Vosk 语音
│   ├── poses.py rh56f2_driver.py          #   姿态词表/序列 + RH56F2 EB90 手驱动
│   └── rh56f2_eb90.py rh56_register_map.py#   EB90 协议帧 + 手册 V1.09 寄存器表
├── family_app/                            # 家属 App(原生安卓,无第三方依赖,纯手工工具链构建)
│   ├── android/                           #   源码(Manifest / MainActivity / GuardService / Cfg)
│   ├── build_app.py                       #   构建脚本(aapt2→javac→d8→zipalign→apksigner,无需 gradle)
│   ├── family_app_v1.1.apk                #   签名成品(桌面版,~30KB)
│   ├── README.md                          #   行为规格 / 4G 中转升级法 / 构建坑位
│   └── legacy_webview_v1/                 #   初版 WebView+MIC 原型(考古)
├── bridges/fall_to_family.py              # 视觉/主脑 → 家属警报 桥接器(姿势 B,带 event_id=bus-<事件id>)
├── voice_pack/                            # TTS 台词播报包:73 条×原速/慢速 + lines.json(+tools/gen_tts.py)
├── deploy_x5/                             # X5 板端 systemd 单元(caremate*.service)+ 开板/翻转脚本
├── tools/                                 # 现场/联调工具
│   ├── smoke_alarm_api.py                 #   警报契约冒烟测试(无扰优先,自建自收,退出码可判 CI)
│   ├── x5_ops/                            #   X5 运维交接包 9 件:部署/模型上传/agent推送/救砖/验证(密码 env 注入)
│   ├── gen_*btns.py gen_voice_prompts.py  #   按键位图与提示音批量生成器
│   └── bg_capture.py probe_x5.py 等       #   串口抓日志/板存活监测/TTS 提示音
└── docs/                                  # 对接卡 / 需求单 / 交接说明 / 队友 agent 任务包
```

## 6. 家属 App 构建与安装

```powershell
# 依赖:JDK 17+(实测 OpenJDK 25)+ Android SDK(android-34 / build-tools 35;脚本里改路径即可)
python family_app/build_app.py        # 产出 family_app.apk,自动同步到桌面
```

手机:APK 发微信传手机 → 点开安装(允许未知来源)→ 连床头同 WiFi → 打开即自动发现床头。
守护服务在 app 首次打开时启动(START_STICKY);Android 13+ 首次需允许"通知"权限。

## 7. 安全与现场注意事项(踩过的坑,务必读)

- **X5#A 供电是第一坑**:5V USB 电源不稳 → 板子整个失联(今晚 5 次)。备一根 USB 线/充电头,现场接稳的电源;24V 是机械手路,先 USB 后 24V(间 20 秒)
- 手机热点超过 ~8 台客户端就开始踢不活跃设备(S3 床头屏最安静、最先被踢)→ **演示时热点设备 ≤5 台**,或独立路由器
- 固件/文档中的 WiFi 密码已统一替换为 `YOUR_WIFI_PASSWORD`,烧录前在 `.ino` 顶部两行改回现场值
- `docs/交接说明.md` 有 X5 环境重建的完整步骤(vosk 模型、systemd、开机自启)

## 8. 已验证状态(2026-10-24 晚)

| 项 | 状态 |
|---|---|
| 双向留言闭环(msg_→app 听 / app 说→fam_→床头播) | ✅ 真人原声全通 |
| 跌倒警报端点(空 body/JSON/自检/确认) | ✅ curl 全通过 |
| v5.7 event_id 去重 + dry 无扰测 | ✅ 契约冒烟测试 ALL PASS(tools/smoke_alarm_api.py) |
| 家属 App v1.1(留言+警报+守护服务+历史) | ✅ 构建/签名通过,装机待现场复测 |
| S3 固件 v5.4 u32 播放修复 + 听留言只播 fam_ | ✅ 烧录验证 |
| 公网中转(4G 免热点) | ⏳ 等中转站,契约已写在 `docs/留言接入队友App_对接卡.md` |