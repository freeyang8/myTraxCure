from __future__ import annotations

import importlib
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 上，使 `mytraxcure` 可导入（无论从哪运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 仅依赖 numpy + eyetrax 的模块（应始终可导入）
CORE_MODULES = [
    "mytraxcure",
    "mytraxcure.core",
    "mytraxcure.core.config",
    "mytraxcure.core.types",
    "mytraxcure.core.gaze",
    "mytraxcure.core.gaze.gaze_wrapper",
    "mytraxcure.core.gaze.smoother_adapter",
    "mytraxcure.core.gaze.startup_calibration",
    "mytraxcure.core.gaze.calibration_store",
    "mytraxcure.core.gaze_logic",
    "mytraxcure.core.gaze_logic.gaze_detector",
    "mytraxcure.core.gaze_logic.region_locker",
    "mytraxcure.core.file",
    "mytraxcure.core.file.file_parser",
    "mytraxcure.core.file.ocr_engine",
    "mytraxcure.core.file.text_mapper",
    "mytraxcure.core.file.file_viewer",
    "mytraxcure.core.translation",
    "mytraxcure.core.translation.translation_engine",
    "mytraxcure.core.translation.ollama_engine",
    "mytraxcure.core.translation.builtin_engine",
    "mytraxcure.core.translation.engine_factory",
    "mytraxcure.core.translation.cache_store",
    "mytraxcure.core.fallback",
    "mytraxcure.core.fallback.fallback_manager",
]

# 依赖 PyQt6 的模块（未安装时跳过）
QT_MODULES = [
    "mytraxcure.core.gaze.camera",
    "mytraxcure.core.gaze_logic.keyboard_handler",
    "mytraxcure.ui",
    "mytraxcure.ui.main_window",
    "mytraxcure.ui.viewer_view",
    "mytraxcure.ui.overlay_view",
    "mytraxcure.ui.feedback_view",
    "mytraxcure.ui.settings_view",
    "mytraxcure.app",
]

def _try_import(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

def run() -> int:
    # 控制台可能是 GBK，强制 UTF-8 输出，避免打印标记时崩
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: S110 - 控制台不支持 UTF-8 时静默跳过
        pass

    failed = False

    print("== core modules ==")
    for name in CORE_MODULES:
        ok, err = _try_import(name)
        status = "OK" if ok else f"FAIL ({err})"
        print(f"  [{'OK' if ok else 'XX'}] {name}  {status}")
        failed |= not ok

    print("== Qt-dependent modules ==")
    for name in QT_MODULES:
        ok, err = _try_import(name)
        if ok:
            print(f"  [OK] {name}")
        else:
            print(f"  [--] {name}  skip ({err})")

    print("\n" + ("PASS" if not failed else "HAS FAILURES"))
    return 1 if failed else 0

def test_core_imports() -> None:
    for name in CORE_MODULES:
        importlib.import_module(name)

if __name__ == "__main__":
    sys.exit(run())
