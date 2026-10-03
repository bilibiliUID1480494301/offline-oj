"""后台工作线程。

界面"卡死"几乎总是因为在主线程里做了耗时操作。这里的每个类都是 ``QThread``，
把编译、判题、批量导入导出这些动辄数百毫秒到几十秒的动作搬离主线程，
只通过信号回传结果。主线程因此始终能响应拖动窗口、点击按钮。

线程与界面的通信严格单向：**工作线程发信号，主线程改控件**。
工作线程绝不直接碰任何控件（Qt 的非 GUI 线程访问控件是未定义行为）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..core.archive import ImportReport, ProblemArchive
from ..core import export as ex
from ..core.compilers import CompilerDetector, CompilerInfo
from ..core.judge import Judge, JudgeEvent
from ..core.models import JudgeConfig, Language, Problem
from ..core.repository import SubmissionLog, SubmissionRecord
from ..core.runners import make_runner, self_test
from ..core.sandbox import RunResult

log = logging.getLogger(__name__)


class CompilerDetectWorker(QThread):
    """自动检测本机编译器。"""

    progress = Signal(str, str)        # key, 说明
    detected = Signal(str, object)     # key, CompilerInfo | None
    done = Signal(dict)                # {key: CompilerInfo | None}

    def __init__(self, keys: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self._keys = keys

    def run(self) -> None:
        result: dict[str, CompilerInfo | None] = {}
        for key in self._keys:
            if self.isInterruptionRequested():
                break
            self.progress.emit(key, f"正在检测 {key} …")
            try:
                info = CompilerDetector.detect(key)
            except Exception:
                log.exception("检测 %s 失败", key)
                info = None
            result[key] = info
            self.detected.emit(key, info)
        self.done.emit(result)


class CompilerVerifyWorker(QThread):
    """对已配置的编译器做端到端自检（真编译 + 真运行）。"""

    progress = Signal(str, str)      # key, 说明
    verified = Signal(str, bool, str)  # key, 是否可用, 说明
    done = Signal(dict)              # {key: (bool, str)}

    #: 配置键 -> 语言
    KEY_LANGUAGES = {
        "cpp": Language.CPP,
        "c": Language.C,
        "python": Language.PYTHON,
        "javac": Language.JAVA,
        "java": Language.JAVA,
    }

    def __init__(self, paths: dict[str, str], work_root: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._paths = dict(paths)
        self._work_root = str(work_root)

    def run(self) -> None:
        results: dict[str, tuple[bool, str]] = {}
        # 同一语言只测一次（javac/java 合并为一次 Java 测试）
        tested: set[Language] = set()
        for key, language in self.KEY_LANGUAGES.items():
            if self.isInterruptionRequested():
                break
            label = {"cpp": "C++", "c": "C", "python": "Python",
                     "javac": "Java", "java": "Java"}[key]
            self.progress.emit(key, f"正在验证 {label} …")
            if language in tested:
                ok, message = results.get(f"lang:{language.value}", (False, "未验证"))
            else:
                try:
                    ok, message = self_test(language, self._paths, self._work_root)
                except Exception as exc:
                    log.exception("自检 %s 异常", language.value)
                    ok, message = False, f"自检异常: {exc}"
                results[f"lang:{language.value}"] = (ok, message)
                tested.add(language)
            results[key] = (ok, message)
            self.verified.emit(key, ok, message)
        self.done.emit(results)


class JudgeWorker(QThread):
    """判题线程。"""

    event = Signal(object)      # JudgeEvent
    crashed = Signal(str)       # 未预期的异常

    def __init__(
        self,
        problem: Problem,
        code: str,
        language: Language,
        compiler_paths: dict[str, str],
        work_root: str | Path,
        *,
        optimize: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._problem = problem
        self._code = code
        self._language = language
        self._paths = dict(compiler_paths)
        self._work_root = str(work_root)
        self._optimize = optimize
        self._stop = False
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """请求中止（由"停止判题"按钮触发）。

        置位事件后，评测循环会在**当前测试点跑完**的下一个检查点退出，
        不会硬杀线程 —— 直接 terminate() 会留下未被回收的子进程。
        """
        self._stop = True
        self._stop_event.set()

    def run(self) -> None:
        stop_event = self._stop_event
        try:
            runner = make_runner(self._language, self._paths, self._work_root,
                                 optimize=self._optimize)
            judge = Judge(runner, stop_event)
            for item in judge.iter_events(self._problem, self._code):
                if self._stop:
                    stop_event.set()
                self.event.emit(item)
        except Exception as exc:
            log.exception("判题线程异常")
            self.crashed.emit(str(exc))


class TestRunWorker(QThread):
    """"测试运行"：用自定义输入跑一次，不比对答案。

    会把题目的判题方式与英文名一并带下去 —— 题目若走文件输入输出，
    选手代码里的 ``freopen("poker.in", …)`` 必须有文件可读，否则自测结果
    与真实判题永远对不上。

    ``optimize`` 是同一个道理：提交按 -O2 编译、自测按 -O0，那么
    "自测跑得挺稳"到了判题就可能变成 TLE（或者白等一场）。
    这个值由写题面板的 O2 勾选传进来，与提交走同一条路。
    """

    done = Signal(object)       # RunResult
    crashed = Signal(str)

    def __init__(
        self,
        code: str,
        language: Language,
        compiler_paths: dict[str, str],
        work_root: str | Path,
        input_data: str,
        *,
        time_limit_ms: int = 10000,
        memory_limit_mb: int = 512,
        judge: JudgeConfig | None = None,
        name: str = "",
        optimize: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._code = code
        self._language = language
        self._paths = dict(compiler_paths)
        self._work_root = str(work_root)
        self._input = input_data
        self._time_limit = time_limit_ms
        self._memory_limit = memory_limit_mb
        self._judge = judge
        self._name = name
        self._optimize = optimize

    def run(self) -> None:
        try:
            runner = make_runner(self._language, self._paths, self._work_root,
                                 optimize=self._optimize)
            result: RunResult = runner.run(
                self._code, self._input, self._time_limit, self._memory_limit,
                judge=self._judge, name=self._name,
            )
            self.done.emit(result)
        except Exception as exc:
            log.exception("测试运行异常")
            self.crashed.emit(str(exc))


class ImportWorker(QThread):
    """批量导入。"""

    progress = Signal(int, int, str)
    done = Signal(object)       # ImportReport
    crashed = Signal(str)

    def __init__(self, archive: ProblemArchive, source: str, *, is_zip: bool,
                 strategy: str, parent=None) -> None:
        super().__init__(parent)
        self._archive = archive
        self._source = source
        self._is_zip = is_zip
        self._strategy = strategy

    def run(self) -> None:
        try:
            callback = lambda done, total, message: self.progress.emit(done, total, message)  # noqa: E731
            if self._is_zip:
                report: ImportReport = self._archive.import_zip(
                    self._source, self._strategy, callback)
            else:
                report = self._archive.import_folder(self._source, self._strategy, callback)
            self.done.emit(report)
        except Exception as exc:
            log.exception("批量导入异常")
            self.crashed.emit(str(exc))


class ExportWorker(QThread):
    """批量导出。"""

    progress = Signal(int, int, str)
    done = Signal(int)          # 导出题目数
    crashed = Signal(str)

    def __init__(self, archive: ProblemArchive, target: str, problems: list[Problem],
                 *, is_zip: bool, parent=None) -> None:
        super().__init__(parent)
        self._archive = archive
        self._target = target
        self._problems = problems
        self._is_zip = is_zip

    def run(self) -> None:
        try:
            callback = lambda done, total, message: self.progress.emit(done, total, message)  # noqa: E731
            if self._is_zip:
                count = self._archive.export_zip(self._target, self._problems, callback)
            else:
                count = self._archive.export_folder(self._target, self._problems, callback)
            self.done.emit(count)
        except Exception as exc:
            log.exception("批量导出异常")
            self.crashed.emit(str(exc))


class TableExportWorker(QThread):
    """导出成绩单 / 提交明细 / 档案包。

    一场考试可能积下几百份提交，xlsx 要拼上千行 XML、zip 还要逐份压源码 ——
    放界面线程里做，老师的窗口会白掉几秒，而"没反应"会让他连点好几次。

    导入 ``core.export`` **必须留在模块顶部**（见文件头那组导入）。以前它是写在
    :meth:`run` 里的惰性导入 —— 也就是说**在工作线程上做首次导入**。首次导入的
    分配动作随时会触发一轮 CPython 分代回收，而 GC 会顺手回收"本轮刚建的、
    还没有 C++ 父对象接管"的 Qt 包装（顶层对话框正是这种），``tp_dealloc`` 于是
    在这条工作线程上析构 C++ 对象 —— Qt 要求控件只能在 GUI 线程析构，
    结果就是偶发 access violation（全量测试里抓到过，事发点就在这次导入上；
    单独跑永远复现不了，因为分配计数还没在危险窗口里越阈）。首次导入发生在
    哪条线程是归我们管的，那就把它管到 GUI 线程上来。

    **PDF 例外：这里只产出 HTML，渲染交回界面线程。** ``QTextDocument`` 的排版
    要碰字体系统，非 GUI 线程做这件事在 Qt 里是可以的、但踩过的坑够多；而一份
    几百行的报告排版只要几十毫秒，不值得为它冒进程级崩溃的风险。所以 PDF 走
    :attr:`needs_pdf` 这个中间信号，其余格式直接写文件。
    """

    done = Signal(list)         # 写出的文件路径（字符串）
    needs_pdf = Signal(str)     # HTML，由界面线程渲染成 PDF
    crashed = Signal(str)

    def __init__(self, data, target, fmt: str, sections,
                 *, bundle: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._target = Path(target)
        self._fmt = fmt
        self._sections = list(sections)
        self._bundle = bundle

    def run(self) -> None:
        try:
            if self._bundle:
                written = [ex.export_bundle(self._target, self._data,
                                            sections=self._sections)]
            elif self._fmt == "pdf":
                self.needs_pdf.emit(ex.html_report(self._data, self._sections))
                return
            else:
                written = ex.export(self._target, self._data, self._fmt,
                                    self._sections)
            self.done.emit([str(path) for path in written])
        except Exception as exc:
            log.exception("导出失败")
            self.crashed.emit(str(exc))


class SubmissionSaveWorker(QThread):
    """把提交记录写盘（异步，避免偶发磁盘延迟卡住界面）。"""

    def __init__(self, log_file: str | Path, record: SubmissionRecord, parent=None) -> None:
        super().__init__(parent)
        self._log = SubmissionLog(log_file)
        self._record = record

    def run(self) -> None:
        self._log.append(self._record)


__all__ = [
    "CompilerDetectWorker",
    "CompilerVerifyWorker",
    "JudgeWorker",
    "TestRunWorker",
    "ImportWorker",
    "ExportWorker",
    "TableExportWorker",
    "SubmissionSaveWorker",
    "JudgeEvent",
]
