"""领域模型。

JSON 结构保持与旧版（1.2）题库文件兼容，键名不变，以便旧题库与导出包仍可导入：

.. code-block:: json

    {
      "P0001": {
        "id": "P0001",
        "title": "两数求和",
        "description": "## 题目描述\\n输入两个整数，输出它们的和。",
        "time_limit": 1000,
        "memory_limit": 256,
        "testcases": [{"input": "1 2\\n", "output": "3\\n"}]
      }
    }

新增的 ``created_at`` / ``updated_at`` / ``source`` / ``slug`` 等字段为可选扩展，
旧版读取时会忽略。

题目英文名（``slug``）与判题方式都**只在配置过时才写出来**，
没配过的题目落盘后与旧版逐字节一致：

.. code-block:: json

    "slug": "poker",
    "judge": {
      "io_mode": "file",
      "input_file": "{name}.in",
      "output_file": "{name}.out",
      "checker_language": "cpp",
      "checker": "int main(int argc, char** argv) { ... }"
    }

路径与命名按 CCF CSP-J/S 第二轮认证（NOI Linux 2.0）的规约组织：
**题目英文名**决定源文件名、可执行文件名与数据文件名（题目 ``poker`` →
源文件 ``poker.cpp``、可执行文件 ``poker``、数据文件 ``poker.in`` / ``poker.out``），
程序在**当前路径**下用不带绝对路径的名字读写数据文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

#: 一个测试点默认值多少分。
#:
#: 取 10 不是随手定的：NOI / NOIP 每题满分 100 分，而一份测试数据通常正好
#: 10 个点 —— 默认值一填，10 个点的题天然就是满分 100。测试点不做成"等权摊分"
#: 是因为那需要题目知道"一共几个点"才能算出每点多少分，而分值是跟着测试点
#: 落盘的：以后有人把 10 个点删成 8 个，等权摊分会**悄悄**把每题满分从 100
#: 变成 100，而分值制会诚实地变成 80。
DEFAULT_TESTCASE_POINTS = 10


def _read_points(raw: Any) -> int:
    """读一个测试点分值，坏数据一律退回默认值。

    宁可当成"没配过"，也不要因为一个写坏的字段把这题变成 0 分 —— 那样整题
    所有人都是 0 分，现场看起来像判题器坏了，比"分值不对"难查得多。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TESTCASE_POINTS
    return value if value >= 0 else DEFAULT_TESTCASE_POINTS


class Language(str, Enum):
    """支持的语言。``value`` 同时用作题库字段与 runner 注册键。"""

    CPP = "cpp"
    C = "c"
    PYTHON = "python"
    JAVA = "java"

    @property
    def display(self) -> str:
        return {
            Language.CPP: "C++ (g++ / clang++)",
            Language.C: "C (gcc / clang)",
            Language.PYTHON: "Python 3",
            Language.JAVA: "Java",
        }[self]

    @property
    def short(self) -> str:
        return {
            Language.CPP: "C++",
            Language.C: "C",
            Language.PYTHON: "Python",
            Language.JAVA: "Java",
        }[self]

    @property
    def suffix(self) -> str:
        return {
            Language.CPP: ".cpp",
            Language.C: ".c",
            Language.PYTHON: ".py",
            Language.JAVA: ".java",
        }[self]

    @property
    def monospace_font(self) -> str:
        return "Consolas"

    @property
    def key_paths(self) -> tuple[str, ...]:
        """该语言需要的编译器/解释器配置键。"""
        if self is Language.JAVA:
            return ("javac", "java")
        return (self.value,)

    @classmethod
    def from_value(cls, value: str) -> "Language | None":
        try:
            return cls(value)
        except ValueError:
            return None


#: 可以做校验器的语言。C 与 Java 不做支持：校验器里主要在处理字符串与空白，
#: 这两种写起来比 C++ / Python 费事得多，而在线评测里的校验器几乎也只有这两种写法。
CHECKER_LANGUAGES: tuple[Language, ...] = (Language.CPP, Language.PYTHON)

