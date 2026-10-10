"""模块名称：test_imports.py

功能描述：冒烟式导入检查——逐个 import 所有模块并打印成败，用来在没有完整依赖的环境里
快速分辨"装了什么"和"哪条 import 链断了"。既可作为 pytest 用例跑，也可直接执行去看清单。

关键约束：
- core 模块清单里的模块只允许依赖 numpy 与 eyetrax，因此它们失败就是真失败（计入退出码）；
  依赖 PyQt6 的清单失败只算跳过，环境没装 UI 依赖不该算测试不过。
- 文件必须先把项目根塞进 sys.path，否则从 tests/ 目录直接跑会找不到顶层包。
- tests 目录被 .gitignore 忽略，本地专属脚本，不进版本库。
"""

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
    "mytraxcure.core.gaze.calibration_runner",
    "mytraxcure.core.gaze.calibration_store",
    "mytraxcure.core.gaze_logic",
    "mytraxcure.core.gaze_logic.gaze_detector",
    "mytraxcure.core.gaze_logic.region_locker",
    "mytraxcure.core.file",
    "mytraxcure.core.file.file_parser",
    "mytraxcure.core.file.segmenter",
    "mytraxcure.core.file.text_mapper",
    "mytraxcure.core.file.file_viewer",
    "mytraxcure.core.translation",
    "mytraxcure.core.translation.translation_engine",
    "mytraxcure.core.translation.ollama_engine",
    "mytraxcure.core.translation.engine_factory",
    "mytraxcure.core.translation.cache_store",
    "mytraxcure.core.translation.translation_worker",
    "mytraxcure.core.fallback",
    "mytraxcure.core.fallback.fallback_manager",
]

# 依赖 PyQt6 的模块（未安装时跳过）
QT_MODULES = [
    "mytraxcure.core.gaze.camera",
    "mytraxcure.core.translation.pre_translator",
    "mytraxcure.core.gaze_logic.keyboard_handler",
    "mytraxcure.ui",
    "mytraxcure.ui.main_window",
    "mytraxcure.ui.viewer_view",
    "mytraxcure.ui.overlay_view",
    "mytraxcure.ui.feedback_view",
    "mytraxcure.ui.settings_view",
    "mytraxcure.ui.gaze_settings_dialog",
    "mytraxcure.app",
]

def _try_import(name: str) -> tuple[bool, str]:
    """尝试导入一个模块，返回 (是否成功, 失败原因)；失败原因用于打印而非抛出。"""
    try:
        importlib.import_module(name)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

def run() -> int:
    """逐项导入并打印清单。

    Returns:
        0 表示 core 清单全部导入成功，1 表示至少有一个失败；
        Qt 清单的失败不影响返回值。
    """
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
    """pytest 入口：core 清单里任一模块导入失败即整体失败。"""
    for name in CORE_MODULES:
        importlib.import_module(name)

if __name__ == "__main__":
    sys.exit(run())
