"""Bounded PC/phone pose input for the isolated mock demo.

Opening devices is lazy: importing this module or showing CLI help opens nothing.
Only coordinates are persisted by the caller; frames remain in memory.
"""
import math
import os
import time


class LiveInput:
    def __init__(self, *, camera=None, phone_stream=None, username=None, password=None,
                 duration=60, fps=10, estimator_factory=None, capture_factory=None):
        if (camera is None) == (phone_stream is None):
            raise ValueError("请选择电脑摄像头或手机 MJPEG 中的一种输入")
        if camera is not None and camera < 0:
            raise ValueError("摄像头编号不得为负数")
        if not math.isfinite(duration) or not 0 < duration <= 600:
            raise ValueError("实时观察时长须大于0且不超过600秒")
        if not math.isfinite(fps) or not 0 < fps <= 60:
            raise ValueError("处理帧率须大于0且不超过60")
        self.camera, self.phone_stream = camera, phone_stream
        self.username, self.password = username, password
        self.duration, self.fps = duration, fps
        self.estimator_factory, self.capture_factory = estimator_factory, capture_factory
        self.kind = "pc_camera" if camera is not None else "phone_mjpeg"
        self.source_name = "pc_camera:%d" % camera if camera is not None else "phone:mjpeg"
        self.open_attempted = self.opened = False
        self.estimator = self.capture = self.source = None

    def __enter__(self):
        try:
            # Validate phone URLs before loading a model. Never put them in reports.
            phone = None
            if self.phone_stream is not None:
                from camera.phone_source import PhoneFrameSource
                phone = PhoneFrameSource(self.phone_stream, mjpeg=True,
                                         username=self.username, password=self.password)
            factory = self.estimator_factory
            if factory is None:
                from vision.pose_backends import PoseEstimator
                factory = PoseEstimator
            self.estimator = factory(complexity=0)
            self.open_attempted = True
            if self.camera is not None:
                factory = self.capture_factory
                if factory is None:
                    import cv2
                    factory = lambda index: cv2.VideoCapture(index, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(index)
                self.capture = factory(self.camera)
                if not self.capture.isOpened():
                    raise RuntimeError("电脑摄像头无法打开，请检查占用和摄像头编号")
                self.opened = True
                self.source = self._pc_frames()
            else:
                from vision.phone_input import phone_source
                self.source = phone_source(phone, self.estimator, duration=self.duration, fps=self.fps)
            return self
        except BaseException:
            self.close()
            raise

    def _pc_frames(self):
        started = due = time.monotonic()
        deadline = started + self.duration
        while time.monotonic() < deadline:
            remaining = min(due, deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if time.monotonic() >= deadline:
                break
            ok, frame = self.capture.read()
            received = time.monotonic()
            if not ok or frame is None:
                raise RuntimeError("电脑摄像头画面读取中断")
            if received >= deadline:
                break
            height, width = frame.shape[:2]
            yield received - started, self.estimator(frame), frame, width / height
            due = max(received + 1 / self.fps, time.monotonic())

    def __iter__(self):
        for item in self.source:
            self.opened = True
            yield item

    def close(self):
        try:
            if self.source is not None:
                self.source.close()
        finally:
            try:
                if self.capture is not None:
                    self.capture.release()
            finally:
                if self.estimator is not None:
                    self.estimator.close()
                self.source = self.capture = self.estimator = None

    def __exit__(self, *_exc):
        self.close()
