from __future__ import annotations

import time
from collections import deque

import cv2
from eyetrax.utils.video import open_camera
from PyQt6.QtCore import QThread, pyqtSignal


class CameraThread(QThread):

    #跨线程通信（子线程 → 主线程）
    frame_ready = pyqtSignal(object)  # np.ndarray (BGR)
    fps_updated = pyqtSignal(float)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_index: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.camera_index = camera_index
        self._running = False
        self._cap: cv2.VideoCapture | None = None
        self._frame_times: deque[float] = deque(maxlen=30)
    #读取帧发送给主线程
    def run(self) -> None:
        try:
            self._cap = open_camera(self.camera_index)
        except RuntimeError as exc:
            self.error_occurred.emit(str(exc))
            return

        self._running = True
        while self._running:
            ok, frame = self._cap.read()
            if not ok:
                continue
            self._track_fps()
            self.frame_ready.emit(frame)

        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def stop(self) -> None:
        self._running = False

    #计算帧率
    def _track_fps(self) -> None:
        now = time.time()
        self._frame_times.append(now)
        if len(self._frame_times) < 2:
            return
        window = now - self._frame_times[0]

        if window > 0:
            #FPS = 帧数 / 时间跨度
            self.fps_updated.emit(len(self._frame_times) / window)
