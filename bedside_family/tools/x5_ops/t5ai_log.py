# -*- coding: utf-8 -*-
"""read serial log from T5AI log port for N seconds (boot dump)."""
import sys, time
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM10"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

try:
    ser = serial.Serial(PORT, 115200, timeout=1)
except Exception as e:
    print("open %s FAIL: %s" % (PORT, e))
    sys.exit(1)

print("listening %s for %.0fs ..." % (PORT, SECS))
t0 = time.time()
buf = b""
while time.time() - t0 < SECS:
    chunk = ser.read(4096)
    if chunk:
        buf += chunk
ser.close()
text = buf.decode("utf-8", errors="replace")
print(text[-4000:])