"""自定义校验器（Special Judge）。

不是所有题目都有唯一答案：

* "输出任意一组可行解" / 方案不唯一；
* 浮点题允许 ``1e-6`` 级别的误差；
* 与顺序、空白、大小写无关的答案。

这些题目用"逐字符比对"必然误判 —— 不是把正确解判成 WA，就是把错误解放过去。
解决办法是给这道题配一个**校验器程序**，由它读三个文件后自己给出结论。

协议沿用在线评测界的通行做法，三个文件路径按顺序走命令行参数：

.. code-block:: text

    校验器 <输入文件> <选手输出文件> <标准答案文件>

    退出码 0 -> 答案正确
    退出码 1 -> 答案错误
    退出码 2 -> 格式错误（Presentation Error）
    其它     -> 校验器自身出了状况

最后一条是这份实现里最要紧的约定：**校验器自己出问题（编译不过、跑超时、
异常退出、甚至被系统拦下）一律记成评测机内部错误（IE），绝不算到选手头上**。
写校验器的是出题人，交代码的是选手，两者不能混为一谈。

几个实现上的选择：

* **每题只编译一次。** 校验器本身也是一份要编译的程序，``prepare()`` 编译一次，
  ``check()`` 在每个测试点上复用它。
* **数据文件一律用相对路径。** 校验器以"每个测试点一个子目录"作为工作目录运行，
  三个文件名都是相对的。这样即使 ``%LOCALAPPDATA%`` 落在中文用户名下面，
  也不会踩到 C 运行库把宽字符命令行转成 ANSI 代码页、进而找不到文件的那条老路
  （与 :class:`~offline_oj.core.runners.MsvcProfile` 用相对文件名的理由相同）。
* **三个文件都由评测机生成。** 标准输入输出模式下选手的输出只在内存里，文件模式下
  它在选手的工作目录里；与其分情况处理，不如统一把「输入 / 选手输出 / 标准答案」
  各写一份到校验器自己的目录里 —— 校验器看到的内容与题目的输入输出方式无关。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..win32.process import popen
from .models import JudgeConfig, Language, Verdict
from .runners import BaseRunner, CompileOutput, make_runner
from .sandbox import ProcessMonitor, RunResult

log = logging.getLogger(__name__)

#: 校验器的时间上限。它要读文件、做比较，比选手程序宽松得多；
#: 但也不能不设 —— 校验器里写出死循环是常有的事。
CHECKER_TIMEOUT_MS = 30_000

#: 校验器的内存上限。校验器常常要把整个答案读进内存，不能按题目的限制卡它。
CHECKER_MEMORY_MB = 1024

#: 校验器诊断信息的截断长度
CHECKER_ERROR_LIMIT = 2000

#: 三个数据文件的名字。校验器的工作目录就是单个测试点的目录，所以用相对名。
INPUT_NAME = "input.txt"
OUTPUT_NAME = "output.txt"
ANSWER_NAME = "answer.txt"
#: 传给校验器的 argv 顺序：输入、选手输出、标准答案
CASE_FILES: tuple[str, str, str] = (INPUT_NAME, OUTPUT_NAME, ANSWER_NAME)

#: 协议退出码 -> 结论
PROTOCOL_EXIT_CODES: dict[int, Verdict] = {
    0: Verdict.AC,
    1: Verdict.WA,
    2: Verdict.PE,
}


@dataclass
class CheckerOutcome:
    """校验器对一个测试点的判定。"""

    verdict: Verdict
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.AC


class Checker:
    """一道题的自定义校验器：编译一次，每个测试点跑一次。

    :param config: 题目的判题配置（``Problem.judge``）
    :param paths: 工具链路径表，直接取自选手运行器（``runner.paths``）
    :param work_root: 工作区根目录（``runner.work_root``）

    没有配置校验器时 :meth:`prepare` 直接返回成功，:meth:`check` 不该被调用 ——
    :class:`~offline_oj.core.judge.Judge` 会退回精确比对。
    """

    def __init__(
        self,
        config: JudgeConfig,
        paths: Mapping[str, str],
        work_root: str | os.PathLike[str],
    ) -> None:
        self.config = config
        self.paths = dict(paths)
        self.work_root = Path(work_root)
        self.language: Language | None = config.checker_language
        self._runner: BaseRunner | None = None
        self._compiled: CompileOutput | None = None
        self._work_dir = ""
        self._count = 0

    # ---- 对外接口 ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.config.uses_checker

    @property
    def ready(self) -> bool:
        return self._compiled is not None

    def prepare(self) -> tuple[bool, str]:
        """编译（Python 为语法自检）校验器。返回 ``(可用, 说明)``。

        没配校验器时返回 ``(True, "")``：这不是错误，只是不需要。
        """
        if not self.enabled:
            return True, ""

        try:
            self._runner = make_runner(self.language, self.paths, self.work_root,
                                       optimize=True)
        except ValueError as exc:
            return False, f"校验器语言不可用：{exc}"

        valid, message = self._runner.check_environment()
        if not valid:
            return False, f"校验器所需工具链未就绪：{message}"

        # 校验器单独一个工作目录：选手的可执行文件也叫 main.exe，
        # 放在一起会互相覆盖。
        self._work_dir = self._runner.make_work_dir()
        try:
            compiled = self._runner.compile(self.config.checker, self._work_dir)
        except Exception as exc:
            log.exception("校验器编译阶段异常")
            return False, f"编译校验器时评测机内部错误：{exc}"

        if not compiled.ok:
            return False, f"校验器编译失败：\n{compiled.message or '编译失败'}"

        self._compiled = compiled
        log.info("校验器就绪：%s", self.language.value if self.language else "?")
        return True, ""

    def check(
        self,
        input_data: str,
        actual: str,
        expected: str,
        *,
        index: int = 0,
    ) -> CheckerOutcome:
        """让校验器判定一个测试点。

        :param actual: 选手输出（文件模式下是输出文件的内容）
        :param expected: 标准答案
        :param index: 测试点序号，只用于给工作目录起名，方便出题人排查
        """
        if self._compiled is None or self._runner is None:
            return CheckerOutcome(Verdict.IE, "校验器尚未准备就绪（内部调用次序错误）")

        self._count += 1
        case_dir = Path(self._work_dir) / f"case_{index or self._count:04d}"
        failure = self._write_case(case_dir, input_data, actual, expected)
        if failure:
            return CheckerOutcome(Verdict.IE, failure)

        try:
            command = self._command(case_dir)
        except Exception as exc:
            log.exception("构造校验器命令失败")
            return CheckerOutcome(Verdict.IE, f"启动校验器前评测机内部错误：{exc}")

        try:
            process = popen(command, cwd=str(case_dir))
        except OSError as exc:
            return CheckerOutcome(Verdict.IE, f"无法启动校验器：{exc.strerror or exc}")

        monitor = ProcessMonitor(process, CHECKER_TIMEOUT_MS, CHECKER_MEMORY_MB)
        result = monitor.wait(None)
        return self._interpret(result)

    def cleanup(self) -> None:
        """删除校验器的工作目录；失败只记日志。"""
        if self._runner is not None and self._work_dir:
            self._runner.cleanup(self._work_dir)
        self._work_dir = ""
        self._compiled = None

    # ---- 内部实现 ---------------------------------------------------------

    @staticmethod
    def _write_case(case_dir: Path, input_data: str, actual: str, expected: str) -> str:
        """把三份数据落盘，返回错误说明（成功时为空字符串）。"""
        try:
            case_dir.mkdir(parents=True, exist_ok=True)
            for name, text in zip(CASE_FILES, (input_data, actual, expected)):
                # newline="\n"：与运行时写输入文件保持一致，不引入 CRLF 差异
                # errors="replace"：选手输出是 decode 出来的，理论上不会有非法字符，
                # 但这里写不出去就等于整道题判不了，不值得为此冒险
                (case_dir / name).write_text(text or "", encoding="utf-8",
                                             errors="replace", newline="\n")
        except OSError as exc:
            return f"准备校验器所需的文件失败：{exc}"
        return ""

    def _command(self, case_dir: Path) -> list[str]:
        """校验器的命令行：语言运行命令 + 三个相对文件名。

        ``cwd`` 已经设成单测试点目录，所以三个名字都是相对的。
        """
        assert self._runner is not None and self._compiled is not None
        base = list(self._runner.build_command(self._compiled, CHECKER_MEMORY_MB))
        return [*base, *CASE_FILES]

    @staticmethod
    def _interpret(result: RunResult) -> CheckerOutcome:
        """把一次运行结果翻译成判定。

        注意这里的次序：**先看进程是被谁终止的，再看退出码**。
        ``ProcessMonitor`` 会把任何非 0 退出码都归成 RE，而 1 / 2 恰恰是协议里
        约定的正常返回，所以不能直接采信它的 verdict。
        """
        if result.verdict is Verdict.TLE:
            return CheckerOutcome(Verdict.IE,
                                  f"校验器运行超时（超过 {CHECKER_TIMEOUT_MS / 1000:.0f} 秒）")
        if result.verdict is Verdict.MLE:
            return CheckerOutcome(Verdict.IE,
                                  f"校验器占用内存超过 {CHECKER_MEMORY_MB} MB")
        if result.verdict is Verdict.OLE:
            return CheckerOutcome(Verdict.IE, "校验器输出内容过多")

        code = result.exit_code
        if code is None:
            return CheckerOutcome(Verdict.IE, "校验器没有正常结束")

        verdict = PROTOCOL_EXIT_CODES.get(code)
        if verdict is None:
            detail = Checker._diagnostic(result)
            suffix = f"：\n{detail}" if detail else ""
            return CheckerOutcome(Verdict.IE, f"校验器异常退出（退出码 {code}）{suffix}")

        if verdict is Verdict.AC:
            return CheckerOutcome(Verdict.AC)

        # 出题人常在 WA / PE 时顺手打印一句原因，直接带给用户比自己编一句有用
        return CheckerOutcome(verdict, Checker._diagnostic(result) or verdict.label)

    @staticmethod
    def _diagnostic(result: RunResult) -> str:
        """取校验器自己打印的说明（优先 stdout，其次 stderr）。"""
        text = (result.stdout or "").strip() or (result.stderr or "").strip()
        return text[:CHECKER_ERROR_LIMIT]
