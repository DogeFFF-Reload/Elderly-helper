# 家属 App(原生安卓,家人留言 v2)

给家属手机装的**原生 APK**:不需要浏览器、不需要访问网页,微信传 APK → 点开装 → 连床头同 WiFi → 打开就用。零第三方依赖,纯 Java 单 Activity + 守护服务,~30KB。

## 功能与行为参数

| 功能 | 行为 | 参数 |
|---|---|---|
| 听 TA 说 | 拉 `GET /messages` → 列表(老人 msg_* + 家属 fam_*) → 点播原声 WAV | 原声不转文字 |
| 我来说 | AudioRecord 16k/16bit/mono 直采 + 手工 WAV 头 → `POST /family_message`(裸 body octet-stream) | 单条 ≤60s |
| 跌倒警报(前台) | 3 秒轮询 `GET /alarms`,见新未确认条 → **全屏红条+警铃循环+震动** | 「知道了」→ `POST /alarms/ack` |
| 跌倒警报(后台) | GuardService 常驻前台服务(dataSync 类型)10 秒轮询 → **系统级高优通知**(sound=alarm+震动+bypassDnd) | START_STICKY |
| 警报历史 | 已确认警报以红卡置顶展示 | — |

轮询双通道设计目标:检测端 POST 落账 → **app 前台 3 秒内响,关着 10 秒内弹系统通知**。

## 网络(4G 场景预留)

`java/com/mocen/caremate/family/Cfg.java`:

```java
LAN_BASE   = "http://10.153.47.252:8013"   // 当前:与床头同 WiFi 直连(usesCleartextTraffic)
CLOUD_BASE = ""                             // 中转站落地后填这里,填了自动优先走云端 → 4G 全域可用
```

升级为公网版的完整步骤:填 `CLOUD_BASE` → 重跑 `build_app.py` → 同签名覆盖安装,无需卸载。

## 构建

```powershell
python family_app/build_app.py
# 产出 family_app_v1.1.apk,自动同步桌面
```

> 交付别名勘误:桌面"家人留言v2.apk"与 family_app_v1.1.apk 为**同一构建**(字节哈希一致),v2 只是给家属的易读叫法。
> CI 版:仓库的 `family-app-ci` 用同样流水线产出**临时 debug 签名**的冒烟 APK(Artifact 可下载);稳定签名密钥永不进 CI/仓库。

| 依赖 | 值 | 坑 |
|---|---|---|
| JDK | 17+(实测 OpenJDK 25) | |
| Android SDK | platforms;android-34 | |
| build-tools | **35.0.0** | **34.0.0 的 d8(8.2.2-dev)写 dex 时 NPE 崩,勿用** |
| 签名 | caremate.ks(本地保管,**不入库**) | 换签名=手机必先卸载重装 |

手工工具链:aapt2 打包资源 → javac → d8 dex → zip 打包 → zipalign → apksigner v2 签名,无 gradle。

## 已装机验证(2026-10-24)

- 安装 / 打开 / 留言双向 / 警报红条+警铃+震动+「知道了」全链路人肉通过(板上警报日志侧证)。
- Android 13+ 首次打开允许"通知"权限,否则后台通道只剩静默角标。