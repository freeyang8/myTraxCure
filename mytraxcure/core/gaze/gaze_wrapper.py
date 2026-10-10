"""模块名称：gaze_wrapper.py

功能描述：eyetrax GazeEstimator 的薄封装，把"提特征 → 预测 → 训练 → 存取模型"收敛到同一入口，
并向上层输出有类型定义的 GazeSample / GazePoint / HeadPose，使 UI 不必直接依赖 eyetrax 内部结构。

关键约束：这里只做转发，不做平滑、滤波与坐标换算——平滑在 SmootherAdapter，
物理像素到 Qt 逻辑像素的换算在 UI 侧按 devicePixelRatio 处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from eyetrax import GazeEstimator

from mytraxcure.core.types import GazePoint, GazeSample, HeadPose


class GazeWrapper:
    """GazeEstimator 的转发层：构造即创建底层 estimator，校准模型由 load_model 从 pkl 整体替换。

    因此构造时传的 model_name 只是占位模型，真实模型取决于载入的 pkl。
    """

    def __init__(
        self,
        model_name: str = "ridge",
        model_kwargs: dict[str, Any] | None = None,
        face_landmarker_model: str | None = None,
        # 不做参数白名单：eyetrax 版本间参数名会变，这里原样透传交给它自己报错
        **estimator_kwargs: Any,
    ) -> None:
        """创建底层 GazeEstimator。

        Args:
            model_name: 传给 eyetrax 的模型名，必须是它认识的合法名字（"auto" 不合法，
                需先经 GazeConfig.runtime_model_name() 解析）。
            model_kwargs: 传给具体模型的构造参数。
            face_landmarker_model: MediaPipe face_landmarker.task 路径；
                None 表示交给 eyetrax 自动下载或读环境变量。
            **estimator_kwargs: 其余参数原样透传给 GazeEstimator。
        """
        self._estimator = GazeEstimator(
            model_name=model_name,
            model_kwargs=model_kwargs,
            face_landmarker_model=face_landmarker_model,
            **estimator_kwargs,
        )

    # ---- 特征 / 预测 ------------------------------------------------------
    def extract(self, frame: np.ndarray) -> GazeSample | None:
        """从一帧提特征并打包成 GazeSample；丢脸（features 为 None）时返回 None。

        Notes:
            yaw/pitch/roll 取特征向量的末三个分量，顺序由 eyetrax 的特征布局决定；
            blink 沿用 eyetrax 基于 EAR 历史的判定，此处不再二次过滤。
            每帧只能调用一次：extract_features 内部维护眨眼历史，重复调用会污染状态。
        """
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
        """把特征映射成屏幕坐标（主屏物理像素坐标系）。

        Notes:
            模型未训练或未载入 pkl 时底层 predict 会抛异常，此处不捕获，
            由调用方（UI 的逐帧处理）兜住并跳过该帧。
        """
        point = self._estimator.predict(np.array([sample.features]))[0]
        return GazePoint(x=float(point[0]), y=float(point[1]))

    # ---- 训练 / 持久化 ----------------------------------------------------
    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        variable_scaling: np.ndarray | None = None,
    ) -> None:
        """在底层 estimator 上训练回归模型。

        Args:
            X: 特征矩阵；y: 目标屏幕坐标（主屏物理像素）。
            variable_scaling: eyetrax 的特征缩放向量，None 表示不缩放。

        Notes:
            正常链路不走这里——校准由 calibration_runner 直接训练 estimator 后用
            save_model 落盘，UI 侧只 load_model，因此本方法目前没有调用方。
        """
        self._estimator.train(X, y, variable_scaling)

    def save_model(self, path: str | Path) -> None:
        """把当前模型（含 scaler 与模型名）序列化成 pkl，供 CalibrationStore 落盘。"""
        self._estimator.save_model(path)

    def load_model(self, path: str | Path) -> None:
        """从 pkl 载入模型，整体替换构造时的占位模型。

        Notes:
            文件缺失或版本不兼容时由底层抛异常且不在此捕获；载入成功后即切换成 pkl 里的模型。
        """
        self._estimator.load_model(path)

    # ---- 访问底层 ---------------------------------------------------------
    @property
    def estimator(self) -> GazeEstimator:
        """底层 GazeEstimator：校准与噪声微调需要直接操作它，故保留出口。"""
        return self._estimator

    def close(self) -> None:
        """释放底层资源（MediaPipe landmarker 等），退出流程里调用。"""
        self._estimator.close()
