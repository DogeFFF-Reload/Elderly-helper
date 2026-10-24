# CHANGELOG — bedside_family(床头侧 + 家属端)

格式:日期 | 变更;**每一条都是真机/板上实测后才写**(不写"计划中")。

## 2026-10-24(演示前一天,全天落地)

### 服务(service_handboard/arm_service.py)
- **v5.7 警报契约定版**:`POST /fall_alarm` 支持 `event_id` 网络重试去重(同号只响一次)、`{"dry":true}` 无扰测试、`GET /fall_alarm` 带版本健康检查;警报 id 毫秒化。响应队友视觉/桥接侧四问后新增。
- **夜间热修·警报 id 同毫秒撞号**:同秒 50 连发实测只出 36 个不重复 id(同毫秒并发取号相撞)。加取号锁自旋"睡到下一毫秒";板上复验同秒并发 `403/499` 各拿各的,smoke ALL PASS。
- **合并终版·取号原子化(合作版 5.7.1)**:Interphynity 以 `_alarm_lock`(RLock)+单调毫秒序列实现"时钟暂停也不忙等"的取号,id 格式不变;两版热修合流,**单测 15 例在任一实现上均全绿(测试的是契约而非实现)**;板端已同步 5.7.1 复验 ALL PASS。
- **可测性重构**:开放语音两处重复的"长词优先"循环收敛为纯函数 `_voice_open_match`;词表补口语变体(拉个勾/拉个钩/勾一勾——vosk 常听成"拉个勾")。
- 语音 agent 链:LLM 理解开放(糊转写还原)、执行封闭(白名单收口),词表只作离线兜底——白名单中文名完备性有硬断言(见 agent_tests)。

### 测试与 CI
- `tests/test_bedside_core.py` 离线单测 15 例:警报 id 唯一性/同事件去重/ack 后仍去重/掉电盘恢复、语音长词优先语义、留言 id 路径穿越拦截;**零硬件零三方依赖**(缺 serial/vosk 自动打桩)。首个提交版还揪出"单测失败但进程退出 0"的地雷(CI 会把红当绿),已修。
- `tools/smoke_alarm_api.py` 板上契约冒烟(无扰优先,自建自收):`--base http://10.153.47.252:8013` → ALL PASS。
- `tools/agent_tests/` PC 冒烟:agent 意图三元组+白名单完备性硬断言、糊转写网关单点;key/网关一律环境变量。
- GitHub Actions:`bedside-ci`(服务 py_compile + 15 例单测)、`family-app-ci`(Ubuntu 复刻手工 APK 流水线,产临时 debug 签名冒烟 APK;稳定密钥永不进 CI)。

### 固件与烧录
- S3 kiosk v5.4 已烧运行(六键留言/语音/姿态 + ES8311 播放链路当晚修通)。
- ~~新增 `firmware_s3/prebuilt/`:预编译四件套直烧~~ → **深夜撤架**:bin 是"现场 WiFi 凭据烧死"的产物,不该进公开仓库;现场包由队内保管,自建走 `tools/flash_s3.ps1` 源码模式(占位密码,烧前改两行)。
- `tools/flash_s3.ps1`(预编译直烧/源码编译双模式+自动找串口)、`tools/flash_t5ai.ps1`(兼容 T5AI 工具链"BUILD SUCCESS 却 exit 1"奇癖)。

### 家属 App(family_app)
- **v1.2**(原生 Java 单 Activity + GuardService;versionCode 3):警报前台 3s 轮询红条+警铃+震动,后台 10s 系统通知,「知道了」→ `POST /alarms/ack` 闭环;留言原声收发。
- **v1.1 追回**:该版本从未真机跑通——`MainActivity` 警报条初始化对未挂载 View 取 `getLayoutParams()` 即 NPE,一开即闪退(编译期合法、CI 抓不到,真机首开现形);v1.2 删冗余行修复,同签名升级覆盖。**教训:APK 交付前必须在真机点开一次。**
- 桌面交付别名:家人留言v2.apk=broken v1.1,v3.apk=v1.2(修复版)。
- 手工工具链 aapt2→javac→d8→zipalign→apksigner;build-tools 35.0.0(34 的 d8 写 dex NPE 勿用);签名密钥本地保管不入库,换签名=必先卸载重装。
- 4G 全域预案:中转站地址到手填 `Cfg.CLOUD_BASE` 重打包即升级(无需改板端)。

### 文档
- 交接说明 / 对接卡(§5 警报契约)/ 队友 agent 任务包(自包含契约+实测回执+可抄代码)/ 四份固件 README / tools·x5_ops·voice_pack·WebView 考古 README。
- 考古卷:`docs/agent_交接总览_考古10-23.md`(10-23 早期版原文照录+现状勘误表:RS485假设→EB90定案、52→73条、PWA→原生APK、双 X5 板号纪律)。
- x5 应急手术 7 件入库(`tools/x5_ops/surgery/`):双进程清场修复/unit 补配/板上残留清理/复位抓日志/网段扫描。

### 演示结论(当天实录)
- 两屏互不相干拓扑定版:T5AI 臂屏控 .251(队友)、S3 手板屏控 .252(本目录),零重烧零配置改动。
- 警报全链实测:S3/桥接发 → 板 8013 → app 前台响铃红条 →「知道了」ack 全闭环已由真人手机走通。
- 待办:4G 中转站地址(到手即升级 CLOUD_BASE)、语音 E2E 复测(比 ok/拉勾)。