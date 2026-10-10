"""模块名称：startup_calibration.py

功能描述：启动期校准三态决策。在 QApplication 创建之前（此时还没有 Qt 事件循环）
用 cv2 全屏窗口跑校准，产出沿用旧档 / 本次新校准 / 无可用校准三种结果之一，供 app.py 决定是否进眼动。

关键约束：
- 必须早于 Qt：校准窗口是 cv2 全屏窗口，且要独占摄像头；校准取消或失败都不阻断启动，
  只影响这次是进眼动还是降级鼠标。
- "是否重新校准"用 ctypes 调 Windows 原生的 MessageBoxW，因为此刻 Qt 对话框还不可用，
  这也使本模块仅限 Windows 使用。
"""

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
    """启动期校准结果，决定 app.py 是进眼动模式还是鼠标降级。"""

    LOADED_EXISTING = auto()   # 沿用已有 .pkl
    NEWLY_CALIBRATED = auto()  # 本次新校准并已保存（app.py 据此补跑噪声微调）
    FALLBACK = auto()          # 无可用校准 → 鼠标降级

def _is_trained(gaze: GazeEstimator) -> bool:
    """判断 estimator 里的模型是否真的训练过。

    Notes:
        以 scaler 有没有 mean_ 作为标志——eyetrax 只有训练时才会 fit scaler，
        未训练的占位模型没有该属性；比试着 predict 一次更早、也更便宜地发现问题。
    """
    return hasattr(gaze.model.scaler, "mean_")

def _ask_recalibrate(mode: str) -> bool:
    """弹 Windows 原生"是/否"对话框，问是否重做校准。

    Args:
        mode: 校准模式名，仅用于在"是"分支上显示给用户看的中文模式名。

    Returns:
        True = 用户选"是"（重新校准并覆盖已有 pkl）。

    Notes:
        走 ctypes 直接调 user32.MessageBoxW：本函数在 QApplication 创建之前执行，
        Qt 对话框在此刻不可用。
    """
    # 以下常量是 MessageBoxW 的按钮组合、图标与返回值，直接按 Win32 定义取值
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
    """跑一次校准并把模型存成 pkl。

    Args:
        store: 校准存储，决定 pkl 落在哪个目录、叫什么名字。
        env: 环境三元组（分辨率 + 摄像头 + 用户）。
        camera_index: 摄像头序号。
        calibration: 校准模式与采集参数；None 时用 CalibrationConfig 默认值。
        gaze: 回归模型名与头部姿态阈值；None 时用 GazeConfig 默认值。

    Returns:
        CalibrationReport；用户取消或模型没训练出来时返回 None。

    Notes:
        这里新建的 estimator 只是临时对象，用完即 close，主线程里的 GazeWrapper
        仍需自行 load_model 载入新 pkl；close 后要留出摄像头释放的时间（见下方 sleep）。
    """
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
        # 给摄像头留出释放时间再交给 CameraThread：两者不能同时 open_camera，
        # 0.3s 是经验缓冲，太短时后续线程可能仍打不开设备
        time.sleep(0.3)

def ensure_calibration(config: AppConfig | None = None) -> CalibrationOutcome:
    """启动期校准决策入口：有旧档先问是否重做，需要就跑校准，失败则按旧档或降级兜底。

    Args:
        config: 应用配置（摄像头序号、校准模式、用户 id）；None 时用默认 AppConfig。

    Returns:
        CalibrationOutcome：沿用已有校准 / 本次新校准并已保存 / 无可用校准。

    Notes:
        校准过程中的异常（无摄像头、模型文件缺失、离线等）只打到 stderr 不向上抛，
        再用"有没有旧档"兜底：有旧档仍按沿用旧档返回，没有才降级，保证启动不被阻断。
    """
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
