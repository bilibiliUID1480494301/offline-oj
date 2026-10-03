"""雷同检测：**完全重复**（确定性）与**高度相似**（启发式）。

两层的可信度差着量级，所以分开算、分开报，界面也必须分开说：

* **完全重复** —— 去注释、去行首尾空白、去空行之后**逐字节**相同。
  等价于"除了注释和排版，一个字都没改"。这一层是确定性的，可以当证据；
* **高度相似** —— 词法归一化（标识符 / 数字 / 字符串字面量换占位符）+ k-gram
  滑窗 + winnowing 取指纹 + Jaccard。**这一层是启发式的，只能当线索**：
  同一道水题的朴素解法本来就长得一样（"读入、求和、输出"能写出多少花样？），
  它给的是"值得人工看一眼的几对"，不是结论。

界面上一律写「供人工复核」——这句话不是免责声明，是这个功能的正确用法。

为什么不用第三方库 / 不用 Rust
-------------------------------
指纹集合是几百个量级、提交是几百份量级。用**倒排索引**（指纹 → 哪些提交有它）
只去比"至少共享一个指纹"的那些对，实际代价近似线性。纯 Python 完全够 ——
这与项目"不为性能引入编译依赖"的约定一致（见 ``docs/升级规划-国赛级.md`` §0）。

本层约定
--------
不 import PySide6，也不 import ``net/`` —— 数据由界面喂普通 dict（键见
:func:`analyse`），与 ``core/export.py`` / ``core/records.py`` 同一个套路。

几个必须防的误报
----------------
* **只在同一道题内比较**：跨题比没有意义（两道题都写 `for` 循环不是抄）；
* **扣掉模板骨架**：`#include <bits/stdc++.h>`、`int main()`、快读模板这类
  "全班都一样"的片段，不扣掉的话第一版报告会全是 90%+。做法是对同一题的
  所有提交统计每个指纹的"公开度"，超过阈值的先扣掉再算相似度；
* **小样本不做骨架扣除**：两份提交里"两份都有"的指纹公开度是 100%，
  照扣不误的话相似度会直接归零。所以骨架判定要求**至少 3 份提交都有**
  （见 :data:`SKELETON_MIN_COUNT`）；
* **一人一题只留一条代表**：同一人同一题交了好几次时，取**最高分**那次
  （同分取最新）。否则 100 份提交会变成 300 条记录，报告读不成。
"""

from __future__ import annotations

import hashlib
import math
import re
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# 语言族
# ---------------------------------------------------------------------------

#: Python 的注释与字符串语法自成一族。
FAMILY_PYTHON = "python"
#: C / C++ / Java 共用 ``//`` 与 ``/* */``，字符串转义也一致。
FAMILY_C = "c"

_PYTHON_ALIASES = {"python", "py", "python3"}
_JAVA_ALIASES = {"java"}

#: 语言 → 家族。认不出来的（老数据、外部接入的判题器）按 C 系处理 ——
#: 它只影响分词，不会让程序报错，最坏情况是某种语言的字符串被切碎。
_FAMILY = {
    "c": FAMILY_C, "cpp": FAMILY_C, "c++": FAMILY_C, "cc": FAMILY_C, "cxx": FAMILY_C,
    "java": FAMILY_C,
    "python": FAMILY_PYTHON, "py": FAMILY_PYTHON, "python3": FAMILY_PYTHON,
}


def language_family(language: Any) -> str:
    """语言名 → 语法家族（``"python"`` 或 ``"c"``）。"""
    value = getattr(language, "value", language)
    return _FAMILY.get(str(value).strip().lower(), FAMILY_C)


#: 真正的语言关键字。**这些不能归一化成占位符** —— 否则 ``for`` 与 ``while``
#: 会变成同一个东西，"换了一种循环"这种真差别就被抹平了。
_KEYWORDS: dict[str, frozenset[str]] = {
    FAMILY_C: frozenset("""
        alignas alignof asm auto bool break case catch char char8_t char16_t char32_t
        class const consteval constexpr constinit const_cast continue co_await
        co_return co_yield decltype default delete do double dynamic_cast else enum
        explicit export extern false float for friend goto if inline int long mutable
        namespace new noexcept nullptr operator private protected public register
        reinterpret_cast requires return short signed sizeof static static_assert
        static_cast struct switch template this thread_local throw true try typedef
        typeid typename union unsigned using virtual void volatile wchar_t while
        abstract assert boolean byte extends final finally implements import
        instanceof interface native package strictfp super synchronized throws
        transient transient_var
    """.split()),
    FAMILY_PYTHON: frozenset("""
        and as assert async await break class continue def del elif else except
        finally for from global if import in is lambda nonlocal not or pass raise
        return try while with yield True False None match case
    """.split()),
}

