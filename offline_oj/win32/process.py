"""子进程创建与终止的 Windows 规范做法。

三个要点：

1. **不闪黑窗**：GUI 进程调用 ``g++`` / ``python`` 这类控制台程序时，Windows 会为
   子进程分配一个新的控制台窗口，屏幕上连续闪出黑框。正确做法是给子进程加
   ``CREATE_NO_WINDOW``；老办法 ``STARTF_USESHOWWINDOW + SW_HIDE`` 只是把窗口藏起来，
   控制台仍然被创建，会带来可见的闪烁与额外的资源开销。
2. **杀进程树**：编译器和用户程序都可能派生孙进程（例如 ``java`` 启动的
   ``javaw``）。只杀父进程会留下孤儿进程吃掉 CPU / 内存，因此统一用
   ``taskkill /T`` 递归终止。
3. **编码**：Windows 中文环境下子进程默认使用 GBK 输出，统一显式指定 UTF-8
   并配合 ``errors="replace"``，避免评测结果里出现解码异常。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

#: 不为子进程创建控制台窗口（仅 Windows 有效）
CREATE_NO_WINDOW = 0x08000000
#: 独立进程组，便于整组终止
CREATE_NEW_PROCESS_GROUP = 0x00000200

IS_WINDOWS = sys.platform == "win32"


def creation_flags() -> int:
    """子进程创建标志：Windows 下不显示控制台窗口。"""
    if not IS_WINDOWS:
        return 0
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def decode(data: bytes | str | None) -> str:
    """把子进程输出统一解码为 ``str``。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_silent(
    cmd: Sequence[str] | str,
    *,
    timeout: float = 5.0,
    cwd: str | os.PathLike[str] | None = None,
    input_data: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """静默执行命令并等待结束（不弹黑窗），输出按 UTF-8 解码。

    :param env: 完整的环境变量字典；``None`` 表示继承当前进程。
        MSVC 的 ``cl.exe`` 必须靠 ``INCLUDE`` / ``LIB`` 才找得到标准库，
        所以编译时要显式传一份（见 :mod:`~offline_oj.win32.msvc`）。

    超时不抛异常，而是返回 ``returncode=124``，让调用方按"工具执行失败"处理，
    避免处处写 try/except TimeoutExpired。
    """
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creation_flags(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124, decode(exc.stdout), f"命令执行超时（{timeout}s）"
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "找不到可执行文件")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 126, "", f"无法执行: {exc}")

    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        decode(completed.stdout),
        decode(completed.stderr),
    )


def popen(
    cmd: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    pipes: bool = True,
) -> subprocess.Popen:
    """启动子进程（不弹黑窗），默认接管 stdin/stdout/stderr。

    调用方负责最终 ``communicate()`` 或 ``kill()``。
    """
    return subprocess.Popen(
        list(cmd) if not isinstance(cmd, str) else cmd,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if pipes else None,
        stdout=subprocess.PIPE if pipes else None,
        stderr=subprocess.PIPE if pipes else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags(),
    )


def kill_tree(pid: int, timeout: float = 3.0) -> None:
    """强制终止进程及其全部子进程。"""
    if pid <= 0:
        return
    if IS_WINDOWS:
        run_silent(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=timeout)
        return
    try:
        import signal

        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def is_removable_drive(path: str | os.PathLike[str]) -> bool:
    """判断路径是否位于可移动驱动器（U 盘、移动硬盘、光驱）。

    用于提醒用户"题库放在可移动设备上，拔盘后数据会丢"。
    """
    if not IS_WINDOWS:
        return False
    try:
        drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
        if not drive:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # DRIVE_REMOVABLE == 2
        return kernel32.GetDriveTypeW(f"{drive}\\") == 2
    except Exception:
        return False


def reveal_in_explorer(path: str | os.PathLike[str]) -> None:
    """在资源管理器中定位文件/目录（Windows 标准交互）。"""
    target = os.path.abspath(str(path))
    if not os.path.exists(target):
        return
    if IS_WINDOWS:
        if os.path.isdir(target):
            os.startfile(target)  # noqa: S606 - Windows 专用
        else:
            subprocess.Popen(["explorer", "/select,", target])
    else:
        run_silent(["xdg-open", os.path.dirname(target)])


def open_path(path: str | os.PathLike[str]) -> None:
    """用系统默认程序打开文件（日志、导出结果等）。"""
    target = os.path.abspath(str(path))
    if not os.path.exists(target):
        return
    if IS_WINDOWS:
        os.startfile(target)  # noqa: S606 - Windows 专用
    else:
        run_silent(["xdg-open", target])


def normalize_extensions(paths: Iterable[str]) -> list[str]:
    """去重并保持顺序的小工具，用于合并编译器候选路径。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in paths:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
