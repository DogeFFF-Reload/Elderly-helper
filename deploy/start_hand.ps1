param(
    [ValidateSet("mock", "real", "auto")]
    [string]$Mode = "mock",
    [int]$Port = 8013,
    [string]$Hand1Port = "COM5",
    [string]$Hand2Port = "COM6",
    [switch]$ConfirmRegisterMap,
    [ValidateSet("mock", "vendor_sdk")]
    [string]$ArmBackend = "mock"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$code = Join-Path $repo "code"
Set-Location $code

python -c "import serial" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[environment] pyserial missing; installing..." -ForegroundColor Yellow
    python -m pip install -r (Join-Path $code "requirements-hardware.txt")
}

if ($Mode -eq "auto") {
    $portNames = [System.IO.Ports.SerialPort]::GetPortNames()
    if ($portNames -contains $Hand1Port) {
        $Mode = "real"
    } elseif ($portNames.Count -eq 1) {
        # A single blue box can carry both slave IDs on one RS485 bus.
        $Hand1Port = $portNames[0]
        $Hand2Port = $portNames[0]
        $Mode = "real"
    } elseif ($portNames.Count -ge 2) {
        $Hand1Port = $portNames[0]
        $Hand2Port = $portNames[1]
        $Mode = "real"
    } else {
        $Mode = "mock"
    }
}

if ($Mode -eq "real" -and -not $ConfirmRegisterMap) {
    throw "Real RH56 writes are blocked until the register map is checked; rerun with -ConfirmRegisterMap after docs/10 is verified."
}

$env:ARM_PORT = "$Port"
$env:HAND1_PORT = $Hand1Port
$env:HAND2_PORT = $Hand2Port
$env:HAND1_ID = "1"
$env:HAND2_ID = "2"
$env:ARM_BACKEND = $ArmBackend
$env:MOCK_SERIAL = if ($Mode -eq "real") { "0" } else { "1" }
$env:RH56_REGMAP_CONFIRMED = if ($ConfirmRegisterMap) { "1" } else { "0" }

Write-Host "[arm] starting repository arm_service on :$Port (mode=$Mode, hand1=$Hand1Port, hand2=$Hand2Port, arm_backend=$ArmBackend)" -ForegroundColor Cyan
python -m hand_controller.arm_service --host 0.0.0.0 --port $Port
