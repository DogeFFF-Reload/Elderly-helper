#!/bin/bash
# CareMate 板侧装填（在 X5 上以 root 运行）:
#   bash /opt/caremate/deploy/x5/setup_board.sh
# 干活: 装 vosk/pyserial(离线轮子) -> 装三件 systemd 单元 -> 起主脑+mic(arm 等换手)
set -e
cd /opt/caremate

echo "== 1) pip install (offline wheels; srt 只有 sdist，现场 setuptools 编装) =="
pip3 install --no-index --find-links /opt/caremate/wheels \
    vosk cffi pycparser pyserial requests idna urllib3 certifi tqdm \
    websockets srt 2>&1 | tail -2
python3 -c "import vosk, serial, srt, requests, tqdm; print('imports OK:', vosk.Model.__name__)"

echo "== 2) systemd units =="
cat > /opt/caremate/deploy/sync_time.sh <<'EOF'
#!/bin/bash
# 板钟对时：板上无 RTC 电池、无外网；开机从笔记本主脑借真时间（RNDIS 线插着才行）。
for i in 1 2 3 4 5; do
  E=$(curl -s -m 2 http://192.168.128.1:8000/now | grep -o '[0-9]\+\.[0-9]\+' | head -1)
  if [ -n "$E" ]; then date -s @$E >/dev/null 2>&1 && break; fi
  sleep 1
done
exit 0
EOF
chmod +x /opt/caremate/deploy/sync_time.sh
cat > /etc/systemd/system/caremate.service <<'EOF'
[Unit]
Description=CareMate master brain (port 8000)
After=network.target
[Service]
ExecStartPre=/bin/bash /opt/caremate/deploy/sync_time.sh
ExecStart=/usr/bin/python3 /opt/caremate/master_service.py
WorkingDirectory=/opt/caremate
Restart=on-failure
RestartSec=3
Environment=TTS_OUT=1
Environment=VOSK_MODEL_DIR=/opt/caremate/asr_model/vosk-model-small-cn-0.22
Environment=ARM_URL=http://192.168.128.1:8013
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/caremate-arm.service <<'EOF'
[Unit]
Description=CareMate arm service (RH56F2 hand via USB-RS485)
After=caremate.service
[Service]
WorkingDirectory=/opt/caremate
ExecStart=/usr/bin/python3 -m hand_controller.arm_service
Restart=on-failure
RestartSec=5
Environment=MOCK_SERIAL=0
Environment=RH56_PROTO=eb90
Environment=HAND1_PORT=/dev/ttyUSB0
Environment=RH56_FORCE=300
Environment=RH56_SPEED=600
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/caremate-mic.service <<'EOF'
[Unit]
Description=CareMate onboard mic loop (arecord -> /asr/push)
After=caremate.service
Requires=caremate.service
[Service]
ExecStart=/usr/bin/python3 /opt/caremate/asr_mic_loop.py plughw:0,0
WorkingDirectory=/opt/caremate
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable caremate caremate-mic caremate-arm >/dev/null

echo "== 3) start master + mic (arm waits for CH340) =="
systemctl restart caremate caremate-mic
sleep 3
systemctl --no-pager -l status caremate | head -3
curl -s http://127.0.0.1:8000/state | head -c 120; echo
curl -s -X POST "http://127.0.0.1:8000/asr/push?id=warm&fin=1" \
     -H "Content-Type: audio/pcm" --data-binary "" | head -c 200; echo
echo SETUP_DONE