#: 常用标准库 / 内置名。把 ``print`` 与 ``foo`` 都归一化成 ``$ID`` 会让
#: "换了函数名的同一段代码"看起来更像，这里留一份常见表把它压住。
#: 表不求全 —— 漏掉的名字最坏只是被判成标识符，不会造成错误结论。
_BUILTINS: dict[str, frozenset[str]] = {
    FAMILY_C: frozenset("""
        printf scanf puts putchar getchar gets strlen strcpy strcmp memset memcpy
        malloc free calloc realloc qsort abs labs llabs fabs sqrt pow min max swap
        sort stable_sort reverse unique lower_bound upper_bound find count fill
        push_back pop_back emplace_back push pop front back top size empty clear
        begin end rbegin rend insert erase substr length at first second make_pair
        cin cout cerr endl ios sync_with_stdio tie push_heap pop_heap make_heap
        vector string map set unordered_map unordered_set pair queue stack deque
        priority_queue list array bitset tuple make_tuple get tie accumulate
        max_element min_element lower upper to_string stoi stoll stod atoi atol
        INT_MAX INT_MIN LLONG_MAX LLONG_MIN LONG_MAX LONG_MIN DBL_MAX
        main include define ifdef ifndef endif pragma namespace std
        BufferedReader InputStreamReader System String StringBuilder ArrayList
        HashMap HashSet Scanner Math Arrays Collections List Map Set Queue
    """.split()),
    FAMILY_PYTHON: frozenset("""
        print input len range int str float bool list dict set tuple sorted sum
        min max abs map filter zip enumerate reversed any all round pow divmod
        ord chr hex bin oct repr format isinstance issubclass type id hash iter
        next open read write append extend insert remove pop index count sort
        split join strip lstrip rstrip replace startswith endswith upper lower
        __name__ __main__ sys stdin stdout stderr
    """.split()),
}

#: k-gram 的 k。C++ 词法更碎（模板、指针符号），窗口开大一点；
#: Python 一行 token 少，k 太大就什么都匹配不上了。
K_GRAM = {FAMILY_C: 15, FAMILY_PYTHON: 10}

#: winnowing 的窗口宽度。取 4 是 MOSS 类工具的经验值：
#: ``k + w - 1`` 保证长度 k+w-1 以上的共同片段一定会被某个窗口选中。
WINDOW = 4

#: 骨架指纹的公开度阈值：同一题里超过这个比例的提交都有它，就认为它是模板。
SKELETON_RATIO = 0.8

#: 骨架判定的最小份数。**这条是为了小样本**：两份提交里"两份都有"的公开度
#: 是 100%，照比例扣的话相似度会直接归零。要求至少 3 份都有才认骨架。
SKELETON_MIN_COUNT = 3

#: 默认只报相似度不低于这个值的对。低于它的对数量大、意义小。
DEFAULT_MIN_SIMILARITY = 0.5

#: 默认最多报多少对。
DEFAULT_TOP_PAIRS = 20

#: 相似度的三个档。界面按档上色，措辞刻意不用"抄袭" —— 这个工具不判案。
BAND_HIGH = 0.9
BAND_MEDIUM = 0.75


def severity_band(similarity: float) -> str:
    """相似度 → 档位（``"high"`` / ``"medium"`` / ``"low"``）。"""
    if similarity >= BAND_HIGH:
        return "high"
    if similarity >= BAND_MEDIUM:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# 词法扫描
# ---------------------------------------------------------------------------

