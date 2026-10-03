"""应用上下文。

把"整个程序只有一个实例"的服务装在一起，用依赖注入的方式传给各个面板：

* 面板不自己去 ``open()`` 文件、不自己去拼 ``%LOCALAPPDATA%`` 路径 —— 那样会让
  路径规则散落各处，也让单元测试无法隔离；
* 需要什么服务，从上下文里取，测试时替换成临时目录的对象即可。

这层刻意不依赖 PySide6，因此可以在无 GUI 环境下构造。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .core.archive import ProblemArchive
from .core.repository import ProblemRepository, SubmissionLog
from .paths import AppPaths
from .settings import AppSettings


@dataclass
class AppContext:
    """应用级服务集合。"""

    paths: AppPaths
    settings: AppSettings
    repository: ProblemRepository
    archive: ProblemArchive
    submission_log: SubmissionLog

    @classmethod
    def create(cls, paths: AppPaths | None = None, *,
               settings: AppSettings | None = None) -> "AppContext":
        """按标准路径装配上下文。"""
        from .paths import build_paths

        resolved = (paths or build_paths()).ensure_layout()
        loaded_settings = settings or AppSettings(resolved.settings_file).load()
        repository = ProblemRepository(
            resolved.problems_file,
            resolved.resources_dir,
            submissions_file=resolved.submissions_dir / "history.jsonl",
        ).load()
        return cls(
            paths=resolved,
            settings=loaded_settings,
            repository=repository,
            archive=ProblemArchive(repository),
            submission_log=SubmissionLog(resolved.submissions_dir / "history.jsonl"),
        )

    # ---- 便捷转发 ---------------------------------------------------------

    def compiler_paths(self) -> dict[str, str]:
        return self.settings.compiler_paths()

    def problem_count(self) -> int:
        return len(self.repository)

    def shutdown(self) -> None:
        """退出前落盘。"""
        try:
            self.repository.save_if_dirty()
        finally:
            self.settings.save()

    def diagnostics(self) -> dict[str, str]:
        """供"关于"对话框与问题反馈使用。"""
        from . import __version__

        data = {
            "版本": __version__,
            "题目数": str(len(self.repository)),
        }
        data.update(self.paths.as_dict())
        return data


@dataclass
class UiState:
    """界面层的临时状态（不落盘）。"""

    judging: bool = False
    busy_message: str = ""
    last_language: str = "cpp"
    extras: dict[str, object] = field(default_factory=dict)
