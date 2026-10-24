# 预编译固件烧录包(免编译直烧)

不想装 Arduino/工具链?**esptool 一条命令把这四件烧进 S3**,十秒级:

## 文件构成(2026-10-24 构建,S3 kiosk v5.4 真机验证版)

| 文件 | 内容 |
|---|---|
| `bedside_s3_arm_kiosk.ino.bin` | 应用(1.7MB) |
| `bedside_s3_arm_kiosk.ino.bootloader.bin` | 二级引导 |
| `bedside_s3_arm_kiosk.ino.partitions.bin` | 分区表(huge_app) |
| `boot_app0.bin` | OTA 数据分区占位(huge_app 无 OTA,按 flash_args 占位) |
| `flash_args` | 官方烧录参数(地址+偏移,逐字从构建目录拷) |
| `partitions.csv` | 分区表源(huge_app 3M app/9M spiffs…看文件) |

## 烧录命令

```powershell
py -m pip install esptool          # 一次性
py -m esptool --chip esp32s3 --port COM7 --baud 921600 `
  write_flash @flash_args
```

`@flash_args` 会展开成四件套+各自的 offset(0x0 bootloader / 0x8000 partitions / 0x10000 app)。

## 注意

- **板端 WiFi 密码在这个预编译包里是"现场值"**,公开仓库的源码版已脱敏——`YOUR_WIFI_PASSWORD` 只存在于源码;此预编译 bin 是**现场烧的**,含当时热点的真实 SSID/密码,故**仅限本队现场复用,勿再外传 bin 本身**;
- WiFi 改动请走源码重编(改 `WIFI_SSID/WIFI_PASS` → `../README.md` 烧录表);
- COM 口以现场为准(插上 `Get-CimInstance Win32_PnPEntity | ? Name -match "COM\d"` 查),或直接 `tools/flash_s3.ps1` 自动找。