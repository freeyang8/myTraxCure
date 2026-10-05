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
    """眼动滤波参数：平滑强度是主观权衡（红点跟手 ↔ 抖动），做成滑条现场试。"""

    def __init__(
        self,
        config: AppConfig,
        config_manager: ConfigManager | None = None,
        on_ema_changed: Callable[[float], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("眼动滤波设置")
        self._config = config
        self._config_manager = config_manager
        self._on_ema_changed = on_ema_changed
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
        self._value_label.setMinimumWidth(48)
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
        raw = int(round(alpha * _SCALE))
        return max(int(EMA_MIN * _SCALE), min(int(EMA_MAX * _SCALE), raw))

    @staticmethod
    def _to_alpha(raw: int) -> float:
        return raw / _SCALE

    def _on_slider_moved(self, raw: int) -> None:
        alpha = self._to_alpha(raw)
        self._value_label.setText(f"{alpha:.2f}")
        self._config.gaze.ema_alpha = alpha
        if self._on_ema_changed is not None:
            self._on_ema_changed(alpha)

    def accept(self) -> None:
        if self._config_manager is not None:
            self._config_manager.save(self._config)
        super().accept()

    def reject(self) -> None:
        # 取消 → 运行中的平滑器与内存配置都退回原值（精确值，不做滑条范围钳制）
        if self._config.gaze.ema_alpha != self._original_alpha:
            self._config.gaze.ema_alpha = self._original_alpha
            if self._on_ema_changed is not None:
                self._on_ema_changed(self._original_alpha)
        super().reject()
