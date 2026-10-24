# X5 运维脚本(tools/x5_ops + deploy_x5)

第二块 X5(.251 臂板)救援/部署/验证的**交接包全集**——队友接手板子时拿这些就能续干,不用重踩。

## 脚本清单

| 脚本 | 用途 | 关键参数 |
|---|---|---|
| `x5_deploy_b.py` | 全新部署:推 hand_controller 源码 + vosk-cn.zip(44MB) + vosk wheel → 解压模型 → 装服务 | 目标 IP |
| `x5_model_up.py` | 只补模型:推 vosk-cn.zip 并解压到 /root/caremate/vosk-cn | — |
| `x5_push_agent.py` | **唯一日常要跑的**:推语音补丁 + 检测 /dev/ttyUSB0 自动切真机/mock(unit.env 改 MOCK_SERIAL/RH56_PROTO) | `py x5_push_agent.py <板IP>` |
| `x5_push_voicearm.py` | 语音+臂补丁推送(变体) | 板 IP |
| `x5_rescue2.py` | **救砖**:USB RNDIS(192.168.128.10)上去重启/修 WiFi(需先在 Windows 提权配对端 IP) | — |
| `x5_voice_test3.py` | 语音配方测试:起 vosk 录 4s → /voice_cmd → 看识别 | 板 IP,测试词 |
| `verify_251.py` | 半死验证:/status 200? SSH 通? journal 尾巴 | — |
| `board_ssh.py` / `push_service.py` | paramiko 一条龙:推文件+restart+验证 | HOST/USER/PW |
| `t5ai_log.py` | T5AI 串口日志(COM9 115200,只打 URL/POST 行) | — |
| `bg_capture.py` | S3 串口抓日志(COM8,自动重连防瞬断) | 秒数 |
| `watch_usb_x5.py` / `wait_board2.py` / `probe_x5.py` | 板死等复活 / 子网存活扫描 | — |

## 密码纪律

脚本全部改成 `os.environ.get("BOARD_PWD","root")` 注入;WiFi 密码一律 `YOUR_WIFI_PASSWORD` 占位。SSH root/root 为现场板默认,公网化前必须改。

## X5 救援 SOP(浓缩三步)

1. 拔电 3 秒重上电(LED 亮≠系统活着;1-2 分钟回归);
2. 不行 → USB 数据线插笔记本走 RNDIS(192.168.128.10),Windows 提权:
   `New-NetIPAddress -InterfaceIndex <idx> -IPAddress 192.168.128.100 -PrefixLength 24`;
   SSH 进去 `journalctl -b --no-pager | tail -50` 看卡在 fsck 还是 WiFi;
3. WiFi profile 平时应已持久化(autoconnect+静态 IP);连不上再 `nmcli dev wifi connect 'Mocen_Bear' password 'YOUR_WIFI_PASSWORD'`。

## systemd 单元(deploy_x5/)

| 单元 | 内容 |
|---|---|
| `caremate.service` | 主脑服务(臂板,8013) |
| `caremate-arm.service` | 臂+手执行端 |
| `caremate-mic.service` | 麦克风输入守护 |

配套:`setup_board.sh`(新板初始化)、`flip_hand_to_board.sh`(手-板翻转切换)、`rdk_deploy.ps1`(笔记本侧一键部署)。单元环境 `/root/caremate/unit.env` 四行:MOCK_SERIAL / RH56_PROTO / HAND1_PORT / ARM_BACKEND(接家属中转站时加 FAMILY_PUSH_URL / FAMILY_PULL_URL / FAMILY_PULL_TOKEN)。

## 板号纪律(血泪)

**.252=手板(用户侧),.251=臂板(队友侧只读勿动)**。曾有队友脚本误推 .252 造成双进程打架清场——**推哪块板之前先看 IP,推错=清场事故**。

## 应急手术工具(surgery/)

事发当时的一次性手术脚本,每个的头注释写明处置步骤;复用时改目标 IP/单元名。密码一律 env 注入(BOARD_PWD),不在脚本里。

| 脚本 | 处置什么 |
|---|---|
| `fix_x5a_service.py` | **双进程打架清场**(板号纪律事故现场):列清全部 unit → 停旧启用新 → 真机 env → 启动验证 |
| `fix_x5a_unit.py` | 补 arm_service.service unit + 真机 env + 启动验证(unit 被覆盖/丢配置时用) |
| `cleanup_board.py` | 板上留言/警报自测残留清理(测试 fam_/msg_ 文件)+ 服务复核 |
| `handover_x5a.py` | 手板交接盘点:unit.env / vosk-cn 模型一屏确认 |
| `capture_boot.py` | S3 COM8 串口启动日志抓取(ES8311 自检验证) |
| `diag_buttons.py` | S3 硬复位重启 + 完整抓 45s 串口(查播放后主循环假死;名字带 buttons 但实际是复位抓日志) |
| `subnet_scan.py` | 热点网段(10.153.47.0/24)SSH 22 扫描——找哪块板活着 |

> 全是 python + 并发 socket / paramiko,零第三方依赖之外仅 paramiko/serial(现场笔记本已装)。