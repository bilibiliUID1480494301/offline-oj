"""高 DPI 感知。

不做这件事的后果：在 125% / 150% 缩放的显示器上，应用被 Windows 位图拉伸，
整个界面发虚、字体毛边。这属于 Windows 应用的硬性体验要求。

优先级依次为 Per-Monitor V2 → Per-Monitor → System Aware，逐级降级以兼容
Windows 8.1 / 7 与缺少该导出函数的老系统。

**必须在创建 QApplication 之前调用**，进程级 DPI 感知一旦确定不可更改。
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2（Windows 10 1703+）
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
#: PROCESS_PER_MONITOR_DPI_AWARE
PROCESS_PER_MONITOR_DPI_AWARE = 2
#: PROCESS_SYSTEM_DPI_AWARE
PROCESS_SYSTEM_DPI_AWARE = 1


def enable_high_dpi() -> str:
    """开启高 DPI 感知，返回实际生效的模式名（便于日志与诊断）。"""
    if sys.platform != "win32":
        return "unsupported"

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # SetProcessDpiAwarenessContext 返回 BOOL
        if user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # S_OK == 0，注意 E_ACCESSDENIED(0x80070005) 表示已被更早调用设置过
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if user32.SetProcessDPIAware():
            return "system-aware"
    except (AttributeError, OSError):
        pass

    return "none"


def enable_high_dpi_quietly() -> None:
    """开启高 DPI 并记录日志，任何失败都不影响启动。"""
    mode = enable_high_dpi()
    log.info("DPI 感知模式: %s", mode)
