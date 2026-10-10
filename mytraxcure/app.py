from __future__ import annotations


def _center_on_screen(window) -> None:
    """把窗口挪到当前屏幕可用区域的中心（默认位置在左上角，阅读时偏头太累）。"""
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    window.move(geo.center() - window.rect().center())


def run() -> int:
    import sys

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from mytraxcure.core.config import ConfigManager
    from mytraxcure.ui.main_window import MainWindow

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    from mytraxcure.core.gaze.startup_calibration import (
        CalibrationOutcome,
        ensure_calibration,
    )

    # 校准模式/回归模型来自配置，先读 config.local.json（纯文件 IO，无需 Qt）
    config_manager = ConfigManager()
    config = config_manager.load()

    # v6：Qt 创建前完成校准三态决策（cv2 全屏窗口，此时无 Qt 事件循环）
    outcome = ensure_calibration(config)

    app = QApplication(sys.argv)
    app.setApplicationName("myTraxCure")

    window = MainWindow(config, config_manager)
    # 调参时一眼确认配置来源（状态栏消息后续会被校准提示覆盖，故用标题栏，持久可见）
    if config_manager.last_source != "file":
        window.setWindowTitle("myTraxCure [默认配置]")

    if outcome is CalibrationOutcome.FALLBACK:
        window.statusBar().showMessage("未校准：已降级为鼠标模式（重启可重新校准）")
    
    window.resize(1280, 787)  # 逐句步进在该窗口高度下的实测容量为 2 句/屏
    _center_on_screen(window)  # 别让它停在左上角
    window.show()
    
    # 启动默认进入眼动模式；FALLBACK（无可用校准）保持鼠标降级。
    # 本次启动刚完成校准（NEWLY_CALIBRATED）→ 进眼动前先收集噪声微调滤波
    if outcome is not CalibrationOutcome.FALLBACK:
        window.start_default_mode(
            tune_noise=(outcome is CalibrationOutcome.NEWLY_CALIBRATED)
        )

    return app.exec()
