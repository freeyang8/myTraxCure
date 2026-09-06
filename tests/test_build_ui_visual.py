from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 `mytraxcure` 可导入（无论从哪运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QEvent, QPointF, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QApplication

from mytraxcure.core.config import AppConfig
from mytraxcure.core.translation.translation_engine import TranslationResult
from mytraxcure.core.types import Region
from mytraxcure.ui.main_window import MainWindow
from mytraxcure.ui.overlay_view import OverlayMode, OverlayView
from mytraxcure.ui.viewer_view import ViewerView

# ---- 假文档内容（用来在 viewer 上画出"文档"的样子）------------------------
MOCK_TITLE = "myTraxCure — gaze-driven translation viewer"
MOCK_LINES = [
    "Line 1: The quick brown fox jumps over the lazy dog.",
    "Line 2: Pack my box with five dozen liquor jugs.",
    "Line 3: The five boxing wizards jump quickly.",
    "Line 4: Sphinx of black quartz, judge my vow.",
    "Line 5: How vexingly quick daft zebras jump!",
]
MOCK_NOTE = "移动鼠标 = 模拟视线（蓝色高亮跟随） | 空格 = 翻译 / 再按隐藏 | Esc = 取消"
MOCK_TRANSLATION = (
    "【示例译文】敏捷的棕色狐狸跳过了懒惰的狗。"
    "这是 mock 结果：真实链路应走 _trigger_translation → engine.translate（异步）。"
)

# 高亮矩形要盖住 Line 3：行起始 y=88、行距 28 → Line 3 的 y = 144
HIGHLIGHT_REGION = Region(x=20, y=128, w=660, h=30)

# ---- 猴补丁 1：viewer 画假文档（原方法有文档时会抛 NotImplementedError）----
def _paint_mock_viewer(self: ViewerView, event) -> None:
    painter = QPainter(self)
    painter.fillRect(self.rect(), QColor(252, 252, 248))  # 米白底

    painter.setPen(QColor(33, 33, 33))
    painter.setFont(QFont("Microsoft YaHei", 14, QFont.Weight.Bold))
    painter.drawText(24, 40, MOCK_TITLE)

    painter.setFont(QFont("Consolas", 11))
    painter.setPen(QColor(60, 60, 60))
    y = 88
    for line in MOCK_LINES:
        painter.drawText(24, y, line)
        y += 28

    painter.setPen(QColor(150, 150, 150))
    painter.setFont(QFont("Microsoft YaHei", 9))
    painter.drawText(24, y + 24, MOCK_NOTE)

# ---- 猴补丁 2：overlay 画"翻译中…"和译文 ---------------------------------
def _paint_overlay_demo(self: OverlayView, event) -> None:
    if self._region is None:
        return
    painter = QPainter(self)
    rect = QRect(self._region.x, self._region.y, self._region.w, self._region.h)

    if self._loading:  # 加载态：与 OverlayView 现有实现一致
        painter.fillRect(rect, QColor(0, 0, 0, 120))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Microsoft YaHei", 11))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), "翻译中…")
        return

    if self._result is None:
        return
    # TRANSLUCENT 模式演示：半透明深色底 + 白字译文，原文隐约可见
    painter.fillRect(rect, QColor(20, 20, 30, 175))
    painter.setPen(QColor(120, 220, 255))
    painter.setFont(QFont("Microsoft YaHei", 9))
    header = f"译文 · {self._result.backend}/{self._result.model} · {self._result.elapsed_ms}ms (mock)"
    painter.drawText(rect.adjusted(10, 8, -10, -10),
                     int(Qt.AlignmentFlag.AlignTop) | int(Qt.AlignmentFlag.AlignLeft),
                     header)
    painter.setPen(QColor(245, 245, 245))
    painter.setFont(QFont("Microsoft YaHei", 11))
    painter.drawText(rect.adjusted(10, 30, -10, -10),
                     int(Qt.TextFlag.TextWordWrap),
                     self._result.text)

