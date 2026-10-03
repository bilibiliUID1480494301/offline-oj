"""编译器 / 解释器探测。

本机装了什么工具链是无法假设的，所以按"从可靠到兜底"四级查找：

1. ``PATH`` 环境变量（``shutil.which``，快且不产生子进程）；
2. Windows 注册表登记的安装路径（Python、JDK 都会写，最权威）；
3. 常见安装目录扫描（MinGW / MSYS2 / Dev-Cpp / LLVM / TDM-GCC / Zulu / Adoptium …）；
4. 用户手工指定（UI 里的"浏览…"）。

找到多个候选时按版本号降序，并让 g++/gcc 在同版本下优先于 clang++（MinGW 生态下
libstdc++ 与常见教材写法兼容性更好）。

**扫描目录不能写死盘符。** 相当多的机器把 Dev-Cpp / MinGW / JDK 装在 D 盘甚至
E 盘，早期版本把路径硬编码成 ``C:\\...``，结果就是"明明装了编译器却检测不到"。
这里改为：目录模板里用 ``{drive}`` 占位，运行时对每个固定盘各展开一份；
再加上用环境变量（``%ProgramFiles%`` 等）表达的盘符无关路径。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..win32 import msvc
from ..win32.process import run_silent
from ..win32.volumes import drive_prefixes, expand_drive_templates

log = logging.getLogger(__name__)

#: 各配置键对应的候选可执行文件名（按优先级）。
#: C/C++ 还会额外补上 MSVC 的 ``cl.exe`` —— 它不在 PATH 上，得靠 Visual Studio
#: 的安装布局去找，不是简单的同名查找（见 :func:`_msvc_candidates`）。
CANDIDATE_NAMES: dict[str, tuple[str, ...]] = {
    "cpp": ("g++", "clang++", "c++"),
    "c": ("gcc", "clang", "cc"),
    "python": ("python", "python3"),
    "javac": ("javac",),
    "java": ("java",),
}

#: 各配置键对应的中文名
KEY_LABELS: dict[str, str] = {
    "cpp": "C++ 编译器",
    "c": "C 编译器",
    "python": "Python 解释器",
    "javac": "Java 编译器",
    "java": "Java 运行时",
}

#: 版本探测参数
_VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "cpp": ("--version",),
    "c": ("--version",),
    "python": ("--version",),
    "javac": ("-version",),
    "java": ("-version",),
}


def _drive_prefixes() -> tuple[str, ...]:
    """扫描模板可用的盘符前缀（转发到平台层，保留本模块内的旧名字）。"""
    return drive_prefixes()


#: 额外扫描目录模板。``{drive}`` 会为每个固定盘各展开一份；
#: 含 ``%VAR%`` 的条目走环境变量展开，与盘符无关。
_EXTRA_DIR_TEMPLATES: dict[str, tuple[str, ...]] = {
    "cpp": (
        r"{drive}Program Files\Dev-Cpp\MinGW64\bin",
        r"{drive}Program Files (x86)\Dev-Cpp\MinGW64\bin",
        r"{drive}Dev-Cpp\MinGW64\bin",
        r"{drive}Dev-Cpp\MinGW32\bin",
        r"{drive}Dev-Cpp\bin",
        r"{drive}mingw64\bin",
        r"{drive}mingw32\bin",
        r"{drive}MinGW\bin",
        r"{drive}msys64\mingw64\bin",
        r"{drive}msys64\ucrt64\bin",
        r"{drive}msys64\clang64\bin",
        r"{drive}TDM-GCC-64\bin",
        r"{drive}TDM-GCC-32\bin",
        r"{drive}Program Files\LLVM\bin",
        r"{drive}Program Files (x86)\LLVM\bin",
        r"%LOCALAPPDATA%\Programs\mingw64\bin",
        r"%ProgramFiles%\Dev-Cpp\MinGW64\bin",
        r"%ProgramFiles(x86)%\Dev-Cpp\MinGW64\bin",
        r"%ProgramFiles%\LLVM\bin",
    ),
    "python": (
        r"%LOCALAPPDATA%\Programs\Python",
        r"%ProgramFiles%",
        r"{drive}Program Files\Python",
        r"{drive}Python",
    ),
    "javac": (
        r"{drive}Program Files\Java",
        r"{drive}Program Files\Eclipse Adoptium",
        r"{drive}Program Files\Microsoft",
        r"{drive}Program Files\Zulu",
        r"{drive}Program Files\Amazon Corretto",
        r"{drive}Program Files\BellSoft",
        r"{drive}Program Files (x86)\Java",
        r"{drive}AndroidDev\jdk",
        r"{drive}Java",
        r"{drive}DevTools\jdk",
    ),
}
_EXTRA_DIR_TEMPLATES["c"] = _EXTRA_DIR_TEMPLATES["cpp"]
_EXTRA_DIR_TEMPLATES["java"] = _EXTRA_DIR_TEMPLATES["javac"]

#: 带版本号目录的通配模板（官方安装包会装到 ``C:\Python312`` 这类固定位置）
_GLOB_TEMPLATES: dict[str, tuple[str, ...]] = {
    "cpp": (),
    "c": (),
    "python": (
        r"{drive}Python3*",
        r"{drive}Program Files\Python3*",
        r"%LOCALAPPDATA%\Programs\Python\Python3*",
    ),
    "javac": (
        r"{drive}Java\jdk*",
        r"{drive}AndroidDev\jdk\jdk*",
    ),
    "java": (),
}


def _expand_templates(templates: Iterable[str]) -> list[str]:
    """把 ``{drive}`` 模板按每个固定盘展开一份（``%VAR%`` 留给调用方展开）。"""
    return expand_drive_templates(templates)


def extra_dirs(key: str) -> tuple[str, ...]:
    """某个配置键的额外扫描目录（已按盘符展开）。"""
    return tuple(_expand_templates(_EXTRA_DIR_TEMPLATES.get(key, ())))


def glob_dirs(key: str) -> tuple[str, ...]:
    """某个配置键的通配扫描目录（已按盘符展开）。"""
    return tuple(_expand_templates(_GLOB_TEMPLATES.get(key, ())))


def _is_msvc(path: str | os.PathLike[str]) -> bool:
    """路径是不是 MSVC 的 ``cl.exe``。"""
    return msvc.is_msvc(path)


def msvc_candidates(key: str) -> list[str]:
    """MSVC 的 ``cl.exe`` 候选。

    ``cl.exe`` 不在 ``PATH`` 上，名字也不在 :data:`CANDIDATE_NAMES` 里 ——
    它得靠 Visual Studio 的安装布局去定位（见 :mod:`~offline_oj.win32.msvc`）。
    C 与 C++ 用的是同一个 ``cl.exe``，靠源文件扩展名区分语言，所以两个 key 共用。
    """
    if key not in ("cpp", "c") or sys.platform != "win32":
        return []
    try:
        return list(msvc.find_compilers())
    except Exception:                                    # noqa: BLE001
        log.debug("查找 MSVC 失败", exc_info=True)
        return []


#: C/C++ 编译器家族优先级。
#:
#: **刻意让 GCC 排在 MSVC 前面**，而不是单纯按版本号比大小 —— 这台机器的
#: cl.exe 是 19.44，Dev-Cpp 的 g++ 是 4.9，按版本排会让 MSVC 胜出。但对一个
#: 刷题工具来说这是错的：题解、教材、洛谷上的报错都以 GCC 为准，而且竞赛常用的
#: ``bits/stdc++.h`` 和 ``%lld`` 在 MSVC 上根本不能用。两者都装了就该选 GCC。
_FAMILY_RANK: dict[str, int] = {"gcc": 2, "clang": 1, "msvc": 0}


def compiler_family(path: str | os.PathLike[str]) -> str:
    """从可执行文件名判断编译器家族（``gcc`` / ``clang`` / ``msvc`` / ``""``）。"""
    name = Path(path).name.lower()
    if name == "cl.exe":
        return "msvc"
    if name.startswith(("g++", "gcc", "c++", "cc", "mingw")):
        return "gcc"
    if name.startswith("clang"):
        return "clang"
    return ""


#: 子目录名的匹配规则（进入这些子目录里找 exe，例如 Python312 / jdk-21）
_SUBDIR_HINTS = re.compile(r"^(python|jdk|jre|zulu|temurin|corretto|liberica|\d)", re.I)


@dataclass(frozen=True)
class CompilerInfo:
    """一个可用的编译器/解释器。"""

    key: str
    path: str
    version: str = ""
    version_tuple: tuple[int, ...] = ()
    works: bool = False
    detail: str = ""

    @property
    def label(self) -> str:
        return KEY_LABELS.get(self.key, self.key)

    @property
    def display(self) -> str:
        if not self.path:
            return "未配置"
        version = f"  ({self.version})" if self.version else ""
        return f"{self.path}{version}"

    @property
    def short_path(self) -> str:
        """过长路径省略中间部分，便于表格展示。"""
        text = self.path
        if len(text) <= 52:
            return text
        head, tail = text[:18], text[-30:]
        return f"{head}…{tail}"

    def with_status(self, works: bool, detail: str = "") -> "CompilerInfo":
        return replace(self, works=works, detail=detail)


class CompilerDetector:
    """探测本机工具链。"""

    # ---- 对外 API ---------------------------------------------------------

    @classmethod
    def detect(cls, key: str) -> CompilerInfo | None:
        """为单个配置键寻找最佳候选。"""
        candidates: list[CompilerInfo] = []
        for path in cls._candidate_paths(key):
            info = cls.probe(path, key)
            if info is not None:
                candidates.append(info)
        if not candidates:
            return None

        candidates.sort(key=cls._sort_key, reverse=True)
        best = candidates[0]
        log.info("检测到 %s: %s (%s)", key, best.path, best.version)
        return best

    @classmethod
    def detect_all(
        cls,
        keys: Iterable[str] = ("cpp", "c", "python", "javac", "java"),
        progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, CompilerInfo | None]:
        """批量探测。``progress(key, 说明)`` 用于向界面回报进度。"""
        result: dict[str, CompilerInfo | None] = {}
        for key in keys:
            if progress:
                progress(key, f"正在检测{KEY_LABELS.get(key, key)}…")
            try:
                result[key] = cls.detect(key)
            except Exception:
                log.exception("检测 %s 失败", key)
                result[key] = None
        return result

    @classmethod
    def probe(cls, path: str, key: str) -> CompilerInfo | None:
        """验证指定路径是否是可用的编译器，返回其版本信息。"""
        if not path or not os.path.isfile(path):
            return None

        # cl.exe 光有文件不算可用 —— 它必须靠 INCLUDE / LIB 才找得到标准库。
        # 这里顺手确认环境能组装出来，免得把一个"一编译就报 C1034"的路径
        # 当成可用编译器塞给用户。
        if _is_msvc(path) and msvc.cached_environment(os.path.abspath(path)) is None:
            log.debug("MSVC 环境不完整，跳过: %s", path)
            return None

        version_text = cls.version_string(path, key)
        if not version_text:
            return None
        return CompilerInfo(
            key=key,
            path=os.path.abspath(path),
            version=version_text,
            version_tuple=cls.parse_version(version_text, key) or (),
        )

    @classmethod
    def version_string(cls, executable: str, key: str) -> str:
        """取工具链版本描述。

        MSVC 的 ``cl.exe`` 要特殊对待：它**不支持** ``--version``，
        不带参数运行会打印横幅与用法，而且退出码是 0 —— 与 gcc 的约定完全不同。
        """
        if _is_msvc(executable):
            return msvc.version_text(executable)

        args = _VERSION_ARGS.get(key, ("--version",))
        completed = run_silent([executable, *args], timeout=6)
        # 无返回码即无法启动（例如架构不匹配、缺少 DLL）
        if completed.returncode not in (0, 1):
            return ""
        text = (completed.stdout or "").strip() or (completed.stderr or "").strip()
        if not text:
            return ""
        first_line = text.splitlines()[0].strip()
        return first_line[:160]

    @classmethod
    def parse_version(cls, text: str, key: str) -> tuple[int, ...]:
        """从版本文本里抽取可比较的数字元组。"""
        if not text:
            return ()
        if key in ("cpp", "c"):
            match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
            if match:
                return tuple(int(g) if g else 0 for g in match.groups())
        elif key == "python":
            match = re.search(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?", text)
            if match:
                return tuple(int(g) if g else 0 for g in match.groups())
            match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
            if match:
                return tuple(int(g) if g else 0 for g in match.groups())
        elif key in ("javac", "java"):
            match = re.search(r'version\s+"?(\d+)(?:\.(\d+))?(?:\.(\d+))?', text)
            if match:
                major, minor, patch = (int(g) if g else 0 for g in match.groups())
                # 1.8.0 这类老版本号，把 major 归一到 8，便于与 17/21 比较
                if major == 1 and minor:
                    return (minor, patch, 0)
                return (major, minor, patch)
            match = re.search(r"javac\s+(\d+)", text)
            if match:
                return (int(match.group(1)), 0, 0)
        return ()

    # ---- 候选路径来源 -----------------------------------------------------

    @classmethod
    def _candidate_paths(cls, key: str) -> list[str]:
        """按优先级返回去重后的候选路径。"""
        names = CANDIDATE_NAMES.get(key, ())
        found: list[str] = []

        # 1) PATH
        from shutil import which

        for name in names:
            located = which(name)
            if located and os.path.isfile(located):
                found.append(located)

        # 2) 注册表
        found.extend(cls._registry_paths(key))

        # 3) 常见目录
        found.extend(cls._scan_dirs(key, names))

        # 4) MSVC：不在 PATH 上，也不是简单的同名查找，单独走 Visual Studio 布局
        found.extend(msvc_candidates(key))

        # 注意：这里**不能**因为"同目录下有 java.exe"就把 java.exe 收进 javac 的候选集。
        # javac 与 java 的候选目录本来就相同（注册表模板与扫描模板对两者一致），
        # 各自按自己的可执行名去找即可。混入对方会让 probe() 把 java.exe 当成 javac。

        # 去重保序（Windows 路径大小写不敏感）
        seen: set[str] = set()
        unique: list[str] = []
        for item in found:
            try:
                resolved = str(Path(item).resolve())
            except OSError:
                resolved = item
            key_name = os.path.normcase(resolved)
            if key_name not in seen:
                seen.add(key_name)
                unique.append(resolved)
        return unique

    @classmethod
    def _registry_paths(cls, key: str) -> list[str]:
        """从注册表读取安装路径（Python 与 JDK 都会登记）。"""
        if sys.platform != "win32":
            return []
        try:
            import winreg
        except ImportError:
            return []

        results: list[str] = []
        candidates: list[tuple[int, str, str, str]] = [
            # (hive, 子键, 值名, 拼接的相对可执行文件)
        ]
        if key == "python":
            candidates = [
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Python\PythonCore", "InstallPath", "python.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Python\PythonCore", "InstallPath", "python.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Python\PythonCore", "InstallPath", "python.exe"),
            ]
        elif key in ("javac", "java"):
            exe = "javac.exe" if key == "javac" else "java.exe"
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                candidates.append((hive, r"SOFTWARE\JavaSoft\JDK", "JavaHome", f"bin\\{exe}"))
                candidates.append((hive, r"SOFTWARE\JavaSoft\Java Development Kit", "JavaHome", f"bin\\{exe}"))
                candidates.append((hive, r"SOFTWARE\JavaSoft\JRE", "JavaHome", f"bin\\{exe}"))
                candidates.append((hive, r"SOFTWARE\JavaSoft\Java Runtime Environment", "JavaHome", f"bin\\{exe}"))
        else:
            return []

        for hive, subkey, value_name, relative in candidates:
            try:
                with winreg.OpenKey(hive, subkey) as root:
                    index = 0
                    while True:
                        try:
                            version_name = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(root, version_name) as version_key:
                                home, _ = winreg.QueryValueEx(version_key, value_name)
                        except OSError:
                            continue
                        exe_path = Path(str(home)) / relative
                        if exe_path.is_file():
                            results.append(str(exe_path))
            except OSError:
                continue
        return results

    @classmethod
    def _scan_dirs(cls, key: str, names: Sequence[str], *, max_entries: int = 40) -> list[str]:
        """扫描常见安装目录，含一层子目录（Python312 / jdk-21.0.1 这类）。"""
        results: list[str] = []
        dirs = list(extra_dirs(key))

        # 通配模板展开成实际存在的目录
        from glob import glob as glob_paths

        for pattern in glob_dirs(key):
            expanded = os.path.expandvars(pattern)
            if "%" in expanded:
                continue          # 环境变量不存在，展开失败，跳过
            results_dirs = sorted(glob_paths(expanded))[:max_entries]
            if results_dirs:
                dirs.extend(results_dirs)
        if key in ("cpp", "c"):
            exe_names = tuple(f"{name}.exe" for name in names)
        elif key == "python":
            exe_names = ("python.exe",)
        else:
            exe_names = tuple(f"{name}.exe" for name in names)

        for raw in dirs:
            expanded = Path(os.path.expandvars(raw))
            if not expanded.is_dir():
                continue

            search_dirs = [expanded]
            try:
                for child in sorted(os.listdir(expanded))[:max_entries]:
                    child_path = expanded / child
                    if child_path.is_dir() and _SUBDIR_HINTS.match(child):
                        search_dirs.append(child_path)
            except OSError:
                pass

            for directory in search_dirs:
                for exe_name in exe_names:
                    candidate = directory / exe_name
                    if candidate.is_file():
                        results.append(str(candidate))
        return results

    @classmethod
    def _sort_key(cls, info: CompilerInfo) -> tuple:
        """排序键：**先比家族优先级，再比版本号**。

        家族优先放在版本号前面是刻意的：同一台机器上 cl.exe 的版本号（19.44）
        天然大于 Dev-Cpp 的 g++（4.9），若按版本排序，装了 VS 的机器会一律
        选中 MSVC —— 而对刷题场景来说 GCC 才是正确答案（``bits/stdc++.h``、
        ``%lld``、与题解一致的报错信息）。详见 :data:`_FAMILY_RANK`。
        """
        family = _FAMILY_RANK.get(compiler_family(info.path), 1)
        # 版本元组补齐到 4 位，避免 (12,) 与 (12,1,0) 比较时长度不一致
        version = info.version_tuple + (0,) * (4 - len(info.version_tuple))
        return (family, *version[:4])
