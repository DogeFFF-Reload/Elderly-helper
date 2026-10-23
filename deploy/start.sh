#!/usr/bin/env bash
# 启动单个服务（systemd、桌面自启、手动调试都用它）：
#   bash deploy/start.sh vision|phone|rps|master|tts|asr|arm|kiosk
#   X5-A：vision、rps   X5-B：master、arm、tts、asr、kiosk
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/x5.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE：先 cp deploy/x5.env.example deploy/x5.env 再按现场网络修改" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$ENV_FILE"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
MASTER="http://${MASTER_HOST:-127.0.0.1}:8000"
export PYTHONUNBUFFERED=1
ARM_BACKEND="${ARM_BACKEND:-mock}"
MOCK_SERIAL="${MOCK_SERIAL:-1}"
RH56_REGMAP_CONFIRMED="${RH56_REGMAP_CONFIRMED:-0}"
HAND1_PORT="${HAND1_PORT:-/dev/ttyUSB0}"
HAND2_PORT="${HAND2_PORT:-/dev/ttyUSB1}"
HAND1_ID="${HAND1_ID:-1}"
HAND2_ID="${HAND2_ID:-2}"
export ARM_BACKEND MOCK_SERIAL RH56_REGMAP_CONFIRMED HAND1_PORT HAND2_PORT HAND1_ID HAND2_ID
cd "$ROOT/code"

# VISION_ARGS / ASR_ARGS 故意不加引号：里面是多个参数
# shellcheck disable=SC2086
case "${1:-}" in
  vision) exec "$PY" -m vision.fall_bridge --camera "${CAMERA_INDEX:-0}" --room "${ROOM:-卧室}" --server "$MASTER" ${VISION_ARGS:-} ;;
  phone)
    [ -n "${PHONE_URL:-}" ] || { echo "缺少 PHONE_URL（手机 IP Webcam 地址，例如 http://192.168.1.30:8080）" >&2; exit 1; }
    exec "$PY" -m camera.phone_fall_watch --phone "$PHONE_URL" --master "$MASTER" ${PHONE_ARGS:-} ;;
  rps)    exec "$PY" -m vision.rps_bridge --camera "${RPS_CAMERA_INDEX:-${CAMERA_INDEX:-0}}" --server "$MASTER" ;;
  master) CAREMATE_ARM_URL="http://${ARM_HOST:-127.0.0.1}:8013" exec "$PY" -m master_service --host 0.0.0.0 ;;
  arm)    exec "$PY" -m hand_controller.arm_service --host 0.0.0.0 ;;
  tts)
    if [ "${TTS_BACKEND:-auto}" = cmd ]; then
      exec "$PY" -m voice.tts_player --server "$MASTER" --backend cmd --cmd "$TTS_CMD"
    fi
    exec "$PY" -m voice.tts_player --server "$MASTER" --backend "${TTS_BACKEND:-auto}" ;;
  asr)    exec "$PY" -m voice.asr_bridge --server "$MASTER" ${ASR_ARGS:-} ;;
  kiosk)
    URL="http://127.0.0.1:8000/ui/monitor"
    for _ in $(seq 60); do curl -fs -o /dev/null "$URL" && break; sleep 1; done
    for b in chromium chromium-browser google-chrome firefox; do
      if command -v "$b" >/dev/null 2>&1; then
        if [ "$b" = firefox ]; then exec "$b" --kiosk "$URL"; fi
        exec "$b" --kiosk --noerrdialogs --disable-infobars --autoplay-policy=no-user-gesture-required "$URL"
      fi
    done
    echo "没找到浏览器（chromium / firefox）" >&2
    exit 1 ;;
  *) echo "用法：$0 vision|phone|rps|master|tts|asr|arm|kiosk" >&2; exit 2 ;;
esac
