# -*- coding: utf-8 -*-
"""gen_voice_prompts.py — SAPI 生成的 WAV → voice_prompts.h(裸 PCM C 数组)
校验采样参数(16k/16bit/mono),完整解析 RIFF 块以稳妥跳头。"""
import os
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC_DIR = r"C:\Users\36197\AppData\Local\Temp\opencode\prompts"
OUT = r"D:\文档\Default Project\caremate_code\code\bedside_s3_arm_kiosk\voice_prompts.h"

PROMPTS = [
    ("P_EMPTY", "还没有新的留言哦，家人忙完就会给您回话的"),
    ("P_SENT", "您的留言已经发送出去啦"),
    ("P_START", "好的，您跟家人说吧，说完再按一下留言键"),
]


def wav_pcm(path):
    """解析 RIFF,返回 (pcm_bytes, rate, ch, bits);坏文件抛异常。尾部静音裁到 0.25s。"""
    with open(path, "rb") as f:
        d = f.read()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ValueError("not RIFF/WAVE")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(d):
        cid = d[pos:pos + 4]
        csz = struct.unpack("<I", d[pos + 4:pos + 8])[0]
        body = d[pos + 8:pos + 8 + csz]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        pos += 8 + csz + (csz & 1)          # 块按字对齐
    if not fmt or pcm is None:
        raise ValueError("missing fmt/data")
    tag, ch, rate, _, _, bits = fmt
    if tag != 1 or bits != 16 or ch != 1 or rate != 16000:
        raise ValueError("fmt mismatch tag=%d ch=%d rate=%d bits=%d" % (tag, ch, rate, bits))
    # 裁尾:最后一个 |sample|>500 的位置再留 0.25s(8000 样本)
    import array as _array
    a = _array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    last = len(a) - 1
    while last > 0 and abs(a[last]) <= 500:
        last -= 1
    keep = min(len(a), last + 1 + 8000)
    return pcm[:keep * 2], rate, ch, bits


def c_bytes(data, per_line=20):
    lines = []
    for i in range(0, len(data), per_line):
        lines.append("  " + ",".join("0x%02X" % b for b in data[i:i + per_line]) + ",")
    return "\n".join(lines)


def main():
    out = ["/* Auto generated: gen_voice_prompts.py (Windows SAPI zh-Huihui, 16k/16bit/mono) */",
           "#pragma once", "#include <pgmspace.h>", ""]
    total = 0
    for name, text in PROMPTS:
        wav = os.path.join(SRC_DIR, name + ".wav")
        pcm, rate, ch, bits = wav_pcm(wav)
        dur = len(pcm) / 32000.0
        total += len(pcm)
        print("%-8s %6.1fKB  %.1fs  %s" % (name, len(pcm) / 1024.0, dur, text))
        out.append("static const uint8_t %s_PCM[%d] PROGMEM = {" % (name, len(pcm)))
        out.append(c_bytes(pcm))
        out.append("};")
        out.append("#define %s_LEN sizeof(%s_PCM)" % (name, name))
        out.append("")
    out.append("/* total %d bytes (%.1f sec) */" % (total, total / 32000.0))
    with open(OUT, "w", encoding="ascii") as f:
        f.write("\n".join(out))
    print("OK ->", OUT, "%.1fKB PCM 合计,文件 %dKB" % (total / 1024.0, os.path.getsize(OUT) / 1024.0))


if __name__ == "__main__":
    main()