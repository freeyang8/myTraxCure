from __future__ import annotations

import ctypes
import sys
import time
from enum import Enum, auto

from eyetrax import GazeEstimator
from mytraxcure.core.gaze.screen_utils import primary_screen_size

from mytraxcure.core.config import AppConfig, CalibrationConfig, GazeConfig
from mytraxcure.core.gaze.calibration_runner import (
    CalibrationReport,
    describe_mode,
    run_calibration,
)
from mytraxcure.core.gaze.calibration_store import (
    CalibrationEnvironment,
    CalibrationStore,
)


class CalibrationOutcome(Enum):

    LOADED_EXISTING = auto()   # 沿用已有 .pkl
    NEWLY_CALIBRATED = auto()  # 本次新校准并已保存
    FALLBACK = auto()          # 无可用校准 → 鼠标降级

def _is_trained(gaze: GazeEstimator) -> bool:
    return hasattr(gaze.model.scaler, "mean_")

def _ask_recalibrate(mode: str) -> bool:
    MB_YESNO = 0x00000004
    MB_ICONQUESTION = 0x00000020
    IDYES = 6
    ans = ctypes.windll.user32.MessageBoxW(
        0,
        "检测到已有的眼动校准文件。\n是否重新校准？\n\n"
        f"[是] 重新校准（{describe_mode(mode)}）并覆盖\n"
        "[否] 使用已有校准",
        "myTraxCure 启动校准",
        MB_YESNO | MB_ICONQUESTION,
    )
    return ans == IDYES

def run_and_save(
    store: CalibrationStore,
    env: CalibrationEnvironment,
    camera_index: int,
    calibration: CalibrationConfig | None = None,
    gaze: GazeConfig | None = None,
) -> CalibrationReport | None:
    """跑一次校准并保存 pkl。返回 None = 用户取消 / 未训练出模型。"""
    calibration = calibration or CalibrationConfig()
    gaze = gaze or GazeConfig()
    estimator = GazeEstimator(model_name=gaze.runtime_model_name())
    try:
        report = run_calibration(estimator, calibration, gaze, camera_index)
        if report is None or not _is_trained(estimator):
            return None
        estimator.save_model(store.path_for(env))
        print(f"[startup_calibration] {report.summary()}", file=sys.stderr)
        return report
    finally:
        estimator.close()
        time.sleep(0.3)  # 摄像头释放后再交给 CameraThread

def ensure_calibration(config: AppConfig | None = None) -> CalibrationOutcome:
    cfg = config or AppConfig()
    sw, sh = primary_screen_size()
    env = CalibrationEnvironment(
        (sw, sh), camera_id=cfg.gaze.camera_index, user_id=cfg.user_id
    )
    store = CalibrationStore()
    has_existing = store.exists(env)

    if has_existing and not _ask_recalibrate(cfg.calibration.mode):
        return CalibrationOutcome.LOADED_EXISTING

    try:
        report = run_and_save(
            store, env, cfg.gaze.camera_index, cfg.calibration, cfg.gaze
        )
        if report is not None:
            return CalibrationOutcome.NEWLY_CALIBRATED
    except Exception as exc:  # 无摄像头 / 模型缺失 / 离线等
        print(f"[startup_calibration] 校准不可用: {exc}", file=sys.stderr)

    # 校准被取消/失败：有旧档用旧档，没旧档降级
    if has_existing:
        return CalibrationOutcome.LOADED_EXISTING
    return CalibrationOutcome.FALLBACK
