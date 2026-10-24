# -*- coding: utf-8 -*-
"""Mocen_CareMate T5AI 大屏位图生成器（单源）

v0.8 视觉（10-25 重做）：深色底 + 渐变卡片按键，左边圆形图标（Windows 彩色 emoji 字体 Segoe UI Emoji 渲染），
右边大字标题 + 一行小字说明；底部语音通栏；顶部状态条（应用名 + 右侧状态胶囊）。
四个键：挥手 / 手指操 / 握手 / 零位待机；语音键；三种状态条（已连接 / 未连接 / 正在听）。

输出：bedside_t5ai_arm/t5ai_bitmaps.h（LVGL v9 RGB565 小端，勿手改）+ preview.png（整屏效果图，看完再烧）。
依赖：Pillow、Windows 字体（微软雅黑 msyh / msyhbd，Segoe UI Emoji seguiemj）。
注意：图标是 Segoe UI Emoji 渲染的位图，展示 / 比赛用没问题；要商用请换成有授权的图标。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "bedside_t5ai_arm")
OUT_H = os.path.join(OUT_DIR, "t5ai_bitmaps.h")
PREVIEW = os.path.join(HERE, "preview.png")
FONTS = r"C:\Windows\Fonts"

# ---- 配色（固件里屏幕底色用同一个 SCREEN_BG，圆角外面才不露边）----
SCREEN_BG = 0x0E1621
WHITE = (255, 255, 255)

# 布局（480x320，和固件 BTNS 表一致）：状态条 34 | 6 | 行1 100 | 6 | 行2 100 | 6 | 语音 50 | 18
BTN_W, BTN_H = 222, 100
BTN_POS = [(12, 40), (246, 40), (12, 146), (246, 146)]
VOICE_W, VOICE_H = 456, 50
VOICE_POS = (12, 252)
BAR_W, BAR_H = 480, 34

# 键：位图名 / 图标 / 标题 / 说明 / 渐变（左上 → 右下）
BTNS = [
    ("WAVE",   "\U0001F44B", "挥手",   "跟大家打招呼",  (0x14B8A6, 0x0F5F5A)),
    ("FINGER", "\U0001F590", "手指操", "十三拍 一起做",  (0xF59E0B, 0x9A4A07)),
    ("SHAKE",  "\U0001F91D", "握手",   "伸手 · 轻轻握",  (0x8B5CF6, 0x4C1D95)),
    ("ZERO",   "\U0001F3E0", "零位待机", "张开手 回零位", (0x64748B, 0x283445)),
]
VOICE = ("VOICE", "\U0001F3A4", "按一下 说话", "例：跟大家挥挥手", (0x6366F1, 0x3730A3))
BARS = [   # 状态条右侧胶囊：键 / 文案 / 胶囊色
    ("OFF",    "未连接",   0xDC2626),
    ("ON",     "已连接",   0x16A34A),
    ("LISTEN", "正在听…", 0xF59E0B),
]


def rgb(h):
    return ((h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF)


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


F_EMOJI = font("seguiemj.ttf", 109)      # 彩色 emoji 只在 109 号字有位图，画大了再缩


def emoji(ch, size):
    """彩色 emoji → size×size 的 RGBA 图标（109 号渲染，LANCZOS 缩小，边缘平滑）。"""
    big = Image.new("RGBA", (136, 136), (0, 0, 0, 0))
    ImageDraw.Draw(big).text((68, 68), ch, font=F_EMOJI, embedded_color=True, anchor="mm")
    box = big.getbbox()
    big = big.crop(box)
    s = size / max(big.size)
    return big.resize((max(1, round(big.width * s)), max(1, round(big.height * s))), Image.LANCZOS)


def gradient(w, h, c0, c1):
    """左上 c0 → 右下 c1 的对角渐变。"""
    a, b = rgb(c0), rgb(c1)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x / (w - 1) * 0.6 + y / (h - 1) * 0.4)
            px[x, y] = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return img


def card(w, h, colors, radius):
    """圆角渐变卡片，圆角外是屏幕底色；上沿一道细高光。"""
    img = Image.new("RGB", (w, h), rgb(SCREEN_BG))
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 4 - 1, h * 4 - 1], radius=radius * 4, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    img.paste(gradient(w, h, *colors), (0, 0), mask)
    hl = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(hl).rounded_rectangle([2, 2, w * 4 - 3, h * 4 - 3], radius=radius * 4, outline=255, width=4)
    hl = hl.resize((w, h), Image.LANCZOS).point(lambda v: v * 0.22)
    img.paste(Image.new("RGB", (w, h), WHITE), (0, 0), hl)
    return img


def fit_font(text, max_w, sizes, bold=True):
    for s in sizes:
        f = font("msyhbd.ttc" if bold else "msyh.ttc", s)
        if f.getlength(text) <= max_w:
            return f
    return f


def badge(img, cx, cy, r, alpha=70):
    """图标底下的半透明白色圆。"""
    m = Image.new("L", (r * 8, r * 8), 0)
    ImageDraw.Draw(m).ellipse([0, 0, r * 8 - 1, r * 8 - 1], fill=alpha)
    m = m.resize((r * 2, r * 2), Image.LANCZOS)
    img.paste(Image.new("RGB", (r * 2, r * 2), WHITE), (cx - r, cy - r), m)


def render_key(icon, title, sub, colors):
    img = card(BTN_W, BTN_H, colors, 18)
    cx, cy, r = 50, BTN_H // 2, 34
    badge(img, cx, cy, r)
    ic = emoji(icon, 46)
    img.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    d = ImageDraw.Draw(img)
    x0, maxw = 96, BTN_W - 96 - 10
    ft = fit_font(title, maxw, (38, 34, 30, 28))
    d.text((x0, 44), title, font=ft, fill=WHITE, anchor="ls")
    fs = fit_font(sub, maxw, (17, 16, 15, 14), bold=False)
    d.text((x0, 74), sub, font=fs, fill=(235, 240, 245), anchor="ls")
    return img


def render_voice(icon, title, sub, colors):
    img = card(VOICE_W, VOICE_H, colors, 25)
    badge(img, 30, VOICE_H // 2, 19)
    ic = emoji(icon, 26)
    img.paste(ic, (30 - ic.width // 2, VOICE_H // 2 - ic.height // 2), ic)
    d = ImageDraw.Draw(img)
    d.text((60, VOICE_H // 2), title, font=font("msyhbd.ttc", 26), fill=WHITE, anchor="lm")
    d.text((VOICE_W - 18, VOICE_H // 2), sub, font=font("msyh.ttc", 16), fill=(225, 228, 255), anchor="rm")
    return img


def render_bar(label, pill):
    img = Image.new("RGB", (BAR_W, BAR_H), rgb(0x0A1018))
    d = ImageDraw.Draw(img)
    d.line([(0, BAR_H - 1), (BAR_W, BAR_H - 1)], fill=rgb(0x1F2B38))
    d.text((12, BAR_H // 2), "CareMate", font=font("msyhbd.ttc", 18), fill=WHITE, anchor="lm")
    x = 12 + font("msyhbd.ttc", 18).getlength("CareMate") + 8
    d.text((x, BAR_H // 2 + 1), "床头助手", font=font("msyh.ttc", 16), fill=(148, 163, 184), anchor="lm")
    f = font("msyhbd.ttc", 15)
    tw = f.getlength(label)
    pw, ph = int(tw + 34), 24
    px0 = BAR_W - 10 - pw
    py0 = (BAR_H - ph) // 2
    m = Image.new("L", (pw * 4, ph * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, pw * 4 - 1, ph * 4 - 1], radius=ph * 2, fill=255)
    m = m.resize((pw, ph), Image.LANCZOS)
    img.paste(Image.new("RGB", (pw, ph), rgb(pill)), (px0, py0), m)
    d.ellipse([px0 + 10, BAR_H // 2 - 4, px0 + 18, BAR_H // 2 + 4], fill=WHITE)
    d.text((px0 + 24, BAR_H // 2), label, font=f, fill=WHITE, anchor="lm")
    return img


def rgb565_bytes_le(img):
    """RGB888 → LVGL v9 RGB565，小端（低字节在前，T5AI 上 LVGL 按本机小端读）。
    10-25 实测：写成大端时整屏颜色两字节对调——白 / 黑不变，渐变变彩虹条纹、绿胶囊变蓝。"""
    px = img.convert("RGB").load()
    w, h = img.size
    out = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out += bytes((v & 0xFF, (v >> 8) & 0xFF))
    return bytes(out)


def emit(lines, name, img):
    data = rgb565_bytes_le(img)
    w, h = img.size
    lines.append("static const uint8_t %s_data[%d] = {" % (name, len(data)))
    for i in range(0, len(data), 16):
        lines.append("    " + ",".join("0x%02X" % b for b in data[i:i + 16]) + ",")
    lines.append("};")
    lines.append("static const lv_image_dsc_t %s = {" % name)
    lines.append("    .header = { .cf = LV_COLOR_FORMAT_RGB565, .w = %d, .h = %d, .stride = %d, .reserved_2 = 0 }," % (w, h, w * 2))
    lines.append("    .data_size = %d," % len(data))
    lines.append("    .data = %s_data," % name)
    lines.append("};")
    lines.append("")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = ["/**",
             " * Mocen_CareMate T5AI 大屏位图 —— 由 firmware/bedside_t5ai/gen_t5ai_btns.py 自动生成，勿手改",
             " * LVGL v9 RGB565（小端字节序）；屏幕底色 SCREEN_BG 要和固件里一致",
             " */",
             "#pragma once",
             '#include "lvgl.h"',
             "#define T5AI_SCREEN_BG 0x%06X" % SCREEN_BG,
             ""]
    keys = []
    for key, icon, title, sub, colors in BTNS:
        img = render_key(icon, title, sub, colors)
        keys.append(img)
        emit(lines, "BMP_BTN_" + key, img)
    vimg = render_voice(*VOICE[1:])
    emit(lines, "BMP_BTN_" + VOICE[0], vimg)
    bars = {}
    for key, label, pill in BARS:
        bars[key] = render_bar(label, pill)
        emit(lines, "BMP_BAR_" + key, bars[key])
    with open(OUT_H, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # 整屏效果图（已连接状态）
    screen = Image.new("RGB", (480, 320), rgb(SCREEN_BG))
    screen.paste(bars["ON"], (0, 0))
    for img, pos in zip(keys, BTN_POS):
        screen.paste(img, pos)
    screen.paste(vimg, VOICE_POS)
    screen.save(PREVIEW)
    print("OK ->", OUT_H)
    print("preview ->", PREVIEW)


if __name__ == "__main__":
    main()
