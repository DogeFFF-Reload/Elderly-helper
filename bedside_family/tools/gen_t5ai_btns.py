# -*- coding: utf-8 -*-
"""Mocen_CareMate T5AI 大屏按钮位图生成器(单源)
渲染中文按钮(220x112)+状态条(480x34)→ LVGL v9 RGB565 C 数组 → t5ai_bitmaps.h
注意:重跑会整写 t5ai_bitmaps.h,手改会被覆盖——改这里。
"""
import sys, os
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from PIL import Image, ImageDraw, ImageFont

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                       "bedside_t5ai", "bedside_t5ai_arm")
OUT_H = os.path.join(OUT_DIR, "t5ai_bitmaps.h")

# ---- 字体(Windows 自带)----
def load_font(size, bold=True):
    for name in (["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhbd.ttc"]):
        p = os.path.join(r"C:\Windows\Fonts", name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

F_CN_B = load_font(52, bold=True)    # 按钮中文(粗,大字老年人看)
F_VOICE = load_font(44, bold=True)   # 语音键文案
F_BAR_CN = load_font(20, bold=True)  # 状态条中文
F_BAR_EN = load_font(20, bold=False) # 状态条英文(左固定)

SZ_SCALE = 1  # 1x 直渲:大字无需超采样,笔画锐利(3x+LANCZOS 反而糊)

def rgb565_bytes_be(img):
    """RGB888 图 → LVGL v9 RGB565 大端字节序(lv_image 约定:R 高位在前)"""
    px = img.convert("RGB").load()
    w, h = img.size
    out = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out.append((v >> 8) & 0xFF)  # 大端:高字节(R 通道位)在前
            out.append(v & 0xFF)
    return bytes(out)

def render_btn(label, bg_hex, border_hex=0x333333, w=222, h=100, radius=14, font=None):
    """按钮:底色圆角矩形+边框+居中白色中文"""
    W, H = w * SZ_SCALE, h * SZ_SCALE
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = ((bg_hex >> 16) & 0xFF, (bg_hex >> 8) & 0xFF, bg_hex & 0xFF)
    bd = ((border_hex >> 16) & 0xFF, (border_hex >> 8) & 0xFF, border_hex & 0xFF)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius * SZ_SCALE, fill=bg, outline=bd, width=2 * SZ_SCALE)
    d.text((W // 2, H // 2), label, font=font or F_CN_B, fill=(255, 255, 255), anchor="mm")
    return img if SZ_SCALE == 1 else img.resize((w, h), Image.LANCZOS)

def render_bar(right_label, bg_hex, w=480, h=34):
    """状态条:左固定 'Mocen CareMate T5AI',右侧状态中文"""
    W, H = w * SZ_SCALE, h * SZ_SCALE
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = ((bg_hex >> 16) & 0xFF, (bg_hex >> 8) & 0xFF, bg_hex & 0xFF)
    d.rectangle([0, 0, W - 1, H - 1], fill=bg)
    d.text((10 * SZ_SCALE, H // 2), "Mocen CareMate T5AI", font=F_BAR_EN, fill=(255, 255, 255), anchor="lm")
    d.text((W - 10 * SZ_SCALE, H // 2), right_label, font=F_BAR_CN, fill=(255, 255, 255), anchor="rm")
    return img.resize((w, h), Image.LANCZOS)

BTNS = [
    ("MED",    "递药盒", 0xe67e22),
    ("RETURN", "还药",   0x2980b9),
    ("SOS",    "按SOS",  0xc0392b),
    ("RESET",  "归位",   0x7f8c8d),
]
VOICE = ("VOICE", "点一下 说话", 0x8e44ad)   # 底部通栏语音键
BARS = [
    ("OFF",    "未连接", 0xc0392b),
    ("ON",     "已连接", 0x27ae60),
    ("LISTEN", "听音中", 0xe67e22),
]

# 布局(480x320):状态条 34 | 顶隙 6 | 行1 100 | 隙 6 | 行2 100 | 隙 6 | 语音条 50 | 底 18
# 列:边距 12 + 222 + 间隙 12 + 222 + 边距 12;语音键 456x50 通栏
BTN_W, BTN_H = 222, 100
BTN_POS = [(12, 40), (246, 40), (12, 146), (246, 146)]
VOICE_W, VOICE_H = 456, 50
VOICE_POS = (12, 252)

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = []
    lines.append("/**")
    lines.append(" * Mocen_CareMate T5AI 按钮位图 —— 由 tools/gen_t5ai_btns.py 自动生成,勿手改")
    lines.append(" * LVGL v9 RGB565(大端字节序,lv_image 约定)")
    lines.append(" */")
    lines.append("#pragma once")
    lines.append('#include "lvgl.h"')
    lines.append("")
    for key, label, col in BTNS:
        img = render_btn(label, col)
        data = rgb565_bytes_be(img)
        w, h = img.size
        arr_name = "BMP_BTN_" + key
        lines.append("static const uint8_t %s_data[%d] = {" % (arr_name, len(data)))
        for i in range(0, len(data), 16):
            lines.append("    " + ",".join("0x%02X" % b for b in data[i:i + 16]) + ",")
        lines.append("};")
        lines.append("static const lv_image_dsc_t %s = {" % arr_name)
        lines.append("    .header = { .cf = LV_COLOR_FORMAT_RGB565, .w = %d, .h = %d, .stride = %d, .reserved_2 = 0 }," % (w, h, w * 2))
        lines.append("    .data_size = %d," % len(data))
        lines.append("    .data = %s_data," % arr_name)
        lines.append("};")
        lines.append("")

    # 语音键(通栏)
    vimg = render_btn(VOICE[1], VOICE[2], w=VOICE_W, h=VOICE_H, font=F_VOICE)
    vdata = rgb565_bytes_be(vimg)
    vw, vh = vimg.size
    lines.append("static const uint8_t BMP_BTN_VOICE_data[%d] = {" % len(vdata))
    for i in range(0, len(vdata), 16):
        lines.append("    " + ",".join("0x%02X" % b for b in vdata[i:i + 16]) + ",")
    lines.append("};")
    lines.append("static const lv_image_dsc_t BMP_BTN_VOICE = {")
    lines.append("    .header = { .cf = LV_COLOR_FORMAT_RGB565, .w = %d, .h = %d, .stride = %d, .reserved_2 = 0 }," % (vw, vh, vw * 2))
    lines.append("    .data_size = %d," % len(vdata))
    lines.append("    .data = BMP_BTN_VOICE_data,")
    lines.append("};")
    lines.append("")
    for key, label, col in BARS:
        img = render_bar(label, col)
        data = rgb565_bytes_be(img)
        w, h = img.size
        arr_name = "BMP_BAR_" + key
        lines.append("static const uint8_t %s_data[%d] = {" % (arr_name, len(data)))
        for i in range(0, len(data), 16):
            lines.append("    " + ",".join("0x%02X" % b for b in data[i:i + 16]) + ",")
        lines.append("};")
        lines.append("static const lv_image_dsc_t %s = {" % arr_name)
        lines.append("    .header = { .cf = LV_COLOR_FORMAT_RGB565, .w = %d, .h = %d, .stride = %d, .reserved_2 = 0 }," % (w, h, w * 2))
        lines.append("    .data_size = %d," % len(data))
        lines.append("    .data = %s_data," % arr_name)
        lines.append("};")
        lines.append("")
    with open(OUT_H, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("OK ->", OUT_H)
    print("btns:", [k for k, _, _ in BTNS], "bars:", [k for k, _, _ in BARS])

if __name__ == "__main__":
    main()