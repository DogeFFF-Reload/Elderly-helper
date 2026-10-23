"""姿态 / 手部关键点（MediaPipe），统一成 fall_rules 的 14 点格式和 rps_engine 的 21 点格式。

新版 mediapipe 用 Tasks API，需要模型文件（.task），首次使用先下载（约 13MB）：
  python -m vision.pose_backends --download
X5 连不上 Google 的话，在笔记本上下载好，把 code/vision/models/ 整个拷过去。
老版 mediapipe 自带模型（mp.solutions），不用下载，这里两种都支持。

画面只在内存里处理，不保存。
"""
import argparse
import sys
import time
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent / "models"
MODELS = {
    "pose_lite": ("pose_landmarker_lite.task",
                  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"),
    "pose_full": ("pose_landmarker_full.task",
                  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"),
    "hand": ("hand_landmarker.task",
             "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"),
}

# MediaPipe Pose 33 点 → 本项目 14 点（neck 取两肩中点）
POSE_MAP = {"head": 0, "l_shoulder": 11, "r_shoulder": 12, "l_elbow": 13, "r_elbow": 14,
            "l_wrist": 15, "r_wrist": 16, "l_hip": 23, "r_hip": 24, "l_knee": 25, "r_knee": 26,
            "l_ankle": 27, "r_ankle": 28}


def to_keypoints(landmarks):
    """MediaPipe 的 landmark 列表（有 x / y / visibility 属性）→ {"head": [x, y, 可见度], ...}。"""
    kp = {}
    for name, i in POSE_MAP.items():
        lm = landmarks[i]
        kp[name] = [round(lm.x, 4), round(lm.y, 4), round(float(getattr(lm, "visibility", 1.0) or 0.0), 3)]
    ls, rs = kp["l_shoulder"], kp["r_shoulder"]
    kp["neck"] = [round((ls[0] + rs[0]) / 2, 4), round((ls[1] + rs[1]) / 2, 4), min(ls[2], rs[2])]
    return kp


def _rgb(frame_bgr):
    """OpenCV 的 BGR → RGB；用 numpy 做，后端本身不依赖 cv2（只有取摄像头画面要 cv2）。"""
    import numpy as np
    return np.ascontiguousarray(frame_bgr[:, :, ::-1])


def _legacy_solutions():
    try:
        import mediapipe as mp
        return mp.solutions if hasattr(mp, "solutions") and hasattr(mp.solutions, "pose") else None
    except (ImportError, AttributeError):
        return None


def _model_path(key):
    path = MODEL_DIR / MODELS[key][0]
    if not path.exists():
        sys.exit("缺少模型 %s。先运行：python -m vision.pose_backends --download\n"
                 "（X5 连不上外网的话，从笔记本拷 code/vision/models/ 过去）" % path)
    return str(path)


class _TasksBase:
    """Tasks API 的 VIDEO 模式要求时间戳严格递增。"""

    def __init__(self):
        self._last_ms = -1

    def _ts(self):
        ms = int(time.monotonic() * 1000)
        self._last_ms = max(ms, self._last_ms + 1)
        return self._last_ms

    @staticmethod
    def _image(frame_bgr):
        import mediapipe as mp
        return mp.Image(image_format=mp.ImageFormat.SRGB, data=_rgb(frame_bgr))


class PoseEstimator(_TasksBase):
    """estimator(frame_bgr) → 14 点 dict 或 None（没检测到人）。"""

    def __init__(self, complexity=0):
        super().__init__()
        legacy = _legacy_solutions()
        if legacy is not None:
            self._legacy = legacy.pose.Pose(model_complexity=complexity, min_detection_confidence=0.5,
                                            min_tracking_confidence=0.5)
            self.backend = "mediapipe.solutions.pose"
            return
        self._legacy = None
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision as mpv
        opts = mpv.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_model_path("pose_lite" if complexity == 0 else "pose_full")),
            running_mode=mpv.RunningMode.VIDEO, num_poses=1,
            min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5, min_tracking_confidence=0.5)
        self._tasks = mpv.PoseLandmarker.create_from_options(opts)
        self.backend = "mediapipe.tasks PoseLandmarker"

    def __call__(self, frame_bgr):
        if self._legacy is not None:
            res = self._legacy.process(_rgb(frame_bgr))
            return to_keypoints(res.pose_landmarks.landmark) if res.pose_landmarks else None
        res = self._tasks.detect_for_video(self._image(frame_bgr), self._ts())
        return to_keypoints(res.pose_landmarks[0]) if res.pose_landmarks else None

    def close(self):
        (self._legacy or self._tasks).close()


class HandEstimator(_TasksBase):
    """estimator(frame_bgr) → 21 个 [x, y, z] 或 None。"""

    def __init__(self):
        super().__init__()
        legacy = _legacy_solutions()
        if legacy is not None:
            self._legacy = legacy.hands.Hands(max_num_hands=1, min_detection_confidence=0.6,
                                              min_tracking_confidence=0.5)
            self.backend = "mediapipe.solutions.hands"
            return
        self._legacy = None
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision as mpv
        opts = mpv.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_model_path("hand")),
            running_mode=mpv.RunningMode.VIDEO, num_hands=1,
            min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
        self._tasks = mpv.HandLandmarker.create_from_options(opts)
        self.backend = "mediapipe.tasks HandLandmarker"

    def __call__(self, frame_bgr):
        if self._legacy is not None:
            res = self._legacy.process(_rgb(frame_bgr))
            hands = res.multi_hand_landmarks
            return [[p.x, p.y, p.z] for p in hands[0].landmark] if hands else None
        res = self._tasks.detect_for_video(self._image(frame_bgr), self._ts())
        return [[p.x, p.y, p.z] for p in res.hand_landmarks[0]] if res.hand_landmarks else None

    def close(self):
        (self._legacy or self._tasks).close()


def download(keys=("pose_lite", "hand")):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for key in keys:
        name, url = MODELS[key]
        dest = MODEL_DIR / name
        if dest.exists() and dest.stat().st_size > 0:
            print("已有 %s" % dest)
            continue
        print("下载 %s …" % name, flush=True)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
        print("  → %s（%.1f MB）" % (dest, dest.stat().st_size / 1e6))


def main():
    from config import utf8_console
    utf8_console()
    ap = argparse.ArgumentParser(description="MediaPipe 模型管理 / 自检")
    ap.add_argument("--download", action="store_true", help="下载姿态和手部模型到 code/vision/models/")
    ap.add_argument("--full", action="store_true", help="另外下载更准但更慢的 pose_full 模型")
    ap.add_argument("--check", action="store_true", help="加载模型并在一张空白图上跑一次")
    args = ap.parse_args()
    if args.download:
        download(("pose_lite", "hand", "pose_full") if args.full else ("pose_lite", "hand"))
    if args.check or not args.download:
        import numpy as np
        blank = np.zeros((480, 640, 3), np.uint8)
        for cls in (PoseEstimator, HandEstimator):
            est = cls()
            print("%-14s %-34s 空白图结果：%s" % (cls.__name__, est.backend, est(blank)))
            est.close()


if __name__ == "__main__":
    main()