def _scan(code: str, family: str) -> Iterator[tuple[str, str]]:
    """把源码切成事件流：``(kind, text)``。

    ``kind`` 取 ``comment`` / ``str`` / ``num`` / ``id`` / ``ws`` / ``op``。

    **一个扫描器服务两层**（去注释与 token 归一化），是为了让两层看到的
    词法完全一致 —— 如果各写一个正则，遇到 ``"http://x"`` 这种字符串里带
    ``//`` 的写法，两层会给出互相矛盾的结果，而那种矛盾极难排查。

    刻意手写状态机而不是用正则：正则在"字符串里的 ``//`` 不算注释"这一条上
    必须靠回看上下文，而状态机天然就是按上下文走的。
    """
    n = len(code)
    # BOM 直接跳过：学生用记事本另存出来的文件带 BOM，不该因此被判成
    # "和别人的不一样"（它连字符都不是，却在逐字节比对里算一个字符）。
    i = 1 if code.startswith("\ufeff") else 0
    line_comment = "#" if family == FAMILY_PYTHON else "//"
    block_open, block_close = (None, None) if family == FAMILY_PYTHON else ("/*", "*/")
    while i < n:
        ch = code[i]

        # ---- 行注释 ----
        if code.startswith(line_comment, i):
            end = code.find("\n", i)
            end = n if end < 0 else end
            yield ("comment", code[i:end])
            i = end
            continue

        # ---- 块注释（C 系）----
        if block_open and code.startswith(block_open, i):
            end = code.find(block_close, i + len(block_open))
            end = n if end < 0 else end + len(block_close)
            yield ("comment", code[i:end])
            i = end
            continue

        # ---- 字符串（含 Python 的三引号与前缀）----
        if ch == '"' or ch == "'":
            end, kind = _scan_quoted(code, i, family)
            yield (kind, code[i:end])
            i = end
            continue

        # ---- 空白 ----
        if ch.isspace():
            end = i
            while end < n and code[end].isspace():
                end += 1
            yield ("ws", code[i:end])
            i = end
            continue

        # ---- 数字 ----
        if ch.isdigit() or (ch == "." and i + 1 < n and code[i + 1].isdigit()):
            end = i
            while end < n and (code[end].isalnum() or code[end] in "._"):
                end += 1
            yield ("num", code[i:end])
            i = end
            continue

        # ---- 标识符（以及 Python 的字符串前缀 r"…" / f"…"）----
        if ch.isalpha() or ch == "_":
            end = i
            while end < n and (code[end].isalnum() or code[end] == "_"):
                end += 1
            word = code[i:end]
            if family == FAMILY_PYTHON and len(word) <= 2 and end < n \
                    and code[end] in "\"'":
                # r"…" / rb'…' 这类前缀：整个当字符串，别切成"标识符 + 字符串"
                qend, kind = _scan_quoted(code, end, family)
                yield (kind, code[i:qend])
                i = qend
                continue
            yield ("id", word)
            i = end
            continue

        # ---- 其余：运算符与标点 ----
        yield ("op", ch)
        i += 1


def _scan_quoted(code: str, start: int, family: str) -> tuple[int, str]:
    """从 ``start``（引号处）扫到一个字符串字面量的结束位置（不含）。

    返回 ``(结束下标, 事件类型)``。类型区分 ``str`` 与 C 系的字符字面量
    ``char`` —— 字符字面量里的数字不该被当成"字面量值"处理，但两者在
    归一化里都塌成同一个占位符，所以这里只影响可读性。
    """
    n = len(code)
    quote = code[start]
    # Python 三引号
    if family == FAMILY_PYTHON and code.startswith(quote * 3, start):
        end = code.find(quote * 3, start + 3)
        return (n if end < 0 else end + 3), "str"
    i = start + 1
    while i < n:
        ch = code[i]
        if ch == "\\":
            i += 2                       # 转义：下一个字符无论是什么都吞掉
            continue
        if ch == quote:
            return i + 1, "str"
        if ch == "\n" and family != FAMILY_PYTHON:
            return i, "str"              # C 系里未闭合的引号：到此为止，别吞全文
        i += 1
    return n, "str"


#: 行内连续空白（不含换行）。压缩成单个空格 —— 见 :func:`strip_comments`。
_INLINE_WS = re.compile(r"[^\S\n]+")


