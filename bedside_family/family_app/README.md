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
LAN_BASE   = "http://10.170.215.251:8013"  // v1.5:仅作初始猜测,失联时自动全段扫描找回(见 MainActivity.subnetScan)
CLOUD_BASE = ""                             // 中转站落地后填这里,填了自动优先走云端 → 4G 全域可用
```

升级为公网版的完整步骤:填 `CLOUD_BASE` → 重跑 `build_app.py` → 同签名覆盖安装,无需卸载。

## 构建

```powershell
python family_app/build_app.py
# 产出 family_app_v1.7.apk,自动同步桌面(家人留言v8.apk)
```

> 版本沿革:v1.1(versionCode 2)首开即闪退(MainActivity 警报条对未挂载 View 取 LayoutParams 的 NPE,**javac/CI 抓不到运行期 NPE**)→ v1.2(3)删冗余行修复,真机验证前台警报闭环 PASS;但熄屏/退后台无提醒(Android 13+ 通知权限无权限时 `nm.notify()` 静默丢弃 + 无电池优化白名单 + 无锁屏弹出能力)→ v1.3(4)=后台提醒修复:权限提醒条+电池白名单引导+锁屏全屏意图+API29+ 显式 dataSync 前台服务类型;真机四轮熄屏仍无响应(用户已开通知/省电/自启动/WLAN 休眠全套开关)→ **v1.4(5)=闹钟级拉活链:`setAlarmClock` 每 10s 挂一拍系统精确闹钟(内核计时,无需特殊权限),进程被 ROM 杀掉也会被 PaceReceiver 重新拉起——闹钟/电话类 App 的标准存活方式;另加"守护自检行"(lastPoll 轮询年龄+通知/后台权限一览),熄屏测试后开 App 一瞄即分清"服务死了"还是"通知被丢"**。同签名升级覆盖安装。桌面交付别名:v2=broken v1.1,v3=v1.2,v4=v1.3,v5=v1.4,**v6=v1.5**。

**v1.5(6)=网络自愈**:手机热点重启会随机换网段(实测 10.153.47.x → 10.170.215.x,全队设备一夜失联一次),App 里写死的板子地址随之作废 → 静态地址连不上时自动枚举本机所有 IPv4 网段、24 线程并行扫各 /24 的 8013 端口 + `/status` 验身(单网段最坏 ~4s,20s 冷却防每轮全扫),命中即回连并存 SharedPreferences;前台 3s 轮询与后台 GuardService(主力)都接了该路径——**只要 App 和板子在同一个网里,板子换任何 IP 都自己找回来,不再需要重新打包**。LAN_BASE 从"写死地址"降级为"初始猜测"。**v1.6(7)=「听 TA 说」列表撤掉已确认警报历史小卡**(用户反馈:置顶红卡差点让人以为留言收不到);真警报表达只剩全屏红条+警铃(前台)与系统高优通知(后台)两处。**v1.7(8)=修播放竞态炸错**(连点两条/快速切换时旧 prepare 线程与释放互相踩状态→IllegalStateException):全部播放器操作改走单一 player 线程串行执行,点播带代次号,旧请求静默作废,试听同通道。

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