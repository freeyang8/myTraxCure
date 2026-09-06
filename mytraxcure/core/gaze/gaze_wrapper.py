from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from eyetrax import GazeEstimator

from mytraxcure.core.types import GazePoint, GazeSample, HeadPose


class GazeWrapper:

    def __init__(
        self,
        model_name: str = "ridge",
        model_kwargs: dict[str, Any] | None = None,
        face_landmarker_model: str | None = None,
        **estimator_kwargs: Any, #将其他参数透传（在这里解包）
    ) -> None:
        self._estimator = GazeEstimator(
            model_name=model_name,
            model_kwargs=model_kwargs,
            face_landmarker_model=face_landmarker_model,
            **estimator_kwargs,
        )

    # ---- 特征 / 预测 ------------------------------------------------------
    def extract(self, frame: np.ndarray) -> GazeSample | None:
        features, blink = self._estimator.extract_features(frame)
        if features is None:
            return None
        yaw, pitch, roll = features[-3], features[-2], features[-1]
        return GazeSample(
            features=features,
            blink=blink,
            head_pose=HeadPose(yaw=float(yaw), pitch=float(pitch), roll=float(roll)),
        )

    def predict(self, sample: GazeSample) -> GazePoint:
        point = self._estimator.predict(np.array([sample.features]))[0]
        return GazePoint(x=float(point[0]), y=float(point[1]))

    # ---- 训练 / 持久化 ----------------------------------------------------
    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        variable_scaling: np.ndarray | None = None,
    ) -> None:
        self._estimator.train(X, y, variable_scaling)

    def save_model(self, path: str | Path) -> None:
        self._estimator.save_model(path)

    def load_model(self, path: str | Path) -> None:
        self._estimator.load_model(path)

    # ---- 访问底层 ---------------------------------------------------------
    @property
    def estimator(self) -> GazeEstimator:
        return self._estimator

    def close(self) -> None:
        self._estimator.close()
