"""模块名称：camera.py

功能描述：摄像头采集线程。在 QThread 子线程里循环 read 帧并 signal 回主线程，
使阻塞式读帧不拖住 UI，同时用滑动窗口统计实际帧率。

关键约束：摄像头是独占资源，本线程运行期间校准与噪声微调都不能再 open_camera；
帧以 numpy BGR 数组跨线程传递，主线程槽里用完即弃。
"""

from __future__ import annotations

import time
from collections import deque

import cv2
from eyetrax.utils.video import open_camera
from PyQt6.QtCore import QThread, pyqtSignal


class CameraThread(QThread):
    """摄像头采集线程：run() 内循环读帧 → frame_ready，stop() 置位后退出并释放摄像头。

    打开摄像头失败（RuntimeError）时只发 error_occurred 并结束线程，由调用方降级到鼠标模式。
    """

    # 跨线程通信（子线程 → 主线程）：Qt 要求信号定义在类体上，不能放进 __init__
    frame_ready = pyqtSignal(object)  # np.ndarray (BGR)
    fps_updated = pyqtSignal(float)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_index: int = 0, parent=None) -> None:
        """Args: camera_index: cv2 摄像头序号；parent: Qt 父对象，随父对象一起析构。"""
        super().__init__(parent)
        self.camera_index = camera_index
        self._running = False
        self._cap: cv2.VideoCapture | None = None
        self._frame_times: deque[float] = deque(maxlen=30)
    # 线程入口：只做采集与 emit，不在此处创建或触碰任何 Qt 控件
    def run(self) -> None:
        """循环读帧并 emit；摄像头打不开时发 error_occurred 后直接结束。

        Notes:
            读帧失败（ok=False）只跳过当前帧继续循环，不重开设备也不退出，
            这样摄像头短暂掉线后能自行恢复，代价是掉线期间会空转。
        """
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
        """置退出标志：非阻塞，不 wait()，线程会在下一次 read 返回后才真正退出。"""
        self._running = False

    # 帧率统计（在子线程内 emit，主线程只负责刷新标签）
    def _track_fps(self) -> None:
        """用最近若干帧的时间戳算帧率并 emit；样本不足 2 帧时静默返回。"""
        now = time.time()
        self._frame_times.append(now)
        if len(self._frame_times) < 2:
            return
        window = now - self._frame_times[0]

        if window > 0:
            # 窗口帧数 / 时间跨度：比逐帧间隔更抗抖动，代价是帧率变化有约一个窗口的滞后
            self.fps_updated.emit(len(self._frame_times) / window)
