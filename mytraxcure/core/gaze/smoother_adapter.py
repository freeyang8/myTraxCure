from __future__ import annotations

from typing import Any

import numpy as np
from eyetrax.filters import (
    KalmanEMASmoother,
    KalmanSmoother,
    KDESmoother,
    NoSmoother,
    make_kalman,
)

SMOOTHER_NAMES = ("none", "kalman", "kalman_ema", "kde")

class SmootherAdapter:

    def __init__(
        self,
        name: str = "kalman",
        screen_w: int = 1920,
        screen_h: int = 1080,
        ema_alpha: float = 0.25,
    ) -> None:
        if name not in SMOOTHER_NAMES:
            raise ValueError(f"unknown smoother '{name}', expect {SMOOTHER_NAMES}")
        self.name = name
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.ema_alpha = ema_alpha   # 必须在 _build 之前：kalman_ema 分支要用
        self._tuned_r: Any = None
        self._smoother: Any = self._build(name)  # eyetrax 各 smoother 的 step 形参注解不精确，鸭子类型适配层按 Any 处理
    def _build(self, name: str):
        if name == "none":
            return NoSmoother()
        if name == "kalman":
            return KalmanSmoother(make_kalman())
        if name == "kalman_ema":
            return KalmanEMASmoother(make_kalman(), ema_alpha=self.ema_alpha)
        # kde
        return KDESmoother(self.screen_w, self.screen_h, confidence=0.5)

    def step(self, x: float, y: float) -> tuple[float, float]:
        return self._smoother.step(x, y)

    def reset(self) -> None:
        self._smoother = self._build(self.name)
        kf = getattr(self._smoother, "kf", None)
        if self._tuned_r is not None and kf is not None:
            kf.measurementNoiseCov = self._tuned_r.copy()

    # ---- KDE 置信轮廓 ----------------------------------------------------
    @property
    def confidence_contours(self) -> list[np.ndarray] | None:
        debug = getattr(self._smoother, "debug", None)
        if not debug:
            return None
        return debug.get("contours")

    def tune(self, gaze_estimator, camera_index: int = 0) -> bool:
        smoother = self._smoother
        tune_fn = getattr(smoother, "tune", None)
        kf = getattr(smoother, "kf", None)
        if tune_fn is None or kf is None:
            return False   # NoSmoother / KDESmoother 没有 tune / kf
        tune_fn(gaze_estimator, camera_index=camera_index)
        # KalmanSmoother.tune 直接改 kf.measurementNoiseCov，把结果存下来供 reset 后恢复
        self._tuned_r = kf.measurementNoiseCov.copy()
        return True
