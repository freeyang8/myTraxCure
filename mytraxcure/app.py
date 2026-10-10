"""模块名称：app.py

功能描述：应用启动编排。决定"本次启动用不用眼动"，并把校准结果翻译成主窗口的
    初始状态：先落配置、再定校准三态、最后才建 Qt 与主窗口。

关键约束：三段顺序不可调换。ConfigManager 只做文件 IO，必须最先；
    ensure_calibration 用 cv2 开全屏窗口采校准，必须排在 QApplication 构造之前
    （Qt 事件循环一旦跑起来，两边会争抢前台窗口）；Qt 相关的初始化只能放最后。
    校准失败不中断启动，而是以 FALLBACK 降级为鼠标模式继续可用。
"""

from __future__ import annotations


def _center_on_screen(window) -> None:
    """把窗口移到主屏可用区的中心。

    Args:
        window: 已构造、尚未 show 的 QWidget；对齐依据是窗口矩形中心。

    Returns:
        None。取不到主屏对象时直接返回，不抛异常。

    Notes:
        用 availableGeometry 而非 geometry，才能避让任务栏占用的那条边。
        QGuiApplication 在函数内导入，使本模块在 import 阶段不加载 Qt
        （本函数只在 QApplication 创建之后被调用）。
    """
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    window.move(geo.center() - window.rect().center())


def run() -> int:
    """完成启动编排并进入 Qt 事件循环，直到主窗口退出才返回。

    Returns:
        int：QApplication.exec() 的退出码；由 main.py / __main__.py 包成
        SystemExit 交给解释器作为进程返回值。

    Notes:
        副作用：可能弹出校准全屏窗口并采集摄像头、写 data/calibration 下的
        pkl，并按校准结果决定主窗口进入眼动模式还是鼠标模式。
        函数内延迟导入 PyQt6 与 MainWindow，使"读配置 + 校准"这一段能在
        Qt 尚未初始化时跑完。
    """
    import sys

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from mytraxcure.core.config import ConfigManager
    from mytraxcure.ui.main_window import MainWindow

    # 高 DPI 缩放策略必须在 QApplication 构造之前设定，Qt 只在构造时读一次
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    from mytraxcure.core.gaze.startup_calibration import (
        CalibrationOutcome,
        ensure_calibration,
    )

    # 先读 config.local.json：校准模式和回归模型都来自它，且这一步是纯文件 IO，
    # 不依赖 Qt，正好排在 QApplication 之前
    config_manager = ConfigManager()
    config = config_manager.load()

    # 校准三态决策必须在 Qt 创建前做完：cv2 的全屏采集窗口要独占前台，
    # 此时不能有 Qt 事件循环
    outcome = ensure_calibration(config)

    app = QApplication(sys.argv)
    app.setApplicationName("myTraxCure")

    window = MainWindow(config, config_manager)
    # 用标题栏而不是状态栏标记配置来源：状态栏很快会被后续提示覆盖，标题栏持久可见
    if config_manager.last_source != "file":
        window.setWindowTitle("myTraxCure [默认配置]")

    if outcome is CalibrationOutcome.FALLBACK:
        window.statusBar().showMessage("未校准：已降级为鼠标模式（重启可重新校准）")

    # 1280x787 是 ReadingConfig 那组排版参数（字号/段间距/一屏句数）的实测基准窗口，
    # 实测容量 2 句/屏；改窗口尺寸会让那组经验值失去对应关系
    window.resize(1280, 787)
    _center_on_screen(window)  # 别让它停在左上角
    window.show()

    # 启动默认进入眼动模式；FALLBACK（无可用校准）保持鼠标降级。
    # 本次启动刚完成校准（NEWLY_CALIBRATED）→ 进眼动前先收集噪声微调滤波
    if outcome is not CalibrationOutcome.FALLBACK:
        window.start_default_mode(
            tune_noise=(outcome is CalibrationOutcome.NEWLY_CALIBRATED)
        )

    return app.exec()
