"""PyInstaller 打包入口。

单独放一个文件而不是直接打包 ``offline_oj/__main__.py``，是为了让分析器
一眼就能看出真正的入口，也方便在打包产物里插入"冻结环境"专属逻辑。
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # 冻结后的程序如果派生子进程，必须调用 freeze_support，
    # 否则子进程会重新执行整个启动流程（在我们的场景里会再开一个窗口）。
    multiprocessing.freeze_support()

    from offline_oj.app import main as app_main

    return app_main()


if __name__ == "__main__":
    sys.exit(main())
