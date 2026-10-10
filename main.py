"""模块名称：main.py

功能描述：仓库根目录下的启动入口，与 `python -m mytraxcure` 完全等价，
    只负责把控制权交给 mytraxcure.app.run()，自身不含任何业务逻辑。

关键约束：本文件是 mytraxcure/__main__.py 的同内容副本，改一处需同步另一处；
    两者都要求仓库根目录在 sys.path 上，否则 import mytraxcure 会失败。
"""

from mytraxcure.app import run

if __name__ == "__main__":
    # run() 返回 Qt 事件循环的退出码，包成 SystemExit 后即进程返回值
    raise SystemExit(run())
