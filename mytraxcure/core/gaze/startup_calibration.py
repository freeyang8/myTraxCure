from __future__ import annotations

import ctypes
import sys
import time
from enum import Enum, auto

from eyetrax import GazeEstimator
from eyetrax.calibration import run_9_point_calibration
from eyetrax.utils.screen import get_screen_size

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

def _ask_recalibrate() -> bool:
    MB_YESNO = 0x00000004
    MB_ICONQUESTION = 0x00000020
    IDYES = 6
    ans = ctypes.windll.user32.MessageBoxW(
        0,
        "检测到已有的眼动校准文件。\n是否重新校准？\n\n[是] 重新校准并覆盖\n[否] 使用已有校准",
        "myTraxCure 启动校准",
        MB_YESNO | MB_ICONQUESTION,
    )
    return ans == IDYES

def run_and_save(
    store: CalibrationStore, env: CalibrationEnvironment, camera_index: int
) -> bool:
    gaze = GazeEstimator(model_name="ridge")
    try:
        run_9_point_calibration(gaze, camera_index=camera_index)
        if not _is_trained(gaze):
            return False
        gaze.save_model(store.path_for(env))
        return True
    finally:
        gaze.close()
        time.sleep(0.3)  # 摄像头释放后再交给 CameraThread

def ensure_calibration(
    user_id: str = "default", camera_index: int = 0
) -> CalibrationOutcome:
    sw, sh = get_screen_size()
    env = CalibrationEnvironment((sw, sh), camera_id=camera_index, user_id=user_id)
    store = CalibrationStore()
    has_existing = store.exists(env)

    if has_existing and not _ask_recalibrate():
        return CalibrationOutcome.LOADED_EXISTING

    try:
        if run_and_save(store, env, camera_index):
            return CalibrationOutcome.NEWLY_CALIBRATED
    except Exception as exc:  # 无摄像头 / 模型缺失 / 离线等
        print(f"[startup_calibration] 校准不可用: {exc}", file=sys.stderr)

    # 校准被取消/失败：有旧档用旧档，没旧档降级
    if has_existing:
        return CalibrationOutcome.LOADED_EXISTING
    return CalibrationOutcome.FALLBACK
