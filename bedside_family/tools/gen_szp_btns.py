# -*- coding: utf-8 -*-
"""Mocen_CareMate 床头屏按钮位图生成器（嘉立创实战派 S3 · ST7789 320x240 横屏）

生成 btn_bitmaps.h：
  IMG_IDLE     320x240 待机整屏（标题 + 6 按钮 + 底部提示 + 四角测试色块）
  IMG_BTN_Pxx  100x88 每个按钮按下态（变深色）
  IMG_STRIP_XX 320x34 顶部状态条（已发送√ / 没发出去× / 连WiFi中··· / 无网络）

字节序：RGB565 标准 uint16（高字节在前发送由驱动决定）。
若上屏颜色红蓝颠倒 → 把 SWAP_BYTES 改 1 重新生成。
"""
from PIL import Image, ImageDraw, ImageFont
import os

SWAP_BYTES = 1          # 现场校色开关（第2轮:屏上颜色颠倒了,打开换序）
FONT = r"C:\Windows\Fonts\simhei.ttf"

OUT = r"D:\文档\Default Project\caremate_code\code\bedside_s3_arm_kiosk\btn_bitmaps.h"

W, H = 320, 240
BTN_W, BTN_H = 100, 88
COLS_X = [4, 110, 216]
ROWS_Y = [36, 130]

BTNS = [  # (中文标签, 底色, 文字色)  手#2=情感陪伴手(10/26 分工)
    ("招手",   (255,140,0),   (255,255,255)),
    ("手指操", (229,57,53),   (255,255,255)),
    ("猜拳",   (0,137,123),   (255,255,255)),
    ("留言",   (255,193,7),   (0,0,0)),      # 点一下开录,再点一下发送
    ("听留言", (142,36,170),  (255,255,255)),  # v5.2:播最新留言/回话(顶替"归位")
    ("语音",   (30,136,229),  (255,255,255)),  # v5.2:挪到末位
]

def rgb565(r, g, b):
    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    if SWAP_BYTES:
        v = ((v & 0xFF) << 8) | (v >> 8)
    return v

def fmt_rgb565(r, f, g, b):
    return "0x%04X" % rgb565(r, g, b)

def emit_img(name, im):
    px = im.load() if hasattr(im, "load") else im
    out = []
    out.append("static const uint16_t %s[%d] PROGMEM = {" % (name, W_of * H_of if False else len(cols) * len(rows)))
    return out

def to_c_array(name, img, cols_per_line=10):
    px = img.load()
    w, h = img.size
    vals = []
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            vals.append(rgb565(r, g, b))
    lines = []
    for i in range(0, len(vals), cols_per_line):
        chunk = ",".join("0x%04X" % v for v in vals[i:i+cols_per_line])
        lines.append(chunk + ",")
    return ("static const uint16_t %s[%d] PROGMEM = {\n" % (name, len(vals))
            + "\n".join(lines)[:-1] + "\n};\n")

