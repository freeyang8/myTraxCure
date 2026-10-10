"""模块名称：gaze_cursor_view.py

功能描述：注视点光标层。一个覆盖整块屏幕的顶层无边框窗口，把平滑后的注视点画成一个
半透明红点，让用户随时能看到"系统认为你在看哪里"，也是调滤波滑条时的实时参照物。

关键约束：窗口几何取 Qt 逻辑屏幕几何（不是物理分辨率），尺寸与屏幕一致，
因此调用方传入的屏幕坐标可直接当作本控件的本地坐标使用，无需再做换算。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class GazeCursorView(QWidget):
    """注视点红点层：置于所有窗口之上的顶层 QWidget，只画一个点。

    窗口层级：无父控件的 Qt 顶层窗口（parent=None），由 MainWindow 在进入眼动模式时
    构造、退出眼动模式时 close()；几何覆盖主屏整块区域，所以屏幕坐标 = 控件本地坐标。
    约束：整层对鼠标/键盘完全透明，不抢焦点，否则会把阅读区的滚轮与按键全部吃掉。
    """

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        """按给定的屏幕矩形建一个透明置顶窗口并立即显示。

        Args:
            x: 窗口左上角横坐标（Qt 逻辑像素，通常取主屏 geometry().x()）。
            y: 窗口左上角纵坐标。
            w: 窗口宽度，通常取屏幕宽度——必须覆盖整屏，注视点才不会被裁掉。
            h: 窗口高度，通常取屏幕高度。

        Notes:
            Tool 窗口类型让它不出现在任务栏；WindowTransparentForInput 与
            WA_TransparentForMouseEvents 一起保证任何输入都穿透到下层应用。
        """
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
        """更新红点位置并请求重绘。

        Args:
            x: 注视点横坐标（Qt 逻辑像素，屏幕坐标系）。
            y: 注视点纵坐标；浮点会被截断成整数像素。
        """
        self._point = QPoint(int(x), int(y))
        self.update()  # update() 只是登记重绘请求，实际绘制由 Qt 在下一帧回调 paintEvent

    def clear(self) -> None:
        """隐藏红点：丢脸、眨眼、转头超限的帧走到这里（对应 demo 里的淡出）。"""
        self._point = None
        self.update()

    def paintEvent(self, event) -> None:
        """覆写 QWidget.paintEvent：Qt 在 update() 后的下一帧调用。

        无点时不建画笔直接返回；画笔在 finally 里 end()，保证提前 return 也能成对结束。
        """
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
