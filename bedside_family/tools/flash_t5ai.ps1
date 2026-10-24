# -*- coding: utf-8 -*-
"""flash_t5ai.ps1 - 涂鸦 T5AI 臂屏一键烧录(编译+上传 / 只烧)

用法:
  .\flash_t5ai.ps1                # 编译+上传(默认)
  .\flash_t5ai.ps1 -Port COM9     # 指定串口
  .\flash_t5ai.ps1 -CompileOnly   # 只编译不烧(验证代码能过)
依赖: arduino-cli(已装 tuya_open 核心);串口日志: tools\x5_ops\t5ai_log.py
"""
param(
    [string]$Port = "COM9",
    [switch]$CompileOnly
)
$ErrorActionPreference = "Stop"
$CLI = "D:\arduino-cli\arduino-cli.exe"
$SRC = Join-Path $PSScriptRoot "..\firmware_t5ai_handover\bedside_t5ai_arm"
$FQBN = "tuya_open:tuya_open:TUYA_T5AI_BOARD"

if (-not (Test-Path $CLI)) { Write-Host "arduino-cli 不在 $CLI;装好或改本行路径" -ForegroundColor Red; exit 1 }
if (-not $Port) {
    $p = Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match "COM\d" -and $_.Name -match "CP210|CH340|CH341|USB" } | Select-Object -First 1
    if (-not $p) { Write-Host "没找到串口设备,用 -Port COMx 指定" -ForegroundColor Red; exit 1 }
    $Port = [regex]::Match($p.Name, "COM\d+").Value
}
Write-Host "[t5ai] $Port, fqbn=$FQBN" -ForegroundColor Green

Write-Host "[compile] T5AI 编译约 1-3 分钟..." -ForegroundColor Cyan
& $CLI compile --fqbn $FQBN $SRC 2>&1 | Tee-Object -Variable out
# T5AI 工具链怪癖:有些版本明明 BUILD SUCCESS 却 exit 1 —— 以输出为准
$buildOk = ($LASTEXITCODE -eq 0) -or (($out -join "`n") -match "BUILD SUCCESS")
if (-not $buildOk) { Write-Host "[compile] 真失败" -ForegroundColor Red; exit 1 }
Write-Host "[compile] OK(exit=$LASTEXITCODE 也认 BUILD SUCCESS)" -ForegroundColor Green
if ($CompileOnly) { exit 0 }

Write-Host "[upload] 约 35s,请勿断线..." -ForegroundColor Cyan
& $CLI upload --fqbn $FQBN -p $Port --input-dir $SRC
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n== 烧录完成:四键+语音键起来后,X5#B(.251) 上线状态条自动变绿 ==" -ForegroundColor Green
    Write-Host "== 挂日志: py tools\x5_ops\t5ai_log.py (COM$Port 115200) ==" -ForegroundColor Green
} else {
    Write-Host "`n== 上传失败:拔插 USB/按住 BOOT 再试 ==" -ForegroundColor Red
}
exit ($LASTEXITCODE -eq 0 ? 0 : 1)