# 预编译固件烧录包(已撤架)

> **2026-10-24 深夜撤架说明**:本目录原本放着"现场构建直出"的四件套 bin(`app/bootloader/partitions/boot_app0` + `flash_args`)——但预编译 bin 把当时热点的**真实 WiFi 凭据烧死在里面**,不该进公开仓库,故撤下二进制;`.gitattributes` 的 `*.bin binary` 保护保留(后续若再放构建产物,同样先过脱敏关)。

## 免编译直烧的需求依旧成立,改两条路走

1. **现场包队内保管(不入库)**:拿到队内持有的 bin 包后,一条命令十秒级:

   ```powershell
   py -m pip install esptool      # 一次性
   py -m esptool --chip esp32s3 --port COM7 --baud 921600 write_flash @flash_args
   ```

   `@flash_args` 展开为 bootloader(0x0)/partitions(0x8000)/app(0x10000) 各自地址。

2. **自建(推荐进仓库的玩法)**:改好 `bedside_s3_arm_kiosk.ino` 顶部 `WIFI_SSID` / `WIFI_PASS`(仓库版是占位符 `YOUR_WIFI_PASSWORD`)后:

   ```powershell
   ..\..\tools\flash_s3.ps1          # 自动找串口,arduino-cli 编译+烧录
   .\..\..\tools\flash_s3.ps1 -Prebuilt   # 队内拿到了 prebuilt 包时直烧
   ```

## 板端 WiFi 凭据纪律

- **仓库源码里 WiFi 密码一律占位符** `YOUR_WIFI_PASSWORD`;真实凭据只活在两处:板子上、队内保管的 bin/示教环境里;
- T5AI 交接固件同理(`bedside_t5ai_arm.ino` 的 `WIFI_PASS` 已恢复占位);
- 复审手感:任何合并后全库 grep 真实热点密码应 0 命中(文本层;二进制见上)。本 README 不再回显该字符串本身。