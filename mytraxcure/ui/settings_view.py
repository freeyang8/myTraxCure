from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from mytraxcure.core.config import AppConfig, ConfigManager
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

    # ---- 装配 ------------------------------------------------------------
    def load(self, config: AppConfig) -> None:
        self._config = config
        raise NotImplementedError("TODO: 构建表单并回填配置")

    def apply(self) -> None:
        raise NotImplementedError("TODO: 读取表单 → 更新 AppConfig → 保存")

    # ---- 清理入口 --------------------------------------------------------
    def clear_cache(self) -> None:
        self.cache_store.clear_all()

    def clear_calibration(self) -> None:
        from mytraxcure.core.gaze.calibration_store import CalibrationStore

        CalibrationStore().clear()

    def clear_all(self) -> None:
        raise NotImplementedError("TODO: 清缓存 + 校准 + uploads/")
