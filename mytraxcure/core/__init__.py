"""core 包：与界面无关的业务内核——配置、数据模型、凝视判定、文档解析与翻译。

这里集中再导出常用的配置段与数据结构，让上层只写
`from mytraxcure.core import X`。__all__ 是手写的白名单：新增导出要同步补上，
否则 `from mytraxcure.core import *` 拿不到。
"""

from mytraxcure.core.config import (
    AppConfig,
    CalibrationConfig,
    ConfigManager,
    GazeConfig,
    RegionConfig,
    TranslationConfig,
)
from mytraxcure.core.types import (
    GazePoint,
    GazeSample,
    HeadPose,
    Region,
    TextBlock,
)

__all__ = [
    "AppConfig",
    "CalibrationConfig",
    "ConfigManager",
    "GazeConfig",
    "GazePoint",
    "GazeSample",
    "HeadPose",
    "Region",
    "RegionConfig",
    "TextBlock",
    "TranslationConfig",
]
