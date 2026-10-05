"""构建期 UI 视觉验证：真实链路演示 + 三层结构断言。

演示不再猴补丁主链路：mock 文档写成临时 .txt 走真实 FileParser → DocumentRenderer，
凝视走真实 MainWindow.on_gaze_point（含驻留判定/段落聚合/容差命中），
翻译走真实 TranslationWorker + 缓存 + 浮层，只把引擎换成本地 MockEngine（不连 Ollama）。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 `mytraxcure` 可导入（无论从哪运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QStackedWidget

from mytraxcure.core.config import AppConfig
from mytraxcure.core.translation.cache_store import CacheStore
from mytraxcure.core.translation.translation_engine import (
    TranslationEngine,
    TranslationResult,
)
from mytraxcure.core.types import GazePoint
from mytraxcure.ui.main_window import MainWindow

# ---- mock 文档内容（写进临时 .txt，按真实 TXT 排版渲染）--------------------
# 空行决定段落边界：_parse_txt 遇空行才推进 para_key，故非空行相邻即同一段
MOCK_DOC_LINES = [
    "myTraxCure — gaze-driven translation viewer",
    "",
    "Line 3: The five boxing wizards jump quickly.",
    "Line 4: Sphinx of black quartz, judge my vow.",
    "",
    "移动鼠标到段落上停住 ~0.6s = 凝视锁定 | 空格 = 翻译 / 再按隐藏 | Esc = 取消",
]

MOCK_TRANSLATION = (
    "【示例译文】敏捷的棕色狐狸跳过了懒惰的狗。"
    "这是 mock 引擎返回的结果：真实链路走 _trigger_translation → TranslationWorker → engine.translate。"
)

class MockEngine(TranslationEngine):
    """本地假引擎：不连 Ollama，其余（worker 线程/信号/缓存/浮层）全部真实。"""

    name = "mock"
    delay_s = 0.7  # 留出捕获"翻译中…"状态的时间；睡在 worker 线程里，不卡 UI

    def translate(self, text, source_lang, target_lang, context=None) -> TranslationResult:
        start = time.time()
        time.sleep(self.delay_s)
        return TranslationResult(
            text=MOCK_TRANSLATION,
            source=text,
            backend=self.name,
            model="demo",
            elapsed_ms=int((time.time() - start) * 1000),
            cached=False,
        )

def _mock_document_path() -> Path:
    path = Path(tempfile.gettempdir()) / "mytraxcure_ui_demo.txt"
    path.write_text("\n".join(MOCK_DOC_LINES), encoding="utf-8")
    return path

def _build_window() -> MainWindow:
    """按真实启动路径装配窗口：真实解析 mock 文档，只替换引擎工厂与缓存库。"""
    import mytraxcure.ui.main_window as main_window

    main_window.create_engine = lambda cfg: MockEngine()  # 唯一的假件：引擎工厂
    window = MainWindow(AppConfig())
    # 换成临时缓存库（不碰 data/cache.db），且每次演示从空缓存开始：
    # 否则第二次跑会直接命中上次的缓存，跳过"翻译中…"状态，截图不稳定
    cache_path = Path(tempfile.gettempdir()) / "mytraxcure_ui_demo_cache.db"
    cache_path.unlink(missing_ok=True)
    window._cache = CacheStore(cache_path)
    window.load_file(_mock_document_path())  # 真实 FileParser → TextMapper.register_blocks
    window.resize(1100, 620)
    window.show()
    return window

def _gaze_target_doc_point(window: MainWindow) -> tuple[float, float]:
    """取最后一个文本块中心作为凝视目标（文档坐标）。

    为什么不取中间块：hit_test 用 hit_tolerance_px=100 把每块 bbox 上下各撑开
    ~3 行，且按 reversed(_blocks) 顺序返回第一个命中 → 下方的块会抢走瞄准上方块的凝视点
    （实测：瞄准标题命中 Line 4，瞄准 Line 3 命中末段）。取最后一块结果才自洽。
    """
    blocks = window.mapper._blocks
    block = blocks[-1]
    return (block.x + block.w / 2, block.y + block.h / 2)

def _feed_gaze(window: MainWindow, doc_point, ticks: int = 24, interval_ms: int = 40) -> None:
    """按 40ms 喂同一个凝视点，凑够 dwell_seconds(0.6s) 触发真实驻留锁定。

    调用前需停掉鼠标轮询定时器：否则真实光标位置也会混进 dwell 窗口，把稳定性判据冲掉。
    """
    gx, gy = window.mapper.document_to_screen(doc_point)

    def tick(i: int = 0) -> None:
        window.on_gaze_point(GazePoint(x=gx, y=gy))
        if i + 1 < ticks:
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(interval_ms, lambda: tick(i + 1))

    tick()

# ---- 实时交互演示 ---------------------------------------------------------
def run_interactive() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    app = QApplication.instance() or QApplication(sys.argv)
    window = _build_window()
    window.setWindowTitle("myTraxCure — 鼠标停住=凝视锁定 | 空格=翻译/隐藏 | Esc=取消")

    print("窗口已打开（真实链路，仅引擎为 mock）：")
    print("  1. 鼠标移到某段落上停住 ~0.6s → 蓝色高亮框住整段（真实驻留判定）")
    print("  2. 空格      → 『翻译中…』，0.7s 后出现 mock 译文（真实 worker+浮层）")
    print("  3. 再按空格  → 浮层隐藏，恢复原文")
    print("  4. Esc       → 取消")
    print("  5. 拖拽窗口边角 → 高亮/浮层跟随 viewer（真实 eventFilter）")
    return app.exec()

# ---- 截图模式（--shots）---------------------------------------------------
def run_shots() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    from PyQt6.QtCore import QTimer

    app = QApplication.instance() or QApplication(sys.argv)
    window = _build_window()
    out_dir = Path(__file__).resolve().parent / "output"

    def step1_viewer_only() -> None:
        # 首帧渲染会回填 TXT 的 bbox 并重新注册文本块，必须先让它画过一次
        _capture(window, out_dir / "build_ui_1_viewer.png")
        window._gaze_timer.stop()  # 改由脚本喂凝视点
        QTimer.singleShot(300, step2_feedback)

    def step2_feedback() -> None:
        _feed_gaze(window, _gaze_target_doc_point(window))
        QTimer.singleShot(1200, step3_loading)  # 24×40ms 喂点 + 驻留判定

    def step3_loading() -> None:
        assert window._locked_region is not None, "凝视驻留未触发锁定（看 step2）"
        _capture(window, out_dir / "build_ui_2_feedback.png")
        window.on_space()  # 真实按键链路 → show_loading
        QTimer.singleShot(250, step4_loading_shot)

    def step4_loading_shot() -> None:
        _capture(window, out_dir / "build_ui_3_overlay.png")
        QTimer.singleShot(900, step5_translated)  # mock 引擎 0.7s 后信号回来

    def step5_translated() -> None:
        _capture(window, out_dir / "build_ui_4_translated.png")
        QTimer.singleShot(200, app.quit)

    QTimer.singleShot(300, step1_viewer_only)
    return app.exec()

def _capture(window: MainWindow, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(path))
    print(f"  [saved] {path}")

# ---- pytest 入口：断言 _build_ui 装配出的三层结构（不起事件循环）----------
def test_build_ui_layer_structure() -> None:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 - 保持 QApplication 引用防止被回收
    window = MainWindow(AppConfig())

    # 1. 中央控件是包一层的 QWidget（右侧要放摄像头预览面板），
    #    文档栈 self._stack 是它的直接子控件
    assert window._stack.parent() is window.centralWidget()
    assert isinstance(window._stack, QStackedWidget)
    assert window._stack.widget(0) is window.viewer

    # 2. feedback / overlay 的父控件是 viewer（同坐标空间，才能叠在上面）
    assert window.feedback.parent() is window.viewer
    assert window.overlay.parent() is window.viewer

    # 3. 两个层都对鼠标透明（事件穿透到 viewer）
    from PyQt6.QtCore import Qt

    assert window.feedback.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert window.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # 4. overlay 默认隐藏（feedback 不隐藏，但 _region=None 时 paintEvent 直接 return）
    assert window.overlay.isVisible() is False

if __name__ == "__main__":
    if "--shots" in sys.argv:
        sys.exit(run_shots())
    sys.exit(run_interactive())
