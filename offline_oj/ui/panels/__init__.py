"""功能面板。

每个面板对应一个选项卡，彼此不直接引用，跨面板的联动统一由主窗口接线
（例如"存题"面板改了题库，主窗口收到信号后通知"写题"面板刷新列表）。
这样面板之间没有隐式依赖，单独拿出来也能跑。

**这里必须用显式导入，不能用 ``importlib`` 惰性加载。** 打包工具（PyInstaller、
Nuitka）靠静态分析源码里的 import 语句收集依赖树；写成
``import_module(f".{name}")`` 之类，分析器看不到具体模块名，打包产物里就会缺文件 ——
源码运行一切正常，双击 exe 才发现 ``ModuleNotFoundError``。
"""

from __future__ import annotations

from .base import Panel
from .compilers_panel import CompilersPanel
from .exam_panel import ConnectWorker, ExamPanel
from .help_panel import HelpPanel
from .problems_panel import ProblemsPanel
from .settings_panel import SettingsPanel
from .solve_panel import SolvePanel

__all__ = [
    "Panel",
    "ProblemsPanel",
    "SolvePanel",
    "ExamPanel",
    "ConnectWorker",
    "CompilersPanel",
    "SettingsPanel",
    "HelpPanel",
]
