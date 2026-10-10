"""模块名称：__init__.py

功能描述：core.gaze 包的对外门面。把校准存储与环境、GazeWrapper、平滑适配器和启动期
校准入口聚合到一处，让上层（app.py / ui）只依赖本包，不必感知内部模块划分。

关键约束：__all__ 是本包承诺的稳定接口，新增导出须同步维护。
"""

from mytraxcure.core.gaze.calibration_store import (
    CalibrationEnvironment,
    CalibrationStore,
)
from mytraxcure.core.gaze.gaze_wrapper import GazeWrapper
from mytraxcure.core.gaze.smoother_adapter import NOISE_TUNE_POINTS, SmootherAdapter
from mytraxcure.core.gaze.startup_calibration import (
    CalibrationOutcome,
    ensure_calibration,
)

__all__ = [
    "CalibrationEnvironment",
    "CalibrationOutcome",
    "CalibrationStore",
    "GazeWrapper",
    "NOISE_TUNE_POINTS",
    "SmootherAdapter",
    "ensure_calibration",
]
