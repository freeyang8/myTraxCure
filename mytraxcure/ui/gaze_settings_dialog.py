"""模块名称：gaze_settings_dialog.py

功能描述：眼动滤波参数的调节对话框。把 AppConfig.gaze.ema_alpha 做成一根滑条，拖动即时
生效并同步给运行中的平滑器，点确定才落盘到 config.local.json。

关键约束：
- 拖动即刻改内存配置并回调 on_ema_changed：平滑强度可接受与否因人而异，脱离"边看红点
  边调"的实时反馈，数值本身没有判断依据。
- 取消必须把内存配置与运行中的平滑器一并还原到对话框打开之前的值，否则
  "试了一下又取消"会留下副作用。
- 滑条量程 EMA_MIN~EMA_MAX 是本项目划定的安全区间，不是 eyetrax 的取值范围；
  滑到位再+、- 都不会把滤波彻底关掉。
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from mytraxcure.core.config import AppConfig, ConfigManager

# eyetrax 的 KalmanEMASmoother 公式：out = a*prev + (1-a)*new
# → a 越大越平滑但越滞后；a=0 等于关掉 EMA 只留卡尔曼
EMA_MIN = 0.10
EMA_MAX = 0.50
_SCALE = 100  # 滑条只有整数刻度，0.01 步长 → 放大 100 倍


class GazeSettingsDialog(QDialog):
    """眼动滤波参数对话框：平滑强度是主观权衡（红点跟手 ↔ 抖动），做成滑条现场试。

    覆写 accept / reject 两个槽，是 QDialogButtonBox 的标准接法：确定先落盘再关闭，
    取消先把内存配置与平滑器还原再关闭。直接绑按钮到 accept/reject 会绕过还原逻辑。
    """

    def __init__(
        self,
        config: AppConfig,
        config_manager: ConfigManager | None = None,
        on_ema_changed: Callable[[float], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """搭出滑条 + 数值标签 + 说明文案，并记下打开时的原始 alpha。

        Args:
            config: 应用配置；拖动过程中直接改它的 gaze.ema_alpha。
            config_manager: 配置管理器，仅在确定时用于落盘；传 None 表示本次改动不入磁盘。
            on_ema_changed: 每次滑条变化都调用，用来把新 alpha 推给运行中的平滑器。
            parent: 父窗口。
        """
        super().__init__(parent)
        self.setWindowTitle("眼动滤波设置")
        self._config = config
        self._config_manager = config_manager
        self._on_ema_changed = on_ema_changed
        # 取消时的还原基准：直接存精确浮点值，不走滑条量程，避免两边取值不一致
        self._original_alpha = float(config.gaze.ema_alpha)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(int(EMA_MIN * _SCALE), int(EMA_MAX * _SCALE))
        self._slider.setSingleStep(1)
        self._slider.setPageStep(5)
        self._slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider.setTickInterval(5)
        self._slider.setValue(self._to_slider(self._original_alpha))
        self._slider.valueChanged.connect(self._on_slider_moved)

        self._value_label = QLabel()
        self._value_label.setMinimumWidth(48)  # 固定宽度：数值变化时 label 宽度不跳，布局不抖
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        row = QHBoxLayout()
        row.addWidget(QLabel(f"更跟手 {EMA_MIN:.2f}"))
        row.addWidget(self._slider, 1)
        row.addWidget(QLabel(f"更平滑 {EMA_MAX:.2f}"))
        row.addWidget(self._value_label)

        hint = QLabel(
            "平滑强度 ema_alpha：向右调大，红点更稳但跟随略滞后；向左调小更跟手、抖动更明显。\n"
            "拖动即生效，可边看红点边调；确定后写入 config.local.json。"
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self._on_slider_moved(self._slider.value())  # 初始化数值标签

    # ---- 内部 ------------------------------------------------------------
    @staticmethod
    def _to_slider(alpha: float) -> int:
        """浮点 alpha → 滑条整数刻度，并钳进 EMA_MIN~EMA_MAX。

        Args:
            alpha: 平滑系数。

        Returns:
            整数刻度值。越界的配置值会被拉回量程，而不是让滑条显示到量程之外。
        """
        raw = int(round(alpha * _SCALE))
        return max(int(EMA_MIN * _SCALE), min(int(EMA_MAX * _SCALE), raw))

    @staticmethod
    def _to_alpha(raw: int) -> float:
        """滑条整数刻度 → 浮点 alpha。"""
        return raw / _SCALE

    def _on_slider_moved(self, raw: int) -> None:
        """滑条槽：更新数值标签、写内存配置并回调给平滑器。

        Args:
            raw: 滑条当前的整数刻度。
        """
        alpha = self._to_alpha(raw)
        self._value_label.setText(f"{alpha:.2f}")
        self._config.gaze.ema_alpha = alpha
        if self._on_ema_changed is not None:
            self._on_ema_changed(alpha)

    def accept(self) -> None:
        """确定：先把当前值落盘，再走 QDialog 的正常关闭流程。"""
        if self._config_manager is not None:
            self._config_manager.save(self._config)
        super().accept()

    def reject(self) -> None:
        """取消：内存配置与运行中的平滑器都退回打开对话框时的原值。

        还原用精确的 _original_alpha 而不是滑条刻度换算出的值：两者在量程边界处
        不一定相等，用后者会把一个从未被改动过的配置值写成另一个值。
        """
        # 取消 → 运行中的平滑器与内存配置都退回原值（精确值，不做滑条范围钳制）
        if self._config.gaze.ema_alpha != self._original_alpha:
            self._config.gaze.ema_alpha = self._original_alpha
            if self._on_ema_changed is not None:
                self._on_ema_changed(self._original_alpha)
        super().reject()
