from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class GazeCursorView(QWidget):

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._point: QPoint | None = None
        self.setGeometry(x, y, w, h)
        self.show()

    def set_gaze_point(self, x: float, y: float) -> None:
        self._point = QPoint(int(x), int(y))
        self.update()  # 触发 paintEvent

    def clear(self) -> None:
        self._point = None
        self.update()

    def paintEvent(self, event) -> None:
        if self._point is None:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(180, 0, 0), 2))
            painter.setBrush(QColor(255, 30, 30, 200))
            painter.drawEllipse(self._point, 8, 8)
        finally:
            painter.end()
