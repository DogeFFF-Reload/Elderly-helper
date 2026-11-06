# 工作报告：ESP32 开发环境搭建（Arduino IDE）交接说明

| 项 | 内容 |
|---|---|
| 文档用途 | 交给**下一个 Agent**，照着做就能把 ESP32 的 Arduino 开发环境装好、配好、验证通过 |
| 目标平台 | Windows（本机为 Windows，用户名 `36197`） |
| 目标硬件 | ESP32 开发板（**作为主控**） |
| 本次要用的能力 | WiFi + 蓝牙(BLE) 无线通信、PWM 输出（舵机/电机） |
| 编写日期 | 2026-10-19 |
| 阅读者假设 | 会用 PowerShell、会看串口输出；不需要嵌入式经验，但**必须严格按步骤执行并核对每一步的实际输出** |

---

## 〇、给下一个 Agent 的三条铁律

1. **先检查、后安装。** 本机已经装好了一套完整可用的环境（见第一节），**大概率你什么都不用装**，只要确认版本、补驱动、跑通 blink 即可。不要一上来就重装覆盖。
2. **每一步都要看到预期输出再往下走。** 下文每步都写了"✅ 应该看到什么"。看不到就停下排查，不要猜。
3. **不要脑补硬件。** 本报告只负责"环境装好、能编译、能上传、能跑无线"。具体的接线、机械、整车集成不属于本文档范围，遇到不确定的就问用户。

---

## 一、环境现状（本机已实测，可直接复用）

> 以下为 2026-10-19 在本机实测结果，**不是推测**。

| 组件 | 状态 | 位置 / 版本 |
|---|---|---|
| Arduino IDE | ✅ 已安装 | `D:\Arduino IDE\Arduino IDE.exe`，版本 **2.3.10** |
| arduino-cli | ✅ 已安装 | `D:\arduino-cli\arduino-cli.exe`，版本 **1.5.1** |
| ESP32 开发板核心 | ✅ 已安装 | `esp32:esp32` **3.3.12** |
| AVR 核心 | ✅ 已安装 | `arduino:avr` 1.8.8（用不到，留着无害） |
| Arduino 数据目录 | — | `C:\Users\36197\AppData\Local\Arduino15` |
| 第三方库 | 已装 | `Ethernet` `Firmata` `Keyboard` `LiquidCrystal` `Mouse` `SD` `Arduino_BuiltIn`（都在数据目录的 `libraries\` 下） |
| 当前串口 | — | **无**（当时没有板子插在电脑上） |
| 草稿目录 | — | 未发现 `C:\Users\36197\Documents\Arduino`，**需要时自建**（见第二节 B） |

**结论**：ESP32 工具链（编译器 `esp-x32` / `esp-rv32`、各系列 libs）全部齐了。
你需要做的是 **①确认版本 → ②补 USB 串口驱动 → ③写测试程序编译上传 → ④看串口输出**。

---

## 二、方案选择

| 场景 | 用哪套 | 说明 |
|---|---|---|
| 你（Agent）要**自动化**、无界面跑编译/上传 | **arduino-cli**（第三节） | 命令行，输出可解析，适合脚本化 |
| 用户要**手动改代码、看图形界面** | **Arduino IDE 2.3.10**（第四节） | 用户日常用这个；首次用 IDE 需要确认"附加开发板管理器网址"已配好 |

两套共用同一个数据目录（`Arduino15`），**装一次互相可见**。所以：cli 装好的核心，IDE 里直接就能选；反之亦然。

---

## 三、方案 A：arduino-cli（推荐 Agent 用）

### A1. 确认可用

```powershell
& 'D:\arduino-cli\arduino-cli.exe' version
& 'D:\arduino-cli\arduino-cli.exe' core list
```

✅ 应该看到：
```
arduino-cli  Version: 1.5.1 ...
ID          Installed Latest Name
arduino:avr 1.8.8     1.8.8  Arduino AVR Boards
esp32:esp32 3.3.12    3.3.12 esp32
```

如果 `esp32:esp32` 那一行**在**，直接跳到 A4。不在就做 A2/A3。

### A2. （仅当核心缺失时）配置附加开发板网址

```powershell
& 'D:\arduino-cli\arduino-cli.exe' config init          # 已存在 config 会提示，忽略即可
& 'D:\arduino-cli\arduino-cli.exe' config add board_manager.additional_urls `
    https://espressif.github.io/arduino-esp32/package_esp32_index.json
& 'D:\arduino-cli\arduino-cli.exe' config dump
& 'D:\arduino-cli\arduino-cli.exe' core update-index
```

