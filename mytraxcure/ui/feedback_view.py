from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.types import Region


class FeedbackView(QWidget):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents) #让这个控件对所有鼠标事件隐身
        self._region: Region | None = None

    def show_region(self, region: Region) -> None: #激活并显示一个高亮矩形
        self._region = region
        self.update()

    def hide_region(self) -> None: #清除高亮
        self._region = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self) #创建画笔并绑定画布
        try:
            if self._region is None:
                return
            #柔和靛蓝 + 圆角 + 半透明填充：亮青蓝的方角框在暖白纸面上太扎眼
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor(37, 99, 235, 205), 3)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QColor(37, 99, 235, 26))
            r = self._region
            painter.drawRoundedRect(r.x, r.y, r.w, r.h, 12, 12) #绘制圆角矩形
        finally:
            painter.end()
