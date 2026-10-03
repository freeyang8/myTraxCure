from __future__ import annotations

from typing import Any

from eyetrax.filters import KalmanEMASmoother, make_kalman

class SmootherAdapter:
    """凝视点平滑：固定 kalman_ema（卡尔曼预测 + EMA 二次平滑），不提供算法切换。"""

    def __init__(self, ema_alpha: float = 0.25) -> None:
        self.ema_alpha = ema_alpha
        self._tuned_r: Any = None
        self._smoother: KalmanEMASmoother = self._build()

    def _build(self) -> KalmanEMASmoother:
        return KalmanEMASmoother(make_kalman(), ema_alpha=self.ema_alpha)

    def step(self, x: float, y: float) -> tuple[float, float]:
        return self._smoother.step(x, y)

    def reset(self) -> None:
        self._smoother = self._build()
        if self._tuned_r is not None:
            self._smoother.kf.measurementNoiseCov = self._tuned_r.copy()

    def tune(self, gaze_estimator, camera_index: int = 0) -> bool:
        self._smoother.tune(gaze_estimator, camera_index=camera_index)
        # KalmanSmoother.tune 直接改 kf.measurementNoiseCov，把结果存下来供 reset 后恢复
        self._tuned_r = self._smoother.kf.measurementNoiseCov.copy()
        return True