✅ `config dump` 里应能看到 `board_manager.additional_urls` 含上面那个网址；
`core update-index` 结束时会打印可用平台列表（可看到 `esp32`）。

> 上面这个网址是 **Espressif 官方** 的 ESP32 包索引，别写成别的第三方地址。

### A3. （仅当核心缺失时）安装 ESP32 核心

```powershell
& 'D:\arduino-cli\arduino-cli.exe' core install esp32:esp32@3.3.12
```

⚠️ 首次安装约 **1~2 GB** 下载量（各系列预编译 libs），会很慢。本机已装 3.3.12，正常情况不需要再下。

### A4. 列出可用板子 / 看当前插了什么

```powershell
& 'D:\arduino-cli\arduino-cli.exe' board list
```

✅ 插着板子时，会列出形如：
```
Port  Protocol Type              Board Name        FQBN              Core
COM5  serial   Serial Port (USB) ESP32 Dev Module  esp32:esp32:esp32 esp32:esp32
```
**没插板子时列表是空的，这是正常的**（本机实测就是空）。此时用第一节的表确认环境，再让用户插上板子。

### A5. 编译 + 上传（样板命令）

假设草图在 `D:\某处\app\app.ino`，板子是经典 ESP32 Dev Module（FQBN `esp32:esp32:esp32`）：

```powershell
# 编译（本次要用 WiFi/BLE，必须选 huge_app 分区）
& 'D:\arduino-cli\arduino-cli.exe' compile `
    --fqbn esp32:esp32:esp32 `
    --board-options PartitionScheme=huge_app `
    "D:\某处\app"

# 上传（COM 号以上一步 board list 为准）
& 'D:\arduino-cli\arduino-cli.exe' upload `
    -p COM5 `
    --fqbn esp32:esp32:esp32 `
    --board-options PartitionScheme=huge_app `
    "D:\某处\app"

# 看串口（115200）
& 'D:\arduino-cli\arduino-cli.exe' monitor -p COM5 -c baudrate=115200
```

- 老版本 cli 不认 `--board-options` 时，改用 FQBN 后缀写法：
  `--fqbn esp32:esp32:esp32:PartitionScheme=huge_app`（两种写法二选一即可）。
- 分区选项键名就是 `PartitionScheme`，值 `huge_app`。
- **为什么必须 huge_app**：ESP32 的 BLE 协议栈本身就占 ~1.1 MB，用默认分区会顶到 **~84%**，几行代码就可能 `region overflowed`；换成 `Huge APP (3MB No OTA/1MB SPIFFS)` 后回落到 **~35%**，留足余量。既然本次要用蓝牙，**默认就选 huge_app**。

### A6. 常用附加命令

```powershell
& 'D:\arduino-cli\arduino-cli.exe' lib list                    # 看已装库
& 'D:\arduino-cli\arduino-cli.exe' lib search <关键字>          # 搜库
& 'D:\arduino-cli\arduino-cli.exe' lib install "库名@版本"      # 装库（需联网）
& 'D:\arduino-cli\arduino-cli.exe' board details -b esp32:esp32:esp32   # 看该板支持的所有选项
```

---

## 四、方案 B：Arduino IDE 图形界面（用户用）

### B1. 确认版本

打开 `D:\Arduino IDE\Arduino IDE.exe`。
✅ 帮助 → 关于，应显示 **2.3.10**。IDE 自带一份 cli，与第三节的数据目录共用。

### B2. 检查"附加开发板管理器网址"（关键）

`文件 → 首选项 → 附加开发板管理器网址` 里确认有：

```
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

没有就粘贴进去、确定。

### B3. 检查开发板（如核心已装会被 B2 自动启用）

`工具 → 开发板 → esp32 → ESP32 Dev Module`。
✅ 能选到就说明核心就绪（本机 3.3.12 已装）。

### B4. 草稿目录（如需要）

IDE 默认草图目录是 `C:\Users\36197\Documents\Arduino`，**本机当前不存在**。
- 让用户在 `首选项 → 项目文件夹位置` 里指定一个自己习惯的目录（比如 `D:\ArduinoProjects`），或
- 直接由你（Agent）用命令行 `New-Item -ItemType Directory` 建好 `Documents\Arduino`。

