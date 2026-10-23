#!/usr/bin/env bash
# 开机自启：bash deploy/install_services.sh a|b|c
#   不需要声卡/屏幕的（vision / master / arm）→ systemd 系统服务：开机就起，挂了 2 秒后自动重启
#   要用喇叭、麦克风、屏幕的（tts / asr / kiosk）→ 桌面登录后自启（~/.config/autostart），
#   因为系统服务拿不到登录用户的音频和显示
# 卸载：sudo systemctl disable --now caremate-<名字>；rm ~/.config/autostart/caremate-*.desktop
set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in a|b|c) ;; *) echo "用法：$0 a|b|c" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
[ -f "$ROOT/deploy/x5.env" ] || { echo "先运行 bash deploy/setup_x5.sh $ROLE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ROOT/deploy/x5.env"

unit() {
  sudo tee "/etc/systemd/system/caremate-$1.service" >/dev/null <<EOF
[Unit]
Description=CareMate $1
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$ROOT/code
ExecStart=/bin/bash "$ROOT/deploy/start.sh" $1
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now "caremate-$1.service"
  echo "✓ systemd：caremate-$1   日志：journalctl -u caremate-$1 -f"
}

autostart() {
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/caremate-$1.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CareMate $1
Exec=/bin/bash -c "sleep 5; exec /bin/bash '$ROOT/deploy/start.sh' $1 >> '$HOME/caremate-$1.log' 2>&1"
X-GNOME-Autostart-enabled=true
EOF
  echo "✓ 登录后自启：caremate-$1   日志：~/caremate-$1.log"
}

case "$ROLE" in
  a) unit vision ;;
  b)
    unit master
    autostart tts
    autostart kiosk
    if [ "${WITH_ASR:-0}" = 1 ]; then autostart asr; fi ;;
  c) unit arm ;;
esac
echo "完成。重启后自动运行；现在手动看状态：systemctl status 'caremate-*'"
