# 给队友 Agent：视觉 → 家属原生 App 报警接入

## 已交付的通道

复用仓库 `gesture.fall_rules.FallDetector`。仅 `update()` 返回事件时调用发送器，候选阶段不发送；保持倒地不重复发送，恢复站立后再次倒地生成新事件号。沿用现有 `InputQuality` 的时间、可见度和画面缺口保护，没有借这次接入改动默认判定阈值。

| 文件 | 用途 |
|---|---|
| `code/vision/family_alarm.py` | 标准库接口客户端、稳定事件号、异步有界发送队列 |
| `code/vision/family_watch.py` | 电脑摄像头 / 手机 MJPEG / 离线验证入口 |
| `run_family_alarm.py`、`START_FAMILY_ALARM.bat` | 不依赖当前工作目录的启动器 |
| `bedside_family/bridges/fall_to_family.py` | 修复现有主脑桥接，严格事件匹配、持久化待发送事件 |
| `bedside_family/service_handboard/arm_service.py` | v5.7.1：原子查重+插入，报警编号增加随机后缀 |
| `code/master_service.py` | 大请求/公网拒绝先回执，再限时丢弃未读正文，避免Windows断线 |
| `code/tests/test_family_alarm*.py`、`test_family_watch.py` | 接口、桥接和入口回归 |

## 发送契约和结果含义

```http
POST http://10.153.47.252:8013/fall_alarm
Content-Type: application/json; charset=utf-8

{"kind":"fall","src":"cam-A","event_id":"vision-<uuid>"}
```

规则没有经过概率校准，故不发送 `conf`。不发送图像、坐标、手机号或摄像头凭据。`event_id` 每次倒地生成一次，重试完全复用；响应必须 `ok:true`，且 `event_id/src/kind` 与请求相符，真实模式还要求非空 `id` 和布尔 `dedup`，才标记接收成功。

`bedside_accepted` 表示床头回执确认；`phone_received` 保留为 `null`，不冒充手机回执。`failed` 的存储情况未知：即使床头已收到但回执丢失，也可能耗尽重试。不要换新事件号重发同一事故。

默认 `off` 不访问床头。`dry` 先 GET核对5.7或更新版本，再带 `dry:true` 验证，不建真实警报。`live` 先做同样无扰检查，通过后才打开实时输入并允许上报。离线骨架/合成场景拒绝 `live`；只有显式 `--send-test --mode live` 可以在不打开摄像头时建立一条接口测试警报。

每次 HTTP 默认超时3秒；每轮最多3次，间隔1、2秒。直接视觉入口在后台线程发送，识别循环不等待网络；排队最多32条，队列满或退出20秒后仍未结束均记录未完成，不显示送达成功。直接入口不自动恢复跨进程待发队列：`events.jsonl` 在第一次POST前写入事件号，异常退出后由操作者核对并用原号补发。

## 现场部署请你完成

1. 拉取团队仓库 `main`。本轮没有访问板子、摄像头、手机报警服务，没有执行部署或重启。
2. 床头板沿用现有部署方式，把 **`bedside_family/service_handboard/arm_service.py`** 更新到实际 systemd 使用的脚本（任务包给出 `/root/caremate/hand_controller/arm_service.py`）。由硬件负责人检查服务路径、保留现场配置、备份旧文件后重启既有 `arm_service`。
3. GET `/fall_alarm` 应返回 `ver:"5.7.1"`。发送端接受5.7及更新版，但并发去重修复必须部署5.7.1源码才存在。报警 `id` 应当视为不透明字符串；现在末尾增加12位随机片段，Android源码只按字符串比较，无需因这次改动重打APK。
4. 核对床头实际IP、路由器和手机App配置。`code/hand_controller/arm_service.py` 是主脑动作服务，**不要把它覆盖为这份床头服务**；两份仓库代码对应不同用途/板子，均不用 kit 自带服务。
5. 按 [用户操作页](31_用户_视觉报警怎么启动.md) 依次完成无扰检查、一条真实接口报警、实际摄像头普通活动观察。确认前台和后台手机表现，记录床头版本、App版本、网络、event_id、alarm_id、实际延迟和是否确认。

