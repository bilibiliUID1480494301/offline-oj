"""路径安全校验。

评测工具会把**用户提供的字符串**当作文件名和命令行参数使用（题目描述里的图片名、
导入包里的资源名、编译器路径）。这些字符串一旦被拼进命令行或文件路径，就可能造成：

* 命令注入 —— ``; rm -rf`` / ``& del`` 之类；
* 目录穿越 —— ``../../../../Windows/System32/x.dll`` 写到目标目录之外；
* 误删 —— 路径解析到系统目录。

因此所有"外部字符串 → 路径"的转换都必须经过本模块。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 命令行元字符：出现在路径里即可疑（Windows 文件名本身也不允许这些字符）
DANGEROUS_CHARS = (";", "|", "&", "$", "`", ">", "<", "\n", "\r", "\0")

#: 不允许作为导入/导出目标的系统敏感目录（小写前缀匹配）
PROTECTED_PREFIXES = tuple(
    p.lower()
    for p in (
        os.environ.get("SystemRoot", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    )
    if p
)


class PathValidator:
    """全部为静态方法的校验工具集。"""

    @staticmethod
    def is_safe(path: str | os.PathLike[str]) -> bool:
        """路径是否不含危险字符、不构成目录穿越。"""
        if not path:
            return False
        text = str(path)
        if any(char in text for char in DANGEROUS_CHARS):
            return False
        if "\0" in text:
            return False
        # 相对路径中出现 ".." 时要求归一化后仍不向上逃逸
        if ".." in text and not os.path.isabs(text):
            normalized = os.path.normpath(text)
            if normalized == ".." or normalized.startswith(f"..{os.sep}"):
                return False
        return True

    @staticmethod
    def validate_executable(path: str | os.PathLike[str]) -> tuple[bool, str]:
        """校验"可执行的编译器/解释器路径"。"""
        if not path:
            return False, "路径为空"
        text = str(path)
        if not PathValidator.is_safe(text):
            return False, "路径包含危险字符"
        if not os.path.exists(text):
            return False, "路径不存在"
        if not os.path.isfile(text):
            return False, "不是文件"
        if sys.platform == "win32":
            if not text.lower().endswith((".exe", ".bat", ".cmd", ".com")):
                return False, "不是有效的可执行文件（.exe/.bat/.cmd）"
        elif not os.access(text, os.X_OK):
            return False, "文件没有可执行权限"
        return True, "路径有效"

    @staticmethod
    def validate_directory(path: str | os.PathLike[str], *, must_exist: bool = True) -> tuple[bool, str]:
        """校验目录路径。"""
        if not path:
            return False, "路径为空"
        text = str(path)
        if not PathValidator.is_safe(text):
            return False, "路径包含危险字符"
        if must_exist and not os.path.isdir(text):
            return False, "目录不存在"
        return True, "路径有效"

    @staticmethod
    def is_protected(path: str | os.PathLike[str]) -> bool:
        """是否位于系统/程序目录（禁止作为导出目标）。"""
        if not path:
            return False
        try:
            resolved = os.path.abspath(str(path)).lower()
        except Exception:
            return True
        return any(resolved.startswith(prefix) for prefix in PROTECTED_PREFIXES)

    @staticmethod
    def safe_join(base: str | os.PathLike[str], *parts: str) -> str:
        """把不可信的相对名拼到基目录下，并确保结果不逃逸基目录。

        这是防"ZIP 目录穿越"（zip-slip）的关键：导入包中的资源名可能形如
        ``..\\..\\Startup\\evil.bat``，拼接后会写到基目录之外。

        :raises ValueError: 结果逃逸出基目录
        """
        base_path = Path(os.path.abspath(str(base)))
        candidate = base_path
        for part in parts:
            text = str(part).replace("\\", "/")
            if not text or text in (".", ".."):
                raise ValueError(f"非法的资源名: {part!r}")
            if os.path.isabs(text) or (len(text) > 1 and text[1] == ":"):
                raise ValueError(f"资源名不允许为绝对路径: {part!r}")
            candidate = candidate / text

        resolved = Path(os.path.abspath(str(candidate)))
        try:
            resolved.relative_to(base_path)
        except ValueError as exc:
            raise ValueError(f"路径逃逸出目标目录: {parts!r}") from exc
        return str(resolved)

    @staticmethod
    def safe_filename(name: str, fallback: str = "problem") -> str:
        """把任意字符串转成可用的文件名（替换 Windows 保留字符）。"""
        cleaned = "".join("_" if char in '\\/:*?"<>|' or ord(char) < 32 else char
                          for char in str(name))
        cleaned = cleaned.strip().strip(".")
        return cleaned or fallback
