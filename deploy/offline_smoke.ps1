param(
    [switch]$Quiet
)

<#
  纯离线验收入口。
  这个脚本故意不调用 hardware_preflight，不扫描串口，不读取设备节点，
  并强制 MOCK_SERIAL=1。适合远程开发机、CI 和不在现场时反复运行。
#>
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$code = Join-Path $repo "code"

$env:MOCK_SERIAL = "1"
$env:RH56_REGMAP_CONFIRMED = "0"
$env:ARM_BACKEND = "mock"
$env:PYTHONUNBUFFERED = "1"

Push-Location $code
try {
    if (-not $Quiet) { Write-Host "[offline] running 22 test suites..." -ForegroundColor Cyan }
    python tests/run_all.py
    if (-not $Quiet) { Write-Host "[offline] compiling Python modules..." -ForegroundColor Cyan }
    python -m compileall -q .
    if (-not $Quiet) { Write-Host "[offline] running local demo..." -ForegroundColor Cyan }
    python -m demo_driver --local --all
    if (-not $Quiet) { Write-Host "[offline] PASS: no hardware access was requested." -ForegroundColor Green }
}
finally {
    Pop-Location
}
