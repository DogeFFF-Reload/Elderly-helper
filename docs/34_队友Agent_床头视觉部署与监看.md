# 给队友Agent：把视觉报警放到床头X5，并提供大屏监看

## 目标与进程关系

新增独立视觉进程，复用已有姿态规则及家属报警客户端，不把摄像头循环塞进硬件HTTP服务。已有床头服务继续处理App请求；两个进程通过本机HTTP接口衔接。

```text
iPhone 摄像头App（已有可用MJPEG地址）
                  ↓ 一条上游视频连接
床头X5：run_bedside_vision.py
         ├─ 姿态模型 → FallDetector → 异步事件发送
         │                             ↓ 本机 /fall_alarm
         │   既有床头服务:8013 → 家属App轮询 /alarms → 手机通知
         └─ 只读监看服务:8015 → 浏览器 → 外接显示器
```

这里的“床头设备”指能运行Linux/Python的X5计算板。S3/T5AI等床头屏幕和微控制器不运行MediaPipe。本次无需修改它们的固件，也没有增加屏幕侧的发送按钮。

家属App沿用队友已有的Android客户端和轮询方式，没有把轮询包装成推送。报警接收成功也不证明手机送达。项目旧文档中的X5-A/B角色分配有历史差异，以现场实际运行 **床头家属服务** 的板子为准。

## 同步到的队友验收状态

本次同步的远端提交 `41cd0365e8729ddd32920a35d2279dfc84c7aa5c` 包含 [床头侧验收回执](verification/20261024_5.7.1_床头侧验收回执.md)：队友报告`.252`床头5.7.1已部署、dry对接通过、v1.2前台报警真机通过；v1.2/v1.3熄屏/后台四轮失败，v1.4(versionCode 5)新增拉活链后待装机复测。不要把“已编译v1.4”写成“后台通知已通过”。本次没有运行队友的部署、扫描、清场或真实报警脚本。

## 文件入口

| 文件 | 用途 |
|---|---|
| `run_bedside_vision.py` | 可从任意目录启动，Windows/Linux通用 |
| `code/vision/bedside_watch.py` | JSON配置检查、模型空白图自检、视觉入口组装、SIGTERM收尾 |
| `code/vision/family_watch.py` | 复用识别和报警流程，新增持续运行及网页监看选项 |
| `code/vision/web_monitor.py`、`monitor.html` | 无桌面依赖的只读监看，最新帧JPEG、中文状态及全屏按钮 |
| `deploy/bedside_vision.json.example` | 不含现场地址/凭据的配置示例，默认off |
| `deploy/install_bedside_vision.sh` | 只安装新的systemd单元，不启动、不启用，不改既有硬件服务 |
| `START_BEDSIDE_VISION.bat` | Windows备用入口，需要项目.venv |

## 1. 先核对板端环境，不启动摄像头

拉取团队仓库最新代码。使用独立仓库目录，保留现场已运行的床头服务和配置。**不要执行旧setup脚本来覆盖现有环境或驱动。** 新视觉程序可以与服务并列运行。

两份同名服务用途不同：

- `bedside_family/service_handboard/arm_service.py`：家属App使用的床头报警服务，源码5.7.1或更新兼容版。
- `code/hand_controller/arm_service.py`：主脑动作服务。不要互相覆盖，也不用ZIP kit自带的arm_service。