def _place_region_around(window: MainWindow, cx: float, cy: float) -> None:
    vw, vh = window.viewer.width(), window.viewer.height()
    w = window.config.region.default_width   # 400，来自 RegionConfig
    h = window.config.region.default_height  # 120
    if vw >= w:
        x = max(0, min(int(cx) - w // 2, vw - w))
    else:
        x, w = 0, vw
    if vh >= h:
        y = max(0, min(int(cy) - h // 2, vh - h))
    else:
        y, h = 0, vh
    region = Region(x=x, y=y, w=w, h=h)
    window._locked_region = region
    window.feedback.show_region(region)

# ---- 猴补丁 3a：viewer 上的 hover/drag 移动 → 高亮矩形跟随（主路径）
#      viewer 已在项目里 setMouseTracking(True)，所以光标在 viewer 上"悬停"
#      就会触发本方法；overlay/feedback 设了鼠标穿透，事件也落到 viewer。
#      ——这才是"跟随"而非"点击拖动"的关键。---------------------------
def _viewer_mouse_move(self: ViewerView, event) -> None:
    pos = event.position()
    main = self.window()
    if isinstance(main, MainWindow):
        _place_region_around(main, pos.x(), pos.y())

# ---- 猴补丁 3b：MainWindow.mouseMoveEvent 的兜底路径
#      （项目里 mouseMoveEvent → on_gaze_point 是 NotImplementedError 桩；
#       这里替换为同样逻辑，避免万一在非 viewer 区域移动时崩溃）---------
def _on_gaze_point(self: MainWindow, point) -> None:
    local = self.viewer.mapFrom(self, QPointF(point.x, point.y))
    _place_region_around(self, local.x(), local.y())

# ---- 猴补丁 4：mock 异步翻译（原方法直接抛 NotImplementedError）------------
def _trigger_translation(self: MainWindow, region: Region) -> None:
    if self.overlay._loading:  # 防重入：加载中重复按空格不重复触发
        return
    self._locked_region = region
    self.overlay.show()
    self.overlay.show_loading(region)
    self._trans_label.setText("翻译: 进行中…(mock)")

    def _done() -> None:
        result = TranslationResult(
            text=MOCK_TRANSLATION,
            source="(highlighted text)",
            backend="mock",
            model="demo",
            elapsed_ms=42,
            cached=False,
        )
        self.overlay.show_translation(region, result, OverlayMode.TRANSLUCENT)
        self._trans_label.setText("翻译: 完成(mock)")

    QTimer.singleShot(900, _done)  # 模拟引擎耗时

# ---- 猴补丁 5：键盘（MainWindow 目前没有 keyPressEvent，on_space/on_esc
#      无人调用；且 hide_overlay() 不调 self.hide()，on_space 的 isVisible()
#      判断在隐藏一次后永远走 hide 分支——所以这里直接实现完整逻辑）--------
def _key_press(self: MainWindow, event) -> None:
    if event.key() == Qt.Key.Key_Space:
        if self.overlay.isVisible() and not self.overlay._loading:
            self.overlay.hide_overlay()
            self.overlay.hide()
            self._trans_label.setText("翻译: 空闲")
        elif self._locked_region is not None:
            _trigger_translation(self, self._locked_region)
    elif event.key() == Qt.Key.Key_Space:
        self.overlay.hide_overlay()
        self.overlay.hide()
        self._trans_label.setText("翻译: 已取消")

# ---- 猴补丁 6：窗口缩放时两层跟随 viewer（演示 eventFilter）---------------
def _event_filter(self: MainWindow, obj, event) -> bool:
    if obj is self.viewer and event.type() == QEvent.Type.Resize:
        sz = self.viewer.size()
        self.feedback.resize(sz)
        self.overlay.resize(sz)
    return False  # 不拦截，继续默认处理

def _install_demo_patches() -> None:
    ViewerView.paintEvent = _paint_mock_viewer  # type: ignore[assignment]
    ViewerView.mouseMoveEvent = _viewer_mouse_move  # type: ignore[assignment]
    OverlayView.paintEvent = _paint_overlay_demo  # type: ignore[assignment]
    MainWindow.on_gaze_point = _on_gaze_point  # type: ignore[assignment]
    MainWindow._trigger_translation = _trigger_translation  # type: ignore[assignment]
    MainWindow.keyPressEvent = _key_press  # type: ignore[assignment]
    MainWindow.eventFilter = _event_filter  # type: ignore[assignment]

# ---- 实时交互演示 ---------------------------------------------------------
def run_interactive() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    app = QApplication.instance() or QApplication(sys.argv)
    _install_demo_patches()

    window = MainWindow(AppConfig())  # 构造里即调用 _build_ui
    window.setWindowTitle("myTraxCure — 移动鼠标=模拟视线 | 空格=翻译/隐藏 | Esc=取消")
    window.setMouseTracking(True)
    window.resize(900, 600)
    window.show()

    def _seed_region() -> None:
        w = window.config.region.default_width
        h = window.config.region.default_height
        vw, vh = window.viewer.width(), window.viewer.height()
        region = Region(x=(vw - w) // 2, y=(vh - h) // 2, w=w, h=h)
        window._locked_region = region
        window.feedback.show_region(region)

    QTimer.singleShot(0, _seed_region)

    print("窗口已打开，试试：")
    print("  1. 移动鼠标  → 蓝色透明矩形跟随（feedback 层 = 凝视锁定区）")
    print("  2. 空格      → 矩形处出现『翻译中…』，0.9s 后变 mock 译文（overlay 层）")
    print("  3. 再按空格  → overlay 隐藏，恢复原文")
    print("  4. Esc       → 取消")
    print("  5. 拖拽窗口边角 → 高亮/浮层跟随 viewer（演示 eventFilter）")
    return app.exec()

# ---- 截图模式（--shots）---------------------------------------------------
def run_shots() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    app = QApplication.instance() or QApplication(sys.argv)
    _install_demo_patches()

    window = MainWindow(AppConfig())
    window.resize(760, 480)
    window.show()

    out_dir = Path(__file__).resolve().parent / "output"

    def step1_viewer_only() -> None:
        window.viewer.update()
        _capture(window, out_dir / "build_ui_1_viewer.png")
        QTimer.singleShot(300, step2_feedback)

    def step2_feedback() -> None:
        window.feedback.show_region(HIGHLIGHT_REGION)
        _capture(window, out_dir / "build_ui_2_feedback.png")
        QTimer.singleShot(300, step3_loading)

    def step3_loading() -> None:
        window.overlay.show()
        window.overlay.show_loading(HIGHLIGHT_REGION)
        _capture(window, out_dir / "build_ui_3_overlay.png")
        QTimer.singleShot(300, step4_translated)

    def step4_translated() -> None:
        result = TranslationResult(
            text=MOCK_TRANSLATION,
            source="(highlighted text)",
            backend="mock",
            model="demo",
            elapsed_ms=42,
            cached=False,
        )
        window.overlay.show_translation(HIGHLIGHT_REGION, result, OverlayMode.TRANSLUCENT)
        _capture(window, out_dir / "build_ui_4_translated.png")
        QTimer.singleShot(300, app.quit)

    QTimer.singleShot(200, step1_viewer_only)
    return app.exec()

def _capture(window: MainWindow, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(path))
    print(f"  [saved] {path}")

# ---- pytest 入口：断言 _build_ui 装配出的三层结构（不起事件循环）----------
def test_build_ui_layer_structure() -> None:
    from PyQt6.QtWidgets import QStackedWidget

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 - 保持 QApplication 引用防止被回收
    window = MainWindow(AppConfig())

    # 1. 中央控件是 QStackedWidget，里面装着 viewer
    assert isinstance(window.centralWidget(), QStackedWidget)
    assert window.centralWidget().widget(0) is window.viewer

    # 2. feedback / overlay 的父控件是 viewer（同坐标空间，才能叠在上面）
    assert window.feedback.parent() is window.viewer
    assert window.overlay.parent() is window.viewer

    # 3. 两个层都对鼠标透明（事件穿透到 viewer）
    assert window.feedback.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert window.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # 4. overlay 默认隐藏（feedback 不隐藏，但 _region=None 时 paintEvent 直接 return）
    assert window.overlay.isVisible() is False

if __name__ == "__main__":
    if "--shots" in sys.argv:
        sys.exit(run_shots())
    sys.exit(run_interactive())