def strip_comments(code: str, language: Any = "cpp") -> str:
    """去注释、去排版差异，得到"排过版的代码"。

    完全重复判定就建立在这个结果之上，所以这里的**每一处归一化都必须
    不会让两段不同的代码变得相同** —— 这是这一层"零误报"承诺的全部内容。

    处理规则（两种语言族不同，这是个容易踩的坑）：

    * **行内连续空白压缩成单空格**（``int  main()`` ≡ ``int main()``）：
      纯排版，压掉不会改变任何非空白字符；
    * **行尾空白去掉**；
    * **C / C++ / Java**：行首缩进也去掉 —— 这门语言里缩进无语义，
      ``for`` 体缩进 2 格还是 4 格是同一段代码；
    * **Python**：**行首缩进原样保留**。缩进在这门语言里是语法：
      ``if x:\\n    y`` 是一个块，``if x:\\ny`` 是语法错误 —— 把它们归一成
      同一个东西就是误报，而这一层不许有误报。代价是"把 4 空格缩进改成
      2 空格"不会被判成完全重复（会落到高度相似那一层）。

    字符串字面量里的 ``//`` / ``#`` 不会被当成注释 —— 靠 :func:`_scan`
    的词法，不是正则。
    """
    family = language_family(language)
    kept = "".join(text for kind, text in _scan(code, family) if kind != "comment")
    lines: list[str] = []
    for line in kept.splitlines():
        if family == FAMILY_PYTHON:
            start = len(line) - len(line.lstrip())
            indent, body = line[:start], line[start:]
            body = _INLINE_WS.sub(" ", body.rstrip())
            if body:
                lines.append(indent + body)
        else:
            body = _INLINE_WS.sub(" ", line.strip())
            if body:
                lines.append(body)
    return "\n".join(lines)


def exact_digest(code: str, language: Any = "cpp") -> str:
    """完全重复的指纹：去注释去空行之后的 SHA-256。"""
    return hashlib.sha256(strip_comments(code, language).encode("utf-8")).hexdigest()


def without_comments(code: str, language: Any = "cpp") -> str:
    """只去掉注释（含整行注释留下的空行），**排版原样保留**。

    与 :func:`strip_comments` 的差别就是它保留缩进与行内空白 ——
    **并排 diff 要用这一份**：把缩进也去掉，两段代码摆在一起会读不懂，
    而"这一段在干什么"正是老师看 diff 时唯一想确认的事。
    """
    family = language_family(language)
    kept = "".join(text for kind, text in _scan(code, family) if kind != "comment")
    lines = (line.rstrip() for line in kept.splitlines())
    return "\n".join(line for line in lines if line.strip())


def normalized_tokens(code: str, language: Any = "cpp") -> list[str]:
    """词法归一化的 token 序列。

    标识符 → ``$ID``、数字 → ``$NUM``、字符串 → ``$STR``；关键字与内置名
    保留原文，运算符与标点原样留下。这样"把 ``a`` 改成 ``x``"躲不掉，
    而"把 ``for`` 换成 ``while``"仍然算改了。
    """
    family = language_family(language)
    keywords, builtins = _KEYWORDS[family], _BUILTINS[family]
    out: list[str] = []
    for kind, text in _scan(code, family):
        if kind in ("ws", "comment"):
            continue
        if kind == "id":
            out.append(text if text in keywords or text in builtins else "$ID")
        elif kind == "num":
            out.append("$NUM")
        elif kind == "str":
            out.append("$STR")
        else:
            out.append(text)
    return out


def _winnow(hashes: Sequence[int], window: int) -> set[int]:
    """winnowing：每个宽度 ``window`` 的窗口取最小哈希，合成指纹集合。

    只取每个窗口的最小值，是为了让指纹集合对"局部插入/删除"稳定 ——
    这是它与"取全部 k-gram"的关键差别：后者会因为改一处而整串错位。
    """
    if not hashes:
        return set()
    if len(hashes) < window:
        return set(hashes)
    return {min(hashes[i:i + window]) for i in range(len(hashes) - window + 1)}


