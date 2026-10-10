"""模块名称：__init__.py（gaze_logic 包）

功能描述：凝视判定链路的包入口。GazeDetector 判断"视线是否稳定停在某个文本块上"，
RegionLocker 把判定结果换算成屏幕上的整数像素矩形，两者串起"凝视 → 文本块 → 选区"整条链路。

对外暴露：GazeDetector、GazeStability、RegionLocker。
KeyboardHandler（空格 / ESC 快捷键）同属本包但不在此转出，需按模块路径单独导入。
"""

from mytraxcure.core.gaze_logic.gaze_detector import GazeDetector, GazeStability
from mytraxcure.core.gaze_logic.region_locker import RegionLocker

__all__ = ["GazeDetector", "GazeStability", "RegionLocker"]
