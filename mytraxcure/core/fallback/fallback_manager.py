from __future__ import annotations

from enum import Enum
from typing import ClassVar

from eyetrax.utils.video import open_camera

from mytraxcure.core.config import GazeConfig
from mytraxcure.core.gaze.gaze_wrapper import GazeWrapper


class InputMode(Enum):
    GAZE = "gaze"
    MOUSE = "mouse"

class FallbackManager:

    # 异常类型 → 用户提示（需求 10.1）
    _ERROR_HINTS: ClassVar[list[tuple[type[Exception], str]]] = [
        (FileNotFoundError, "eyetrax 模型文件缺失：请下载 face_landmarker.task 并设置 EYETRAX_FACE_LANDMARKER_MODEL"),
        (ImportError, "MediaPipe/NumPy 冲突：请 pip install 'numpy<2' 后重装 eyetrax"),
    ]

    def __init__(self) -> None:
        self._mode = InputMode.MOUSE  # 默认鼠标模式，P0 即可用
        self._last_reason: str | None = None
        self.gaze_wrapper = None
        
    # ---- 模式 ------------------------------------------------------------
    @property
    def mode(self) -> InputMode:
        return self._mode
        
    @property
    def last_reason(self) -> str | None:
        return self._last_reason

    def switch_to_mouse(self, reason: str) -> None:
        self._mode = InputMode.MOUSE
        self._last_reason = reason

    def try_enable_gaze(self,gaze_config=None) -> bool:
        cfg = gaze_config or GazeConfig() #导入 GazeConfig 配置类

        #尝试开启摄像头
        try:
            cap = open_camera(cfg.camera_index) # 打开摄像头
            cap.release()  #立即释放摄像头资源,只尝试不占用
            self.gaze_wrapper = GazeWrapper(
                model_name = cfg.model_name,
                face_landmarker_model = cfg.face_landmarker_model
            )
        except Exception as exc:
            self.gaze_wrapper = None
            self.switch_to_mouse(self.handle_error(exc))
            return False
        self._mode = InputMode.GAZE
        self._last_reason = None
        return True

    # ---- 异常映射 --------------------------------------------------------
    def handle_error(self, exc: BaseException) -> str:
        for exc_type, hint in self._ERROR_HINTS:
            if isinstance(exc, exc_type): #isinstance->检查一个对象是否属于某个类型
                return hint
        if isinstance(exc, RuntimeError) and "camera" in str(exc).lower():
            return "无法打开摄像头，已降级到鼠标模式"
        return f"发生错误：{exc}"

    def is_gaze_available(self) -> bool:
        return self._mode is InputMode.GAZE
