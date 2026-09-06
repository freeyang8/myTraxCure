from __future__ import annotations


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

    # v6：Qt 创建前完成校准三态决策（cv2 全屏窗口，此时无 Qt 事件循环）
    outcome = ensure_calibration()

    app = QApplication(sys.argv)
    app.setApplicationName("myTraxCure")

    config = ConfigManager().load()
    window = MainWindow(config)
    if outcome is CalibrationOutcome.FALLBACK:
        window.statusBar().showMessage("未校准：已降级为鼠标模式（重启可重新校准）")
    window.resize(1280, 720)
    window.show()
    # 启动默认进入眼动模式；FALLBACK（无可用校准）保持鼠标降级。
    # 本次启动刚完成 9 点校准（NEWLY_CALIBRATED）→ 进眼动前先收集噪声微调滤波
    if outcome is not CalibrationOutcome.FALLBACK:
        window.start_default_mode(
            tune_noise=(outcome is CalibrationOutcome.NEWLY_CALIBRATED)
        )

    return app.exec()

if __name__ == "__main__":
    raise SystemExit(run())