核对X5的CPU架构、Python版本、可用内存和已有视觉虚拟环境。已有可用`.venv`则复用；没有时，在**仓库根目录**按实际系统准备视觉环境，例如：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r code/requirements-vision.txt
.venv/bin/python run_bedside_vision.py --check-runtime
```

最后一条只对内存空白图加载姿态模型，不打开摄像头、不发报警、不连硬件服务。Tasks后端需要已有 `code/vision/models/pose_landmarker_lite.task`；从此前可用的电脑视觉环境复制，或由操作者明确执行仓库现有模型下载命令。自检不会自动下载。

**ARM64的MediaPipe安装、模型加载、实际性能本次未在X5实测。** 如果没有适配当前Python/ARM64的包，先解决运行时兼容或使用本文末尾Windows备用演示。CPU空白图通过只证明依赖和模型能跑，不证明现场实时速度。不要因此声称已经完成板端部署。

## 2. 填一份现场配置，先保持off

```bash
cp deploy/bedside_vision.json.example deploy/bedside_vision.json
```

只在新建的本地JSON里修改：

| 字段 | 现场怎么填 |
|---|---|
| `phone_stream` | iPhone摄像头App上已确认可用的完整MJPEG地址，包含端口和路径；不是网页首页 |
| `mode` | 先`off`，验收后`dry`或`live` |
| `family` | 视觉与床头服务同板用`http://127.0.0.1:8013`；异板使用实际床头IP |
| `monitor_host` | 显示器直接接X5用`127.0.0.1`；Windows电脑经局域网看X5画面改为`0.0.0.0` |
| `monitor_port` | 默认8015，确认不与现有服务冲突 |
| `fps_cap` | 识别处理帧率上限，默认10；实际速度以页面显示为准 |
| `continuous` | 默认`true`，持续运行，不在600秒后自动结束 |
| `duration` | 仅`continuous:false`时生效，大于0且不超过600秒 |
| `rules_file` | 可选的已有规则JSON，默认null；相对路径从配置文件所在目录计算 |
| `output_root` | 默认仓库`code/data/bedside_vision`，每次创建新的批次目录 |

JSON无注释，文字用双引号。连续模式仍保留同一个检测器，不按十分钟重启或重置倒地状态。

若摄像头App需要Basic认证，使用进程环境变量 `PHONE_USERNAME` 和 `PHONE_PASSWORD`；不要把账号密码嵌入URL。systemd可以从本地 `deploy/bedside_vision.env` 读取这两个变量；将该文件权限设为600并保证服务用户能读取。两个现场配置文件都已加入gitignore，不进仓库和交接包。

```bash
.venv/bin/python run_bedside_vision.py --config deploy/bedside_vision.json --check-config
```

这一步只检查字段、地址格式和规则，不联网、不加载模型、不打开摄像头、不创建运行结果目录。示例的 `phone_stream` 留空会明确报错，这是尚未填写的现场信息。

## 3. 前台运行off，确认监看和帧率

```bash
.venv/bin/python run_bedside_vision.py --config deploy/bedside_vision.json
```

这一步才开始读取手机并运行模型。off不访问报警接口。X5本机浏览器打开 `http://127.0.0.1:8015/`；同网络Windows浏览器打开 `http://X5的实际IP:8015/`，此时需配置监听0.0.0.0并允许当前局域网访问8015端口。页面无需互联网资源。

显示器接电脑时，将浏览器移到外接屏，点“全屏显示”或F11。直接接X5时，由硬件负责人确认实际显示接口、线材、桌面系统和浏览器，再打开本机网页；本次没有验证真实视频输出接口。

先固定手机并做站立、走动、缓慢坐下站起，观察骨架、画面连续性、实际处理帧率、CPU/内存及服务日志。只启动一个视觉检测入口；监看浏览器可以有多个，但不再新建手机上游连接。网页关闭不会停止识别。

## 4. 床头与家属手机验收

先退出前台视觉程序（Ctrl+C）。按既有接入文档，使用同一虚拟环境从仓库根目录检查本机床头接口：

```bash
.venv/bin/python run_family_alarm.py --check-link --mode dry --family http://127.0.0.1:8013
```

只有返回兼容健康版本和匹配的dry回执才继续。在队友拿着家属手机、知道这是测试的情况下，建立一条明确的接口测试警报：

```bash
.venv/bin/python run_family_alarm.py --send-test --mode live --family http://127.0.0.1:8013
```

记录event_id、alarm_id、App是否出现警报、铃声/震动、前台/后台延迟，并在App确认处理。这个命令不启动摄像头，但会创建真实测试警报。不要以床头HTTP成功代替手机验收。

验收后将现场JSON的mode改为live，再运行第3步命令。规则候选不发；确认事件后异步发送；保持倒地不重复发送，恢复后再次倒地才生成新事件。live在打开手机输入前先做床头健康及dry检查。

## 5. 验证前台可用后，再安排自启动