#: 题目英文名里唯一合法的字符（CCF 规约：小写英文字母、数字、下划线）
_SLUG_INVALID = re.compile(r"[^a-z0-9_]+")
#: 题目英文名的合法形态
_SLUG_OK = re.compile(r"[a-z0-9_]+")

#: 推不出英文名时的兜底。CCF 不会有这种情况（题目英文名是官方给定的），
#: 但本程序允许用中文当题目 ID，总得给个能用的名字。
FALLBACK_SLUG = "task"

#: 文件名模板里的占位符，会被替换成题目英文名
TASK_NAME_PLACEHOLDER = "{name}"

#: 文件模式下输入 / 输出文件名的默认值。
#: 按 CCF CSP-J/S 第二轮认证的规约：数据文件与题目同名，后缀 .in / .out
#: （题目 poker → poker.in / poker.out）。这里是**模板**，`{name}` 在判题时
#: 由题目的英文名替换 —— 因为 JudgeConfig 自己看不到题目的名字。
DEFAULT_INPUT_FILE = f"{TASK_NAME_PLACEHOLDER}.in"
DEFAULT_OUTPUT_FILE = f"{TASK_NAME_PLACEHOLDER}.out"


def sanitize_slug(text: str) -> str:
    """把任意文本清洗成 CCF 规约的题目英文名。

    CCF 要求「文件夹名称必须使用英文小写字母，且与题目名称完全一致」，
    所以这里做三件事：转小写、把不合法字符并成下划线、去掉首尾下划线。
    全都被洗掉时返回 :data:`FALLBACK_SLUG`（中文题目 ID 就是这种情况）。
    """
    cleaned = _SLUG_INVALID.sub("_", (text or "").strip().lower()).strip("_")
    return cleaned or FALLBACK_SLUG


class IOMode(str, Enum):
    """输入输出方式。"""

    STDIO = "stdio"     # 标准输入输出
    FILE = "file"       # 文件输入输出（题目指定文件名）

    @property
    def display(self) -> str:
        return {IOMode.STDIO: "标准输入输出", IOMode.FILE: "文件输入输出"}[self]

    @classmethod
    def from_value(cls, value: Any) -> "IOMode":
        # 先认枚举实例本身：这是 ``(str, Enum)`` 的坑 —— ``str(IOMode.FILE)``
        # 在 Python 3.11+ 得到的是 ``"IOMode.FILE"`` 而不是 ``"file"``，
        # 直接 ``cls(str(value))`` 会把一个完全合法的枚举值判成非法。
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.STDIO


