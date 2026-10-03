"""单实例互斥。

**为什么必须单实例**：题库是"整份读入 → 内存修改 → 整份写回"的模型。两个实例同时
打开 ``problems.json``，后关闭的那个会用陈旧的内存快照覆盖前一个的改动，用户会看到
"刚保存的题目不见了"。Windows 应用的常规做法是命名互斥体 + 唤出已有窗口。

互斥体名称放在 ``Local\\`` 命名空间：同一用户会话内唯一即可，放在 ``Global\\`` 反而
会跨会话（如"快速用户切换"下的另一个登录用户）互相阻塞。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

MUTEX_NAME = r"Local\OfflineOJ.SingleInstance.v2"
ERROR_ALREADY_EXISTS = 183


class InstanceLock:
    """命名互斥体封装。

    ::

        lock = InstanceLock()
        if not lock.acquire():
            ...  # 已有实例在运行
        # 退出时 lock.release()，进程结束由系统自动释放
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle: int | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """尝试取得互斥体。

        :return: ``True`` 表示本进程是唯一实例；``False`` 表示已有实例在运行。
        """
        if sys.platform != "win32":
            # 非 Windows 平台不做限制，交由上层（如 fcntl）处理
            return True
        if self._handle is not None:
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, self._name)
        last_error = ctypes.get_last_error()

        if not handle:
            log.warning("创建互斥体失败，跳过单实例检查（err=%s）", last_error)
            return True

        if last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            log.info("检测到已有实例在运行")
            return False

        self._handle = handle
        log.info("单实例互斥体已持有: %s", self._name)
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
        except Exception:
            pass
        finally:
            self._handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
