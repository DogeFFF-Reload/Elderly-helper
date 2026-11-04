param(
    [int]$Port = 8013,
    [int]$Hand = 1
)

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"
function Invoke-Pose([string]$Name, [int]$Wait = 1) {
    $uri = "$base/pose/$Name`?hand=$Hand"
    Write-Host "[pose] $Name (hand=$Hand)" -ForegroundColor Cyan
    $result = Invoke-RestMethod -Method Post -Uri $uri -TimeoutSec 10
    if (-not $result.ok) { throw ($result | ConvertTo-Json -Compress) }
    Start-Sleep -Seconds $Wait
}

Write-Host "Repository arm_service hand smoke test" -ForegroundColor Green
Invoke-Pose "fingers_5" 2
Invoke-Pose "wave" 3
Invoke-Pose "thumbs_up" 2
Write-Host "Hand smoke sequence accepted." -ForegroundColor Green
