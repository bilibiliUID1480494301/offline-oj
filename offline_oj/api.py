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
    "similarity_diff",
    "export_report",
    "ExportData",
    "export",
    "export_bundle",
    "open_repository",
    "Repo",
    "import_problems",
    "export_problems",
    "list_exam_archives",
    "read_exam_archive",
    "load_roster",
    "generate_passcode",
    "check_toolchain",
    "similarity",
    "encrypt_message",
    "decrypt_message",
    "SUPPORTED_SUITES",
]

# 加密套件重导出 / crypto re-exports
from .net.crypto import SUPPORTED_SUITES  # noqa: E402,F401
from .net.crypto import (  # noqa: E402,F401
    DEFAULT_SUITE,
    SUPPORTED_SUITES as _ALL_SUITES,
    aead_decrypt_suite,
    aead_encrypt_suite,
    derive_secret_key,
    negotiate_suite,
    random_nonce,
    random_salt,
)
from .core.compilers import CompilerDetector
from .core.roster import generate_passcode

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


# ---------------------------------------------------------------------------
# 题库 / 题目导入导出 / 考试档案 / 花名册 / 工具链 / 加密便捷层
# ---------------------------------------------------------------------------


class Repo:
    """题库的薄封装 / Thin wrapper over :class:`ProblemRepository`.

    中文：``open_repository(dir)`` 创建；add/get/list/search/remove 直接
    喂 dict、拿 dict，省去理解内部类型。底层就是 ProblemRepository，
    与 GUI / CLI 完全同一份文件格式（problems.json + problem_resources/）。

    English: thin facade over ``ProblemRepository`` — feed dicts, get dicts.
    Same on-disk format as the GUI and the CLI.
    """

    def __init__(self, data_dir):
        from .core.repository import ProblemRepository

        base = Path(data_dir)
        base.mkdir(parents=True, exist_ok=True)
        self._repo = ProblemRepository(
            base / "problems.json", base / "problem_resources"
        )
        self._repo.load()

    # -- 查询 / queries ------------------------------------------------------

    def count(self):
        return len(self._repo)

    def ids(self):
        return self._repo.ids()

    def get(self, problem_id):
        """单题详情 dict；不存在返回 None / one problem as dict, or None."""
        p = self._repo.get(problem_id)
        return p.to_dict() if p else None

    def list_problems(self):
        return [p.to_dict() for p in self._repo.all()]

    def search(self, query):
        return [p.to_dict() for p in self._repo.search(query)]

    def stats(self):
        s = self._repo.stats()
        return {"total": s.total, "testcases": s.testcases,
                "with_description": s.with_description, "latest": s.latest}

    # -- 变更 / mutations ----------------------------------------------------

    def add(self, problem, *, strategy: str = "rename"):
        """添加/更新一道题（喂 dict），返回题目 ID / add one problem, return its id."""
        from .core.models import Problem as _Problem

        p = problem if isinstance(problem, _Problem) else _Problem.from_dict(dict(problem))
        pid = self._repo.put(p, strategy=strategy)
        self.save()  # 底库只改内存，封装层负责落盘
        return pid

    def remove(self, problem_id):
        removed = self._repo.remove(problem_id) is not None
        if removed:
            self.save()
        return removed

    def save(self):
        self._repo.save()

    def __len__(self):
        return len(self._repo)

    def __contains__(self, problem_id):
        return problem_id in self._repo


def open_repository(data_dir):
    """打开（不存在则创建）一个题库 / open (or create) a problem repository.

    目录里生成 ``problems.json`` 与 ``problem_resources/``，与 GUI / CLI 同格式。
    """
    return Repo(data_dir)


def import_problems(repo, path, *, strategy: str = "rename"):
    """Import problems from a zip / folder / single JSON into a repo.

    按扩展名自动选择导入方式（.zip / 目录 / 单个 .json），返回
    ``{"imported": n, "skipped": n, "failed": n, "overwritten": n, "renamed": n}``。
    """
    from .core.archive import ProblemArchive

    target = Path(path)
    pa = ProblemArchive(repo._repo)  # noqa: SLF001 — 同包内的封装层
    if target.is_dir():
        report = pa.import_folder(target, strategy=strategy)
    elif target.suffix.lower() == ".zip":
        report = pa.import_zip(target, strategy=strategy)
    else:
        report = pa.import_single(target, strategy=strategy)
    return {"imported": report.imported, "skipped": report.skipped,
            "failed": report.failed, "overwritten": report.overwritten,
            "renamed": report.renamed}


