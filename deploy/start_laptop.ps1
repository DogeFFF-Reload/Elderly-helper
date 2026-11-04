# One laptop, whole demo: every service in its own window (rehearsals / Plan B).
#   powershell -ExecutionPolicy Bypass -File deploy\start_laptop.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\start_laptop.ps1 -Vision camera -Asr vosk
# (ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 scripts without a BOM.)
param(
    [ValidateSet("none", "camera")][string]$Vision = "none",
    [ValidateSet("none", "stdin", "vosk")][string]$Asr = "none",
    [switch]$NoTts
)

$root = Split-Path -Parent $PSScriptRoot
$code = Join-Path $root "code"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

function Start-Service2([string]$title, [string]$cmdArgs) {
    $cmd = "`$host.UI.RawUI.WindowTitle = '$title'; & '$py' $cmdArgs"
    Start-Process powershell -WorkingDirectory $code -ArgumentList "-NoExit", "-Command", $cmd
}

Start-Service2 "CareMate master :8000" "-m master_service"
Start-Sleep -Seconds 2
Start-Service2 "CareMate arm mock :8013" "-m hand_controller.arm_service"
if (-not $NoTts) { Start-Service2 "CareMate TTS" "-m voice.tts_player" }
if ($Asr -ne "none") { Start-Service2 "CareMate ASR" "-m voice.asr_bridge --backend $Asr" }
if ($Vision -eq "camera") { Start-Service2 "CareMate vision" "-m vision.fall_bridge --camera 0 --preview" }

Start-Process "http://127.0.0.1:8000/ui/console"
Start-Process "http://127.0.0.1:8000/ui/monitor"
Write-Host "Started. Close each window (or Ctrl+C in it) to stop that service."
