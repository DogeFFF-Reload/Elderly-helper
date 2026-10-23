#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""phone_fall_watch.py — 手机摄像头简易视觉哨兵（CareMate 保底视觉链 v0）

链路：手机(IP Webcam) --HTTP/MJPEG单帧--> 本脚本 --POST /mock/fall--> 主脑全套跌倒闭环

角色说明（对评委口径）：
  官方视觉 = X5 板载 hobot_falldown_detection（MIPI 摄像头，骨架级）
  本脚本   = 摄像头借不到时的保底哨兵（画面能量+重心突变启发式）
           —— 只发"疑似跌倒"事件给主脑，决策永远在主脑（红线）。

用法：
  python phone_fall_watch.py --phone http://192.168.x.x:8080 \
                             --master http://127.0.0.1:8000 \
                             --thresh 18 --cooldown 30 --arm 1

参数：
  --thresh   动作能量阈值(0-100)。安静时应<10；拍手>30；假倒地>40。现场校准。
  --arm      0=只预览不触发(试机)；1=真触发
  --conn     连续多少帧超过阈值才报警(默认3, 抗抖动)
"""
import argparse
import io
import sys
import time
import urllib.request

# Pillow 是唯一必需依赖；tkinter 只是可选预览能力，不能因为系统没装 GUI
# 组件就让 X5 上的无头哨兵无法启动。
try:
    from PIL import Image, ImageChops
except ImportError:
    Image = None
    ImageChops = None

W, H = 48, 32          # 灰度降采样目标(快且够用)
GRID_TXT_W = 48

def fetch_shot(phone):
    """从 IP Webcam 拉一帧 JPEG（/shot.jpg 端点, 每次独立请求, 稳）"""
    with urllib.request.urlopen(phone.rstrip("/") + "/shot.jpg", timeout=5) as r:
        return r.read()

def to_small_gray(jpeg_bytes):
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
    return img.resize((W, H))

def energy_profile(g1, g2):
    """返回 (动作能量0-100, 纵向重心0-1(0=顶部,1=底部), ASCII 预览行列表)"""
    diff = ImageChops.difference(g1, g2)
    px = list(diff.getdata())
    tot = sum(px)
    energy = min(100.0, tot / (len(px) * 12))          # 经验缩放
    # 纵向重心：diff 亮点的行分布
    rows = []
    for yy in range(H):
        s = sum(px[yy * W:(yy + 1) * W]) + 1e-6
        rows.append(s)
    tot_r = sum(rows)
    cy = sum(i * r for i, r in enumerate(rows)) / tot_r / (H - 1) if tot_r > 1 else 0.5
    # ASCII 预览：能量条 + 行热力
    bar = "#" * int(energy / 4)
    ascii_rows = ["%02d %s" % (yy, "|" * int(rows[yy] / max(1, max(rows)) * 30))
                  for yy in range(0, H, 4)]
    return energy, cy, ascii_rows

def post_fall(master, room="客厅", src="phone_watch"):
    """疑似跌倒上报主脑（决策链归 master, 本脚本无权直接碰机械手——红线）"""
    from urllib.parse import quote
    url = master.rstrip("/") + "/mock/fall?room=%s&src=%s" % (quote(room), quote(src))
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except Exception as e:
        print("[MASTER] 上报失败: %s" % e)
        return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True, help="IP Webcam 地址 http://ip:8080")
    ap.add_argument("--master", default="http://127.0.0.1:8000")
    ap.add_argument("--thresh", type=float, default=18.0)
    ap.add_argument("--cooldown", type=int, default=30)
    ap.add_argument("--arm", type=int, choices=(0, 1), default=0, help="0=试机预览 1=真触发")
    ap.add_argument("--conn", type=int, default=3, help="连续多少帧超过阈值才报警")
    ap.add_argument("--room", default="客厅", help="上报给主脑的房间名")
    ap.add_argument("--src", default="phone_watch", help="上报来源标记")
    ap.add_argument("--interval", type=float, default=0.5)
    a = ap.parse_args()

    if Image is None or ImageChops is None:
        print("缺 pillow：pip install pillow 后再跑"); sys.exit(1)
    if a.thresh < 0 or a.thresh > 100:
        ap.error("--thresh 必须在 0 到 100 之间")
    if a.cooldown < 0 or a.conn < 1 or a.interval <= 0:
        ap.error("--cooldown、--conn、--interval 必须为正数或零")

    print("==== CareMate 手机哨兵 v0 ====  phone=%s master=%s arm=%d" %
          (a.phone, a.master, a.arm))
    prev = None
    streak = 0
    last_fire = 0.0
    n = 0
    while True:
        t0 = time.time()
        try:
            jb = fetch_shot(a.phone)
        except Exception as e:
            print("[NET] 手机串流不通: %s (5秒后重试)" % e)
            time.sleep(5); continue
        try:
            g = to_small_gray(jb)
        except Exception:
            time.sleep(a.interval); continue
        if prev is not None:
            e_, cy, rows = energy_profile(prev, g)
            tag = ""
            n += 1
            if e_ > a.thresh:
                streak += 1
            else:
                streak = 0
            if streak >= a.conn and time.time() - last_fire > a.cooldown:
                if a.arm:
                    st = post_fall(a.master, room=a.room, src=a.src)
                    print("[%s] >>> 疑似跌倒! 已通知主脑(st=%s) 连击帧=%d 能量=%.0f 重心=%.2f"
                          % ("FALL ARMED" if a.arm else "FALL?", st, streak, e_, cy))
                    last_fire = time.time()
                else:
                    print("[FALL 试机] 能量=%.0f 重心=%.2f (arm=0 未上报)" % (e_, cy))
                streak = 0
                tag = " <<<"
            if n % 4 == 0 or tag:
                print("[M] e=%5.1f >%s 重心=%.2f %s" % (e_, bar(a.thresh), cy, tag))
        prev = g
        # 预览: 控制台轻量(每4帧打一行) —— GUI 省略保稳定
        time.sleep(max(0.05, a.interval - (time.time() - t0)))

def bar(thresh):
    k = int(thresh / 4)
    return "─" * k + ("■" if k < 25 else "●")

if __name__ == "__main__":
    main()