@dataclass
class JudgeConfig:
    """一道题的判题方式：输入输出走哪里、用不用自定义校验器。

    默认值就是"老行为"——标准输入输出 + 精确比对，此时 :meth:`to_dict` 返回空字典，
    题目落盘不会多出任何字段，旧题库文件与导出包保持逐字节一致。
    """

    io_mode: IOMode = IOMode.STDIO
    #: 文件模式下的输入 / 输出文件名（相对运行目录，不允许路径分隔符）
    input_file: str = DEFAULT_INPUT_FILE
    output_file: str = DEFAULT_OUTPUT_FILE
    #: 校验器语言；``None`` 表示没有校验器
    checker_language: Language | None = None
    #: 校验器源码。空源码等同于"没有校验器"，一律按精确比对
    checker: str = ""

    @property
    def uses_files(self) -> bool:
        return self.io_mode is IOMode.FILE

    def resolved(self, name: str) -> "JudgeConfig":
        """把文件名模板里的 ``{name}`` 换成题目英文名，返回一份副本。

        题目名是 :class:`Problem` 的属性，``JudgeConfig`` 自己看不到它，
        所以模板的落地只能发生在已知题目名的调用点（判题器 / 自测运行）。
        """
        task = sanitize_slug(name)
        if not self.uses_files or TASK_NAME_PLACEHOLDER not in (
                self.input_file + self.output_file):
            return self
        return replace(self,
                       input_file=self.input_file.replace(TASK_NAME_PLACEHOLDER, task),
                       output_file=self.output_file.replace(TASK_NAME_PLACEHOLDER, task))

    @property
    def uses_checker(self) -> bool:
        """真的要跑校验器吗 —— 源码和语言都得有。"""
        return bool(self.checker.strip()) and self.checker_language is not None

    def to_dict(self) -> dict[str, Any]:
        """只写非默认项。"""
        data: dict[str, Any] = {}
        if self.uses_files:
            data["io_mode"] = self.io_mode.value
            data["input_file"] = self.input_file
            data["output_file"] = self.output_file
        if self.uses_checker:
            data["checker_language"] = self.checker_language.value
            data["checker"] = self.checker
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "JudgeConfig":
        """从 JSON 构造；结构不对时退回默认（标准输入输出 + 精确比对）。"""
        if not isinstance(data, dict):
            return cls()

        checker_language = Language.from_value(str(data.get("checker_language", "") or ""))
        if checker_language not in CHECKER_LANGUAGES:
            checker_language = None

        return cls(
            io_mode=IOMode.from_value(data.get("io_mode", IOMode.STDIO.value)),
            input_file=str(data.get("input_file", "") or "").strip() or DEFAULT_INPUT_FILE,
            output_file=str(data.get("output_file", "") or "").strip() or DEFAULT_OUTPUT_FILE,
            checker_language=checker_language,
            checker=str(data.get("checker", "") or ""),
        )

    def problems(self) -> list[str]:
        """返回配置问题列表，供 :meth:`Problem.validate` 汇总。"""
        found: list[str] = []
        if self.uses_files:
            if not self.input_file.strip():
                found.append("文件模式下输入文件名不能为空")
            if not self.output_file.strip():
                found.append("文件模式下输出文件名不能为空")
            for label, name in (("输入文件", self.input_file), ("输出文件", self.output_file)):
                bad = re.search(r'[\\/:*?"<>|]', name)
                if bad:
                    found.append(f"{label}名不能包含路径分隔符或非法字符：{name}")
            if (self.input_file.strip().lower() == self.output_file.strip().lower()
                    and self.input_file.strip()):
                found.append("输入文件名与输出文件名不能相同")
        if self.checker.strip() and self.checker_language is None:
            found.append("填了校验器源码，但没有选择校验器语言")
        if self.checker_language is not None and not self.checker.strip():
            # 只有写题界面能凑出这种状态（从 JSON 读回来的配置不会有半截校验器）：
            # 勾了复选框却没写源码，实际仍按精确比对走，不说破的话很难发现
            found.append("选择了校验器语言，但校验器源码是空的")
        return found


