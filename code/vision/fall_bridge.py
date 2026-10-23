"""摄像头桥（视觉同学维护；这里只实现 --mock）。

--mock   ：发送一组倒地骨架 + 触发 /mock/fall（与真实模块同一格式）
--camera ：读摄像头跑姿态模型（需 opencv + 姿态模型，视觉同学实现）
--video  ：读预录跌倒视频（Plan B，视觉同学实现）

运行：python -m vision.fall_bridge --mock --room 卧室
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request

from config import utf8_console
from gesture.fall_rules import FALLEN, is_fallen


def post(url, payload=None):
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--camera", action="store_true")
    mode.add_argument("--video")
    ap.add_argument("--room", default="卧室")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    if not args.mock:
        sys.exit("--camera / --video 由视觉模块实现（需 opencv + 姿态模型），此 mock 版只支持 --mock")

    kp = {k: list(v) for k, v in FALLEN.items()}
    post(args.server + "/api/skeleton", {"keypoints": kp, "fallen": is_fallen(FALLEN)})
    print(post(args.server + "/mock/fall?room=" + urllib.parse.quote(args.room)))


if __name__ == "__main__":
    main()
