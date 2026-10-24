# -*- coding: utf-8 -*-
"""trim_wav.py — 裁首尾静音,各留 0.25s,输出 16k/16bit/mono WAV"""
import sys, wave

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

src, dst = sys.argv[1], sys.argv[2]
KEEP = int(16000 * 0.25)          # 两端各留 0.25s
TH = 60                           # 静音阈值(int16)

w = wave.open(src, "rb")
assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
pcm = w.readframes(w.getnframes())
w.close()

n = len(pcm) // 2
samples = [int.from_bytes(pcm[i*2:i*2+2], "little", signed=True) for i in range(n)]

first = 0
while first < n and abs(samples[first]) < TH:
    first += 1
last = n - 1
while last > first and abs(samples[last]) < TH:
    last -= 1

a = max(0, first - KEEP)
b = min(n, last + 1 + KEEP)
out = samples[a:b]
wb = wave.open(dst, "wb")
wb.setnchannels(1); wb.setsampwidth(2); wb.setframerate(16000)
wb.writeframes(b"".join(s.to_bytes(2, "little", signed=True) for s in out))
wb.close()
print("裁后 %.2fs (原 %.2fs)" % (len(out) / 16000.0, n / 16000.0))