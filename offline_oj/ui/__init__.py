"""PySide6 界面层。

约定：

* 面板（:mod:`offline_oj.ui.panels`）只负责"显示 + 收集用户输入"，
  业务动作一律交给 :mod:`offline_oj.core`，耗时的动作交给
  :mod:`offline_oj.ui.workers` 里的 ``QThread``；
* 主线程只做界面刷新。任何会阻塞 100ms 以上的操作都不允许在主线程执行，
  否则 Windows 会给窗口标题栏挂上"无响应"。

本模块刻意只放文档字符串、不做任何再导出：``from offline_oj.ui import MainWindow``
这种"顺手再导出"会让打包工具多分析一整棵依赖树，而调用方直接写
``from offline_oj.ui.main_window import MainWindow`` 更明确。
"""

from __future__ import annotations

__all__: list[str] = []
