"""Windows 平台集成层。

本包把"Windows 应用应该做的事"集中起来，让上层代码只关心业务：

* :mod:`~offline_oj.win32.dpi` —— 高 DPI 感知（Per-Monitor V2）
* :mod:`~offline_oj.win32.appid` —— AppUserModelID，任务栏正确归组、图标不回退
* :mod:`~offline_oj.win32.single_instance` —— 命名互斥体，禁止多开
* :mod:`~offline_oj.win32.process` —— 无窗口创建子进程、进程树终止、驱动器类型

所有函数在非 Windows 平台上都会安全降级为空操作，因此 core 层单测可以在任意系统跑。
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

__all__ = ["IS_WINDOWS"]
