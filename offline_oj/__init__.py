"""离线 OJ 系统 —— 面向 Windows 10/11 的本地代码评测客户端。

包结构::

    offline_oj/
        app.py            应用装配与启动（QApplication、单实例、异常兜底）
        paths.py          %LOCALAPPDATA% 目录规约
        settings.py       设置持久化
        logging_setup.py  滚动日志
        cli.py            无界面命令行入口（自动化评测）
        win32/            Windows 平台集成层
        core/             评测内核（与 UI 完全解耦，可单测）
        ui/               PySide6 界面层
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "__version_info__",
    "APP_NAME",
    "APP_DISPLAY_NAME",
    "APP_ORGANIZATION",
    "__author__",
]

#: 语义化版本号，打包时写入 Windows 版本资源，须与 packaging/version_info.txt 保持一致
__version__ = "2.2.0"
__version_info__ = (2, 2, 0, 0)
__author__ = "Offline OJ Project"

APP_NAME = "OfflineOJ"
APP_DISPLAY_NAME = "离线 OJ 系统"
APP_ORGANIZATION = "OfflineOJ"
