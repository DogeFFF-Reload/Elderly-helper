#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""phone_fall_watch.py — 手机摄像头简易视觉哨兵（CareMate 保底视觉链 v0）

链路：手机 --HTTP JPEG/MJPEG--> 本脚本 --POST /mock/fall--> 主脑全套跌倒闭环

角色说明（对评委口径）：
  官方视觉 = X5 板载 hobot_falldown_detection（MIPI 摄像头，骨架级）
  本脚本   = 摄像头借不到时的保底哨兵（连续画面变化能量过阈值；重心仅作观察输出）
           —— 只发"疑似跌倒"事件给主脑，决策永远在主脑（红线）。

用法：
  python phone_fall_watch.py --phone http://192.168.x.x:8080 \
                             --master http://127.0.0.1:8000 \
                             --thresh 18 --cooldown 30 --arm 1

  iPhone IP Camera Lite（使用 App 实际显示的地址，不附加 /shot.jpg）：
  python phone_fall_watch.py --stream-url http://<手机主机名>.local:8081/ \
                             --username admin --password admin --arm 0 --frames 10

参数：
  --thresh   动作能量阈值。当前缩放下理论最大值为 21.25；用 --arm 0 实测后校准。
  --arm      0=只预览不触发(试机)；1=真触发
  --conn     连续多少帧超过阈值才报警(默认3, 抗抖动)
"""
import argparse
import io
import math
import os
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from .phone_source import PhoneFrameSource
else:  # Also support python code/camera/phone_fall_watch.py.
    from phone_source import PhoneFrameSource

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
    """返回 (动作能量0-21.25, 纵向重心0-1(0=顶部,1=底部), ASCII 预览行列表)"""
    diff = ImageChops.difference(g1, g2)
    pixels = getattr(diff, "get_flattened_data", diff.getdata)
    px = list(pixels())
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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--phone", help="安卓 IP Webcam 基址，自动加 /shot.jpg")
    source.add_argument("--stream-url", help="完整的 HTTP MJPEG 地址（iPhone 可用 App 首页地址）")
    ap.add_argument("--username", default=os.environ.get("PHONE_USERNAME"), help="摄像头账号")
    ap.add_argument("--password", default=os.environ.get("PHONE_PASSWORD"), help="摄像头密码，也可设置 PHONE_PASSWORD")
    ap.add_argument("--master", default="http://127.0.0.1:8000")
    ap.add_argument("--thresh", type=float, default=18.0)
    ap.add_argument("--cooldown", type=int, default=30)
    ap.add_argument("--arm", type=int, choices=(0, 1), default=0, help="0=试机预览 1=真触发")
    ap.add_argument("--conn", type=int, default=3, help="连续多少帧超过阈值才报警")
    ap.add_argument("--room", default="客厅", help="上报给主脑的房间名")
    ap.add_argument("--src", default="phone_watch", help="上报来源标记")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--frames", type=int, default=0, help="处理多少帧后退出（0=一直观察）")
    a = ap.parse_args()

    if Image is None or ImageChops is None:
        print("缺 pillow：pip install pillow 后再跑"); sys.exit(1)
    if not math.isfinite(a.thresh) or a.thresh < 0 or a.thresh > 100:
        ap.error("--thresh 必须在 0 到 100 之间")
    if a.cooldown < 0 or a.conn < 1 or not math.isfinite(a.interval) or a.interval <= 0:
        ap.error("--cooldown 必须非负；--conn 和 --interval 必须为正数")
    if a.frames < 0:
        ap.error("--frames 必须非负")
    phone_url = a.stream_url or a.phone.rstrip("/") + "/shot.jpg"
    try:
        camera = PhoneFrameSource(phone_url, mjpeg=bool(a.stream_url),
                                  username=a.username, password=a.password)
    except ValueError as exc:
        ap.error(str(exc))

    print("==== CareMate 手机哨兵 v0 ====  phone=%s master=%s arm=%d" %
          (phone_url, a.master, a.arm))
    if not a.arm:
        print("[观察模式] 只读取手机画面，不向主脑上报事件。Ctrl+C 停止。")
    frames = camera.frames()
    prev = None
    streak = 0
    last_fire = 0.0
    n = 0
    processed = 0
    next_sample = 0.0
    try:
        while True:
            t0 = time.monotonic()
            try:
                jb = next(frames)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    print("[NET] 摄像头登录失败，请核对 --username / --password。")
                else:
                    print("[NET] 摄像头返回 HTTP %s，请核对视频地址。" % exc.code)
                return 1
            except ValueError as exc:
                print("[NET] %s" % exc)
                return 1
            except Exception as exc:
                print("[NET] 手机串流不通: %s (5秒后重试)" % exc)
                frames.close()
                frames = camera.frames()
                prev = None
                streak = 0
                next_sample = 0.0
                time.sleep(5)
                continue
            # Drain the continuous stream between samples, instead of sleeping
            # and accumulating old frames in the socket buffer.
            now = time.monotonic()
            if camera.mjpeg and now < next_sample:
                continue
            next_sample = now + a.interval
            try:
                g = to_small_gray(jb)
            except Exception:
                print("[CAMERA] 收到的数据不是有效 JPEG 画面，请核对视频地址。")
                prev = None
                streak = 0
                if not camera.mjpeg:
                    time.sleep(a.interval)
                continue
            processed += 1
            if processed == 1:
                print("[CAMERA] 已收到并解码第一帧画面，正在观察。")
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
                        print("[FALL ARMED] 疑似跌倒上报结果 st=%s 连击帧=%d 能量=%.0f 重心=%.2f"
                              % (st, streak, e_, cy))
                        last_fire = time.time()
                    else:
                        print("[FALL 试机] 能量=%.0f 重心=%.2f (arm=0 未上报)" % (e_, cy))
                    streak = 0
                    tag = " <<<"
                if n % 4 == 0 or tag:
                    print("[M] e=%5.1f >%s 重心=%.2f %s" % (e_, bar(a.thresh), cy, tag))
            prev = g
            if a.frames and processed >= a.frames:
                print("[CAMERA] 已观察 %d 帧，结束。" % processed)
                return 0
            if not camera.mjpeg:
                time.sleep(max(0.05, a.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        print("\n[CAMERA] 已停止观察。")
        return 0
    finally:
        frames.close()

def bar(thresh):
    k = int(thresh / 4)
    return "─" * k + ("■" if k < 25 else "●")

if __name__ == "__main__":
    sys.exit(main())