class Verdict(str, Enum):
    """评测结论，沿用 OJ 界惯例缩写。"""

    AC = "AC"    # Accepted
    WA = "WA"    # Wrong Answer
    TLE = "TLE"  # Time Limit Exceeded
    MLE = "MLE"  # Memory Limit Exceeded
    RE = "RE"    # Runtime Error
    CE = "CE"    # Compile Error
    PE = "PE"    # Presentation Error
    OLE = "OLE"  # Output Limit Exceeded
    IE = "IE"    # Internal Error（本地工具异常，非用户代码问题）

    @property
    def label(self) -> str:
        return {
            Verdict.AC: "通过",
            Verdict.WA: "答案错误",
            Verdict.TLE: "运行超时",
            Verdict.MLE: "内存超限",
            Verdict.RE: "运行错误",
            Verdict.CE: "编译错误",
            Verdict.PE: "格式错误",
            Verdict.OLE: "输出超限",
            Verdict.IE: "评测机内部错误",
        }[self]

    @property
    def text(self) -> str:
        """``AC 通过`` 形式的短标签。"""
        return f"{self.value} {self.label}"

    @property
    def accepted(self) -> bool:
        return self is Verdict.AC


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TestCase:
    """单个测试点。"""

    input: str = ""
    output: str = ""
    name: str = ""
    #: 是否作为**样例**展示。局域网测验里只有样例会下发给客户端，正式测试点
    #: 的期望输出永远留在主机上 —— 这是"主机统一判题"这个架构最大的安全红利。
    #: 与 JudgeConfig 同样遵循"只写非默认值"的约定，旧题库落盘结果逐字节不变。
    sample: bool = False
    #: 这个测试点值多少分。
    #:
    #: NOI 是**按测试点给分**的（部分分）：过几个点拿几个点的分，因此每个点
    #: 自带分值。默认 10 分 —— 于是 10 个点的题满分正好 100，与 CCF 每题 100 分
    #: 的约定一致；想让 5 个点的题也是满分 100，就把每个点设成 20。
    points: int = DEFAULT_TESTCASE_POINTS

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"input": self.input, "output": self.output}
        if self.name:
            data["name"] = self.name
        if self.sample:
            data["sample"] = True
        # 分值同样"只写非默认值"：默认 10 分的历史题库落盘结果逐字节不变
        if self.points != DEFAULT_TESTCASE_POINTS:
            data["points"] = self.points
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "TestCase":
        if isinstance(data, dict):
            return cls(
                input=str(data.get("input", "") or ""),
                output=str(data.get("output", "") or ""),
                name=str(data.get("name", "") or ""),
                sample=bool(data.get("sample", False)),
                points=_read_points(data.get("points")),
            )
        raise TypeError(f"测试点必须是对象，收到 {type(data).__name__}")

    @property
    def preview(self) -> str:
        head = (self.input or "").strip().splitlines()
        return head[0][:40] if head else "(空输入)"