def fingerprints(code: str, language: Any = "cpp") -> set[int]:
    """一份源码的指纹集合。"""
    tokens = normalized_tokens(code, language)
    family = language_family(language)
    k = K_GRAM[family]
    if not tokens:
        return set()
    if len(tokens) < k:
        # 太短，连一个 k-gram 都凑不出来（一行代码的学习题）。整个序列当
        # 一个指纹 —— 否则空集合会让"两份都很短但一模一样"算成 0%。
        return {zlib.crc32("\x1f".join(tokens).encode("utf-8"))}
    gram_hashes = [
        zlib.crc32("\x1f".join(tokens[i:i + k]).encode("utf-8"))
        for i in range(len(tokens) - k + 1)
    ]
    return _winnow(gram_hashes, WINDOW)


# ---------------------------------------------------------------------------
# 报告载体
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Person:
    """参与比对的一份提交（"这个人这道题的代表作"）。"""

    serial: int
    device_id: str
    username: str
    problem_id: str
    language: str = ""
    attempt: int = 1
    score: int = 0
    verdict: str = ""

    @property
    def display(self) -> str:
        """界面上怎么称呼这个人。**与项目里其他地方一致**：有名字用名字，
        没有就用设备号后四位（学生端不填名字时，那就是唯一能区分他的东西）。"""
        name = (self.username or "").strip()
        if name:
            return name
        tail = (self.device_id or "").strip()[-4:]
        return f"设备 {tail}" if tail else f"#{self.serial}"

    @property
    def key(self) -> str:
        """这个人在比对里的身份。

        用**设备号**而不是提交编号：同一个人在不同题目里的编号当然不同，
        按编号聚合的话"甲在两道题都与乙像"会变成两条记录，而报告要说的
        恰恰是"这两个人最像"。
        """
        return self.device_id or f"#{self.serial}"


@dataclass
class ExactGroup:
    """一组"除了注释与空行一字不差"的提交。"""

    problem_id: str
    problem_title: str
    digest: str
    members: list[Person] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [member.display for member in self.members]


@dataclass
class SimilarPair:
    """一对"值得人工看一眼"的提交。"""

    problem_id: str
    problem_title: str
    left: Person
    right: Person
    similarity: float
    shared: int
    #: 参与算 Jaccard 的指纹规模（``left`` 与 ``right`` 各自的指纹数）
    left_size: int = 0
    right_size: int = 0
    #: 已被判为完全重复？界面上要写清楚"这一对其实一模一样"，
    #: 而不是让它以 96% 的身份混在"高度相似"里 —— 那是两种性质的事。
    exact: bool = False

    @property
    def band(self) -> str:
        return severity_band(self.similarity)

    @property
    def percent(self) -> float:
        return round(self.similarity * 100, 1)


@dataclass
class ProblemMatrix:
    """一道题内的人 × 人相似度矩阵（给热力图用）。"""

    problem_id: str
    problem_title: str
    people: list[Person] = field(default_factory=list)
    #: ``values[i][j]`` = ``people[i]`` 与 ``people[j]`` 的相似度（对角线 1.0）
    values: list[list[float]] = field(default_factory=list)


@dataclass
class Report:
    """一次分析的全部产出。**纯数据**，界面直接照着画。"""

    exact_groups: list[ExactGroup] = field(default_factory=list)
    pairs: list[SimilarPair] = field(default_factory=list)
    analysed: list[Person] = field(default_factory=list)
    #: 提交编号 → 源码（参与比对的那些）。并排 diff 要用。
    #:
    #: 放在报告上而不是塞进 :class:`Person`：身份与内容是两件事，
    #: 而且同一个人在不同题目里的源码本来就不同 —— 挂在"人"上会被覆盖掉。
    codes: dict[int, str] = field(default_factory=dict)
    #: 想比对但没源码的（档案勾了"只存成绩不存代码"）
    missing_code: list[str] = field(default_factory=list)
    #: 每人每题被合并掉的其余提交数量
    merged: int = 0
    matrices: dict[str, ProblemMatrix] = field(default_factory=dict)
    #: 口径说明。界面必须显示 —— 老师要知道"这个百分比是怎么算出来的"。
    notes: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.exact_groups and not self.pairs


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _person(row: dict[str, Any]) -> Person:
    return Person(
        serial=int(row.get("serial") or 0),
        device_id=str(row.get("device_id") or ""),
        username=str(row.get("username") or ""),
        problem_id=str(row.get("problem_id") or ""),
        language=str(row.get("language") or ""),
        attempt=int(row.get("attempt") or 1),
        score=int(row.get("score") or 0),
        verdict=str(row.get("verdict") or ""),
    )


