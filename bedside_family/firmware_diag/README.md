# 嘉立创 S3 面板诊断配置(bedside_diag)

给"面板没图/触摸失灵/颜色颠倒"排障用的**诊断板配置集**:一个极简 .ino + ESP_Panel(LovyanGFX 前身体系)板级配置 7 件。烧进去就是一条屏驱/触摸逐项自测,确认硬件好坏再回头查 kiosk 固件。

## 文件清单

| 文件 | 作用 |
|---|---|
| `bedside_diag.ino` | 诊断主程序:逐项点亮/清屏/读触摸坐标并打印 |
| `ESP_Panel_Board_Custom.h` | 板级自定义(引脚映射:屏 I2C/I2S/触摸) |
| `esp_panel_board_custom_conf.h` | 板级参数(分辨率 320x240 横屏、旋转、背光) |
| `ESP_Panel_Board_Supported.h` | 支持的板型号开关 |
| `esp_panel_board_supported_conf.h` | 板支持表配置 |
| `ESP_PanelConf.h` | 库总开关(日志/缓冲) |
| `esp_panel_drivers_conf.h` | 屏驱/触摸 IC 驱动选择(ST7789 / FT 系触摸) |
| `esp_utils_conf.h` | 工具宏 |

## 用法

1. Arduino IDE 选 ESP32S3 Dev Module + 板级 `Custom` 诊断配置(这 7 个 .h 覆盖库默认值);
2. 烧录:分区随意(程序极小),`-p COM7`,日志 `COM8` 115200;
3. 按屏上提示逐条回显:清屏→色条→触摸坐标;**触摸没反应先查 I2C 地址,颜色颠倒改 `SWAP_BYTES` 或 MADCTL**。

## 排障结论(当时实测留存)

- 面板背光/复位脚由 PCA9557 扩展位控制,BIT1=功放 PA_EN——顺序:`先扩展开→再初始化屏`;
- 触摸 FT6336 I2C 地址 0x38(INT 脚 40);屏 ST7789,SPI 割在 45/47/48 引脚组;
- 诊断通过 = 面板硬件无恙,问题在 kiosk 固件(回 `../firmware_s3/`)。

> 本目录只是"板级配置档案 + 诊断脚本",日常主固件在 `../firmware_s3/`。