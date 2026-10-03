"""评测流程编排。

输出是**事件流**而不是字符串，这是与原实现最重要的结构差别：

* 原实现在判题线程里直接往 ``Text`` 控件里 insert 文本，评测逻辑与 Tk 控件绑死，
  既无法单测，也没法做成进度条 / 实时表格；
* 现在 :meth:`Judge.iter_events` 依次产出"开始 → 编译结果 → 每个测试点 → 汇总"事件，
  UI 层负责渲染，命令行入口负责打印，测试代码可以直接消费事件做断言。

单题只编译一次，然后依次跑完所有测试点；已通过的测试点不会因为后续某点超时而回滚。

判定阶段有两种策略，由题目的 :class:`~offline_oj.core.models.JudgeConfig` 决定：

* 默认是**精确比对**（忽略行尾空白与末尾空行）；
* 题目配了校验器则交给**校验器程序**判定，用于答案不唯一 / 允许浮点误差的题目。

校验器在开跑前编译一次（见 :class:`~offline_oj.core.checker.Checker`）；校验器
自己不可用时整题记为评测机内部错误，不会算成选手的 CE。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from .checker import Checker
from .models import Language, Problem, TestCase, Verdict
from .runners import BaseRunner, compare_output, diff_summary
from .sandbox import RunResult

log = logging.getLogger(__name__)

#: 事件种类
KIND_START = "start"            # 开始评测
KIND_ENVIRONMENT = "env"        # 环境检查失败
KIND_COMPILED = "compiled"      # 编译结束
KIND_CASE = "case"              # 单个测试点结束
KIND_CHECKER = "checker"        # 自定义校验器不可用（评测无法进行）
KIND_FINISH = "finish"          # 全部结束
KIND_ABORTED = "aborted"        # 用户中止


@dataclass
class TestOutcome:
    """单个测试点的评测结论。"""

    index: int                                  # 从 1 开始
    total: int
    verdict: Verdict
    time_ms: float = 0.0
    memory_mb: float = 0.0
    message: str = ""
    actual: str = ""
    expected: str = ""
    diff: str = ""
    #: 这个测试点值多少分。由 :meth:`Judge.iter_events` 统一从题目上挂过来，
    #: 不在 ``_judge_case`` 的各条 return 里逐个传 —— 那里有 RE / TLE / WA / AC /
    #: 校验器五六个出口，漏掉一个就会让那种结论**静默**按 0 分算。
    points: int = 0

    @property
    def passed(self) -> bool:
        return self.verdict.accepted

    @property
    def earned(self) -> int:
        """这个点给到了多少分。"""
        return self.points if self.passed else 0

    def detail_lines(self, *, indent: str = "    ") -> list[str]:
        lines: list[str] = []
        if self.time_ms or self.memory_mb:
            metrics = []
            if self.time_ms:
                metrics.append(f"{self.time_ms:.1f} ms")
            if self.memory_mb:
                metrics.append(f"{self.memory_mb:.1f} MB")
            lines.append(indent + " / ".join(metrics))
        if self.message:
            lines.append(indent + self.message)
        if self.verdict is Verdict.WA:
            if self.diff:
                lines.extend(indent + part for part in self.diff.split("\n"))
            else:
                lines.append(f"{indent}期望输出: {self.expected[:200]!r}")
                lines.append(f"{indent}实际输出: {self.actual[:200]!r}")
        return lines


@dataclass
class JudgeReport:
    """一次完整评测的汇总。"""

    problem_id: str = ""
    problem_title: str = ""
    language: Language | None = None
    verdict: Verdict = Verdict.IE
    outcomes: list[TestOutcome] = field(default_factory=list)
    compile_ok: bool = True
    compile_message: str = ""
    #: 题目的自定义校验器是否可用。不可用时整题判不了 —— 但这是出题人的问题，
    #: 与选手代码无关，所以单独一个字段，不去污染 compile_ok
    checker_ok: bool = True
    checker_message: str = ""
    elapsed_s: float = 0.0
    aborted: bool = False
    #: 这次评测是不是开了编译优化（C/C++ 的 ``-O2`` / MSVC 的 ``/O2``）。
    #:
    #: 单独记下来的理由：同一份代码开不开优化能差好几倍耗时，"自测挺稳、判题 TLE"
    #: 这类结论，事后回看必须能分辨是代码的问题还是编译参数的问题。
    #: Python / Java 没有这个开关，恒为 False。
    optimized: bool = False
    #: "编译优化"这一行要显示的文字：``-O2`` / ``-O0``（MSVC 是 ``/O2`` / ``/Od``）。
    #: 空串表示这门语言不适用，界面就不显示这一行。
    optimize_label: str = ""

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def max_time_ms(self) -> float:
        return max((outcome.time_ms for outcome in self.outcomes), default=0.0)

    @property
    def max_memory_mb(self) -> float:
        return max((outcome.memory_mb for outcome in self.outcomes), default=0.0)

    @property
    def accepted(self) -> bool:
        return self.verdict.accepted and not self.aborted

    @property
    def points_total(self) -> int:
        """本题满分 = 各测试点分值之和。

        ``0`` 表示这次评测**根本没有分值信息**（外部接入的判题器可以只填
        ``verdict`` 与通过数），调用方据此回退到"按通过比例折算 100 分制"。
        """
        return sum(outcome.points for outcome in self.outcomes)

    @property
    def points_earned(self) -> int:
        """这次评测拿了多少分：**通过的**那些测试点的分值之和。"""
        return sum(outcome.earned for outcome in self.outcomes)

    def summary(self) -> str:
        if self.aborted:
            return f"评测已中止（{self.passed}/{self.total} 通过）"
        if not self.compile_ok:
            return f"编译失败 · {self.verdict.value} {self.verdict.label}"
        if not self.checker_ok:
            return f"自定义校验器不可用 · {self.verdict.value} {self.verdict.label}"
        head = "全部通过" if self.accepted else "未全部通过"
        parts = [f"{head} · {self.verdict.value} {self.verdict.label}",
                 f"{self.passed}/{self.total} 测试点通过"]
        # 得分只在真有分值信息时才写：外部判题器只填通过数，写"得分 0/0"是噪音
        if self.points_total:
            parts.append(f"得分 {self.points_earned}/{self.points_total}")
        if self.max_time_ms:
            parts.append(f"峰值 {self.max_time_ms:.0f} ms")
        if self.max_memory_mb:
            parts.append(f"{self.max_memory_mb:.1f} MB")
        return " · ".join(parts)

    @property
    def optimization_text(self) -> str:
        """「编译优化」那一行要显示什么；空串 = 这门语言没有这个概念。

        刻意把**未开启**也写出来（而不是只在开启时才标）：TLE 的成因里，
        "忘了开优化"和"算法确实慢"是两件完全不同的事，只标一半等于没标。
        """
        if not self.optimize_label:
            return ""
        if self.optimized:
            return f"编译优化：{self.optimize_label}"
        return f"编译优化：{self.optimize_label}（未开启优化）"

    def text_report(self) -> str:
        """纯文本报告，供剪贴板与命令行输出。"""
        lines = [f"题目: {self.problem_id} {self.problem_title}".rstrip(),
                 f"语言: {self.language.short if self.language else '-'}",
                 "=" * 46]
        if not self.compile_ok:
            lines.append("编译失败:")
            lines.append(self.compile_message)
        elif not self.checker_ok:
            lines.append("自定义校验器不可用:")
            lines.append(self.checker_message)
        else:
            for outcome in self.outcomes:
                mark = "●" if outcome.passed else "■"
                lines.append(f"{mark} 测试点 {outcome.index}/{outcome.total}: "
                             f"{outcome.verdict.text}")
                lines.extend(outcome.detail_lines())
        lines.extend(["=" * 46, self.summary()])
        return "\n".join(lines)


@dataclass
class JudgeEvent:
    """评测事件流中的一个事件。"""

    kind: str
    total: int = 0
    message: str = ""
    outcome: TestOutcome | None = None
    report: JudgeReport | None = None


class Judge:
    """评测器。

    :param runner: 已按语言构造好的运行器
    :param stop_event: 置位后中止评测（UI 的"停止"按钮）
    """

    def __init__(self, runner: BaseRunner, stop_event: threading.Event | None = None) -> None:
        self.runner = runner
        self.stop_event = stop_event or threading.Event()

    def _optimize_label(self) -> str:
        """这次评测的编译优化该怎么写（``-O2`` / ``/Od`` / 空串表示不适用）。"""
        labels = self.runner.optimize_labels
        if not labels:
            return ""
        return labels[0] if self.runner.optimize else labels[1]

    def _new_report(self, problem: Problem) -> JudgeReport:
        return JudgeReport(
            problem_id=problem.id,
            problem_title=problem.title,
            language=self.runner.language,
            optimized=self.runner.optimize,
            optimize_label=self._optimize_label(),
        )

    # ---- 事件流 -----------------------------------------------------------

    def iter_events(self, problem: Problem, code: str) -> Iterator[JudgeEvent]:
        """逐个产出评测事件。"""
        started = time.perf_counter()
        report = self._new_report(problem)
        cases: Sequence[TestCase] = problem.testcases

        # 题目英文名只推导一次：源程序/产物命名、文件模式的数据文件名、
        # 每题一个的子目录名，全都由它决定（CCF 规约）。
        task_name = problem.english_name

        # 开始行顺带交代这道题"怎么判"——出了 AC 才发现用了校验器会很意外
        label = f"{problem.id} · {len(cases)} 个测试点 · 英文名 {task_name}"
        if problem.judge.uses_files:
            input_file, output_file = problem.io_files()
            label += f" · 文件 {input_file}/{output_file}"
        if problem.judge.uses_checker:
            language = problem.judge.checker_language
            label += f" · 自定义校验器（{language.short if language else '?'}）"
        yield JudgeEvent(KIND_START, total=len(cases), message=label)

        # ---- 环境检查 ----
        valid, message = self.runner.check_environment()
        if not valid:
            report.compile_ok = False
            report.verdict = Verdict.CE
            report.compile_message = f"运行环境未就绪：{message}"
            report.elapsed_s = time.perf_counter() - started
            yield JudgeEvent(KIND_ENVIRONMENT, message=report.compile_message)
            yield JudgeEvent(KIND_FINISH, report=report)
            return

        work_dir = self.runner.make_work_dir()
        # 校验器虽然独立编译，但共用同一份工具链路径与工作区根目录
        checker = Checker(problem.judge, self.runner.paths, self.runner.work_root)
        try:
            # ---- 编译（每题一次） ----
            # 传题目英文名：源程序落成 <题目英文名>.cpp、产物落成 <题目英文名>，
            # 并且整个题目单独占一层子目录（CCF：<考号>/<题目英文名>/…）
            try:
                compiled = self.runner.compile(code, work_dir, name=task_name)
            except Exception as exc:
                log.exception("编译阶段异常")
                compiled = None
                report.compile_ok = False
                report.verdict = Verdict.CE
                report.compile_message = f"评测机内部错误: {exc}"

            if compiled is None or not compiled.ok:
                report.compile_ok = False
                report.verdict = Verdict.CE
                report.compile_message = (compiled.message if compiled else "") or "编译失败"
                report.elapsed_s = time.perf_counter() - started
                yield JudgeEvent(KIND_COMPILED, message=report.compile_message)
                yield JudgeEvent(KIND_FINISH, report=report)
                return

            yield JudgeEvent(KIND_COMPILED, message="")

            # ---- 准备校验器（每题一次） ----
            # 放在跑测试点之前：校验器编译不过就没必要白跑一遍选手程序，
            # 而且这时给结论比跑到一半再报更清楚。
            if checker.enabled:
                ready, checker_message = checker.prepare()
                if not ready:
                    report.checker_ok = False
                    report.checker_message = checker_message
                    report.verdict = Verdict.IE
                    report.elapsed_s = time.perf_counter() - started
                    yield JudgeEvent(KIND_CHECKER, message=checker_message)
                    yield JudgeEvent(KIND_FINISH, report=report)
                    return

            # ---- 逐测试点运行 ----
            for index, case in enumerate(cases, start=1):
                if self.stop_event.is_set():
                    report.aborted = True
                    yield JudgeEvent(KIND_ABORTED, message="已中止")
                    break

                outcome = self._judge_case(compiled, case, index, len(cases), problem,
                                           checker, task_name)
                # 分值统一在这里挂上：跟着**题目**走，不跟着结论走
                outcome.points = case.points
                report.outcomes.append(outcome)
                yield JudgeEvent(KIND_CASE, outcome=outcome)

            report.verdict = self._aggregate(report)
            report.elapsed_s = time.perf_counter() - started
            yield JudgeEvent(KIND_FINISH, report=report)
        finally:
            checker.cleanup()
            self.runner.cleanup(work_dir)

    def judge(self, problem: Problem, code: str) -> JudgeReport:
        """跑完整流程并只返回汇总结果（命令行 / 测试用）。"""
        report = self._new_report(problem)
        report.verdict = Verdict.IE
        for event in self.iter_events(problem, code):
            if event.report is not None:
                report = event.report
        return report

    # ---- 内部 -------------------------------------------------------------

    def _judge_case(
        self,
        compiled,
        case: TestCase,
        index: int,
        total: int,
        problem: Problem,
        checker: Checker,
        task_name: str,
    ) -> TestOutcome:
        try:
            result: RunResult = self.runner.execute(
                compiled, case.input, problem.time_limit, problem.memory_limit,
                # 文件名模板在这里落地：{name}.in → poker.in
                judge=problem.judge.resolved(task_name),
            )
        except Exception as exc:
            log.exception("测试点 %s 运行异常", index)
            return TestOutcome(index=index, total=total, verdict=Verdict.IE,
                               message=f"评测机内部错误: {exc}")

        if result.ok:
            return self._compare(result, case, index, total, checker)

        # 运行阶段就已经有结论了（RE/TLE/MLE/OLE，或文件模式没生成输出文件）。
        # 这种时候不劳校验器：没有输出可供它判断。
        return TestOutcome(
            index=index, total=total, verdict=result.verdict,
            time_ms=result.time_ms, memory_mb=result.memory_mb,
            actual=result.stdout, expected=case.output,
            message=result.error_text,
        )

    def _compare(
        self,
        result: RunResult,
        case: TestCase,
        index: int,
        total: int,
        checker: Checker,
    ) -> TestOutcome:
        """比对阶段：配了校验器就交给它，否则按精确比对。"""
        metrics = {"time_ms": result.time_ms, "memory_mb": result.memory_mb}

        if checker.enabled:
            outcome = checker.check(case.input, result.stdout, case.output, index=index)
            if outcome.verdict is Verdict.AC:
                return TestOutcome(index=index, total=total, verdict=Verdict.AC, **metrics)
            # 校验器说不对时**不生成逐行 diff**：答案可能本来就是合法的另一种形式，
            # 拿它和标准答案逐行对比只会误导。仍带上双方内容供人工核对。
            return TestOutcome(
                index=index, total=total, verdict=outcome.verdict,
                actual=result.stdout, expected=case.output,
                message=outcome.message,
                **metrics,
            )

        if compare_output(result.stdout, case.output):
            return TestOutcome(index=index, total=total, verdict=Verdict.AC, **metrics)
        return TestOutcome(
            index=index, total=total, verdict=Verdict.WA,
            actual=result.stdout, expected=case.output,
            diff=diff_summary(result.stdout, case.output),
            message="输出与期望不一致",
            **metrics,
        )

    @staticmethod
    def _aggregate(report: JudgeReport) -> Verdict:
        """汇总结论：取"最严重"的那个，全对则 AC。"""
        if report.aborted:
            return report.outcomes[-1].verdict if report.outcomes else Verdict.IE
        if not report.outcomes:
            return Verdict.IE
        severity = (Verdict.WA, Verdict.TLE, Verdict.MLE, Verdict.RE,
                    Verdict.OLE, Verdict.PE, Verdict.IE)
        for candidate in severity:
            if any(outcome.verdict is candidate for outcome in report.outcomes):
                return candidate
        return Verdict.AC if all(outcome.passed for outcome in report.outcomes) else Verdict.WA


def sample_report(problem: Problem, language: Language) -> JudgeReport:
    """构造一个空报告，供 UI 在开始评测前占位。"""
    return JudgeReport(problem_id=problem.id, problem_title=problem.title,
                       language=language, verdict=Verdict.IE)