接收端5.7.1将同一 `event_id` 的查重和新增放进同一可重入锁，防止超时后的并发请求各落一条。不同事件在同一毫秒也会获得不同报警编号。历史仍保留原来的内存100条、落盘50条策略；不能声称无限期去重。服务回执也未增加磁盘持久化确认或手机送达确认。

## 主脑桥接备用通道

优先采用独立视觉入口；它不向主脑发事件，所以不会再被这个桥重复转发。如果现场原本已经跑 `vision.fall_bridge --camera ...` 向主脑上报，才使用这个备用通道：

```powershell
python bedside_family/bridges/fall_to_family.py --master "http://主脑IP:8000" --family "http://10.153.47.252:8013" --mode live
```

先启动桥接器，再开始检测。默认模式现在是 `off`，旧命令不带模式不会再建立真实报警。只有事件 `type=="fall"` 且 `level=="danger"` 才转发，符合当前主脑首次进入询问时的记录；询问、家属通知、取消、照片 `fall/warn`、处理中重复检测均忽略。

事件号由主脑地址、事件 `id` 和 `ts` 稳定生成，避免主脑重启后从1计数而撞号。首次启动跳过已存在历史；未发送事件在第一次POST前写入状态文件，失败保留，下轮或重启复用原号重试，主脑暂时不可达时也会继续尝试已保存的报警。状态按主脑、床头、模式和来源隔离；不要运行两个实例共用同一状态文件。

桥接器只能拿到主脑有限事件快照，长时间停机或事件突发可能错过尚未捕获的事件，不能当作无限保留的可靠消息队列。它也不能判断主脑上的一次 `mock/fall` 是否来自真人，**live只连现场实际主脑，离线/模拟验证使用off或dry**。旧 `--selftest` 已改为健康检查+dry；需要真实接口验收请用明确的 `--send-test --mode live`。

同一个场景只保留一种检测/上报路径。两个独立检测器会产生不同事件号，接收端无法据此判断它们是不是同一个人、同一次跌倒。

## 失败事件补发

恢复连接后，先看 `summary.json` / `events.jsonl` 的原事件号，再核对床头记录。需要重试时沿用该号及原来源：

```powershell
.\START_FAMILY_ALARM.bat --send-test --mode live --src cam-A --event-id "原来的vision-事件号"
```

不要对dry或普通模拟事件执行真实补发。桥接器的pending会自动重试，通常不需要人工再发。

## 可复现的无硬件验证

```powershell
.\.venv\Scripts\python.exe -X utf8 code/tests/run_all.py
.\.venv\Scripts\python.exe -X utf8 code/tests/run_all.py family_alarm family_watch
.\.venv\Scripts\python.exe -X utf8 run_family_alarm.py --synthetic fall --mode off
```

完整回归 **41套570条全部通过，无失败、无跳过**；其中接入专项34条，另增加2条主脑HTTP拒绝请求回归。完整模拟流程10/10通过；一次已确认正常活动的三个片段各0次事件，9个合成场景均符合标签。数量、原始录制校验值和源文件校验值统一记录在 [本次验证摘要](verification/20261024_family_alarm.json)。

全量检查曾复现Windows提前关闭未读大请求，使413回执变成断线的问题；初次失败日志保留，修复后重新全量通过。拒绝后仅在字节数和时间上限内丢弃正文，不解析或执行它，且无需等待正文到齐才回应。测试仅绑定 `127.0.0.1`；通过AST白名单提取实际床头报警函数和HTTP处理器，未导入硬件服务顶层；网络连接受loopback守卫约束。

没有真实跌倒标签，不能报告真人跌倒检出率；单次正常活动拆成的三个片段不能当作三轮真人测试。队友任务包里的3/10秒手机轮询描述仍需现场实测。源码合并、离线通过、板端已部署和手机实际收到四件事分别记录。
