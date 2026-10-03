"""磁盘卷枚举。

为什么单独成一个模块：工具链探测（``core.compilers``）和 MSVC 定位
（:mod:`~offline_oj.win32.msvc`）都需要"本机有哪些固定盘"，而这两处都在
``win32`` 之上，不应该各自去调一次 ``ctypes``。

**为什么不能只扫 C 盘**：相当多的机器把 Dev-Cpp / MinGW / JDK / Visual Studio
装在 D 盘或 E 盘。早期把候选路径硬编码成 ``C:\\...``，结果就是
"明明装了编译器，程序却说未检测到"。所以扫描模板统一用 ``{drive}`` 占位，
运行时按这里返回的盘符各展开一份。
"""

from __future__ import annotations

import functools
import logging
import os
import sys

log = logging.getLogger(__name__)

#: ``GetDriveTypeW`` 的返回值：固定磁盘
DRIVE_FIXED = 3

#: 字母表的长度（A-Z）
_ALPHABET_SIZE = 26


def fixed_drives() -> tuple[str, ...]:
    """枚举本机固定磁盘的根目录，形如 ``("C:\\\\", "D:\\\\")``。

    用 ``GetLogicalDrives`` 的位图配合 ``GetDriveTypeW`` 过滤，软驱 / 光驱 /
    网络映射盘 / 可移动盘都不参与扫描 —— 否则探测工具链时会被空光驱拖住好几秒。
    """
    if sys.platform != "win32":
        return ()
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = int(kernel32.GetLogicalDrives())
    except Exception:                                    # noqa: BLE001
        # 拿不到位图就退回系统盘，至少不会一无所获
        log.debug("GetLogicalDrives 失败，仅使用系统盘", exc_info=True)
        return _system_drive()

    drives: list[str] = []
    for index in range(_ALPHABET_SIZE):
        if not (mask >> index) & 1:
            continue
        root = f"{chr(ord('A') + index)}:\\"
        try:
            if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_FIXED:
                drives.append(root)
        except Exception:                                # noqa: BLE001
            continue
    return tuple(drives) or _system_drive()


def _system_drive() -> tuple[str, ...]:
    return (os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\",)


@functools.lru_cache(maxsize=1)
def drive_prefixes() -> tuple[str, ...]:
    """可用于模板展开的盘符前缀。结果缓存 —— 盘符在一次运行内不会变。"""
    return fixed_drives() or _system_drive()


def expand_drive_templates(templates) -> list[str]:
    """把含 ``{drive}`` 的路径模板按每个固定盘展开一份；其余原样返回。"""
    expanded: list[str] = []
    for template in templates:
        if "{drive}" in template:
            expanded.extend(template.replace("{drive}", drive)
                            for drive in drive_prefixes())
        else:
            expanded.append(template)
    return expanded


__all__ = [
    "DRIVE_FIXED",
    "drive_prefixes",
    "expand_drive_templates",
    "fixed_drives",
]
