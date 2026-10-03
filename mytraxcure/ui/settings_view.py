from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from mytraxcure.core.config import AppConfig, ConfigManager
from mytraxcure.core.gaze.calibration_store import CalibrationStore
from mytraxcure.core.translation.cache_store import CacheStore


class SettingsView(QWidget):

    def __init__(
        self,
        config_manager: ConfigManager,
        cache_store: CacheStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_manager = config_manager
        self.cache_store = cache_store
        self._config: AppConfig | None = None

    # ---- 清理入口 --------------------------------------------------------
    def clear_cache(self) -> None:
        self.cache_store.clear_all()

    def clear_calibration(self) -> None:
        CalibrationStore().clear()
