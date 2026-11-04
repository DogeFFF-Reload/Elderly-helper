#!/usr/bin/env bash
# Install this NEW vision unit only. Does not start it or alter hardware services.
# Run by the hardware teammate AFTER the foreground/runtime checks in docs/34.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
CONFIG="$ROOT/deploy/bedside_vision.json"
if [ ! -x "$PY" ]; then
  echo "缺少项目.venv/bin/python，请先按docs/34准备视觉环境。" >&2
  exit 1
fi
if [ ! -f "$CONFIG" ]; then
  echo "缺少deploy/bedside_vision.json，请复制示例并填写现场配置。" >&2
  exit 1
fi
"$PY" "$ROOT/run_bedside_vision.py" --config "$CONFIG" --check-config
TASK_USER="${SUDO_USER:-$(id -un)}"
# systemd has its own quoting/specifier rules; reject unusual installation paths.
case "$ROOT" in *'"'*|*'\'*|*'%'*|*$'\n'*|*$'\r'*) echo "仓库路径含不支持的字符，请使用普通目录。" >&2; exit 1 ;; esac
TASK_UNIT="$(mktemp)"
trap 'rm -f -- "$TASK_UNIT"' EXIT
cat > "$TASK_UNIT" <<EOF
[Unit]
Description=CareMate bedside phone vision and read-only monitor
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$TASK_USER
WorkingDirectory="$ROOT"
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
EnvironmentFile="-$ROOT/deploy/bedside_vision.env"
ExecStart="$PY" "$ROOT/run_bedside_vision.py" --config "$CONFIG"
Restart=on-failure
RestartSec=10
TimeoutStopSec=35
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$TASK_UNIT" /etc/systemd/system/caremate-bedside-vision.service
sudo systemctl daemon-reload
echo "仅安装完成，尚未启动或启用。现场验证后由队友执行："
echo "sudo systemctl enable --now caremate-bedside-vision.service"