def export_problems(repo, zip_path, problem_ids=None):
    """Export problems (all, or the given ids) into a zip / 打包导出题目。

    Returns the number of problems written / 返回导出的题目数。
    """
    from .core.archive import ProblemArchive

    pa = ProblemArchive(repo._repo)  # noqa: SLF001 — 同包内的封装层
    if problem_ids is None:
        problems = repo._repo.all()  # noqa: SLF001
    else:
        problems = [p for pid in problem_ids if (p := repo._repo.get(pid))]
    return pa.export_zip(zip_path, problems)


def list_exam_archives(root):
    """List exam archives under a directory / 列出目录下的全部考试档案。

    每项含 ``name / directory / started_at / contestants / problems / integrity_ok``。
    """
    from .core.records import list_archives as _list

    return [s.__dict__ | {"directory": str(s.directory)} for s in _list(root)]


def read_exam_archive(directory):
    """Read one exam archive / 读回一份考试档案。

    Returns record (meta + roster + leaderboard) / submissions / integrity_ok。
    """
    from .core.records import read_archive as _read

    a = _read(directory)
    return {
        "record": a.record.to_dict(),
        "manifest": a.manifest.to_dict(),
        "submissions": a.submissions,
        "leaderboard": a.leaderboard,
        "integrity_ok": a.integrity_ok,
        "skipped_lines": a.skipped_lines,
    }


def load_roster(source):
    """Load a contestant roster from CSV text or file / 从 CSV 文本或文件读名单。

    Returns :class:`offline_oj.core.roster.Roster`（可迭代出 Contestant）。
    """
    from .core.roster import Roster

    p = Path(source)
    if p.exists():
        return Roster.load_csv(p)
    return Roster.from_csv_text(str(source))


def check_toolchain(*, keys=("cpp", "c", "python", "javac", "java")):
    """Detect and self-test local toolchains / 探测并自检本机工具链。

    Returns ``{key: {found, path, version, works, detail}}``——建站/开考前
    先跑一遍，能省掉"学生端编译不了"的一半排查。
    """
    result = {}
    for key, info in CompilerDetector.detect_all(keys).items():
        if key == "python" and (info is None or not info.works):
            # 当前解释器本身就是 python：PATH 里没有 python 命令也算可用
            info = info.with_status(True, sys.executable) if info else None
            result[key] = {"found": True, "path": sys.executable,
                           "version": "", "works": True, "detail": "sys.executable"}
            continue
        result[key] = {
            "found": info is not None,
            "path": info.path if info else "",
            "version": info.version if info else "",
            "works": bool(info and info.works),
            "detail": info.detail if info else "",
        }
    return result


def similarity_diff(left, right, *, language: str = "cpp"):
    """Line-level diff of two submissions / 两份代码的行级差异。

    Returns a list of ``{"kind": "same|add|del|change", "left": n, "right": n, ...}``。
    """
    from .core.similarity import diff_lines as _diff

    return [d.__dict__ for d in _diff(left, right, language=language)]


def encrypt_message(key, plaintext, *, aad=b""):
    """AEAD encrypt with a random nonce prefix / 加密（随机 nonce 前缀，开箱即用）。

    ``返回 bytes = nonce(12B) || ciphertext+tag``。key 长度须匹配套件
    （ChaCha20=32B；SM4-GCM=16B，见 ``offline_oj.api.SUPPORTED_SUITES``）。
    """
    nonce = random_nonce()
    return nonce + aead_encrypt_suite(DEFAULT_SUITE, key, nonce, plaintext, aad)


def decrypt_message(key, blob, *, aad=b""):
    """Decrypt a blob produced by :func:`encrypt_message` / 解密 encrypt_message 的产物。"""
    return aead_decrypt_suite(DEFAULT_SUITE, key, blob[:12], blob[12:], aad)
