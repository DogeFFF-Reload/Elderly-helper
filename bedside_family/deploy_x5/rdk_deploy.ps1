# Deploy the CareMate master brain (port 8000) onto RDK X5.
# Usage (laptop, Windows):  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\rdk_deploy.ps1 [-IP 192.168.128.10]
# Prereq: X5 data USB-C plugged to laptop (RNDIS, ssh root@IP, pwd root -- typed by hand, never stored here)
# What runs on the board: master_service.py ONLY (stdlib, python3.8+).
#   arm_service stays on the laptop (USB-RS485 to the hands).
param(
  [string]$IP = "192.168.128.10",
  [string]$User = "root",
  [string]$Dest = "/opt/caremate"
)
$ErrorActionPreference = "Stop"
$code = Join-Path $PSScriptRoot ".."          # .../code
$sshOpts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
Set-Location $code

Write-Host "== 0) reach X5 $IP =="
ping -n 2 $IP | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "X5 unreachable -- check RNDIS (ipconfig) & data USB-C"; exit 1 }

Write-Host "== 1) create $Dest on board (type 'root' when it asks) =="
ssh $sshOpts "$User@$IP" "mkdir -p $Dest"
if ($LASTEXITCODE -ne 0) { Write-Host "ssh failed (password wrong / port cable data not connected)"; exit 1 }

Write-Host "== 2) push code =="
foreach ($t in @("agent", "state_machine", "ui", "voice_pack")) {
  scp -r $sshOpts "$code\$t" "${User}@${IP}:$Dest/"
}
scp $sshOpts "$code\master_service.py" "${User}@${IP}:$Dest/"

Write-Host "== 3) systemd service (autostart on boot) =="
$unit = "/etc/systemd/system/caremate.service"
$unitBody = "[Unit]\nDescription=CareMate master (port 8000)\nAfter=network.target\n[Service]\nExecStart=/usr/bin/python3 $Dest/master_service.py\nWorkingDirectory=$Dest\nRestart=on-failure\nEnvironment=TTS_OUT=1\n[Install]\nWantedBy=multi-user.target\n"
ssh $sshOpts "$User@$IP" "printf '%b' '$unitBody' > $unit && systemctl daemon-reload && systemctl enable caremate && systemctl restart caremate"
Start-Sleep 3
ssh $sshOpts "$User@$IP" "curl -s http://127.0.0.1:8000/ping || journalctl -u caremate -n 20 --no-pager"

Write-Host "DONE. Board now serves: http://$IP/ui/monitor  /ui/family  /ui/console"
Write-Host "Redeploy any change: rerun this script (stop first: ssh $User@$IP systemctl stop caremate)"
