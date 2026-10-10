"""模块名称：feedback_view.py

功能描述：凝视选区的高亮反馈层。把 RegionLocker 算出的屏幕选区画成一个圆角高亮框，
告诉用户"当前锁定的是这一块"，按空格翻译的就是框住的内容。

关键约束：只负责画框，不参与任何命中与判定；区域坐标是父控件（阅读区）本地像素，
由 main_window 在锁定成功后传入，本层不做坐标系换算。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from mytraxcure.core.types import Region


class FeedbackView(QWidget):
    """选区高亮层：覆盖在整个阅读区之上的一层透明 QWidget，画一个圆角高亮框。

    窗口层级：由 MainWindow 以 ViewerView 为父控件构造（self.feedback = FeedbackView(self.viewer)），
    尺寸随阅读区 resize 同步，位于阅读内容之上、译文浮层之下。
    生命周期：凝视锁定成功 → show_region() 画框；视线移开或切换阅读模式 → hide_region() 清框。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """建一个无区域状态的透明高亮层。

        Args:
            parent: 父控件，实际使用中固定传 ViewerView——区域坐标是它的本地坐标。
        """
        super().__init__(parent)
        # 整层对鼠标事件隐身：它盖在阅读区上方，若吃掉事件会让滚轮滚动与鼠标模拟视线失效
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._region: Region | None = None

    def show_region(self, region: Region) -> None:
        """记录待高亮的区域并请求重绘。

        Args:
            region: 父控件本地像素坐标的矩形；None 之外的取值都会被原样画出，
                是否落在可见范围内由调用方负责（本层不裁剪）。
        """
        self._region = region
        self.update()

    def hide_region(self) -> None:
        """清除高亮：视线离开当前段落或阅读模式切换时调用。"""
        self._region = None
        self.update()

    def paintEvent(self, event) -> None:
        """覆写 QWidget.paintEvent：Qt 在控件需要重绘时（update() 后、resize、显露）调用。

        无区域时不开画笔直接返回，避免空绘制破坏 Qt 的绘制状态。
        画笔一律在 finally 里 end()：中途 return 也要成对结束，否则 Qt 会报
        "Painter ended with active painter" 一类的状态错误。
        """
        painter = QPainter(self)
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
            painter.drawRoundedRect(r.x, r.y, r.w, r.h, 12, 12)
        finally:
            painter.end()
