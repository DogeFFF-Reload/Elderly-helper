#!/usr/bin/env bash
# 一次性装环境（每块 X5 跑一次）：bash deploy/setup_x5.sh a|b|c
#   a = X5-A 视觉   b = X5-B 主脑 + 大屏 + 语音   c = X5-C 执行端
set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in a|b|c) ;; *) echo "用法：$0 a|b|c" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$ROOT/deploy/x5.env" ]; then
  cp "$ROOT/deploy/x5.env.example" "$ROOT/deploy/x5.env"
  echo "已生成 deploy/x5.env，记得改 MASTER_HOST / ARM_HOST"
fi
# shellcheck source=/dev/null
source "$ROOT/deploy/x5.env"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

APT=(python3-venv python3-pip curl)
case "$ROLE" in
  a) APT+=(v4l-utils libgl1 libglib2.0-0) ;;   # opencv 运行时要 libGL / glib
  b) APT+=(espeak-ng alsa-utils) ;;
esac
sudo apt-get update
sudo apt-get install -y "${APT[@]}"

python3 -m venv "$ROOT/.venv"
PIP=("$ROOT/.venv/bin/python" -m pip install -i "$PIP_INDEX")
"${PIP[@]}" --upgrade pip
case "$ROLE" in
  a) "${PIP[@]}" -r "$ROOT/code/requirements-vision.txt" ;;
  b) if [ "${WITH_ASR:-0}" = 1 ]; then "${PIP[@]}" -r "$ROOT/code/requirements-voice.txt"; fi ;;
  c) : ;;   # 目前只用标准库；RS485 实装后在这里加 pyserial
esac

echo "--- 自检 ---"
cd "$ROOT/code"
"$ROOT/.venv/bin/python" tests/run_all.py
case "$ROLE" in
  a)
    "$ROOT/.venv/bin/python" -c "import cv2, mediapipe; print('cv2', cv2.__version__, '| mediapipe', mediapipe.__version__)"
    v4l2-ctl --list-devices || true
    if ! id -nG | grep -qw video; then
      echo "提示：当前用户不在 video 组，摄像头可能打不开：sudo usermod -aG video $USER 后重新登录"
    fi ;;
  b) espeak-ng -v cmn "喇叭测试" || true ;;
esac
echo "装好了。下一步：bash deploy/install_services.sh $ROLE"