def pick_representatives(rows: Iterable[dict[str, Any]]) -> tuple[
        list[tuple[Person, str]], int, list[str]]:
    """每人每题挑一条代表作。

    规则：**先比分数，分数相同取编号大的**（= 更晚交的那次）。取"最高分"
    而不是"最后一次"，是因为最后交的可能是试错到一半的半成品 —— 抄来的
    那份通常就是他得分最高的那一版。

    返回 ``(代表作列表, 被合并掉的份数, 没有源码的提交说明)``。
    """
    best: dict[tuple[str, str], tuple[Person, str]] = {}
    merged = 0
    missing: list[str] = []
    for row in rows:
        person = _person(row)
        code = row.get("code") or ""
        if not code.strip():
            missing.append(f"{person.display} · {person.problem_id}")
            continue
        key = (person.problem_id, person.device_id or f"#{person.serial}")
        current = best.get(key)
        if current is None:
            best[key] = (person, code)
            continue
        merged += 1
        if (person.score, person.serial) > (current[0].score, current[0].serial):
            best[key] = (person, code)
    return list(best.values()), merged, missing


def _skeleton(prints: Iterable[set[int]]) -> set[int]:
    """一组指纹里"公开度"过高的那些 —— 模板骨架。

    **调用方要按题分别调用**：一道题全班都用快读、另一道题没人用，不该互相影响。
    """
    group = list(prints)
    count = len(group)
    if count < SKELETON_MIN_COUNT:
        return set()                     # 样本太小，见 SKELETON_MIN_COUNT 的说明
    floor = max(SKELETON_MIN_COUNT, math.ceil(SKELETON_RATIO * count))
    shared: Counter[int] = Counter()
    for one in group:
        shared.update(one)
    return {item for item, times in shared.items() if times >= floor}


def _matrix(people: list[Person],
            prints: dict[int, set[int]]) -> ProblemMatrix:
    """人 × 人相似度矩阵。对称，对角线 1.0。

    行序按提交编号 —— 与完全重复组同一个理由：交卷顺序是有意义的信息。
    """
    order = sorted(people, key=lambda p: p.serial)
    values: list[list[float]] = []
    for i, left in enumerate(order):
        row: list[float] = []
        for j, right in enumerate(order):
            if i == j:
                row.append(1.0)
            elif j < i:
                row.append(values[j][i])
            else:
                row.append(_jaccard(prints[left.serial], prints[right.serial]))
        values.append(row)
    return ProblemMatrix(problem_id=order[0].problem_id if order else "",
                         problem_title="", people=order, values=values)