@dataclass
class Problem:
    """一道题。"""

    id: str
    title: str = ""
    description: str = ""
    time_limit: int = 1000          # 毫秒
    memory_limit: int = 256         # MB
    testcases: list[TestCase] = field(default_factory=list)
    source: str = ""
    #: 题目英文名（CCF 规约：小写字母 / 数字 / 下划线）。留空则用题目 ID 推导，
    #: 它决定源文件名、可执行文件名与 `{name}.in` / `{name}.out` 这类数据文件名
    slug: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    #: 判题方式（文件输入输出 / 自定义校验器）。默认即标准输入输出 + 精确比对
    judge: JudgeConfig = field(default_factory=JudgeConfig)

    # ---- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "time_limit": self.time_limit,
            "memory_limit": self.memory_limit,
            "testcases": [case.to_dict() for case in self.testcases],
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        # 没填英文名的题目不写这个键，落盘结果与旧版本逐字节一致
        if self.slug:
            data["slug"] = self.slug
        # 没配置过判题方式的题目不写这个键，落盘结果与旧版本逐字节一致
        judge = self.judge.to_dict()
        if judge:
            data["judge"] = judge
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Problem":
        """从 JSON 对象构造。字段缺失或类型异常时使用安全默认值。"""
        if not isinstance(data, dict):
            raise TypeError("题目必须是 JSON 对象")

        raw_cases = data.get("testcases") or []
        if not isinstance(raw_cases, list):
            raw_cases = []
        cases = [TestCase.from_dict(item) for item in raw_cases]

        return cls(
            id=str(data.get("id", "") or "").strip(),
            title=str(data.get("title", "") or ""),
            description=str(data.get("description", "") or ""),
            time_limit=_as_positive_int(data.get("time_limit"), 1000),
            memory_limit=_as_positive_int(data.get("memory_limit"), 256),
            testcases=cases,
            source=str(data.get("source", "") or ""),
            slug=str(data.get("slug", "") or "").strip(),
            created_at=str(data.get("created_at", "") or _now()),
            updated_at=str(data.get("updated_at", "") or _now()),
            judge=JudgeConfig.from_dict(data.get("judge")),
        )

    # ---- 校验 -------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回问题列表，为空表示合法。"""
        problems: list[str] = []
        if not self.id:
            problems.append("题目 ID 不能为空")
        elif not re.fullmatch(r"[\w\u4e00-\u9fff.\- ]{1,64}", self.id):
            problems.append("题目 ID 只能包含字母、数字、下划线、中文、点、连字符，且不超过 64 字符")
        slug = self.slug.strip()
        if slug and not _SLUG_OK.fullmatch(slug):
            problems.append("题目英文名只能用小写字母、数字、下划线（CCF 规约），"
                            "例如 poker")
        if not self.title.strip():
            problems.append("题目标题不能为空")
        if not self.description.strip():
            problems.append("题目描述不能为空")
        if self.time_limit <= 0:
            problems.append("时间限制必须为正整数（毫秒）")
        if self.memory_limit <= 0:
            problems.append("内存限制必须为正整数（MB）")
        if not self.testcases:
            problems.append("至少需要一个测试点")
        for index, case in enumerate(self.testcases, start=1):
            if not case.output.strip():
                problems.append(f"测试点 {index} 的期望输出为空")
        problems.extend(self.judge.problems())
        return problems

    def is_valid(self) -> bool:
        return not self.validate()

    # ---- 便利方法 ---------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = _now()

    @property
    def testcase_count(self) -> int:
        return len(self.testcases)

    @property
    def total_points(self) -> int:
        """本题满分 = 各测试点分值之和。

        没有测试点时是 0 —— 那本身就是一份不合法的题目（``validate`` 会拦），
        调用方读到 0 应当按"没法给分"处理，而不是当成满分 0。
        """
        return sum(case.points for case in self.testcases)

    @property
    def english_name(self) -> str:
        """题目的英文名，用于源文件 / 可执行文件 / 数据文件的命名。

        优先用显式填写的「英文名」；留空就从题目 ID 推导，这样老题目不改也能判。
        ID 是纯中文这种推导不出来的情况会落到 :data:`FALLBACK_SLUG`。
        """
        return sanitize_slug(self.slug or self.id)

    @property
    def slug_is_derived(self) -> bool:
        """英文名是否由题目 ID 推导而来（界面据此提示「建议填一个英文名」）。"""
        return not self.slug.strip()

    def io_files(self) -> tuple[str, str]:
        """文件模式下实际使用的输入 / 输出文件名（模板已展开）。"""
        resolved = self.judge.resolved(self.english_name)
        return resolved.input_file, resolved.output_file

    def summary(self) -> str:
        return (f"{self.id} · {self.title} · {len(self.testcases)} 个测试点 · "
                f"满分 {self.total_points} · "
                f"{self.time_limit}ms / {self.memory_limit}MB{self.judge_note()}")

    def judge_note(self) -> str:
        """判题方式的极简标注，给题目列表用；默认方式返回空串。"""
        marks: list[str] = []
        if self.judge.uses_files:
            input_file, output_file = self.io_files()
            marks.append(f"文件 {input_file}/{output_file}")
        if self.judge.uses_checker:
            marks.append("特殊判题")
        return " · " + " · ".join(marks) if marks else ""

    def image_refs(self) -> list[str]:
        """描述中以 Markdown 图片语法引用的本地资源名。"""
        refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", self.description or "")
        return [ref for ref in refs if not ref.lower().startswith(("http://", "https://", "data:"))]

    def used_assets(self) -> list[str]:
        """描述中被引用到的资源名集合（去重保序）。"""
        seen: set[str] = set()
        result: list[str] = []
        for ref in self.image_refs():
            name = ref.replace("\\", "/").lstrip("./")
            if name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def clone(self, new_id: str | None = None) -> "Problem":
        data = self.to_dict()
        if new_id:
            data["id"] = new_id
            data["title"] = self.title
        data["created_at"] = _now()
        data["updated_at"] = _now()
        return Problem.from_dict(data)


def _as_positive_int(value: Any, fallback: int) -> int:
    """把 JSON 里的数字安全转成正整数。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def iter_problems(payload: Any) -> Iterable[Problem]:
    """把 ``{id: {...}}`` 形态的题库字典转为 :class:`Problem` 序列。"""
    if not isinstance(payload, dict):
        return []
    result: list[Problem] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        data = dict(value)
        data.setdefault("id", key)
        try:
            result.append(Problem.from_dict(data))
        except Exception:
            continue
    return result
