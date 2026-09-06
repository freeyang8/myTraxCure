from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut


class KeyboardHandler(QObject):

    space_pressed = pyqtSignal()
    esc_pressed = pyqtSignal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._space = QShortcut(QKeySequence(Qt.Key.Key_Space), parent)
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), parent)
        self._space.activated.connect(self.space_pressed.emit)
        self._esc.activated.connect(self.esc_pressed.emit)

    def set_enabled(self, enabled: bool) -> None:
        self._space.setEnabled(enabled)
        self._esc.setEnabled(enabled)
