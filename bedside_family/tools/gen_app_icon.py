# -*- coding: utf-8 -*-
"""gen_app_icon.py — 生成 PWA 图标(紫底白气泡,192/512 两张)"""
import os
from PIL import Image, ImageDraw, ImageFont

OUT = r"C:\Users\36197\AppData\Local\Temp\opencode"
font = None
for cand in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
    if os.path.exists(cand):
        font = cand
        break

for size in (192, 512):
    img = Image.new("RGB", (size, size), (98, 63, 210))          # 紫 623FD2
    d = ImageDraw.Draw(img)
    # 白色圆角气泡
    pad = int(size * 0.16)
    d.rounded_rectangle([pad, pad, size - pad, int(size * 0.74)],
                        radius=int(size * 0.12), fill=(255, 255, 255))
    # 气泡尾巴
    cx = int(size * 0.38)
    d.polygon([(cx, int(size * 0.70)), (cx + int(size * 0.14), int(size * 0.70)),
               (cx - int(size * 0.02), int(size * 0.86))], fill=(255, 255, 255))
    # 三个紫点
    r = int(size * 0.045)
    yy = int(size * 0.45)
    for i, xxoff in enumerate((-0.18, 0, 0.18)):
        xx = int(size * 0.5 + size * xxoff)
        d.ellipse([xx - r, yy - r, xx + r, yy + r], fill=(98, 63, 210))
    # 底部小字
    if font:
        f = ImageFont.truetype(font, int(size * 0.13))
        d.text((size / 2, int(size * 0.87)), "家人留言", font=f,
               fill=(255, 255, 255), anchor="mm")
    img.save(os.path.join(OUT, "app_icon_%d.png" % size))
    print("saved app_icon_%d.png" % size)