"""模块名称：keyboard_handler.py

功能描述：把空格（确认 / 翻页）与 ESC（取消 / 退出）封装成 Qt 全局快捷键，
以信号形式抛给上层槽函数，省去在各个子控件里重复写按键事件过滤。

关键约束：QShortcut 注册在传入的 parent 上，只在 parent 为活动窗口时生效，
且会拦截这两个键原本到达子控件（如滚动区、输入框）的事件；
本类目前尚未被 UI 装配，上层走的是自己的键盘事件处理，接入前需确认不会重复触发。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut


class KeyboardHandler(QObject):
    """空格 / ESC 的信号封装：构造即在 parent 上注册两个快捷键，用 set_enabled 整组开关。

    信号 space_pressed、esc_pressed 在对应键被按下时发射，均不带参数。
    """

    space_pressed = pyqtSignal()
    esc_pressed = pyqtSignal()

    def __init__(self, parent: QObject) -> None:
        """注册空格与 ESC 快捷键并接到对应信号。

        Args:
            parent: 快捷键的宿主窗口或控件，同时作为本 QObject 的 Qt 父对象托管生命周期。
        """
        super().__init__(parent)
        self._space = QShortcut(QKeySequence(Qt.Key.Key_Space), parent)
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), parent)
        self._space.activated.connect(self.space_pressed.emit)
        self._esc.activated.connect(self.esc_pressed.emit)

    def set_enabled(self, enabled: bool) -> None:
        """整组启用或停用快捷键；停用期间按键不再发射信号。"""
        self._space.setEnabled(enabled)
        self._esc.setEnabled(enabled)