def _jaccard(left: set[int], right: set[int]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def analyse(rows: Iterable[dict[str, Any]], *,
            problem_titles: dict[str, str] | None = None,
            min_similarity: float = DEFAULT_MIN_SIMILARITY,
            top_pairs: int = DEFAULT_TOP_PAIRS) -> Report:
    """跑一次雷同检测。

    ``rows`` 是普通 dict 的可迭代对象，认这几个键（都容错，缺的当空）：
    ``serial`` / ``device_id`` / ``username`` / ``problem_id`` / ``language`` /
    ``code`` / ``score`` / ``verdict`` / ``attempt``。

    界面把 :class:`offline_oj.net.session.Submission` 转成这样的 dict 再喂进来 ——
    本层不 import ``net/``。
    """
    titles = dict(problem_titles or {})
    report = Report()
    representatives, merged, missing = pick_representatives(rows)
    report.merged = merged
    report.missing_code = missing

    by_problem: dict[str, list[tuple[Person, str]]] = defaultdict(list)
    for person, code in representatives:
        by_problem[person.problem_id].append((person, code))

    for problem_id, group in by_problem.items():
        title = titles.get(problem_id, problem_id)
        # ---- 完全重复：先按归一化摘要分组 ----
        by_digest: dict[str, list[Person]] = defaultdict(list)
        for person, code in group:
            by_digest[exact_digest(code, person.language)].append(person)

        exact_serials: set[tuple[int, int]] = set()
        for digest in sorted(by_digest):
            # 按**提交编号**排，不按名字：谁先交的是有意义的顺序，
            # 而按中文名排序只会让报告看起来随机（"甲"的码点比"乙"大）。
            members = sorted(by_digest[digest], key=lambda p: p.serial)
            if len(members) > 1:
                for i, left in enumerate(members):
                    for right in members[i + 1:]:
                        exact_serials.add((left.serial, right.serial))
                report.exact_groups.append(
                    ExactGroup(problem_id=problem_id, problem_title=title,
                               digest=digest, members=members))

        # ---- 高度相似：指纹 + 倒排索引 ----
        raw: dict[int, set[int]] = {}
        for person, code in group:
            raw[person.serial] = fingerprints(code, person.language)
        # 骨架**按题**统计完就扣：扣完可能把某一对压到很低，那正是我们要的 ——
        # 他们只共享了模板。
        skeleton = _skeleton(raw.values())
        prints = {serial: (items - skeleton) for serial, items in raw.items()}

        # 倒排索引：指纹 → 有它的提交编号
        index: dict[int, list[int]] = defaultdict(list)
        for serial, items in prints.items():
            for item in items:
                index[item].append(serial)

        seen: set[tuple[int, int]] = set()
        for serials in index.values():
            if len(serials) < 2:
                continue
            for i, left in enumerate(sorted(serials)):
                for right in sorted(serials)[i + 1:]:
                    seen.add((left, right))

        people = {person.serial: person for person, _ in group}
        for left_serial, right_serial in sorted(seen):
            left, right = people[left_serial], people[right_serial]
            similarity = _jaccard(prints[left_serial], prints[right_serial])
            if similarity < min_similarity:
                continue
            report.pairs.append(SimilarPair(
                problem_id=problem_id, problem_title=title,
                left=left, right=right, similarity=similarity,
                shared=len(prints[left_serial] & prints[right_serial]),
                left_size=len(prints[left_serial]),
                right_size=len(prints[right_serial]),
                exact=(left_serial, right_serial) in exact_serials
                or (right_serial, left_serial) in exact_serials))

        matrix = _matrix([person for person, _ in group], prints)
        matrix.problem_title = title
        report.matrices[problem_id] = matrix
        for person, code in group:
            report.codes[person.serial] = code
            report.analysed.append(person)

    # 跨题聚合：同一对人只保留他们最像的那一道题 —— 否则 20 人的班在
    # 3 道题上都像的话，会翻成 3 倍的条目，而老师想看的只是"谁和谁最像"。
    # 身份用 ``Person.key``（设备号）而不是提交编号：同一个人在不同题目里
    # 编号本来就不同。
    best_pair: dict[tuple[str, str], SimilarPair] = {}
    for pair in report.pairs:
        key = tuple(sorted((pair.left.key, pair.right.key)))
        current = best_pair.get(key)
        if current is None or pair.similarity > current.similarity:
            best_pair[key] = pair
    report.pairs = sorted(best_pair.values(),
                          key=lambda p: (-p.similarity, p.left.display,
                                         p.right.display))[:max(0, top_pairs)]
    report.exact_groups.sort(key=lambda g: (-len(g.members), g.problem_title))

    under_sampled = [pid for pid, group in by_problem.items()
                     if len(group) < SKELETON_MIN_COUNT]
    report.notes = _notes(report, len(representatives), min_similarity,
                          under_sampled, titles)
    return report


def _notes(report: Report, picks: int, min_similarity: float,
           under_sampled: list[str], titles: dict[str, str]) -> list[str]:
    """报告口径。**每条都要能回答老师的一个疑问**，不是装饰。"""
    notes = [
        "只在同一道题内比较：两道题都写 for 循环不算相似。",
        f"每人每题只取代表一次（最高分，同分取更晚的那次）。"
        f"本次比对 {picks} 份，另合并 {report.merged} 份。",
        f"高度相似是启发式结果，只报了 {round(min_similarity * 100)}% 以上的对，"
        "**必须人工复核**：同一道水题的朴素解法本来就长得一样。",
    ]
    if under_sampled:
        # **这句必须说**：样本不足时公共模板没被扣掉，题目自带的骨架会
        # 把相似度整体抬高（两位互不相干的选手也可能到四五成）。
        # 不说的话，老师会拿这个数字去质问一个没抄的人。
        names = "、".join(titles.get(pid, pid) for pid in under_sampled[:3])
        notes.append(
            f"有 {len(under_sampled)} 道题的提交太少（不足 {SKELETON_MIN_COUNT} 人），"
            f"**没能自动扣除公共模板**（{names} 等），这些题的相似度整体偏高，"
            "请只当作线索。")
    else:
        notes.append("已扣除本题的公共模板（所有提交都有的片段）。")
    if report.exact_groups:
        notes.insert(0, "「完全重复」是确定性的：去掉注释与空行后一字不差。")
    if report.missing_code:
        notes.append(f"有 {len(report.missing_code)} 份提交没有源码，未参与比对"
                     "（档案可能勾了「只存成绩不存代码」）。")
    return notes


# ---------------------------------------------------------------------------
# 并排 diff
# ---------------------------------------------------------------------------

#: diff 里的三种行标记 → 界面上的名字。
DIFF_EQUAL = "equal"
DIFF_LEFT_ONLY = "left"      # 只有左边有（右边删掉了）
DIFF_RIGHT_ONLY = "right"    # 只有右边有（右边新增）
DIFF_CHANGED = "changed"     # 同一位置两边不同


@dataclass(frozen=True)
class DiffLine:
    """并排 diff 里的一行。行号从 1 开始，``None`` 表示这一侧没有这一行。"""

    tag: str
    left_no: int | None
    left_text: str
    right_no: int | None
    right_text: str


def diff_lines(left_code: str, right_code: str, *,
               language: Any = "cpp", context: int = 0) -> list[DiffLine]:
    """按行并排比对。``context`` > 0 时相同的行会折叠成 ``…``。

    比的是 :func:`without_comments` 的结果（**保留缩进**）—— 注释行不参与，
    因为"他抄的时候把注释也抄了"跟"代码像不像"是两件事，注释还会把
    真正的差异淹掉。行号是**去注释之后**的行号，界面上要照此说明。

    行内再细比字符是加分项但会显著拖慢大文件；这里做到行级 ——
    老师要判断的是"这两段逻辑像不像"，行级已经足够看清。
    """
    left = without_comments(left_code, language).splitlines()
    right = without_comments(right_code, language).splitlines()
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    rows: list[DiffLine] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == DIFF_EQUAL:
            block = [DiffLine(DIFF_EQUAL, i1 + k + 1, left[i1 + k],
                              j1 + k + 1, right[j1 + k])
                     for k in range(i2 - i1)]
            if context and len(block) > context * 2 + 1:
                rows.extend(block[:context])
                rows.append(DiffLine(DIFF_EQUAL, None, "…", None, "…"))
                rows.extend(block[-context:])
            else:
                rows.extend(block)
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            has_left = i1 + k < i2
            has_right = j1 + k < j2
            if has_left and has_right:
                one = DIFF_CHANGED
            elif has_left:
                one = DIFF_LEFT_ONLY
            else:
                one = DIFF_RIGHT_ONLY
            rows.append(DiffLine(
                one, i1 + k + 1 if has_left else None,
                left[i1 + k] if has_left else "",
                j1 + k + 1 if has_right else None,
                right[j1 + k] if has_right else ""))
    return rows


__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "DEFAULT_TOP_PAIRS",
    "DIFF_CHANGED",
    "DIFF_EQUAL",
    "DIFF_LEFT_ONLY",
    "DIFF_RIGHT_ONLY",
    "DiffLine",
    "ExactGroup",
    "K_GRAM",
    "Person",
    "ProblemMatrix",
    "Report",
    "SKELETON_MIN_COUNT",
    "SKELETON_RATIO",
    "SimilarPair",
    "analyse",
    "diff_lines",
    "exact_digest",
    "fingerprints",
    "language_family",
    "normalized_tokens",
    "pick_representatives",
    "severity_band",
    "strip_comments",
    "without_comments",
]
