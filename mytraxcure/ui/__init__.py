"""模块名称：__init__.py

功能描述：mytraxcure.ui 包的对外门面。集中导出主窗口与六个子视图/对话框，
使 app.py 只需 `from mytraxcure.ui.main_window import MainWindow` 之类的单点引用，
不必感知 ui 包内部的模块划分。

关键约束：__all__ 是本包承诺的稳定接口，新增导出须同步维护；
导入顺序按模块名字母排，避免新增模块时的合并冲突。
"""

from mytraxcure.ui.feedback_view import FeedbackView
from mytraxcure.ui.gaze_settings_dialog import GazeSettingsDialog
from mytraxcure.ui.main_window import MainWindow
from mytraxcure.ui.overlay_view import OverlayMode, OverlayView
from mytraxcure.ui.settings_view import SettingsView
from mytraxcure.ui.viewer_view import ViewerView

__all__ = [
    "FeedbackView",
    "GazeSettingsDialog",
    "MainWindow",
    "OverlayMode",
    "OverlayView",
    "SettingsView",
    "ViewerView",
]
