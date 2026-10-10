"""模块名称：__main__.py

功能描述：`python -m mytraxcure` 的包内入口，与仓库根目录的 main.py 等价，
    只负责把控制权交给 mytraxcure.app.run()。

关键约束：本文件是根目录 main.py 的同内容副本，改一处需同步另一处；
    作为包内模块执行，要求 mytraxcure 包可被 import（仓库根目录在 sys.path 上）。
"""

from mytraxcure.app import run

if __name__ == "__main__":
    # run() 返回 Qt 事件循环的退出码，包成 SystemExit 后即进程返回值
    raise SystemExit(run())
