"""各语言的"编译 + 运行"实现。

统一契约：``run(code, input_data, time_limit_ms, memory_limit_mb) -> RunResult``。
调用方（评测引擎 / UI 单次测试）不需要关心语言差异。

设计要点：

* **编译与运行分离**：``_compile`` 负责产出可执行文件，``_execute`` 负责喂输入、
  跑监控。这样评测时"编译一次、跑 N 个测试点"是自然的，不必每个测试点重编译。
* **临时目录隔离**：每次运行在 ``%LOCALAPPDATA%\\OfflineOJ\\workspace`` 下建独立子目录，
  避免用户程序读写当前目录时污染安装目录，也避免残留文件互相干扰。
* **清理策略**：运行结束后删除临时目录；被强杀进程留下的文件句柄由 Windows 延迟释放，
  因此删除失败只记日志不报错。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..win32.process import popen, run_silent
from .models import JudgeConfig, Language, Verdict, sanitize_slug
from .sandbox import MAX_OUTPUT_BYTES, CompilerLock, ProcessMonitor, RunResult
from .validation import PathValidator

log = logging.getLogger(__name__)

#: 编译超时（秒）
COMPILE_TIMEOUT = 25.0
#: 编译错误信息截断长度
COMPILE_ERROR_LIMIT = 4000

#: 兜底：连标准参数都不支持的极老编译器，最后退到"不加参数"（用编译器默认标准）
_NO_STANDARD = ""

#: C++ 标准候选，从新到旧。
#: GCC 4.8/4.9（Dev-Cpp 自带的就是 4.9）最高只到 C++14，写死 C++17 会让那类机器
#: 上每一份 C++ 提交都编译失败。
CPP_STANDARD_LADDER: tuple[str, ...] = (
    "-std=c++17", "-std=c++14", "-std=c++11", _NO_STANDARD,
)
#: C 标准候选，从新到旧。
C_STANDARD_LADDER: tuple[str, ...] = (
    "-std=c11", "-std=c99", "-std=c90", _NO_STANDARD,
)

#: (语言, 编译器路径) -> 实测可用的标准参数。
#: 同一台机器上没必要每次判题都把注定失败的标准再试一遍。
_STANDARD_CACHE: dict[tuple[str, str], str] = {}

#: 这些字样说明失败原因是"编译器不认识这个参数"，可以换一个标准重试。
#: 注意只认"参数本身相关的"错误，语法错误不在此列 —— 否则每份错代码都要白编译三次。
_STANDARD_REJECT_MARKERS = (
    "unrecognized command line option",
    "unrecognized option",
    "unknown argument",
    "unknown option",
    "unknown command line argument",
)


#: Windows 可执行文件后缀
EXE_SUFFIX = ".exe"


def executable_name(stem: str) -> str:
    """按平台给编译产物补后缀。

    CCF 的评测环境（NOI Linux 2.0）下产物没有扩展名，就叫 ``poker``。
    Windows 上 GCC 与 MSVC 都会**自动**往 ``-o poker`` / ``/Fe:poker`` 的结果上
    补 ``.exe``，所以想判断"产物出来了没有"就必须按平台把后缀拼回来。
    """
    return stem + EXE_SUFFIX if os.name == "nt" else stem


def _cache_key(compiler: str) -> str:
    return os.path.normcase(os.path.abspath(compiler))


def _standard_rejected(stderr: str, flag: str) -> bool:
    """编译失败是否可能仅仅因为 ``flag`` 不被支持。"""
    text = (stderr or "").lower()
    if not text:
        return False
    if flag and flag.lower() in text:
        return True
    return any(marker in text for marker in _STANDARD_REJECT_MARKERS)


def _diagnose(completed, work_dir: str, source_name: str) -> str:
    """把编译器输出整理成给人看的编译错误信息。

    两件事：

    * ``cl.exe`` 会在 stdout 回显一行源文件名，对用户毫无意义，删掉；
    * 报错里的源文件路径是完整临时目录（``C:\\Users\\...\\oj_cpp_ab12\\main.cpp(3)``），
      把工作目录前缀摘掉后就是 ``main.cpp(3)``，清爽且与在线评测的报错形态一致。
    """
    chunks = [text for text in (completed.stderr, completed.stdout) if text and text.strip()]
    text = "\n".join(chunks)
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip() != source_name]
    text = "\n".join(lines).strip()
    if work_dir:
        for prefix in (work_dir + os.sep, work_dir + "/"):
            text = text.replace(prefix, "")
    return text.strip()


def _launch_failure(exc: OSError, artifact: str, work_root: str | os.PathLike[str]) -> str:
    """把"启动不了编译产物"翻译成用户能照着做的提示。

    这条路径踩过一次很难查的坑：判题机装了 360 / 火绒这类杀毒软件时，
    刚编译出来的可执行文件**会被拦截执行，随后连文件一起删掉**。
    现象是 ``[WinError 5] 拒绝访问``，下一次再尝试就变成"找不到文件"。
    而在代码里完全看不出问题 —— 编译明明成功了。

    实测的特征很好认：产物编译出来不动它就一直在，一旦尝试启动就消失。
    所以这里不去猜，直接看产物还在不在，并给出「把工作目录加入杀软信任区」这个
    唯一有效的处置办法。
    """
    reason = exc.strerror or str(exc)
    vanished = not os.path.exists(artifact)
    blocked = getattr(exc, "winerror", None) == 5 or vanished

    lines = [f"无法启动编译产物：{reason}"]
    if not blocked:
        lines.append(f"产物路径：{artifact}")
        return "\n".join(lines)

    lines.append("")
    lines.append("编译本身是成功的，问题出在「启动这个程序」这一步被系统拒绝了。"
                 "最常见的原因是杀毒软件（360、火绒、联想电脑管家、Windows Defender 等）"
                 "把刚编译出来的程序当成可疑文件拦了下来 —— 这与代码无关。")
    if vanished:
        lines.append(f"佐证：产物已经不在了（{artifact}），是被拦截后删除的。")
    lines.append("")
    lines.append(f"处理办法：把评测工作目录加入杀毒软件的信任区 / 排除列表：\n    {work_root}")
    lines.append("（360：设置 → 安全防护中心 → 信任区；火绒：设置 → 信任区 → 添加目录）")
    lines.append(f"原始错误：{exc!r}")
    return "\n".join(lines)


@dataclass
class CompileOutput:
    """一次编译（或解释执行前的准备）结果。"""

    ok: bool
    message: str = ""
    artifact: str = ""       # 可执行文件 / 类文件所在目录
    entry: str = ""          # 运行入口（可执行文件名或主类名）
    work_dir: str = ""


class BaseRunner(ABC):
    """语言运行器基类。

    源文件与可执行文件的命名有两种模式：

    * 给了**题目英文名**（``name``）—— 按 CCF 规约命名：题目 ``poker`` 就是
      ``poker.cpp`` 与 ``poker``（Linux 下可执行文件没有扩展名），
      文件模式的数据文件 ``poker.in`` / ``poker.out`` 与它们同目录，
      程序在**当前路径**下用不带绝对路径的名字访问；
    * 没给名字 —— 退回 ``main``，用于"单次测试运行"与工具链自检这类没有题目的场景。
    """

    language: Language
    #: 需要的配置键（顺序有意义：第一个是编译期依赖）
    required_keys: tuple[str, ...] = ()
    #: 没有题目英文名时使用的文件名主干
    DEFAULT_STEM = "main"

    def __init__(
        self,
        paths: Mapping[str, str],
        work_root: str | os.PathLike[str],
        *,
        optimize: bool = True,
    ) -> None:
        self.paths = {key: (paths.get(key) or "").strip() for key in paths}
        self.work_root = Path(work_root)
        self.optimize = optimize

    @property
    def optimize_labels(self) -> tuple[str, str] | None:
        """优化开关在界面上怎么写，没编译过的语言返回 ``None``。

        基类恒为 ``None``（Python / Java 没有编译优化这回事），由
        :class:`CRunner` 覆写成实际参数名。
        """
        return None

    # ---- 对外接口 ---------------------------------------------------------

    def check_environment(self) -> tuple[bool, str]:
        """检查所需工具链是否已配置且有效。"""
        missing: list[str] = []
        for key in self.required_keys:
            valid, message = PathValidator.validate_executable(self.paths.get(key, ""))
            if not valid:
                missing.append(f"{self._label(key)}：{message}")
        if missing:
            return False, "；".join(missing)
        return True, ""

    def compile(self, code: str, work_dir: str, *, name: str = "") -> CompileOutput:
        """默认实现：无需编译的语言直接落盘源码。

        :param name: 题目英文名，决定源文件名；留空时用 ``main``
        """
        source = os.path.join(work_dir, f"{self.stem(name)}{self.language.suffix}")
        with open(source, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(code)
        return CompileOutput(ok=True, work_dir=work_dir, entry=source)

    def execute(
        self,
        compile_output: CompileOutput,
        input_data: str,
        time_limit_ms: int,
        memory_limit_mb: int,
        *,
        judge: JudgeConfig | None = None,
    ) -> RunResult:
        """启动进程并监控资源占用。

        ``judge`` 决定数据从哪进、从哪出：

        * 默认（或 ``io_mode=stdio``）——输入喂给标准输入，输出取标准输出；
        * ``io_mode=file``——先把输入写进工作目录里的输入文件，
          运行结束后读**输出文件**参与比对。

        工作目录（``cwd``）本来就是每题一个独立的，所以文件模式不需要额外隔离。
        """
        io = judge or JudgeConfig()
        output_path: Path | None = None
        output_name = ""

        if io.uses_files:
            work_dir = Path(compile_output.work_dir)
            input_path = work_dir / io.input_file
            output_path = work_dir / io.output_file
            output_name = io.output_file
            try:
                # newline="\n"：与写入源码保持一致，不引入 CRLF 差异
                input_path.write_text(input_data or "", encoding="utf-8", newline="\n")
            except OSError as exc:
                return RunResult(Verdict.IE, message=f"写入输入文件 {io.input_file} 失败：{exc}")
            # 同一个工作目录会连着跑所有测试点，上一轮的输出文件必须先清掉 ——
            # 否则本轮程序什么都没写时，会读到上一轮的答案而误判为通过。
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                log.debug("清理上一轮输出文件失败: %s", output_path)

        command = self.build_command(compile_output, memory_limit_mb)
        try:
            process = popen(command, cwd=compile_output.work_dir)
        except OSError as exc:
            return RunResult(Verdict.RE, message=_launch_failure(
                exc, compile_output.artifact or compile_output.entry, self.work_root))

        monitor = ProcessMonitor(
            process,
            time_limit_ms,
            memory_limit_mb,
            include_children=self.language is Language.JAVA,
        )
        # 文件模式下不喂标准输入：真正读文件的程序不会碰 stdin，
        # 而关掉它能让"误用 scanf 等输入"更快暴露（立刻拿到 EOF）
        result = monitor.wait(None if io.uses_files else input_data)
        if output_path is not None:
            return self._collect_output_file(result, output_path, output_name)
        return result

    @staticmethod
    def _collect_output_file(result: RunResult, path: Path, name: str) -> RunResult:
        """文件模式：以输出文件的内容作为比对依据。

        程序没生成输出文件时必须把运行结果标成"不通过"。若只是让 ``stdout``
        保持为空，而该测试点的期望输出又恰好为空，两边空字符串相等就会判成
        AC —— 把一个明显的错误判成通过，这是最不能接受的一类误判。
        """
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return RunResult(Verdict.IE, message=f"读取输出文件 {name} 失败：{exc}")
            if len(text) > MAX_OUTPUT_BYTES:
                result.truncated = True
                text = text[:MAX_OUTPUT_BYTES]
            result.stdout = text
            return result

        if result.verdict is Verdict.AC:
            hint = ""
            if result.stdout.strip():
                hint = "（程序把内容打印到了标准输出，但本题的答案要写进文件）"
            result.verdict = Verdict.WA
            result.stdout = ""
            result.message = f"程序没有生成输出文件 {name}{hint}"
        return result

    def run(
        self,
        code: str,
        input_data: str,
        time_limit_ms: int,
        memory_limit_mb: int,
        *,
        judge: JudgeConfig | None = None,
        name: str = "",
    ) -> RunResult:
        """一步到位：编译 + 运行 + 清理（供"单次测试运行"使用）。

        ``judge`` / ``name`` 传进来，是因为"测试运行"也得模拟真实判题环境：
        题目要是走文件输入输出，选手的 ``freopen("poker.in", ...)`` 得有文件可读。
        """
        valid, message = self.check_environment()
        if not valid:
            return RunResult(Verdict.CE, message=message)

        work_dir = self.make_work_dir()
        try:
            comp = self.compile(code, self.task_dir(work_dir, name), name=name)
            if not comp.ok:
                return RunResult(Verdict.CE, message=comp.message)
            return self.execute(comp, input_data, time_limit_ms, memory_limit_mb,
                                judge=(judge or JudgeConfig()).resolved(name))
        except Exception as exc:
            log.exception("运行 %s 代码失败", self.language.value)
            return RunResult(Verdict.IE, message=f"评测机异常: {exc}")
        finally:
            self.cleanup(work_dir)

    # ---- 供子类实现 -------------------------------------------------------

    @abstractmethod
    def build_command(self, compile_output: CompileOutput, memory_limit_mb: int) -> list[str]:
        """构造运行命令。"""

    # ---- 工具方法 ---------------------------------------------------------

    def make_work_dir(self) -> str:
        """在 workspace 下建一个隔离的临时目录。"""
        self.work_root.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix=f"oj_{self.language.value}_", dir=str(self.work_root))

    def stem(self, name: str) -> str:
        """源文件 / 可执行文件的文件名主干。

        就是题目英文名（CCF 规约），没给名字时退回 :attr:`DEFAULT_STEM`。
        """
        return sanitize_slug(name) if (name or "").strip() else self.DEFAULT_STEM

    def task_dir(self, work_dir: str, name: str) -> str:
        """在临时工作目录下再套一层"题目目录"，返回程序的运行目录。

        对齐 CCF 的目录结构（``<考号>/<题目英文名>/<题目英文名>.cpp``）：
        程序、可执行文件、数据文件都落在这一层里，程序以裸名访问数据文件。
        没给题目名时不套这一层。
        """
        if not (name or "").strip():
            return work_dir
        path = os.path.join(work_dir, sanitize_slug(name))
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def cleanup(work_dir: str) -> None:
        """删除临时目录；失败只记日志。"""
        if not work_dir:
            return
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            log.debug("清理临时目录失败: %s", work_dir)

    @staticmethod
    def _label(key: str) -> str:
        from .compilers import KEY_LABELS

        return KEY_LABELS.get(key, key)

    def _compiler(self, key: str) -> str:
        return self.paths.get(key, "")

    def _write_source(self, code: str, work_dir: str, filename: str) -> str:
        path = os.path.join(work_dir, filename)
        # newline="\n" 保证跨设备一致性；评测机上不引入 CRLF 差异
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(code)
        return path


class CompileProfile(ABC):
    """一个编译器家族的"编译参数写法"。

    存在的理由：MSVC 与 GCC 系的差异不只是换几个参数名，而是**行为模型不同** ——

    * MSVC 认不出 ``/std:`` 时**不报错**，只发一条 ``warning D9002``，退出码仍是 0，
      然后静默按默认标准编译。所以"编译成功"完全不能证明参数被接受了。
    * MSVC 的编译错误走 **stdout**，诊断警告（D9002）走 **stderr**，与 GCC 正好相反。
    * MSVC 没有 ``INCLUDE`` / ``LIB`` 就找不到标准库，编译时必须显式传环境。

    把这些收进 profile 之后，:class:`CRunner` 只剩下"试错 + 缓存 + 判定成功"这套通用逻辑。
    """

    #: 用于日志与提示的家族名
    name = "generic"

    #: 编译时是否必须提供额外环境变量（环境组装不出来即视为不可用）
    needs_environment = False

    #: 优化开关在界面上怎么写：(开, 关)。``None`` 表示这门语言没有这个概念。
    OPTIMIZE_LABELS: tuple[str, str] | None = None

    @abstractmethod
    def command(
        self,
        *,
        compiler: str,
        work_dir: str,
        source_name: str,
        target_name: str,
        flag: str,
        optimize: bool,
        language: Language,
    ) -> list[str]:
        """构造一次编译的命令行。"""

    def environment(self, compiler: str) -> dict[str, str] | None:
        """编译所需的环境变量；``None`` 表示继承当前进程。"""
        return None

    def ignored(self, completed, flag: str) -> bool:
        """编译器是否**忽略**了 ``flag``（是"忽略"而非"拒绝"）。

        GCC 系不会出现这种情况（不认识的参数直接报错），所以默认返回 ``False``。
        """
        return False

    def ladder(self, language: Language, compiler: str) -> tuple[str, ...]:
        """标准参数候选，从优到劣。空元组表示不需要任何标准参数。"""
        return ()


class GccProfile(CompileProfile):
    """GCC / Clang 系：``-std=`` 家族，不认识的参数会硬报错。"""

    name = "gcc"
    #: 优化开关在界面上怎么写：(开, 关)
    OPTIMIZE_LABELS = ("-O2", "-O0")

    def command(
        self,
        *,
        compiler: str,
        work_dir: str,
        source_name: str,
        target_name: str,
        flag: str,
        optimize: bool,
        language: Language,
    ) -> list[str]:
        source = os.path.join(work_dir, source_name)
        target = os.path.join(work_dir, target_name)
        command = [compiler]
        if optimize:
            command.append("-O2")
        command.append("-Wall")
        if flag:
            command.append(flag)
        command += [source, "-o", target]
        return command

    def ladder(self, language: Language, compiler: str) -> tuple[str, ...]:
        return CPP_STANDARD_LADDER if language is Language.CPP else C_STANDARD_LADDER


class MsvcProfile(CompileProfile):
    """MSVC（``cl.exe``）：``/`` 风格参数，且必须外带 INCLUDE / LIB 环境。

    参数取舍：

    * ``/utf-8`` —— 源码统一按 UTF-8 落盘。不加的话 cl 会按系统 ANSI 代码页
      （中文 Windows 是 936）去读，中文字符串字面量在"读进来再写出去"的过程中
      碰巧能还原，但 ``L"中文"`` 这种宽字符字面量会直接编错，而且遇到无法映射的
      字节还会刷 ``C4819`` 警告。显式声明 UTF-8 才是正解。
    * ``/EHsc`` —— 启用标准 C++ 异常语义。GCC 默认就开启，不写会让"能过的代码在
      这台机器上编不过"。只对 C++ 加，C 模式下这个选项无意义。
    * ``/MT`` —— 静态链接 C 运行库。判题产物要在别人的机器上跑，``/MD`` 会引入
      ``vcruntime140.dll`` 依赖，缺了就报"找不到 DLL"。
    * ``/W3`` —— 对应 ``-Wall`` 的告警级别。``/Wall`` 会把系统头文件里的
      每一条提示都倒出来，噪音太大。
    * 源文件与产物都用**相对名**。工作目录在 ``%LOCALAPPDATA%`` 下，
      用户名带空格时 ``/Fe:C:\\Users\\John Doe\\...`` 会被 cl 的参数解析绊住；
      而 ``cwd`` 已经设成工作目录，相对名既绕开空格问题，报错里也只会显示
      ``main.cpp(3): error C...`` 这种干净形态。
    """

    name = "msvc"
    needs_environment = True
    #: 优化开关在界面上怎么写：(开, 关)。注意 MSVC 关优化是 ``/Od`` 而不是"没有参数"。
    OPTIMIZE_LABELS = ("/O2", "/Od")

    def command(
        self,
        *,
        compiler: str,
        work_dir: str,
        source_name: str,
        target_name: str,
        flag: str,
        optimize: bool,
        language: Language,
    ) -> list[str]:
        command = [compiler, "/nologo"]
        if flag:
            command.append(flag)
        command.append("/utf-8")
        if language is Language.CPP:
            command.append("/EHsc")
        if optimize:
            command.append("/O2")
        command += ["/W3", "/MT"]
        command += [
            f"/Fe:{target_name}",
            f"/Fo:{os.path.splitext(target_name)[0]}.obj",
            source_name,
        ]
        return command

    def environment(self, compiler: str) -> dict[str, str] | None:
        from ..win32 import msvc

        return msvc.cached_environment(os.path.abspath(compiler))

    def ignored(self, completed, flag: str) -> bool:
        if not flag:
            return False
        from ..win32 import msvc

        return flag in msvc.ignored_options(completed)

    def ladder(self, language: Language, compiler: str) -> tuple[str, ...]:
        from ..win32 import msvc

        # 与 GCC 系的阶梯思路不同：MSVC 认不出 /std: 时不会失败，所以没法"试出来"，
        # 只能按工具集版本号直接选。选完再留一个"不加参数"的兜底，
        # 万一这个版本确实忽略了它（D9002），下一轮就会退到无参数并把结果记进缓存。
        flag = msvc.standard_flag(os.path.abspath(compiler), language is Language.CPP)
        return (flag, _NO_STANDARD) if flag else (_NO_STANDARD,)


_GCC_PROFILE = GccProfile()
_MSVC_PROFILE = MsvcProfile()


def profile_for(compiler: str) -> CompileProfile:
    """按编译器路径挑参数写法。"""
    from ..win32 import msvc

    return _MSVC_PROFILE if msvc.is_msvc(compiler) else _GCC_PROFILE


class CRunner(BaseRunner, ABC):
    """C / C++ 共用实现。

    语言标准用"候选阶梯 + 失败降级"来选，而不是写死一个。

    原因很实际：Dev-Cpp 自带的 TDM-GCC 4.9 **不认 ``-std=c++17``**，
    这类机器在中学/机房环境里非常常见。早期写死 ``-std=c++17`` 的后果是
    "Python 能判、Java 能判，C++ 一律编译失败"，而且报错信息（unrecognized
    command line option）看起来像是用户代码的问题。

    做法是：从新标准往旧标准依次试，只在失败**确实与标准参数有关**时才降级 ——
    语法错误之类的真实问题立刻返回，不会白编译三次。

    编译器家族差异（GCC 系 / MSVC 系）交给 :class:`CompileProfile` 处理。
    """

    #: 上一次编译用的编译器家族。界面上的"编译优化"该标 ``-O2`` 还是 ``/O2``
    #: 由它决定 —— 这两个是同一个意思的不同写法，标错了比不标还糟。
    _profile: CompileProfile | None = None

    @property
    def optimize_labels(self) -> tuple[str, str] | None:
        # 还没编译过时按 GCC 显示：本机的默认工具链就是 Dev-Cpp 的 TDM-GCC，
        # 真用 MSVC 的话第一次编译之后就会改过来。
        return (self._profile or GccProfile()).OPTIMIZE_LABELS

    def compile(self, code: str, work_dir: str, *, name: str = "") -> CompileOutput:
        valid, message = self.check_environment()
        if not valid:
            return CompileOutput(ok=False, message=message, work_dir=work_dir)

        compiler = self._compiler(self.language.value)
        profile = profile_for(compiler)
        self._profile = profile
        environment = profile.environment(compiler)
        if profile.needs_environment and environment is None:
            return CompileOutput(
                ok=False,
                message=(f"{os.path.basename(compiler)} 的编译环境不可用："
                         "找不到 Visual Studio 的 INCLUDE / LIB 目录。\n"
                         "请确认已安装「使用 C++ 的桌面开发」工作负载。"),
                work_dir=work_dir,
            )

        # CCF 规约：源程序 <题目英文名>.cpp，编译产物就叫 <题目英文名>（Linux 无扩展名）。
        # Windows 上 gcc/cl 都会自动补 .exe，所以检查产物时得按平台加上。
        stem = self.stem(name)
        source_name = f"{stem}{self.language.suffix}"
        self._write_source(code, work_dir, source_name)
        target_name = executable_name(stem)
        target = os.path.join(work_dir, target_name)

        failure = "编译失败"
        attempts = self._standard_attempts(compiler, profile)
        for position, flag in enumerate(attempts):
            command = profile.command(
                compiler=compiler,
                work_dir=work_dir,
                source_name=source_name,
                target_name=target_name,
                flag=flag,
                optimize=self.optimize,
                language=self.language,
            )
            try:
                with CompilerLock.hold(compiler):
                    completed = run_silent(command, timeout=COMPILE_TIMEOUT,
                                           cwd=work_dir, env=environment)
            except TimeoutError as exc:
                return CompileOutput(ok=False, message=str(exc), work_dir=work_dir)

            output = _diagnose(completed, work_dir, source_name)
            ignored = profile.ignored(completed, flag)
            produced = completed.returncode == 0 and os.path.exists(target)

            if produced and (not ignored or position + 1 >= len(attempts)):
                # 参数被忽略时记录成"不用参数"，下次编译就不带它，省掉一条警告
                _STANDARD_CACHE[(self.language.value, _cache_key(compiler))] = (
                    _NO_STANDARD if ignored else flag
                )
                if position:
                    log.info("%s 不支持 %s，本次改用 %s",
                             self.language.value, attempts[0], flag or "(不加标准参数)")
                return CompileOutput(ok=True, work_dir=work_dir,
                                     artifact=target, entry=target)

            if ignored or _standard_rejected(output, flag):
                if position + 1 < len(attempts):
                    log.debug("%s 忽略/拒绝 %s，尝试下一个标准",
                              os.path.basename(compiler), flag or "(不加标准参数)")
                    continue
            # 是真的编译错误，换标准救不了
            failure = (output or "编译失败")[:COMPILE_ERROR_LIMIT]
            break

        return CompileOutput(ok=False, message=failure, work_dir=work_dir)

    def build_command(self, compile_output: CompileOutput, memory_limit_mb: int) -> list[str]:
        return [compile_output.artifact]

    def _standard_attempts(self, compiler: str, profile: CompileProfile) -> tuple[str, ...]:
        """把上次实测可用的标准排在最前面，省掉一次注定失败的编译。"""
        ladder = profile.ladder(self.language, compiler) or (_NO_STANDARD,)
        cached = _STANDARD_CACHE.get((self.language.value, _cache_key(compiler)))
        if cached is None:
            return ladder
        return (cached, *(flag for flag in ladder if flag != cached))


class CppRunner(CRunner):
    language = Language.CPP
    required_keys = ("cpp",)


class CCodeRunner(CRunner):
    language = Language.C
    required_keys = ("c",)


class PythonRunner(BaseRunner):
    """Python 无需编译，但仍会生成中间文件并做一次语法自检。"""

    language = Language.PYTHON
    required_keys = ("python",)

    def compile(self, code: str, work_dir: str, *, name: str = "") -> CompileOutput:
        valid, message = self.check_environment()
        if not valid:
            return CompileOutput(ok=False, message=message, work_dir=work_dir)

        interpreter = self._compiler("python")
        source = self._write_source(code, work_dir, f"{self.stem(name)}.py")

        # 用 py_compile 提前暴露语法错误，避免把 SyntaxError 混在运行输出里
        completed = run_silent([interpreter, "-X", "utf8", "-m", "py_compile", source],
                               timeout=COMPILE_TIMEOUT, cwd=work_dir)
        if completed.returncode != 0:
            return CompileOutput(
                ok=False,
                message=(completed.stderr or completed.stdout or "语法检查失败")[:COMPILE_ERROR_LIMIT],
                work_dir=work_dir,
            )
        return CompileOutput(ok=True, work_dir=work_dir, entry=source, artifact=interpreter)

    def build_command(self, compile_output: CompileOutput, memory_limit_mb: int) -> list[str]:
        return [
            self._compiler("python"),
            "-X", "utf8",          # 强制标准流使用 UTF-8
            "-I",                  # 隔离模式：忽略环境变量与用户 site-packages
            "-B",                  # 不写 .pyc
            compile_output.entry,
        ]


class JavaRunner(BaseRunner):
    """Java：``javac`` 编译到工作目录，``java`` 以主类名启动。"""

    language = Language.JAVA
    required_keys = ("javac", "java")

    #: 匹配主类名（public class X / class X 且含 main）
    _PUBLIC_CLASS = re.compile(r"public\s+(?:final\s+|abstract\s+)?class\s+(\w+)")
    _ANY_CLASS = re.compile(r"class\s+(\w+)")
    _COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)

    def compile(self, code: str, work_dir: str, *, name: str = "") -> CompileOutput:
        """编译 Java 源码。

        这里**不按题目英文名给源文件命名**，原因是 ``javac`` 强制要求文件名与
        ``public class`` 名一致（``poker.java`` 里只能是 ``public class poker``），
        两者无法同时满足。CCF 的 CSP-J/S 本身不提供 Java，所以不与 CCF 对齐，
        优先保证能编译通过；工作目录仍然按题目英文名分层（由调用方的
        ``task_dir`` 负责），题的隔离性不受影响。
        """
        valid, message = self.check_environment()
        if not valid:
            return CompileOutput(ok=False, message=message, work_dir=work_dir)

        javac = self._compiler("javac")
        main_class = self.extract_main_class(code)
        source = self._write_source(code, work_dir, f"{main_class}.java")

        command = [javac, "-encoding", "UTF-8", "-d", work_dir, source]
        try:
            with CompilerLock.hold(javac):
                completed = run_silent(command, timeout=COMPILE_TIMEOUT, cwd=work_dir)
        except TimeoutError as exc:
            return CompileOutput(ok=False, message=str(exc), work_dir=work_dir)

        if completed.returncode != 0:
            return CompileOutput(
                ok=False,
                message=(completed.stderr or "编译失败")[:COMPILE_ERROR_LIMIT],
                work_dir=work_dir,
            )
        return CompileOutput(ok=True, work_dir=work_dir, entry=main_class, artifact=work_dir)

    def build_command(self, compile_output: CompileOutput, memory_limit_mb: int) -> list[str]:
        java = self._compiler("java")
        # 堆上限略小于题目内存限制，给 JVM 自身（元空间、线程栈）留出余量
        heap = max(16, int(memory_limit_mb * 0.8))
        return [
            java,
            f"-Xmx{heap}m",
            "-XX:+UseSerialGC",       # 小内存场景下 GC 行为更可预测
            "-Dfile.encoding=UTF-8",
            "-cp", compile_output.work_dir,
            compile_output.entry,
        ]

    @classmethod
    def extract_main_class(cls, code: str) -> str:
        """从源码中找出主类名（``javac`` 要求文件名与 public 类名一致）。"""
        stripped = cls._COMMENT.sub("", code or "")
        match = cls._PUBLIC_CLASS.search(stripped)
        if match:
            return match.group(1)
        match = cls._ANY_CLASS.search(stripped)
        if match:
            return match.group(1)
        return "Main"


#: 语言 -> Runner 类
RUNNER_TYPES: dict[Language, type[BaseRunner]] = {
    Language.CPP: CppRunner,
    Language.C: CCodeRunner,
    Language.PYTHON: PythonRunner,
    Language.JAVA: JavaRunner,
}


def make_runner(
    language: Language,
    paths: Mapping[str, str],
    work_root: str | os.PathLike[str],
    *,
    optimize: bool = True,
) -> BaseRunner:
    """工厂：按语言构造运行器。"""
    try:
        runner_type = RUNNER_TYPES[language]
    except KeyError as exc:
        raise ValueError(f"不支持的语言: {language}") from exc
    return runner_type(paths, work_root, optimize=optimize)


#: 各语言的"自检程序"：编译并运行它，用于确认工具链真的能用
SELF_TEST_PROGRAMS: dict[Language, str] = {
    Language.CPP: '#include <iostream>\nint main() { std::cout << "OJSELFTEST"; return 0; }\n',
    Language.C: '#include <stdio.h>\nint main() { printf("OJSELFTEST"); return 0; }\n',
    Language.PYTHON: 'print("OJSELFTEST")\n',
    Language.JAVA: ('public class OjSelfTest {\n'
                    '    public static void main(String[] args) {\n'
                    '        System.out.print("OJSELFTEST");\n'
                    '    }\n'
                    '}\n'),
}

SELF_TEST_MARK = "OJSELFTEST"


def self_test(
    language: Language,
    paths: Mapping[str, str],
    work_root: str | os.PathLike[str],
) -> tuple[bool, str]:
    """真实编译并运行一段最小程序，验证工具链可用。

    只看 ``--version`` 是不够的：路径存在、版本能打印，不代表能编译链接
    （常见于 32/64 位不匹配、缺少 DLL、权限受限）。这里做端到端验证。
    """
    runner = make_runner(language, paths, work_root, optimize=False)
    result = runner.run(SELF_TEST_PROGRAMS[language], "", 20000, 512)

    if result.verdict is Verdict.CE:
        return False, result.message or "编译失败"
    if not result.ok:
        return False, f"{result.verdict.text}：{result.message or result.stderr}".strip()
    if SELF_TEST_MARK not in result.stdout:
        return False, f"输出不符合预期：{result.stdout.strip()[:120]!r}"
    return True, "可用"


def compare_output(actual: str, expected: str) -> bool:
    """比对输出。

    评测惯例：忽略行尾空白（``\\r``、多余空格）与末尾空行，但保留行内与中间空行差异。
    这样"Windows 换行 vs Linux 换行"、"末尾多一个换行"这类无关差异不会误判为 WA。
    """
    return _normalize(actual) == _normalize(expected)


def _normalize(text: str) -> list[str]:
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def diff_summary(actual: str, expected: str, *, limit: int = 3) -> str:
    """定位首个不同的行，用于结果面板给出可读的 WA 原因。"""
    left = _normalize(actual)
    right = _normalize(expected)
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else "<缺少该行>"
        b = right[index] if index < len(right) else "<多出该行>"
        if a != b:
            return (f"第 {index + 1} 行不一致\n"
                    f"    期望: {b[:120]}\n"
                    f"    实际: {a[:120]}")
    return ""


def kill_all(work_root: str | os.PathLike[str]) -> None:
    """清理残留：删除 workspace 下所有子目录（启动时调用）。"""
    root = Path(work_root)
    if not root.is_dir():
        return
    for child in root.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception:
            log.debug("清理残留失败: %s", child)