以下由现场硬件负责人执行，本次开发没有运行这些硬件部署命令：

```bash
bash deploy/install_bedside_vision.sh
sudo systemctl enable --now caremate-bedside-vision.service
sudo systemctl status caremate-bedside-vision.service
journalctl -u caremate-bedside-vision.service -n 80 --no-pager
```

安装器在执行任何sudo操作前先做离线配置检查，只生成 `caremate-bedside-vision.service`；不重启既有arm/master服务。单元使用安装时操作者身份、项目虚拟环境和本地JSON，网络就绪后启动，异常退出10秒后重试。修改JSON后需要由操作者重启新的视觉单元才生效。

常用停止指令：

```bash
sudo systemctl stop caremate-bedside-vision.service
sudo systemctl disable caremate-bedside-vision.service
```

这两条只控制新增视觉进程。SIGTERM/Ctrl+C会关闭输入、尝试收尾发送队列并写结果。输入断开时该进程报告错误并退出；systemd再启动会重新加载模型和基准，**不是无损断线续识别**。重启间隔里的跌倒可能错过，恢复画面不能视为重建了之前的事件。

## 监看与报警的准确边界

- `GET /` 为页面，`GET /api/status` 为状态，`GET /frame.jpg` 为最新JPEG。没有远程触发报警、控制摄像头或硬件的写接口，POST返回405。
- 监看默认仅本机监听；0.0.0.0模式会把实时画面传给能访问此端口的局域网浏览器，无登录功能，使用现场受控局域网。只保留内存最新JPEG及一个待编码帧，不保存录像。
- JPEG在后台编码，监看默认最多5帧/秒、最长边960像素。识别处理上限独立，默认10帧/秒。网页反复取图不会反复推理；慢客户端处理器数量有界，不会让输入队列积压。
- 超过3秒没有新识别画面时，页面遮住旧画面并标记中断；网页连不到服务时也提示连接断开。JPEG编码故障独立显示，不能因此假设识别已停止或仍可用。
- 报警仅发送事件字段，不带图像。`summary.json`中的`alarm_images_sent:false`表示未把画面发到家属报警接口；`images_sent:true`及`monitor_frames_served`表示曾向监看浏览器提供画面，两者含义不同。
- 新批次的 `events.jsonl` 先记录稳定event_id，再开始请求；最多3次、每次超时3秒，重试沿用原号。没有跨进程自动恢复直接入口的待发队列；失败要核对旧日志和床头记录，必要时沿用原event_id补发。既有接收端去重保留窗口有限。
- 没有家属手机的送达回执，因此`phone_received`始终保留null。没有真人跌倒标签，不报告识别准确率。仍需现场单独评估误报、漏报、运行帧率和iPhone连续输入。

## Windows备用演示

适用于板端运行时暂未解决的情况。把同一份JSON放到Windows仓库的deploy目录：`family`改为真实床头IP，`monitor_host`保持127.0.0.1，先off。然后运行：

```powershell
.\START_BEDSIDE_VISION.bat --config deploy/bedside_vision.json --check-config
.\START_BEDSIDE_VISION.bat --config deploy/bedside_vision.json
```

浏览器打开 `http://127.0.0.1:8015/`，拖到外接屏全屏。此方案由Windows识别并向床头服务上报，床头服务和家属App仍按原接口工作。必须如实标注识别在电脑上，且先停掉同场景其他视觉入口。

## 可复现的离线验证

在已安装视觉依赖且有Node、Git Bash的开发环境中：

```powershell
.\.venv\Scripts\python.exe -X utf8 code/tests/run_all.py web_monitor bedside_watch live_input phone_pose family_watch
.\.venv\Scripts\python.exe -X utf8 code/tests/run_all.py
```

新增测试使用假摄像头、假姿态模型、内存图像、127.0.0.1 HTTP服务和假sudo，不启动真实摄像头、GUI或硬件驱动。浏览器脚本状态和中断竞态通过Node模拟DOM检查；页面截图若另有提供，使用独立的无登录浏览器离屏渲染离线合成画面，不代表现场输入或真实外接屏验收。最终数量和证据见 [本次验证记录](verification/20261025_bedside_vision.json)。