⚠️ 草图目录下**每个 .ino 必须独占一个与其同名的子文件夹**（Arduino 的硬性规定），例如
`D:\ArduinoProjects\app\app.ino`。这个坑下一个 Agent 很容易踩。

### B5. 上传设置（每次都要核对）

| 设置项 | 值 |
|---|---|
| 开发板 | ESP32 Dev Module |
| 分区表 | **Huge APP (3MB No OTA/1MB SPIFFS)** |
| 上传速率 | 默认 921600；**上传失败就降到 115200** |
| 端口 | 板子对应的 COMx（插拔一次看哪个出现/消失） |
| 串口监视器波特率 | 115200 |

---

## 五、USB 串口驱动（"插上认不到 COM 口"就是这个）

ESP32 板子上的 USB 转串口芯片不止一种，**先看板子上那颗芯片的丝印**，再装对应驱动：

| 芯片丝印 | 驱动 | 备注 |
|---|---|---|
| `CP2102` / `CP2104` | Silicon Labs CP210x VCP Driver | 最常见 |
| `CH340` / `CH341` | WCH CH34x Driver | 国产板遍地都是 |
| `CH9102` | WCH CH9102 Driver | 需要 win7+ |
| 无转换芯片（原生 USB） | 免驱 | ESP32-S3/C3 等原生 USB 板，Win10/11 免驱；此时会出现两个口（JTAG + Serial），**烧录用 Serial 那个** |

安装后：`[System.IO.Ports.SerialPort]::GetPortNames()` 应能列出 `COMx`；设备管理器 → 端口(COM和LPT) 无黄色感叹号。

> ⚠️ **GPIO12 是 strapping 脚**：上电/烧录时若它被外部电路拉高，板子可能进不了下载模式、烧录失败。
> 如果 D12 上接了东西，**先拔掉那根线再烧**，或把输出改到 GPIO14 / GPIO27。

---

## 六、最小验证：blink 全流程（环境装完必做）

写 `D:\ArduinoProjects\app\app.ino`：

```cpp
void setup() {
  Serial.begin(115200);
  pinMode(2, OUTPUT);              // 多数 ESP32 开发板板载 LED 在 GPIO2
  Serial.println("ESP32 ready");
}
void loop() {
  digitalWrite(2, !digitalRead(2));
  Serial.println(millis());
  delay(500);
}
```

用第三节 A5 的命令 **编译 → 上传 → monitor**。

✅ 验收标准：
1. 编译输出末尾有 `Sketch uses ... bytes (xx%)`，且**没有 error**；
2. 上传输出有 `Hash of data verified.` / `Hard resetting via RTS pin...`；
3. `monitor` 每 500ms 打印一次递增的毫秒数，板载 LED 闪。

三件事都对，**环境就算彻底通了**，可以进下一步。

---

## 七、本次开发的功能 → 对应的环境要求

ESP32 在这里是**主控**，自己跑逻辑、自己发指令、自己对外通信。

### 7.1 无线通信：WiFi 和 蓝牙(BLE) 都要能用

| 通信 | 用什么 | 要不要装库 | 备注 |
|---|---|---|---|
| WiFi | 核心自带 `WiFi.h` | **不用装** | 可做 TCP / UDP / HTTP / WebSocket 服务端或客户端 |
| 蓝牙 BLE | 核心自带 `BLEDevice.h` / `BLEServer` / `BLEClient` | **不用装** | 可做 BLE 外设(Service/Characteristic)或中央设备 |

- 两者可以**同时开**，但会抢射频时间和内存，注意 RAM 余量（用 huge_app 分区后空间充足）。
- 手机/上位机侧的连接、配对、重连逻辑属于应用层，不在本文档范围。
- **必须选 `Huge APP` 分区**（见 A5），这是本次环境配置里唯一一个"不注意就会失败"的点。

### 7.2 执行机构：PWM 输出

- 舵机直接用 ESP32 核心自带的 **LEDC 硬件 PWM**（`ledcSetup` / `ledcAttachPin` / `ledcWrite`），**零第三方库**。
- 电机/电调一般也是给 PWM 信号，同样走 LEDC 或普通 `analogWrite`。
- ⚠️ 不要用 `delay()` 打 PWM，用硬件 LEDC 才稳。

### 7.3 供电（用户已有：充电宝 / 24V 电池）

