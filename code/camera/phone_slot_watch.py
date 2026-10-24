#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""phone_slot_watch.py — iPhone 看取物架：每一格有没有东西（L1 固定格位取物的格位确认）

链路：iPhone --HTTP JPEG/MJPEG--> 本脚本 --POST /api/fetch/slots {"slots": {"tray": true, "1": false}}--> 主脑
只回传每格有 / 空（布尔），画面不出本机、不存盘（--snapshot / --capture-empty 是现场摆位时手动存的）。
拿不拿永远由主脑定：格位空着就不动臂；本脚本离线 3 秒，主脑按 fetch_camera 处理（默认照样拿）。

做法：每一格框一个矩形（ROI，按画面宽高的 0~1 比例写，换分辨率不用改），先拍一张"全空"的参考，
之后每帧把这一格和参考比：缩成小灰度图、先扣掉整体亮度变化，平均差值超过 --thresh 算"有东西"，
连续 --conn 帧一致才改口（抗手晃、人影一闪）。

现场三步（手机用支架固定死，挪过就重做 2、3）：
  1. 存一帧带网格的图，照着网格把每格的框写进 slots.json：
     python -m camera.phone_slot_watch --stream-url http://<iPhone>:8081/ --snapshot D:/caremate/slots_grid.jpg
  2. 架子清空，拍参考：
     python -m camera.phone_slot_watch --stream-url ... --rois D:/caremate/slots.json --capture-empty D:/caremate/slots_ref
  3. 东西放回去，开始看（--arm 0 只打印不上报，先看读数定阈值）：
     python -m camera.phone_slot_watch --stream-url ... --rois D:/caremate/slots.json --ref D:/caremate/slots_ref --arm 1

slots.json 例（名字要和 agent/fetch_items.json 的 slot 一致：tray / 1 / 2 / 3）：
  {"tray": [0.05, 0.40, 0.20, 0.30], "1": [0.30, 0.40, 0.20, 0.30], "2": [0.55, 0.40, 0.20, 0.30]}
  每格 = [左, 上, 宽, 高]
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from .phone_source import PhoneFrameSource
else:  # Also support python code/camera/phone_slot_watch.py.
    from phone_source import PhoneFrameSource

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

CELL = 24   # 每格缩成 24×24 灰度再比


