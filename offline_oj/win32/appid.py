"""AppUserModelID（应用用户模型 ID）。

这是 Windows 任务栏的行为基础。没有显式 AppUserModelID 时：

* 任务栏把进程按"可执行文件路径"归组，用 ``python.exe`` 源码运行时，
  本应用会和所有其它 Python 程序挤在同一个任务栏按钮里；
* 任务栏图标回退成 Python 默认图标，自己设置的 ``.ico`` 不生效；
* 无法固定（Pin）到任务栏，通知与缩略图行为异常。

必须在创建任何窗口之前调用。
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

#: 反向域名风格，安装包与快捷方式需使用同一字符串才能正确归组
APP_USER_MODEL_ID = "OfflineOJ.Desktop.Client"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """设置当前进程的 AppUserModelID。"""
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        # HRESULT: S_OK == 0
        hr = shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        if hr == 0:
            log.info("AppUserModelID = %s", app_id)
            return True
        log.warning("设置 AppUserModelID 失败，HRESULT=0x%08X", hr & 0xFFFFFFFF)
    except (AttributeError, OSError) as exc:
        log.warning("无法设置 AppUserModelID: %s", exc)
    return False
