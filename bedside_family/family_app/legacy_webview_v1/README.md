# 家属端初版原型(WebView+MIC,已归档)

> **考古说明**:这是家属端的第一形态——WebView 壳 + 安卓 MIC 桥,后被"原生重写"决策替代(用户要求"下载安装、点开就用、像微信一样"的重体验,WebView 版体验不够)。保留在此作为迭代记录,**请勿基于本目录开发**;现行版本在 `bedside_family/family_app/`。

## 与现行版的差异

| 项 | 本原型(WebView 版) | 现行(原生版) |
|---|---|---|
| UI | 网页套壳(TextView 挂 Web 页) | 纯 Java 原生 Activity |
| 录音 | MicChromeClient 桥接网页 getUserMedia | AudioRecord 直采 16k/16bit/mono,手工 WAV 头 |
| 警报 | 无(当时还没有警报端点) | 3s 轮询红条+警铃+震动+守护服务 10s 系统通知 |
| 体积 | ~16KB | ~30KB |

## 文件

| 文件 | 作用 |
|---|---|
| `MainActivity.java` | WebView 壳(onPageFinish 后注入 MIC 桥) |
| `ConnectListener.java` | WebJS↔原生消息桥 |
| `MicChromeClient.java` | getUserMedia 权限让路 |
| `AndroidManifest.xml` | 旧版权限(无警报时代:INTERNET/RECORD_AUDIO) |
| `res/drawable/icon.png` | 当时的图标 |
| `build_apk.ps1` | 当时的手工构建脚本(aapt2/jarsinger 流程) |
| `CareMate_family.apk` | 当时的成品(签名方式已淘汰) |

## 从这版学到的(写进现行版的决定)

1. WebView 的 getUserMedia 在安卓上路径长、机型适配碎——改成 AudioRecord 直采一步到位;
2. 网页套壳在"警报弹全屏+守护轮询"这种系统级体验上天生短腿——警报端点上马后直接原生重写;
3. 手工 WAV 头(44 字节 PCM header)的构造这段代码后来被原生 MainActivity 继承复用。