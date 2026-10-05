from __future__ import annotations

import math
import time
from typing import Any

import cv2
import numpy as np
from eyetrax.calibration.common import compute_grid_points_from_shape
from eyetrax.filters import KalmanEMASmoother, make_kalman
from eyetrax.utils.screen import get_screen_size
from eyetrax.utils.video import open_camera

# ---- 噪声微调（tune）参数 --------------------------------------------------
# 本项目自建实现：eyetrax 的 KalmanSmoother.tune() 只测 3 个点，全屏方差估计太粗；
# 改 .venv 里的包会被 pip 重装冲掉，所以在这里复刻同一套逻辑、只换注视点布局。
_TUNE_GRID_ROWS = 3
_TUNE_GRID_COLS = 3
NOISE_TUNE_POINTS = _TUNE_GRID_ROWS * _TUNE_GRID_COLS  # 9：给 UI 提示用
_TUNE_WINDOW = "Fine Tuning"
_TUNE_PROXIMITY_RATIO = 1 / 5  # 命中半径 = 屏宽/5（与 eyetrax 一致）
_TUNE_INITIAL_DELAY_S = 0.5    # 命中后先等视线稳定（与 eyetrax 一致）
_TUNE_COLLECT_S = 0.5          # 每点采集时长（与 eyetrax 一致）


def _tune_points(sw: int, sh: int) -> list[tuple[int, int]]:
    """3x3 网格，边距 10%（与校准 9 点同一覆盖范围）。"""
    return compute_grid_points_from_shape(
        _TUNE_GRID_ROWS, _TUNE_GRID_COLS, sw, sh, order="serpentine"
    )


def _jitter_covariance(per_point_samples: list[list[list[float]]]) -> np.ndarray | None:
    """每点各自算方差再平均 → 测量噪声协方差（对角）。

    只统计"同一注视点内 predict 的波动"，所以不含注视点之间的间距：
    若把所有点混起来算方差，量级会被屏幕布局主导（9 点约 7e5），卡尔曼被迫重度平滑
    （实测阶跃到 90% 要 11 帧，而同点抖动对应的 R 只要 2~4 帧）。
    少于 2 帧的点算不出方差，跳过；全都不足返回 None。
    """
    per_point_vars = [
        np.var(np.asarray(samples, dtype=float), axis=0)
        for samples in per_point_samples
        if len(samples) >= 2
    ]
    if not per_point_vars:
        return None
    var = np.mean(np.asarray(per_point_vars), axis=0)
    var[var == 0] = 1e-4  # 恒定值样本方差为 0，会让卡尔曼除零/过度自信
    return np.array([[var[0], 0.0], [0.0, var[1]]], dtype=np.float32)


def tune_noise(gaze_estimator, camera_index: int = 0) -> np.ndarray | None:
    """收集各注视点附近 predict 的抖动，返回测量噪声协方差（同点内抖动）；None = 取消/样本不足。

    流程与 eyetrax 的 KalmanSmoother.tune() 相同：9 个绿点同时显示，视线进入某点
    命中半径后等 _TUNE_INITIAL_DELAY_S 稳定，再采集 _TUNE_COLLECT_S；点采够即消失。
    ESC 直接放弃（不写噪声协方差）。
    """
    sw, sh = get_screen_size()
    proximity = sw * _TUNE_PROXIMITY_RATIO
    states = [
        {"pos": pos, "start": None, "collecting": False, "collect_start": None, "samples": []}
        for pos in _tune_points(sw, sh)
    ]

    cv2.namedWindow(_TUNE_WINDOW, cv2.WND_PROP_FULLSCREEN)
    cv2.setWindowProperty(_TUNE_WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cap = open_camera(camera_index)
    per_point_samples: list[list[list[float]]] = []
    try:
        while states:
            ok, frame = cap.read()
            if not ok:
                continue
            features, blink = gaze_estimator.extract_features(frame)

            canvas = np.zeros((sh, sw, 3), dtype=np.uint8)
            for st in states:
                cv2.circle(canvas, st["pos"], 20, (0, 255, 0), -1)
            text = f"Look at the {NOISE_TUNE_POINTS} points until they disappear"
            size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.5, 2)
            cv2.putText(
                canvas, text, ((sw - size[0]) // 2, sh - 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2,
            )

            now = time.time()
            if features is not None and not blink:
                gx, gy = map(int, gaze_estimator.predict(np.array([features]))[0])
                cv2.circle(canvas, (gx, gy), 10, (255, 0, 0), -1)

                for st in states[:]:
                    px, py = st["pos"]
                    if math.hypot(gx - px, gy - py) > proximity:
                        # 视线离开：该点重新计时、丢弃半截样本
                        st.update(start=None, collecting=False, collect_start=None, samples=[])
                        continue
                    if st["start"] is None:
                        st["start"] = now
                    if not st["collecting"] and now - st["start"] >= _TUNE_INITIAL_DELAY_S:
                        st["collecting"] = True
                        st["collect_start"] = now
                    if not st["collecting"]:
                        cv2.circle(canvas, (px, py), 25, (0, 255, 255), 2)
                        continue
                    data_elapsed = now - st["collect_start"]
                    st["samples"].append([gx, gy])
                    progress = data_elapsed / _TUNE_COLLECT_S if _TUNE_COLLECT_S > 0 else 1.0
                    shake = int(5 + progress * 20)
                    cv2.circle(
                        canvas,
                        (px + int(np.random.uniform(-shake, shake)),
                         py + int(np.random.uniform(-shake, shake))),
                        20, (0, 255, 0), -1,
                    )
                    if data_elapsed >= _TUNE_COLLECT_S:
                        per_point_samples.append(st["samples"])
                        states.remove(st)

            cv2.imshow(_TUNE_WINDOW, canvas)
            if cv2.waitKey(1) == 27:
                return None
    finally:
        cap.release()
        cv2.destroyWindow(_TUNE_WINDOW)

    return _jitter_covariance(per_point_samples)


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

    def set_ema_alpha(self, alpha: float) -> None:
        """运行时改平滑强度，不重建滤波器（保留卡尔曼状态与已 tuning 的噪声协方差）。

        注意 eyetrax 的方向：out = a*prev + (1-a)*new，a 越大越平滑但越滞后。
        """
        self.ema_alpha = float(alpha)
        self._smoother.ema_alpha = float(alpha)

    def reset(self) -> None:
        self._smoother = self._build()
        if self._tuned_r is not None:
            self._smoother.kf.measurementNoiseCov = self._tuned_r.copy()

    def tune(self, gaze_estimator, camera_index: int = 0) -> bool:
        """9 点噪声微调。返回 False = 被 ESC 取消或样本不足，滤波器参数保持不变。"""
        tuned_r = tune_noise(gaze_estimator, camera_index=camera_index)
        if tuned_r is None:
            return False
        self._smoother.kf.measurementNoiseCov = tuned_r
        # 存下来供 reset() 后恢复（reset 会重建卡尔曼滤波器）
        self._tuned_r = tuned_r.copy()
        return True
