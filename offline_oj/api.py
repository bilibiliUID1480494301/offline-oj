"""离线 OJ 的高层函数接口 / High-level function API for offline_oj.

中文：
    内核（``offline_oj.core.*``）为了驱动 GUI，类型设计得比较细。
    这一模块把最常见的三件事封装成"喂普通数据、拿普通 dict"的函数：

    1. :func:`judge` —— 给一段代码和测试点，返回评测结果摘要（自动探测
       语言、自动找编译器）；
    2. :func:`detect_similarity` —— 给一批提交 dict，跑雷同检测；
    3. :func:`export_report` —— 把成绩数据导出为 docx / xlsx / txt / csv。

    加密协议栈（零依赖的 X25519 / SM4-GCM / ChaCha20-Poly1305 套件）从
    ``offline_oj.net.crypto`` 重导出，方便单独复用。

English:
    The core modules are typed for GUI use. This module wraps the three most
    common tasks into plain-data-in / plain-dict-out functions, plus re-exports
    the dependency-free crypto suites from ``offline_oj.net.crypto``.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .core.compilers import CompilerDetector
from .core.export import ExportData, export, export_bundle
from .core.judge import Judge
from .core.models import Language, Problem, TestCase, Verdict
from .core.runners import make_runner
from .core.similarity import analyse as _analyse

__all__ = [
    "judge",
    "detect_similarity",
    "export_report",
    "ExportData",
    "export",
    "export_bundle",
    "SUPPORTED_SUITES",
]

# 加密套件重导出 / crypto re-exports
from .net.crypto import SUPPORTED_SUITES  # noqa: E402,F401

#: 各语言默认的编译/解释器探测键 / compiler probe keys per language
_DETECT_KEYS = {"cpp": "cpp", "c": "c", "java": "javac"}


def _guess_language(code: str) -> str:
    head = code.lstrip()[:200].lower()
    if head.startswith("#include") or "std::" in head:
        return "cpp"
    if head.startswith("#include <stdio") and "cout" not in head:
        return "c"
    if "public class" in head and "static void main" in head:
        return "java"
    return "python"


def _resolve_language(language: str | None, code: str) -> Language:
    key = (language or _guess_language(code)).strip().lower()
    aliases = {"py": "python", "python3": "python", "c++": "cpp", "g++": "cpp"}
    key = aliases.get(key, key)
    try:
        return Language(key)
    except ValueError as exc:
        raise ValueError(
            f"不支持的语言 / unsupported language: {language!r} "
            f"(可选 / available: cpp, c, python, java)"
        ) from exc


def _toolchain_paths(language: Language) -> dict[str, str]:
    """编译/解释器路径表 / toolchain paths for :func:`make_runner`."""
    if language == Language.PYTHON:
        return {"python": sys.executable}
    key = _DETECT_KEYS.get(language.value, "")
    if not key:
        raise ValueError(f"{language.value} 需要显式提供编译器路径 / no auto-detect for this language")
    info = CompilerDetector.detect(key)
    if info is None or not info.path:
        raise RuntimeError(
            f"未找到 {key} 工具链 / {key} toolchain not found on PATH；"
            f"请先安装或改用 python 提交 / install it or submit in python"
        )
    return {key: info.path}


def judge(
    code: str,
    tests: Sequence[Mapping[str, str] | tuple[str, str]],
    *,
    language: str | None = None,
    time_limit_ms: int = 1000,
    memory_mb: int = 256,
    optimize: bool | None = None,
    work_dir: str | Path | None = None,
    problem_id: str = "api",
    problem_title: str = "API 题目",
) -> dict[str, Any]:
    """Judge a snippet against test cases and return a plain summary dict.

    Args:
        code: source code / 源代码。
        tests: test cases — each a ``{"input": ..., "output": ...}`` dict or a
            ``(input, output)`` tuple / 测试点列表，字典或二元组均可。
        language: ``"cpp" | "c" | "python" | "java"``; auto-detected from the
            code when omitted / 不传则按代码内容自动探测。
        time_limit_ms: per-case wall limit in milliseconds / 单点时限（毫秒）。
        memory_mb: per-case memory limit in MB / 单点内存限制（MB）。
        optimize: enable ``-O2`` for C/C++; default True for compiled languages /
            C/C++ 是否开优化，默认开。
        work_dir: scratch directory; a temp dir when omitted / 工作目录，缺省用临时目录。
        problem_id, problem_title: labels carried into the summary / 汇总里带的题目标识。

    Returns:
        dict with ``verdict`` / ``passed`` / ``total`` / ``max_time_ms`` /
        ``compile_ok`` / ``compile_message`` / ``cases`` (per-case results)。

    评测一段代码并返回普通 dict 摘要：语言自动探测、编译器自动寻找，
    判完即清理工作目录。
    """
    lang = _resolve_language(language, code)
    cases: list[TestCase] = []
    for i, t in enumerate(tests, 1):
        if isinstance(t, dict):
            cases.append(TestCase(input=str(t.get("input", "")),
                                  output=str(t.get("output", "")),
                                  name=str(t.get("name") or f"case{i}")))
        else:
            inp, outp = t
            cases.append(TestCase(input=str(inp), output=str(outp), name=f"case{i}"))
    problem = Problem(
        id=problem_id, title=problem_title,
        time_limit=int(time_limit_ms), memory_limit=int(memory_mb),
        testcases=cases,
    )

    paths = _toolchain_paths(lang)
    if optimize is None:
        optimize = lang in (Language.CPP, Language.C)

    tmp_mgr = None
    work_root = str(work_dir) if work_dir else None
    if work_root is None:
        tmp_mgr = tempfile.TemporaryDirectory(prefix="offline_oj_api_")
        work_root = tmp_mgr.name
    try:
        runner = make_runner(lang, paths, work_root, optimize=optimize)
        report = Judge(runner).judge(problem, code)
        return {
            "verdict": report.verdict.value,
            "passed": report.passed,
            "total": report.total,
            "max_time_ms": round(report.max_time_ms, 1),
            "compile_ok": report.compile_ok,
            "compile_message": report.compile_message.strip(),
            "elapsed_s": round(report.elapsed_s, 3),
            "language": report.language.value if report.language else "",
            "cases": [
                {
                    "index": o.index,
                    "verdict": o.verdict.value,
                    "passed": o.verdict.value == "AC",
                    "time_ms": round(o.time_ms, 1),
                    "memory_mb": round(o.memory_mb, 1),
                    "points": o.points,
                }
                for o in report.outcomes
            ],
        }
    finally:
        if tmp_mgr is not None:
            tmp_mgr.cleanup()


def detect_similarity(
    rows: Iterable[dict[str, Any]],
    *,
    problem_titles: Mapping[str, str] | None = None,
    min_similarity: float | None = None,
    top_pairs: int | None = None,
) -> Any:
    """Run plagiarism/similarity detection over submission rows.

    Args:
        rows: iterable of dicts with keys ``serial / device_id / username /
            problem_id / language / code / score / verdict / attempt``
            (all tolerant of missing keys) / 提交记录字典的可迭代对象，键见上，缺省容错。
        problem_titles: ``problem_id → title`` for nicer reports / 题目名映射。
        min_similarity: report threshold (default = the core default) / 上报阈值。
        top_pairs: how many top pairs to keep / 保留的 Top 对数。

    Returns:
        :class:`offline_oj.core.similarity.Report` (dataclass: ``exact_groups``,
        ``similar_pairs``, ``matrix`` …) / 雷同检测报告。

    跑一次雷同检测：完全重复（确定性）与高度相似（winnowing 启发式，需人工复核）。
    """
    kwargs: dict[str, Any] = {}
    if min_similarity is not None:
        kwargs["min_similarity"] = min_similarity
    if top_pairs is not None:
        kwargs["top_pairs"] = top_pairs
    return _analyse(rows, problem_titles=dict(problem_titles or {}), **kwargs)


def export_report(
    path: str | Path,
    data: ExportData,
    fmt: str = "docx",
    sections: Iterable[str] | None = None,
) -> list[Path]:
    """Export competition data to docx / xlsx / txt / csv.

    Args:
        path: target file (csv uses it as a stem and writes two files) / 目标
            文件路径（csv 以此为主干名写两个文件）。
        data: :class:`offline_oj.core.export.ExportData` — build it from the
            archive / 成绩数据，从存档构建。
        fmt: ``"docx" | "xlsx" | "txt" | "csv"``。
        sections: optional subset of sections / 可选的 section 子集。

    Returns:
        list of written file paths / 实际写出的文件列表。

    导出成绩数据；fmt 与 section 语义见内核 :func:`offline_oj.core.export.export`。
    """
    kwargs: dict[str, Any] = {}
    if sections is not None:
        kwargs["sections"] = sections
    return export(path, data, fmt, **kwargs)