def make_idle():
    im = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(im)
    # 标题
    d.rectangle([0, 0, W, 30], fill=(20, 20, 20))
    f_t = ImageFont.truetype(FONT, 22)
    d.text((W//2, 15), "床头屏 · 点按键指挥机械手", font=f_t, fill=(255, 255, 255), anchor="mm")
    # 四角测试色块（校色用）：左上红/右上绿/左下蓝/右下白
    d.rectangle([0, 31, 10, 41], fill=(255, 0, 0))
    d.rectangle([W-11, 31, W-1, 41], fill=(0, 255, 0))
    d.rectangle([0, H-12, 10, H-2], fill=(0, 0, 255))
    d.rectangle([W-11, H-12, W-1, H-2], fill=(255, 255, 255))
    # 6 个按钮
    f_b = ImageFont.truetype(FONT, 30)
    for i, (label, bg, fg) in enumerate(BTNS):
        x = COLS_X[i % 3]; y = ROWS_Y[i // 3]
        d.rectangle([x, y, x+BTN_W-1, y+BTN_H-1], fill=bg, outline=(255,255,255), width=2)
        d.text((x+BTN_W//2, y+BTN_H//2), label, font=f_b, fill=fg, anchor="mm")
    # 底部提示
    d.rectangle([0, H-11, W-1, H-1], fill=(0, 0, 0))
    f_s = ImageFont.truetype(FONT, 12)
    d.text((W//2, H-6), "Mocen_CareMate v5.2 留言收发版", font=f_s, fill=(120, 120, 120), anchor="mm")
    return im

def make_pressed(idx):
    label, bg, fg = BTNS[idx]
    scale = 0.45   # 压暗
    bg2 = tuple(int(c*scale) for c in bg)
    im = Image.new("RGB", (BTN_W, BTN_H), bg2)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, BTN_W-1, BTN_H-1], outline=(200,200,200), width=2)
    f_b = ImageFont.truetype(FONT, 30)
    d.text((BTN_W//2, BTN_H//2), label, font=f_b, fill=fg, anchor="mm")
    return im

def make_strip(text, bg, fg=(255,255,255)):
    im = Image.new("RGB", (W, 34), bg)
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 22)
    d.text((W//2, 17), text, font=f, fill=fg, anchor="mm")
    return im

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="ascii") as f:
    f.write("/* Auto generated: bedside kiosk bitmaps (gen_szp_btns.py) SWAP_BYTES=%d */\n" % SWAP_BYTES)
    f.write("#pragma once\n#include <pgmspace.h>\n\n")
    f.write("/* Button rects (screen coords, landscape 320x240) */\n")
    f.write("#define BTN_W 100\n#define BTN_H 88\n")
    f.write("static const int BTN_X[6] = {%d, %d, %d, %d, %d, %d};\n" % (COLS_X[0], COLS_X[1], COLS_X[2], COLS_X[0], COLS_X[1], COLS_X[2]))
    f.write("static const int BTN_Y[6] = {%d, %d, %d, %d, %d, %d};\n" % (ROWS_Y[0], ROWS_Y[0], ROWS_Y[0], ROWS_Y[1], ROWS_Y[1], ROWS_Y[1]))
    f.write("\n/* Button action endpoints (arm_service :8013) */\n")
    f.write("static const char* BTN_EP[6] = {\n")
    for ep in ["/pose/wave", "/pose/hand_exercise", "/pose/rps_random",
               "/message", "/message_play", "/voice_cmd"]:
        f.write('  "%s",\n' % ep)
    f.write("};\n\n")
    f.write(to_c_array("IMG_IDLE", make_idle()))
    for i in range(6):
        f.write(to_c_array("IMG_BTN_P%d" % i, make_pressed(i)))
    f.write(to_c_array("IMG_STRIP_OK",   make_strip("已发送 √", (0, 150, 0))))
    f.write(to_c_array("IMG_STRIP_ERR",  make_strip("没发出去 ×（网络）", (190, 20, 20))))
    f.write(to_c_array("IMG_STRIP_WIFI", make_strip("正在连接 WiFi ···", (90, 90, 90))))
    f.write(to_c_array("IMG_STRIP_AP",   make_strip("无线没连上", (190, 120, 0))))
    f.write(to_c_array("IMG_STRIP_REC",  make_strip("正在留言 · 说完请再按一下", (21, 101, 192))))
    f.write(to_c_array("IMG_STRIP_PLAY", make_strip("正在播放留言", (0, 105, 92))))
    # 校准条（两行小字）
    im_cal = Image.new("RGB", (320, 34), (30, 60, 150))
    d = ImageDraw.Draw(im_cal)
    f1 = ImageFont.truetype(FONT, 15)
    d.text((160, 9),  "触摸校准（校完自动进入按键屏）", font=f1, fill=(255,255,255), anchor="mm")
    d.text((160, 25), "按住四角各1秒：左上 右上 左下 右下", font=f1, fill=(255,230,80), anchor="mm")
    f.write(to_c_array("IMG_STRIP_CAL", im_cal))

print("OK ->", OUT)
print("大小(字节):", os.path.getsize(OUT))