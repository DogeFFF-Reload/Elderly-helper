# -*- coding: utf-8 -*-
"""flash_s3.ps1 — 嘉立创 S3 床头屏一键烧录入(自动找串口/编译/直烧预编译包)

用法:
  .\flash_s3.ps1 -Prebuilt            # 免编译:直接烧 prebuilt 四件套(最快,10s 级)
  .\flash_s3.ps1                      # 源码编译烧录(改过 SSID/密码/按钮后用)
  .\flash_s3.ps1 -Port COM7           # 指定串口(默认自动找)
依赖:D:\arduino-cli\arduino-cli.exe(源码模式)/ py -m esptool(prebuilt 模式)
"""
param(
    [switch]$Prebuilt,
    [string]$Port = ""
)
$ErrorActionPreference = "Stop"
$CLI = "D:\arduino-cli\arduino-cli.exe"
$SRC = Join-Path $PSScriptRoot "..\firmware_s3"
$PRE = Join-Path $SRC "prebuilt"
$FQBN = "esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app"
$BAUD = 921600

if (-not $Port) {
    Write-Host "[port] 自动搜索 CP210x/CH34x 串口..." -ForegroundColor Cyan
    $p = Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match "COM\d" -and $_.Name -match "CP210|CH340|CH341|USB" } | Select-Object -First 1
    if (-not $p) { Write-Host "没找到串口设备,插上数据线或用 -Port COMx 指定" -ForegroundColor Red; exit 1 }
    $Port = [regex]::Match($p.Name, "COM\d+").Value
}
Write-Host "[port] 用 $Port (baud $BAUD)" -ForegroundColor Green

if ($Prebuilt) {
    Write-Host "[flash] 预编译包直烧(不动源码)..." -ForegroundColor Cyan
    Push-Location $PRE
    py -m esptool --chip esp32s3 --port $Port --baud $BAUD write_flash "@flash_args"
    $ok = $LASTEXITCODE -eq 0
    Pop-Location
    if ($ok) { Write-Host "`n== 烧录完成:按任意键后屏应亮出六键待机图 ==" -ForegroundColor Green }
    else { Write-Host "`n== 烧录失败:查数据线/按住 BOOT 再插 ==" -ForegroundColor Red }
    exit ($ok ? 0 : 1)
}

# 源码编译 + 烧录
if (-not (Test-Path $CLI)) { Write-Host "arduino-cli 不在 $CLI;装好或改本行路径" -ForegroundColor Red; exit 1 }
Write-Host "[compile] $FQBN ..." -ForegroundColor Cyan
& $CLI compile --fqbn $FQBN $SRC
if ($LASTEXITCODE -ne 0) { Write-Host "编译失败(exit=$LASTEXITCODE)" -ForegroundColor Red; exit 1 }
Write-Host "[upload] ..." -ForegroundColor Cyan
& $CLI upload --fqbn $FQBN -p $Port --input-dir $SRC
if ($LASTEXITCODE -eq 0) { Write-Host "`n== 上传完成,COM8(115200) 开日志:bg_capture.py ==" -ForegroundColor Green }
else { Write-Host "`n== 上传失败:查 COM/按住 BOOT 再插 ==" -ForegroundColor Red }
exit ($LASTEXITCODE -eq 0 ? 0 : 1)