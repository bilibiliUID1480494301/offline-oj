"""题目的导入与导出。

支持两种交换格式，与旧版保持兼容：

* **导出** —— ``problems/<ID>.json`` + ``resources/<图片>`` + ``index.json``，
  可以打包成 ZIP 或写成文件夹；
* **导入** —— 从 ZIP 或文件夹批量导入，冲突策略为跳过 / 覆盖 / 重命名。

安全要点：导入包里的文件名与题目描述里的图片名都是**不可信输入**。所有"写入到磁盘"
的操作都经过 :meth:`PathValidator.safe_join`，拒绝 ``..\\..\\Windows\\...`` 这类
目录穿越（压缩包内的路径穿越即 zip-slip 漏洞）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .models import Problem
from .repository import OVERWRITE, RENAME, SKIP, ProblemRepository
from .validation import PathValidator

log = logging.getLogger(__name__)

#: 导出包格式版本
ARCHIVE_VERSION = "2.0"

#: 资源在包内可能出现的目录前缀（兼容各种手工打包习惯）
RESOURCE_PREFIXES = ("resources/", "resource/", "images/", "assets/", "")

#: 进度回调：(已完成, 总数, 说明)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ImportReport:
    """导入结果汇总。"""

    imported: int = 0
    skipped: int = 0
    failed: int = 0
    overwritten: int = 0
    renamed: int = 0
    resources: int = 0
    logs: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return self.imported > 0 or self.overwritten > 0

    def log(self, message: str) -> None:
        self.logs.append(message)
        log.debug("[导入] %s", message)

    def text(self) -> str:
        lines = [f"来源: {self.source}" if self.source else "批量导入结果", "=" * 46]
        lines.extend(self.logs)
        lines.extend([
            "=" * 46,
            f"新增 {self.imported} · 覆盖 {self.overwritten} · 跳过 {self.skipped} · 失败 {self.failed}",
            f"复制资源 {self.resources} 个",
        ])
        return "\n".join(lines)


class ProblemArchive:
    """导入导出服务。"""

    def __init__(self, repository: ProblemRepository) -> None:
        self.repository = repository

    # ---- 导入：ZIP --------------------------------------------------------

    def import_zip(
        self,
        zip_path: str | os.PathLike[str],
        strategy: str = SKIP,
        progress: ProgressCallback | None = None,
    ) -> ImportReport:
        report = ImportReport(source=os.path.basename(str(zip_path)))
        try:
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                problem_entries = [
                    name for name in names
                    if name.endswith(".json")
                    and name not in ("index.json",)
                    and (name.startswith("problems/") or "/" not in name.rstrip("/"))
                ]
                if not problem_entries:
                    report.log("压缩包中未找到题目文件（期望 problems/*.json）")
                    return report

                report.log(f"发现 {len(problem_entries)} 个题目文件")
                for position, entry in enumerate(problem_entries, start=1):
                    if progress:
                        progress(position, len(problem_entries), f"导入 {entry}")
                    try:
                        raw = archive.read(entry).decode("utf-8")
                        data = json.loads(raw)
                    except Exception as exc:
                        report.failed += 1
                        report.log(f"✗ 解析失败 {entry}: {exc}")
                        continue
                    self._import_payload(data, entry, strategy, report,
                                         copier=lambda name, problem: self._copy_zip_asset(
                                             archive, names, name, problem))
        except zipfile.BadZipFile as exc:
            report.log(f"不是有效的压缩包: {exc}")
        except Exception as exc:
            log.exception("导入 ZIP 失败")
            report.log(f"处理失败: {exc}")
        return report

    # ---- 导入：文件夹 -----------------------------------------------------

    def import_folder(
        self,
        folder: str | os.PathLike[str],
        strategy: str = SKIP,
        progress: ProgressCallback | None = None,
    ) -> ImportReport:
        folder_path = Path(folder)
        report = ImportReport(source=str(folder_path))
        try:
            problems_dir = folder_path / "problems"
            search_dir = problems_dir if problems_dir.is_dir() else folder_path
            files = sorted(search_dir.glob("*.json"))
            files = [item for item in files if item.name != "index.json"]

            if not files:
                report.log("文件夹中未找到题目文件（期望 problems/*.json）")
                return report

            report.log(f"发现 {len(files)} 个题目文件")
            for position, item in enumerate(files, start=1):
                if progress:
                    progress(position, len(files), f"导入 {item.name}")
                try:
                    data = json.loads(item.read_text(encoding="utf-8"))
                except Exception as exc:
                    report.failed += 1
                    report.log(f"✗ 解析失败 {item.name}: {exc}")
                    continue
                self._import_payload(
                    data, item.name, strategy, report,
                    copier=lambda name, problem: self._copy_folder_asset(
                        folder_path, name, problem),
                )
        except Exception as exc:
            log.exception("导入文件夹失败")
            report.log(f"处理失败: {exc}")
        return report

    # ---- 导入：单题 -------------------------------------------------------

    def import_single(
        self,
        path: str | os.PathLike[str],
        strategy: str = RENAME,
    ) -> tuple[bool, str]:
        """导入单个 JSON 题目文件。返回 ``(是否成功, 说明)``。"""
        report = ImportReport(source=os.path.basename(str(path)))
        source_dir = Path(path).parent
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"读取失败: {exc}"
        self._import_payload(
            data, Path(path).name, strategy, report,
            copier=lambda name, problem: self._copy_folder_asset(source_dir, name, problem),
        )
        if report.failed:
            return False, report.logs[-1] if report.logs else "导入失败"
        return True, report.text()

    # ---- 导出 -------------------------------------------------------------

    def export_zip(
        self,
        zip_path: str | os.PathLike[str],
        problems: Sequence[Problem],
        progress: ProgressCallback | None = None,
    ) -> int:
        """打包导出，返回导出的题目数。"""
        total = len(problems)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for position, problem in enumerate(problems, start=1):
                if progress:
                    progress(position, total, f"导出 {problem.id}")
                safe_id = PathValidator.safe_filename(problem.id, "problem")
                archive.writestr(
                    f"problems/{safe_id}.json",
                    json.dumps(problem.to_dict(), ensure_ascii=False, indent=2),
                )
                for asset in problem.used_assets():
                    source = self.repository.resource_path(asset)
                    if source is None:
                        continue
                    try:
                        archive.write(source, f"resources/{asset}")
                    except Exception:
                        log.debug("打包资源失败: %s", asset)
            archive.writestr("index.json", json.dumps(
                self._index_payload(problems), ensure_ascii=False, indent=2
            ))
        return total

    def export_folder(
        self,
        folder: str | os.PathLike[str],
        problems: Sequence[Problem],
        progress: ProgressCallback | None = None,
    ) -> int:
        """导出为文件夹结构，返回导出的题目数。"""
        root = Path(folder)
        problems_dir = root / "problems"
        resources_dir = root / "resources"
        problems_dir.mkdir(parents=True, exist_ok=True)
        resources_dir.mkdir(parents=True, exist_ok=True)

        total = len(problems)
        for position, problem in enumerate(problems, start=1):
            if progress:
                progress(position, total, f"导出 {problem.id}")
            safe_id = PathValidator.safe_filename(problem.id, "problem")
            (problems_dir / f"{safe_id}.json").write_text(
                json.dumps(problem.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for asset in problem.used_assets():
                source = self.repository.resource_path(asset)
                if source is None:
                    continue
                try:
                    destination = Path(PathValidator.safe_join(resources_dir, asset))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                except Exception:
                    log.debug("复制资源失败: %s", asset)

        (root / "index.json").write_text(
            json.dumps(self._index_payload(problems), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return total

    def export_single(self, problem: Problem, path: str | os.PathLike[str]) -> None:
        """导出单个题目为 JSON。"""
        Path(path).write_text(
            json.dumps(problem.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- 内部 -------------------------------------------------------------

    def _import_payload(
        self,
        data: object,
        source_name: str,
        strategy: str,
        report: ImportReport,
        *,
        copier: Callable[[str, Problem], bool],
    ) -> None:
        """校验并落库一条题目数据。"""
        if not isinstance(data, dict):
            report.failed += 1
            report.log(f"✗ {source_name}: 不是 JSON 对象")
            return

        try:
            problem = Problem.from_dict(data)
        except Exception as exc:
            report.failed += 1
            report.log(f"✗ {source_name}: {exc}")
            return

        problems_found = problem.validate()
        # 导入时放宽描述要求：老题库里可能只有标题 + 测试点
        fatal = [item for item in problems_found if "描述" not in item]
        if fatal:
            report.failed += 1
            report.log(f"✗ {source_name}: {'；'.join(fatal)}")
            return

        original_id = problem.id
        already_exists = original_id in self.repository
        if already_exists and strategy == SKIP:
            report.skipped += 1
            report.log(f"→ 跳过已存在: {original_id}")
            return

        final_id = self.repository.put(problem, strategy=strategy)
        if already_exists and strategy == OVERWRITE:
            report.overwritten += 1
            report.log(f"↻ 覆盖: {final_id}")
        elif final_id != original_id:
            report.renamed += 1
            report.imported += 1
            report.log(f"＋ 导入 {original_id} → 重命名为 {final_id}")
        else:
            report.imported += 1
            report.log(f"＋ 导入 {final_id}")

        stored = self.repository.get(final_id) or problem
        for asset in stored.used_assets():
            if copier(asset, stored):
                report.resources += 1

    def _copy_zip_asset(self, archive: zipfile.ZipFile, names: list[str],
                        asset: str, problem: Problem) -> bool:
        """把 ZIP 里的资源复制到资源目录。"""
        target_name = self._asset_target(problem, asset)
        if target_name is None:
            return False
        normalized = asset.replace("\\", "/").lstrip("./")
        for prefix in RESOURCE_PREFIXES:
            candidate = f"{prefix}{normalized}"
            if candidate not in names:
                continue
            try:
                destination = Path(PathValidator.safe_join(self.repository.resources_dir, target_name))
            except ValueError as exc:
                log.warning("拒绝写入资源 %s: %s", asset, exc)
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(candidate) as source, open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                return True
            except Exception:
                log.debug("复制 ZIP 资源失败: %s", candidate, exc_info=True)
                return False
        return False

    def _copy_folder_asset(self, folder: Path, asset: str, problem: Problem) -> bool:
        """把文件夹里的资源复制到资源目录。"""
        target_name = self._asset_target(problem, asset)
        if target_name is None:
            return False
        normalized = asset.replace("\\", "/").lstrip("./")
        for prefix in RESOURCE_PREFIXES:
            candidate = folder / f"{prefix}{normalized}"
            if not candidate.is_file():
                continue
            try:
                destination = Path(PathValidator.safe_join(self.repository.resources_dir, target_name))
            except ValueError as exc:
                log.warning("拒绝写入资源 %s: %s", asset, exc)
                return False
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, destination)
                return True
            except Exception:
                log.debug("复制资源失败: %s", candidate, exc_info=True)
                return False
        return False

    @staticmethod
    def _asset_target(problem: Problem, asset: str) -> str | None:
        """计算资源在本地资源目录中的目标相对名。

        包里的资源名可能带目录前缀（``images/a.png``），本地统一拍平存放，
        与题目描述中的引用名保持一致，这样 Markdown 预览能直接找到图片。
        """
        normalized = asset.replace("\\", "/").lstrip("./")
        if not normalized or normalized.endswith("/"):
            return None
        name = normalized.split("/")[-1]
        if not name or name in (".", ".."):
            return None
        # 描述里的引用名可能本身就带路径，此时保留其结构
        return normalized if "/" in asset else name

    @staticmethod
    def _index_payload(problems: Iterable[Problem]) -> dict[str, object]:
        items = list(problems)
        return {
            "version": ARCHIVE_VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(items),
            "problems": [problem.id for problem in items],
        }
