"""模块名称：settings_view.py

功能描述：设置面板，当前承载"清空译文缓存"与"清除眼动校准"两个清理入口。

关键约束：本面板只做清理，改参数走配置文件 config.local.json（眼动滤波另由
GazeSettingsDialog 提供滑条）；清理动作不可撤销，调用前应由界面层二次确认。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from mytraxcure.core.config import AppConfig, ConfigManager
from mytraxcure.core.gaze.calibration_store import CalibrationStore
from mytraxcure.core.translation.cache_store import CacheStore


class SettingsView(QWidget):
    """设置面板：持有配置与缓存的句柄，对外提供两个清理动作。

    窗口层级：独立的 QWidget，由上层按需构造；当前主窗口尚未把它挂进界面，
    两个清理方法由外部直接调用。
    状态说明：config_manager 与 _config 已持有但界面里还没有读取它们的控件——
    配置项编辑未接入，改参数现在只能直接改 config.local.json。
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        cache_store: CacheStore,
        parent: QWidget | None = None,
    ) -> None:
        """记下配置与缓存句柄；面板自身不读配置，清理时才用到这两个对象。

        Args:
            config_manager: 配置读写入口，供后续接配置项编辑用。
            cache_store: 译文缓存库，clear_cache() 直接清它。
            parent: 父控件，可为 None。
        """
        super().__init__(parent)
        self.config_manager = config_manager
        self.cache_store = cache_store
        self._config: AppConfig | None = None

    # ---- 清理入口 --------------------------------------------------------
    def clear_cache(self) -> None:
        """清空整张译文缓存表（data/cache.db）。

        Notes:
            不可撤销：清掉后预翻译要重跑一遍，本地模型会重新占用一段时间。
            缓存没有过期时间与条数上限，这是当前唯一的回收手段。
        """
        self.cache_store.clear_all()

    def clear_calibration(self) -> None:
        """删除 data/calibration 下所有校准 pkl，不分用户与环境。

        Notes:
            清完下次启动会重新走校准流程；正在眼动模式里用的那份模型不受影响，
            但已经载入内存、不会被这里删掉。
        """
        CalibrationStore().clear()
