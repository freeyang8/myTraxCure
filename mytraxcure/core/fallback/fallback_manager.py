"""模块名称：fallback_manager.py

功能描述：输入源降级管理。启动时探测摄像头与 eyetrax 是否可用，可用则构造 GazeWrapper
切入眼动模式；任何一步失败都降级为鼠标模式并记下失败原因，保证无摄像头的环境
仍能完整走通阅读与翻译链路。

关键约束：
- 只负责"能不能用眼动"的模式切换，不做校准、不加载校准模型；
  校准由上层（main_window + CalibrationStore）拿到 gaze_wrapper 后自行完成。
- 摄像头探测是开一次就释放，真正采集由上层 CameraThread 重新打开；
  探测成功不等于随后一定能再次打开同一设备。
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from eyetrax.utils.video import open_camera

from mytraxcure.core.config import GazeConfig
from mytraxcure.core.gaze.gaze_wrapper import GazeWrapper


class InputMode(Enum):
    """当前视线输入源：GAZE 为摄像头眼动，MOUSE 为鼠标位置模拟视线。"""

    GAZE = "gaze"
    MOUSE = "mouse"

class FallbackManager:
    """输入源模式管理器：维护当前是眼动还是鼠标，以及最近一次降级的原因。

    生命周期：构造即鼠标模式 → try_enable_gaze() 探测成功才切 GAZE；
    运行期摄像头出错时由上层调用 switch_to_mouse() 回退。
    gaze_wrapper 只在 GAZE 模式下有效，回退后需由上层停止摄像头并丢弃它。
    """

    # 已知异常 → 用户可直接照做的修复提示。按顺序逐个 isinstance 匹配，
    # 因此更具体的异常类型必须排在更通用的类型之前
    _ERROR_HINTS: ClassVar[list[tuple[type[Exception], str]]] = [
        (FileNotFoundError, "eyetrax 模型文件缺失：请下载 face_landmarker.task 并设置 EYETRAX_FACE_LANDMARKER_MODEL"),
        (ImportError, "MediaPipe/NumPy 冲突：请 pip install 'numpy<2' 后重装 eyetrax"),
    ]

    def __init__(self) -> None:
        """以鼠标模式起步：无摄像头、未校准的场景也能立刻走通阅读与翻译。"""
        self._mode = InputMode.MOUSE  # 鼠标优先：眼动要等探测成功才切，避免启动即卡住
        self._last_reason: str | None = None
        self.gaze_wrapper = None

    # ---- 模式 ------------------------------------------------------------
    @property
    def mode(self) -> InputMode:
        """当前输入源模式。"""
        return self._mode

    @property
    def last_reason(self) -> str | None:
        """最近一次降级的原因文案（可直接展示给用户）；从未降级或已恢复眼动时为 None。"""
        return self._last_reason

    def switch_to_mouse(self, reason: str) -> None:
        """回退到鼠标模式并记录降级原因。

        Args:
            reason: 面向用户的中文说明，通常由 handle_error() 生成。

        Notes:
            只改模式与原因，不释放 gaze_wrapper；上层负责停掉摄像头并丢弃该 wrapper，
            否则会出现"已降级但仍在推理"的状态。
        """
        self._mode = InputMode.MOUSE
        self._last_reason = reason

    def try_enable_gaze(self,gaze_config=None) -> bool:
        """探测眼动链路是否可用，可用则构造 GazeWrapper 并切入眼动模式。

        Args:
            gaze_config: GazeConfig；None 时取默认配置（默认摄像头、默认模型策略）。

        Returns:
            True 表示已进入眼动模式且 gaze_wrapper 可用；
            False 表示已降级为鼠标模式，原因见 last_reason。

        Notes:
            摄像头只做开一次即释放的探测，不长期占用设备。
            GazeWrapper 用的是占位模型名，真正生效的预测模型来自上层后续
            load_model 载入的校准 pkl；本方法不加载校准，未校准也能返回 True。
        """
        cfg = gaze_config or GazeConfig()  # 未传配置时用默认配置：允许无配置文件直接探测

        # 探测摄像头：开一次确认设备可用后即释放，设备留给上层 CameraThread 独占
        try:
            cap = open_camera(cfg.camera_index)
            cap.release()  # 只探测不占用
            self.gaze_wrapper = GazeWrapper(
                # 这里只是构造占位；实际预测模型来自 load_model 载入的校准 pkl
                model_name = cfg.runtime_model_name(),
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
        """把探测期抛出的异常翻译成一句可直接展示给用户的中文原因。

        Args:
            exc: 探测过程中抛出的任意异常或 BaseException。

        Returns:
            面向用户的提示文案；没有匹配项时兜底为 "发生错误：{exc}"。

        Notes:
            RuntimeError 是否算"摄像头打不开"靠异常消息里是否含 "camera" 来判断，
            属于字符串匹配：eyetrax / OpenCV 改动报错文案或改为本地化文案后，
            这条会失配并落到兜底提示，届时应改成判断摄像头索引或错误码。
        """
        for exc_type, hint in self._ERROR_HINTS:
            if isinstance(exc, exc_type):
                return hint
        if isinstance(exc, RuntimeError) and "camera" in str(exc).lower():
            return "无法打开摄像头，已降级到鼠标模式"
        return f"发生错误：{exc}"

    def is_gaze_available(self) -> bool:
        """当前是否处于眼动模式（等价于 mode is InputMode.GAZE）。"""
        return self._mode is InputMode.GAZE
