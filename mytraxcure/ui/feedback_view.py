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
            #设置画笔样式
            pen = QPen(QColor(0, 180, 255), 2) 
            painter.setPen(pen)
            color = QColor(0, 180, 255, 40)
            painter.setBrush(color)
            r = self._region
            painter.drawRect(r.x, r.y, r.w, r.h) #绘制矩形
        finally:
            painter.end()