| 供电源 | 给谁 | 注意 |
|---|---|---|
| 充电宝 (USB 5V) | ESP32 开发板 | 走板子的 USB 口最省事；也可接 5V/VIN 脚 |
| 24V 电池 | 电机、舵机等大电流负载 | **必须经降压模块**，降压到 5V(舵机) / 6~12V(电调，看规格) |
| ESP32 板载 3.3V | **只给传感器等小电流** | **绝对不要**用它给舵机/电机供电 |

通用红线：
1. **共地**：ESP32、电源、被执行器件 **GND 必须接在一起**。
2. **电平**：ESP32 是 **3.3V 逻辑**，接 5V 信号前确认对方容忍；ESP32 输出的 3.3V 去驱动需要 5V 的输入可能不识别，要加电平转换。
3. **GPIO 选择**：避开 strapping 脚 **0 / 2 / 12 / 15**；**34~39 只能做输入**且没有内部上拉。
4. 板子插 USB 时若电脑重启/断连，说明供电不足，换带供电的 Hub 或走外部供电。

---

## 八、故障排查清单（按顺序试）

| 现象 | 先查这个 |
|---|---|
| `board list` 里没有 COM 口 | 驱动（第五节）；换一根**能传数据**的 USB 线（很多线只供电）；换一个 USB 口 |
| 上传报 `Failed to connect to ESP32: Timed out waiting for packet header` | **按住板子 BOOT 键**再点上传，看到 `Connecting...` 后松手；或降速到 115200 |
| 上传 "could not open port / access denied" | 串口监视器没关，或另一个程序占用了；关掉串口监视器/关掉 IDE 再试 |
| 上传成功但串口没输出 | 波特率对不对（115200）；板子可能停在 bootloader 模式，按一下 RST/EN 键复位 |
| 编译报 `region ... overflowed` | 分区表没选 **Huge APP**（用了 BLE 必选） |
| 编译报找不到某库 | `arduino-cli lib list` 确认；用 `lib install` 装，或让用户下 zip 用"添加 .ZIP 库"离线装 |
| 板子插上后电脑重启/断连 | 板子耗电大，换带供电的 USB Hub 或外部供电 |
| WiFi 连不上 / BLE 搜不到 | 天线区域别被金属遮挡；确认没把射频脚占作他用；供电不稳会掉线 |
| 中文乱码 | 是 **PowerShell 控制台编码**问题，不是板子问题。校验内容请用文件读取或 `PYTHONIOENCODING=utf-8` |

---

## 九、验收清单（做完打勾）

- [ ] `arduino-cli version` 有输出，`core list` 里 **`esp32:esp32 3.3.12`** 在
- [ ] 插板后 `board list` 能看到 `esp32:esp32:esp32` / COM 口
- [ ] `app.ino` 编译 **0 error**，且分区表选的是 **Huge APP**
- [ ] `app.ino` 上传成功
- [ ] `monitor -c baudrate=115200` 能看到打印
- [ ] 若装了新库，已登记到第七节表格

---

## 十、附：本机关键路径速查

```
Arduino IDE 2.3.10     D:\Arduino IDE\Arduino IDE.exe
arduino-cli 1.5.1      D:\arduino-cli\arduino-cli.exe
ESP32 核心 3.3.12      C:\Users\36197\AppData\Local\Arduino15\packages\esp32\hardware\esp32\3.3.12
Arduino 数据目录       C:\Users\36197\AppData\Local\Arduino15
  第三方库             C:\Users\36197\AppData\Local\Arduino15\libraries\
  工具链               C:\Users\36197\AppData\Local\Arduino15\packages\esp32\tools\  (esp-x32 / esp-rv32 / esp32-libs ...)
板子 FQBN（经典 ESP32）esp32:esp32:esp32       分区选项 PartitionScheme=huge_app
串口波特率             115200
```

---

## 十一、交接给下一个 Agent 的"下一步"

1. 按第三节 A1 确认环境（预期：无需安装）。
2. 让用户插上 ESP32，装对应 USB 驱动，确认 `board list` 出现 COM 口。
3. 跑通第六节 blink，完成第九节验收。
4. 环境通了之后，按第七节确认 WiFi / BLE / 供电三项就绪，即可进入应用开发。
5. 把过程中新增的事实（板子型号、COM 号、装过的库）**回写到本文档**，让文档保持最新。
