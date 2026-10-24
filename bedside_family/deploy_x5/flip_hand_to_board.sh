#!/bin/bash
# 换手交接（CH340 小模块从笔记本移到 X5 的 USB-A 后，在 X5 上跑）:
#   bash /opt/caremate/deploy/x5/flip_hand_to_board.sh
# 干活: 找 /dev/ttyUSB* -> 写进 arm 单元 -> 主脑 ARM_URL 改指本机 -> 起 arm + 重启全套
set -e
DEV=$(ls /dev/ttyUSB* 2>/dev/null | head -1 || true)
if [ -z "${DEV:-}" ]; then
    echo "NO_TTYUSB: CH340 没在板上? lsusb:"
    lsusb | head -6
    exit 1
fi
echo "serial dev: $DEV"
sed -i "s|^Environment=HAND1_PORT=.*|Environment=HAND1_PORT=$DEV|" \
    /etc/systemd/system/caremate-arm.service
sed -i "s|^Environment=ARM_URL=.*|Environment=ARM_URL=http://127.0.0.1:8013|" \
    /etc/systemd/system/caremate.service
systemctl daemon-reload
systemctl enable caremate-arm >/dev/null 2>&1
systemctl restart caremate-arm caremate
sleep 3
echo "-- arm status --"
systemctl --no-pager -l status caremate-arm | head -5
echo "-- hand via board --"
curl -s http://127.0.0.1:8013/status | head -c 400; echo
echo FLIPPED