def load_rois(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rois = {}
    for name, box in raw.items():
        if name.startswith("_"):
            continue
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            raise ValueError("%s 的框要写成 [左, 上, 宽, 高]（0~1 的比例）" % name)
        x, y, w, h = box
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.0001 or y + h > 1.0001:
            raise ValueError("%s 的框 %r 超出画面（每个数都是 0~1 的比例）" % (name, box))
        rois[str(name)] = (x, y, w, h)
    if not rois:
        raise ValueError("%s 里一个格位都没有" % path)
    return rois


def decode(jpeg):
    return Image.open(io.BytesIO(jpeg)).convert("L")


def crop_cell(img, box):
    W, H = img.size
    x, y, w, h = box
    region = img.crop((int(x * W), int(y * H), max(int((x + w) * W), int(x * W) + 1), max(int((y + h) * H), int(y * H) + 1)))
    return region.resize((CELL, CELL))


def _pixels(img):
    return list(getattr(img, "get_flattened_data", img.getdata)())


def cell_diff(cell, ref):
    """两格的平均差（0~255），先各自减掉平均亮度：开灯关灯、手机自动曝光不算"有东西"。"""
    a, b = _pixels(cell), _pixels(ref)
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    return sum(abs((p - ma) - (q - mb)) for p, q in zip(a, b)) / len(a)


class SlotJudge:
    """每格连续 conn 帧一致才改口；刚开始按第一帧的读数定。"""

    def __init__(self, refs, thresh, conn=3):
        self.refs, self.thresh, self.conn = refs, thresh, conn
        self.state = {}
        self.streak = {k: 0 for k in refs}

    def update(self, cells):
        scores = {}
        for name, ref in self.refs.items():
            score = cell_diff(cells[name], ref)
            scores[name] = score
            now = score > self.thresh
            if name not in self.state:
                self.state[name] = now
            elif now != self.state[name]:
                self.streak[name] += 1
                if self.streak[name] >= self.conn:
                    self.state[name], self.streak[name] = now, 0
            else:
                self.streak[name] = 0
        return dict(self.state), scores


def save_refs(img, rois, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, box in rois.items():
        crop_cell(img, box).save(os.path.join(out_dir, "slot_%s.png" % name))


def load_refs(rois, ref_dir):
    refs = {}
    for name in rois:
        path = os.path.join(ref_dir, "slot_%s.png" % name)
        if not os.path.exists(path):
            raise ValueError("没有 %s 的空架参考图 %s：先用 --capture-empty 拍一张" % (name, path))
        refs[name] = Image.open(path).convert("L").resize((CELL, CELL))
    return refs


def grid_snapshot(img, out_path, rois=None):
    """存一帧带 10% 网格的图（和已有的框），照着写 slots.json。"""
    rgb = img.convert("RGB")
    d = ImageDraw.Draw(rgb)
    W, H = rgb.size
    for i in range(1, 10):
        d.line([(W * i / 10, 0), (W * i / 10, H)], fill=(255, 255, 0), width=1)
        d.line([(0, H * i / 10), (W, H * i / 10)], fill=(255, 255, 0), width=1)
        d.text((W * i / 10 + 2, 2), "%.1f" % (i / 10), fill=(255, 255, 0))
        d.text((2, H * i / 10 + 2), "%.1f" % (i / 10), fill=(255, 255, 0))
    for name, (x, y, w, h) in (rois or {}).items():
        d.rectangle([x * W, y * H, (x + w) * W, (y + h) * H], outline=(0, 255, 0), width=3)
        d.text((x * W + 4, y * H + 4), name, fill=(0, 255, 0))
    rgb.save(out_path)


def post_slots(master, slots, src):
    body = json.dumps({"slots": slots, "src": src}).encode("utf-8")
    req = urllib.request.Request(master.rstrip("/") + "/api/fetch/slots", data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def fmt(state, scores):
    return "  ".join("%s:%s(%.0f)" % (k, "有" if state[k] else "空", scores[k]) for k in sorted(state))


def main(argv=None):
    ap = argparse.ArgumentParser(description="iPhone 看取物架每格有没有东西，回传主脑")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--stream-url", help="完整的 HTTP MJPEG 地址（iPhone IP Camera 类 App 首页显示的地址）")
    source.add_argument("--shot-url", help="单帧 JPEG 地址（每次请求一张）")
    ap.add_argument("--username", default=os.environ.get("PHONE_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("PHONE_PASSWORD"))
    ap.add_argument("--rois", help="slots.json：每格的框")
    ap.add_argument("--ref", help="空架参考图目录（--capture-empty 存的）")
    ap.add_argument("--snapshot", help="只存一帧带网格的图就退出")
    ap.add_argument("--capture-empty", metavar="DIR", help="架子清空后拍参考图存到 DIR 就退出")
    ap.add_argument("--master", default="http://127.0.0.1:8000")
    ap.add_argument("--thresh", type=float, default=14.0, help="平均差超过多少算有东西；先 --arm 0 看读数再定")
    ap.add_argument("--conn", type=int, default=3, help="连续多少帧一致才改口")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--arm", type=int, choices=(0, 1), default=0, help="0=只打印 1=回传主脑")
    ap.add_argument("--src", default="phone_slots")
    ap.add_argument("--frames", type=int, default=0, help="处理多少帧后退出（0=一直看）")
    args = ap.parse_args(argv)

    if Image is None:
        print("缺 Pillow：pip install -r requirements-phonecam.txt", file=sys.stderr)
        return 2
    try:
        rois = load_rois(args.rois) if args.rois else None
        if not args.snapshot and rois is None:
            raise ValueError("要 --rois slots.json（先 --snapshot 存一张网格图，照着写框）")
        refs = None
        if not (args.snapshot or args.capture_empty):
            if not args.ref:
                raise ValueError("要 --ref 空架参考图目录（先 --capture-empty 拍一张）")
            refs = load_refs(rois, args.ref)
        src = PhoneFrameSource(args.stream_url or args.shot_url, mjpeg=bool(args.stream_url),
                               username=args.username, password=args.password)
    except (ValueError, OSError) as e:
        print("参数有误：%s" % e, file=sys.stderr)
        return 2

    judge = SlotJudge(refs, args.thresh, args.conn) if refs else None
    seen = 0
    last_post = None
    print("==== CareMate 取物架格位确认 ====  %s  %s" % (
        args.stream_url or args.shot_url, "回传 %s" % args.master if args.arm else "只打印（--arm 0）"), flush=True)
    while True:
        try:
            next_at = 0.0
            for jpeg in src.frames():
                if args.stream_url and time.monotonic() < next_at:
                    continue        # MJPEG 一直在推：跳过的帧不解码，读走就行，免得越积越慢
                next_at = time.monotonic() + args.interval
                img = decode(jpeg)
                if args.snapshot:
                    grid_snapshot(img, args.snapshot, rois)
                    print("已存网格图 %s（%d×%d）" % (args.snapshot, img.size[0], img.size[1]))
                    return 0
                if args.capture_empty:
                    save_refs(img, rois, args.capture_empty)
                    print("已存 %d 格空架参考图到 %s" % (len(rois), args.capture_empty))
                    return 0
                state, scores = judge.update({k: crop_cell(img, b) for k, b in rois.items()})
                seen += 1
                print("[SLOTS] " + fmt(state, scores), flush=True)
                if args.arm:
                    try:
                        post_slots(args.master, state, args.src)
                        if last_post is False:
                            print("[NET] 主脑连上了", flush=True)
                        last_post = True
                    except (urllib.error.URLError, OSError, ValueError) as e:
                        if last_post is not False:
                            print("[NET] 主脑连不上：%s（继续看，连上自动恢复）" % e, flush=True)
                        last_post = False
                if args.frames and seen >= args.frames:
                    return 0
                if not args.stream_url:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
        except (urllib.error.URLError, OSError, ValueError, ConnectionError) as e:
            print("[NET] 手机画面不通：%s（3 秒后重试）" % e, flush=True)
            if args.frames and seen >= args.frames:
                return 0
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
