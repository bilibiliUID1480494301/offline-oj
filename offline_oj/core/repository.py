"""题库持久化。

数据一致性策略（原本完全没有，是"题目莫名消失"的根源）：

1. **原子替换** —— 先写 ``problems.json.tmp``，再 ``os.replace`` 覆盖。
   即便保存过程中断电，磁盘上要么是旧文件、要么是新文件，不会出现半截 JSON。
2. **滚动备份** —— 每次成功保存前把旧文件复制为 ``problems.json.bak``，
   误删题目或数据损坏时还有一次回退机会。
3. **损坏隔离** —— 打开时若 JSON 无法解析，把文件改名为 ``.broken-<时间戳>`` 后
   以空题库启动，并把线索写进日志，而不是静默丢数据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .models import Language, Problem, TestCase, Verdict
from .validation import PathValidator

log = logging.getLogger(__name__)

#: 导入冲突策略
SKIP = "skip"
OVERWRITE = "overwrite"
RENAME = "rename"


@dataclass
class RepositoryStats:
    """题库概况，用于状态栏与概览面板。"""

    total: int = 0
    testcases: int = 0
    with_description: int = 0
    latest: str = ""

    def as_text(self) -> str:
        if not self.total:
            return "题库为空"
        return (f"共 {self.total} 道题 · {self.testcases} 个测试点"
                + (f" · 最近更新 {self.latest}" if self.latest else ""))


class ProblemRepository:
    """题库读写。所有变更方法都只改内存，需显式 :meth:`save` 落盘。"""

    def __init__(
        self,
        problems_file: str | os.PathLike[str],
        resources_dir: str | os.PathLike[str],
        *,
        submissions_file: str | os.PathLike[str] | None = None,
    ) -> None:
        self.problems_file = Path(problems_file)
        self.resources_dir = Path(resources_dir)
        self.submissions_file = Path(submissions_file) if submissions_file else None
        self._problems: dict[str, Problem] = {}
        self._lock = threading.RLock()
        self._dirty = False

    # ---- 加载 / 保存 ------------------------------------------------------

    def load(self) -> "ProblemRepository":
        """读取题库。文件不存在时视为空题库。"""
        with self._lock:
            self.problems_file.parent.mkdir(parents=True, exist_ok=True)
            self.resources_dir.mkdir(parents=True, exist_ok=True)

            if not self.problems_file.exists():
                self._problems = {}
                log.info("题库文件不存在，将创建: %s", self.problems_file)
                return self

            try:
                payload = json.loads(self.problems_file.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("题库根节点不是对象")
            except Exception as exc:
                log.error("题库文件损坏，已隔离: %s", exc)
                self._quarantine()
                self._problems = {}
                return self

            problems: dict[str, Problem] = {}
            skipped = 0
            for key, value in payload.items():
                if not isinstance(value, dict):
                    skipped += 1
                    continue
                data = dict(value)
                data.setdefault("id", key)
                try:
                    problem = Problem.from_dict(data)
                except Exception:
                    skipped += 1
                    continue
                if problem.id:
                    problems[problem.id] = problem
            self._problems = problems
            log.info("已加载 %d 道题目（跳过 %d 条损坏记录）", len(problems), skipped)
            return self

    def save(self) -> None:
        """原子写盘，并保留一份 ``.bak``。"""
        with self._lock:
            self.problems_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                problem.id: problem.to_dict()
                for problem in sorted(self._problems.values(), key=lambda p: p.id)
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            tmp = self.problems_file.with_name(self.problems_file.name + ".tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                if self.problems_file.exists():
                    shutil.copy2(self.problems_file,
                                 self.problems_file.with_name(self.problems_file.name + ".bak"))
                os.replace(tmp, self.problems_file)
                self._dirty = False
            except Exception as exc:
                log.error("保存题库失败: %s", exc)
                tmp.unlink(missing_ok=True)
                raise

    def save_if_dirty(self) -> bool:
        if self._dirty:
            self.save()
            return True
        return False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---- 查询 -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._problems)

    def __contains__(self, problem_id: object) -> bool:
        return str(problem_id) in self._problems

    def __iter__(self) -> Iterator[Problem]:
        return iter(self.all())

    def get(self, problem_id: str) -> Problem | None:
        with self._lock:
            return self._problems.get(problem_id)

    def all(self) -> list[Problem]:
        with self._lock:
            return sorted(self._problems.values(), key=lambda p: p.id)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._problems.keys())

    def search(self, query: str) -> list[Problem]:
        """按 ID / 标题 / 来源模糊搜索，忽略大小写。"""
        text = (query or "").strip().lower()
        if not text:
            return self.all()
        with self._lock:
            matched = [
                problem for problem in self._problems.values()
                if text in problem.id.lower()
                or text in problem.title.lower()
                or text in problem.source.lower()
            ]
        return sorted(matched, key=lambda p: p.id)

    def stats(self) -> RepositoryStats:
        with self._lock:
            problems = list(self._problems.values())
        if not problems:
            return RepositoryStats()
        latest = max((p.updated_at for p in problems if p.updated_at), default="")
        return RepositoryStats(
            total=len(problems),
            testcases=sum(p.testcase_count for p in problems),
            with_description=sum(1 for p in problems if p.description.strip()),
            latest=latest[:10],
        )

    def next_id(self, prefix: str = "P", start: int = 1001) -> str:
        """生成下一个可用 ID，形如 ``P0001``。"""
        with self._lock:
            used = set(self._problems.keys())
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        highest = start - 1
        for problem_id in used:
            match = pattern.match(problem_id)
            if match:
                highest = max(highest, int(match.group(1)))
        candidate = highest + 1
        while f"{prefix}{candidate}" in used:
            candidate += 1
        return f"{prefix}{candidate}"

    def suggest_id(self, title: str) -> str:
        """由标题生成一个语义化 ID 候选，失败则回落到序号。"""
        cleaned = PathValidator.safe_filename(title, fallback="") if title else ""
        cleaned = re.sub(r"[\s]+", "_", cleaned).strip("_")[:24]
        ascii_only = re.sub(r"[^0-9A-Za-z_\-]", "", cleaned)
        if len(ascii_only) >= 3:
            candidate = ascii_only
            with self._lock:
                if candidate not in self._problems:
                    return candidate
            return self.next_id(prefix=f"{candidate}_")
        return self.next_id()

    # ---- 变更 -------------------------------------------------------------

    def put(self, problem: Problem, *, strategy: str = RENAME, touch: bool = True) -> str:
        """新增或按策略处理冲突，返回最终落库的 ID。

        :param strategy: ``skip`` / ``overwrite`` / ``rename``
        """
        with self._lock:
            final_id = problem.id
            if final_id in self._problems:
                if strategy == SKIP:
                    return final_id
                if strategy == RENAME:
                    final_id = self._unique_id(final_id)
                    problem = problem.clone(final_id)
                # overwrite 走下面的直接赋值
            if touch:
                problem.touch()
            self._problems[final_id] = problem
            self._dirty = True
            return final_id

    def remove(self, problem_id: str) -> Problem | None:
        with self._lock:
            problem = self._problems.pop(problem_id, None)
            if problem is not None:
                self._dirty = True
            return problem

    def rename(self, old_id: str, new_id: str) -> tuple[bool, str]:
        """改 ID（键与内部字段同步改）。"""
        new_id = (new_id or "").strip()
        if not new_id:
            return False, "新 ID 不能为空"
        if new_id == old_id:
            return True, ""
        with self._lock:
            if old_id not in self._problems:
                return False, "题目不存在"
            if new_id in self._problems:
                return False, f"ID {new_id} 已被占用"
        problem = self._problems.pop(old_id)
        data = problem.to_dict()
        data["id"] = new_id
        self._problems[new_id] = Problem.from_dict(data)
        self._dirty = True
        return True, ""

    def replace_all(self, problems: Iterable[Problem]) -> None:
        with self._lock:
            self._problems = {problem.id: problem for problem in problems}
            self._dirty = True

    def _unique_id(self, base: str) -> str:
        counter = 1
        candidate = f"{base}_{counter}"
        while candidate in self._problems:
            counter += 1
            candidate = f"{base}_{counter}"
        return candidate

    def _quarantine(self) -> None:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            broken = self.problems_file.with_name(f"{self.problems_file.name}.broken-{stamp}")
            os.replace(self.problems_file, broken)
            log.warning("损坏题库已改名保留: %s", broken)
        except Exception:
            pass

    # ---- 题目资源 ---------------------------------------------------------

    def resource_path(self, name: str) -> Path | None:
        """定位题目资源（图片）。名称不可信，须经目录逃逸检查。"""
        try:
            path = Path(PathValidator.safe_join(self.resources_dir, name))
        except ValueError:
            log.warning("拒绝非法的资源名: %s", name)
            return None
        return path if path.is_file() else None

    def import_resource(self, source: str | os.PathLike[str], name: str | None = None) -> str:
        """把外部图片复制进资源目录，返回资源名。

        默认用 UUID 重命名，避免同名图片互相覆盖（原本就是这样，这里保持）。
        """
        import uuid

        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(str(source))
        self.resources_dir.mkdir(parents=True, exist_ok=True)

        resource_name = name or f"{uuid.uuid4().hex}{source_path.suffix.lower()}"
        try:
            destination = Path(PathValidator.safe_join(self.resources_dir, resource_name))
        except ValueError as exc:
            raise ValueError(f"非法资源名: {resource_name}") from exc

        shutil.copy2(source_path, destination)
        return resource_name

    def list_orphan_resources(self) -> list[str]:
        """列出没有被任何题目引用的资源文件（清理用）。"""
        if not self.resources_dir.is_dir():
            return []
        used: set[str] = set()
        with self._lock:
            for problem in self._problems.values():
                used.update(problem.used_assets())
        orphans: list[str] = []
        for path in sorted(self.resources_dir.rglob("*")):
            if path.is_file():
                relative = path.relative_to(self.resources_dir).as_posix()
                if relative not in used:
                    orphans.append(relative)
        return orphans


@dataclass
class SubmissionRecord:
    """一次提交的留档记录。"""

    problem_id: str
    title: str
    language: str
    verdict: str
    passed: int
    total: int
    time_ms: float = 0.0
    memory_mb: float = 0.0
    at: str = ""
    #: 判这次提交时的编译优化开关。同一份代码开不开优化能差好几倍耗时，
    #: 留档里没有这一项的话，回看历史记录会把"参数差异"误读成"代码变快了"。
    optimized: bool = False

    def to_dict(self) -> dict[str, object]:
        data = {
            "problem_id": self.problem_id,
            "title": self.title,
            "language": self.language,
            "verdict": self.verdict,
            "passed": self.passed,
            "total": self.total,
            "time_ms": round(self.time_ms, 2),
            "memory_mb": round(self.memory_mb, 2),
            "at": self.at or datetime.now().isoformat(timespec="seconds"),
        }
        # 只写非默认值，老记录逐字节不变
        if self.optimized:
            data["optimized"] = True
        return data

    @classmethod
    def from_report(cls, report, language: Language) -> "SubmissionRecord":
        return cls(
            problem_id=report.problem_id,
            title=report.problem_title,
            language=language.value,
            verdict=report.verdict.value,
            passed=report.passed,
            total=report.total,
            time_ms=report.max_time_ms,
            memory_mb=report.max_memory_mb,
            optimized=bool(getattr(report, "optimized", False)),
        )


class SubmissionLog:
    """提交历史（JSON Lines，追加写，超过上限自动裁剪）。"""

    MAX_ENTRIES = 500

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, record: SubmissionRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            self._trim_if_needed()
        except Exception:
            log.warning("写入提交历史失败", exc_info=True)

    def recent(self, limit: int = 20) -> list[dict[str, object]]:
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        records: list[dict[str, object]] = []
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass

    def _trim_if_needed(self) -> None:
        try:
            if not self.path.is_file():
                return
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= self.MAX_ENTRIES:
                return
            kept = lines[-self.MAX_ENTRIES:]
            tmp = self.path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.debug("裁剪提交历史失败", exc_info=True)


def make_testcase(input_text: str, output_text: str) -> TestCase:
    """UI 便捷构造。"""
    return TestCase(input=input_text, output=output_text)


__all__ = [
    "ProblemRepository",
    "RepositoryStats",
    "SubmissionLog",
    "SubmissionRecord",
    "SKIP",
    "OVERWRITE",
    "RENAME",
    "Verdict",
    "make_testcase",
]
