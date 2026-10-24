#!/usr/bin/env bash
# 一次性装环境（两块 X5 各跑一次）：bash deploy/setup_x5.sh a|b
#   a = X5-A 视觉 / 手机保底哨兵   b = X5-B 主脑 + 大屏 + 语音 + 执行端（USB-RS485 连双手）
set -euo pipefail

ROLE="${1:-}"
if [ "$ROLE" = c ]; then
  echo "提示：只有两块 X5，执行端已并进 b（X5-B），按 b 装" >&2
  ROLE=b
fi
case "$ROLE" in a|b) ;; *) echo "用法：$0 a|b" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$ROOT/deploy/x5.env" ]; then
  cp "$ROOT/deploy/x5.env.example" "$ROOT/deploy/x5.env"
  echo "已生成 deploy/x5.env，记得改 MASTER_HOST（X5-B 的地址）"
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
  a)
    "${PIP[@]}" -r "$ROOT/code/requirements-vision.txt"
    # 手机哨兵只依赖 Pillow；即使 PHONE_URL 还没填，提前装好也方便现场换机位
    "${PIP[@]}" -r "$ROOT/code/requirements-phonecam.txt" ;;
  b)
     # X5-B 跑仓库的 arm_service，蓝盒在 /dev/ttyUSB*：装 pyserial，并把当前用户加进 dialout 组才打得开串口（重新登录生效）
     "${PIP[@]}" -r "$ROOT/code/requirements-hardware.txt"
     sudo usermod -aG dialout "$USER"
     if [ "${WITH_ASR:-0}" = 1 ]; then "${PIP[@]}" -r "$ROOT/code/requirements-voice.txt"; fi ;;
esac

echo "--- 自检 ---"
cd "$ROOT/code"
"$ROOT/.venv/bin/python" tests/run_all.py
case "$ROLE" in
  a)
    "$ROOT/.venv/bin/python" -c "import cv2, mediapipe, PIL; print('cv2', cv2.__version__, '| mediapipe', mediapipe.__version__, '| pillow', PIL.__version__)"
    v4l2-ctl --list-devices || true
    if ! id -nG | grep -qw video; then
      echo "提示：当前用户不在 video 组，摄像头可能打不开：sudo usermod -aG video $USER 后重新登录"
    fi ;;
  b)
    espeak-ng -v cmn "喇叭测试" || true
    if ! id -nG | grep -qw dialout; then
      echo "提示：当前用户不在 dialout 组，USB-RS485 可能打不开：sudo usermod -aG dialout $USER 后重新登录"
    fi ;;
esac
echo "装好了。下一步：bash deploy/install_services.sh $ROLE"
