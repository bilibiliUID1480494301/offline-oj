"""可复用控件。

这里放"多个面板都要用"的东西，避免把界面逻辑重复抄来抄去：

* :class:`CodeEditor` —— 带行号、Tab 转空格、按语言着色 + 代码提示的编辑器
* :class:`PathPicker` —— 路径输入 + 浏览按钮 + 状态徽标
* :class:`MarkdownView` —— 能正确显示本地图片的题目描述预览
* :class:`OutputView` —— 判题输出面板（按结论着色）
* :class:`TestCaseRows` —— 测试点的增删编辑区

关于"代码提示"：这不是 IDE 的语义补全 —— 没有编译前端就没有类型信息，
做不到 ``obj.`` 之后的成员列表。这里提供的是**前缀词补全**，数据来自两层：

1. 静态词表（关键字 / 类型 / 标准库 API / 代码片段），按语言预置；
2. 动态词表（当前文件里已经出现过的标识符）。

对竞赛/练习场景足够用，而且完全离线、零依赖、开销可忽略。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QStandardItem,
    QStandardItemModel,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QCompleter,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.models import DEFAULT_TESTCASE_POINTS, Language, TestCase
from .theme import Palette, mono_font, resolve_palette

# ======================================================================
# 代码高亮
# ======================================================================

KEYWORDS: dict[str, tuple[str, ...]] = {
    "c": (
        "auto", "break", "case", "char", "const", "continue", "default", "do", "double",
        "else", "enum", "extern", "float", "for", "goto", "if", "inline", "int", "long",
        "register", "restrict", "return", "short", "signed", "sizeof", "static", "struct",
        "switch", "typedef", "union", "unsigned", "void", "volatile", "while",
        "NULL", "true", "false", "bool",
    ),
    "cpp": (
        "alignas", "auto", "bool", "break", "case", "catch", "char", "class", "const",
        "constexpr", "continue", "decltype", "default", "delete", "do", "double", "else",
        "enum", "explicit", "extern", "false", "final", "float", "for", "friend", "goto",
        "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept",
        "nullptr", "operator", "override", "private", "protected", "public", "return",
        "short", "sizeof", "static", "struct", "switch", "template", "this", "throw",
        "true", "try", "typedef", "typename", "union", "unsigned", "using", "virtual",
        "void", "volatile", "while", "cin", "cout", "cerr", "endl", "string", "vector",
        "map", "set", "queue", "stack", "pair", "sort", "min", "max", "size_t",
    ),
    "python": (
        "and", "as", "assert", "async", "await", "break", "class", "continue", "def",
        "del", "elif", "else", "except", "False", "finally", "for", "from", "global",
        "if", "import", "in", "is", "lambda", "None", "nonlocal", "not", "or", "pass",
        "raise", "return", "True", "try", "while", "with", "yield", "print", "input",
        "range", "len", "int", "str", "float", "list", "dict", "set", "tuple", "map",
        "sum", "max", "min", "sorted", "enumerate", "zip", "abs", "open",
    ),
    "java": (
        "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
        "class", "const", "continue", "default", "do", "double", "else", "enum",
        "extends", "final", "finally", "float", "for", "goto", "if", "implements",
        "import", "instanceof", "int", "interface", "long", "native", "new", "package",
        "private", "protected", "public", "return", "short", "static", "strictfp",
        "super", "switch", "synchronized", "this", "throw", "throws", "transient",
        "try", "void", "volatile", "while", "true", "false", "null", "String",
        "System", "Scanner", "Math", "Integer", "Long", "Double", "out", "println",
    ),
}

COMMENT_PREFIX: dict[str, str] = {
    "c": "//", "cpp": "//", "java": "//", "python": "#",
}


# ======================================================================
# 代码提示数据
# ======================================================================


@dataclass(frozen=True)
class CompletionEntry:
    """一条补全候选。

    :param text: 参与前缀匹配的文本，也是默认插入的内容
    :param kind: 中文分类标签，显示在候选列表第二列
    :param insert: 实际插入的内容；留空表示插入 ``text``。
        片段会用它把短名字展开成多行模板。
    """

    text: str
    kind: str = ""
    insert: str = ""


#: 片段里光标最终停留的位置标记
CARET_MARKER = "$0"

#: 静态词表：语言 -> 分类 -> 词条。
#: 分类顺序即候选列表的显示顺序，所以"关键字"放最前面。
COMPLETION_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "c": {
        "关键字": (
            "auto", "break", "case", "char", "const", "continue", "default", "do",
            "double", "else", "enum", "extern", "float", "for", "goto", "if", "inline",
            "int", "long", "register", "restrict", "return", "short", "signed",
            "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned",
            "void", "volatile", "while",
        ),
        "类型": (
            "size_t", "ptrdiff_t", "int8_t", "int16_t", "int32_t", "int64_t",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t", "bool", "FILE",
        ),
        "常量": (
            "NULL", "true", "false", "EOF", "INT_MAX", "INT_MIN", "LLONG_MAX",
            "LLONG_MIN", "UINT_MAX", "SIZE_MAX", "DBL_MAX", "M_PI",
        ),
        "标准库": (
            "printf", "fprintf", "sprintf", "snprintf", "scanf", "fscanf", "sscanf",
            "puts", "putchar", "getchar", "fgets", "fputs", "fopen", "fclose",
            "malloc", "calloc", "realloc", "free", "memcpy", "memset", "memmove",
            "memcmp", "strlen", "strcmp", "strncmp", "strcpy", "strncpy", "strcat",
            "strstr", "strchr", "atoi", "atol", "atoll", "atof", "strtol", "strtoll",
            "strtod", "qsort", "bsearch", "abs", "labs", "llabs", "fabs", "sqrt",
            "pow", "log", "log2", "log10", "exp", "floor", "ceil", "round", "fmod",
            "hypot",
        ),
        "字符与时间": (
            "isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower",
            "toupper", "tolower", "rand", "srand", "clock", "CLOCKS_PER_SEC",
            "time", "assert",
        ),
        "头文件": (
            "#include <stdio.h>", "#include <stdlib.h>", "#include <string.h>",
            "#include <math.h>", "#include <stdbool.h>", "#include <limits.h>",
            "#include <ctype.h>", "#include <time.h>", "#include <assert.h>",
            "#include <stdint.h>", "#include <inttypes.h>", "#include <float.h>",
            "#include <stdarg.h>",
        ),
    },
    "cpp": {
        "关键字": (
            "alignas", "alignof", "auto", "bool", "break", "case", "catch", "char",
            "class", "const", "constexpr", "continue", "decltype", "default", "delete",
            "do", "double", "else", "enum", "explicit", "extern", "false", "final",
            "float", "for", "friend", "goto", "if", "inline", "int", "long", "mutable",
            "namespace", "new", "noexcept", "nullptr", "operator", "override",
            "private", "protected", "public", "return", "short", "sizeof", "static",
            "static_cast", "struct", "switch", "template", "this", "throw", "true",
            "try", "typedef", "typename", "union", "unsigned", "using", "virtual",
            "void", "volatile", "while",
        ),
        "类型": (
            "string", "vector", "map", "set", "unordered_map", "unordered_set",
            "multiset", "multimap", "queue", "deque", "stack", "priority_queue",
            "pair", "tuple", "array", "list", "bitset", "optional", "string_view",
            "size_t", "ptrdiff_t", "int64_t", "uint64_t", "long long",
        ),
        "容器方法": (
            "push_back", "pop_back", "emplace_back", "push", "pop", "front", "back",
            "top", "size", "empty", "clear", "begin", "end", "rbegin", "rend",
            "insert", "erase", "resize", "reserve", "at", "find", "count", "first",
            "second", "lower_bound", "upper_bound", "equal_range", "make_pair",
            "make_tuple", "get",
        ),
        "算法": (
            "sort", "stable_sort", "reverse", "unique", "binary_search", "min",
            "max", "min_element", "max_element", "swap", "fill", "accumulate",
            "next_permutation", "prev_permutation", "gcd", "lcm", "abs", "sqrt",
            "pow", "to_string", "stoi", "stoll", "stod", "substr", "getline",
            "ios", "sync_with_stdio", "tie", "cin", "cout", "cerr", "endl",
            "setprecision", "fixed",
            "nth_element", "partition", "count_if", "find_if", "all_of", "any_of",
            "none_of", "iota", "partial_sum", "clamp", "shuffle", "greater", "less",
        ),
        "C 库函数": (
            "printf", "scanf", "sprintf", "sscanf", "puts", "putchar", "getchar",
            "memset", "memcpy", "strlen", "strcmp", "strcpy", "strcat",
            "atoi", "atoll", "strtoll", "qsort", "fabs", "floor", "ceil", "round",
            "hypot", "log", "log2", "log10", "exp", "fmod",
        ),
        "竞赛常用": (
            "unsigned long long", "__int128", "INF", "MOD",
            "mt19937", "uniform_int_distribution", "INT_MAX", "INT_MIN",
            "LLONG_MAX", "LLONG_MIN", "UINT_MAX", "tuple_element",
        ),
        "头文件": (
            "#include <bits/stdc++.h>", "#include <iostream>", "#include <vector>",
            "#include <algorithm>", "#include <string>", "#include <map>",
            "#include <set>", "#include <queue>", "#include <cmath>",
            "#include <cstring>", "#include <cstdio>", "#include <cstdlib>",
            "#include <climits>", "#include <iomanip>", "#include <numeric>",
            "#include <functional>",
        ),
    },
    "python": {
        "关键字": (
            "and", "as", "assert", "async", "await", "break", "class", "continue",
            "def", "del", "elif", "else", "except", "False", "finally", "for",
            "from", "global", "if", "import", "in", "is", "lambda", "None",
            "nonlocal", "not", "or", "pass", "raise", "return", "True", "try",
            "while", "with", "yield",
        ),
        "内置函数": (
            "print", "input", "len", "range", "enumerate", "zip", "map", "filter",
            "sorted", "sum", "min", "max", "abs", "round", "int", "float", "str",
            "list", "dict", "set", "tuple", "bool", "ord", "chr", "bin", "hex",
            "oct", "divmod", "pow", "any", "all", "reversed", "isinstance", "type",
            "open", "format", "repr", "hash", "iter", "next", "bytes", "callable",
        ),
        "模块": (
            "sys", "math", "collections", "heapq", "bisect", "itertools",
            "functools", "string", "random", "re", "os", "json", "decimal",
            "fractions", "operator", "copy",
        ),
        "常用": (
            "deque", "defaultdict", "Counter", "OrderedDict", "heapify", "heappush",
            "heappop", "bisect_left", "bisect_right", "combinations", "permutations",
            "product", "readline", "read", "split", "splitlines", "strip", "lstrip",
            "rstrip", "join", "append", "extend", "pop", "remove", "sort", "reverse",
            "count", "index", "keys", "values", "items", "get", "setdefault",
            "update", "add", "discard", "stdin", "stdout", "sys.maxsize",
            "float('inf')", "float('-inf')",
            "lru_cache", "cache", "reduce", "inf", "nan", "setrecursionlimit",
            "stdin.buffer",
        ),
    },
    "java": {
        "关键字": (
            "abstract", "assert", "boolean", "break", "byte", "case", "catch",
            "char", "class", "continue", "default", "do", "double", "else", "enum",
            "extends", "final", "finally", "float", "for", "if", "implements",
            "import", "instanceof", "int", "interface", "long", "native", "new",
            "package", "private", "protected", "public", "return", "short", "static",
            "super", "switch", "synchronized", "this", "throw", "throws", "try",
            "void", "volatile", "while", "true", "false", "null",
        ),
        "类型": (
            "String", "StringBuilder", "StringBuffer", "Integer", "Long", "Double",
            "Character", "Boolean", "Object", "Math", "Arrays", "Collections",
            "List", "ArrayList", "LinkedList", "Map", "HashMap", "TreeMap",
            "LinkedHashMap", "Set", "HashSet", "TreeSet", "Queue", "Deque",
            "ArrayDeque", "PriorityQueue", "Scanner", "BufferedReader",
            "InputStreamReader", "StringTokenizer", "BigInteger", "BigDecimal",
            "Comparator", "Iterator", "Optional",
        ),
        "常用方法": (
            "System.out.println", "System.out.print", "System.out.printf",
            "System.in", "Integer.parseInt", "Long.parseLong", "Double.parseDouble",
            "Integer.MAX_VALUE", "Integer.MIN_VALUE", "Long.MAX_VALUE",
            "Math.abs", "Math.max", "Math.min", "Math.sqrt", "Math.pow",
            "Math.floor", "Math.ceil", "Math.round", "Arrays.sort", "Arrays.fill",
            "Arrays.toString", "Collections.sort", "Collections.reverse",
            "String.valueOf", "charAt", "length", "substring", "indexOf",
            "lastIndexOf", "equals", "compareTo", "toCharArray", "split", "trim",
            "toLowerCase", "toUpperCase", "replace", "contains", "startsWith",
            "endsWith", "isEmpty", "add", "get", "set", "remove", "size", "clear",
            "put", "containsKey", "containsValue", "keySet", "values", "entrySet",
            "poll", "offer", "peek", "push", "pop", "append", "toString", "nextInt",
            "nextLong", "nextDouble", "nextLine", "next", "hasNext", "hasNextInt",
        ),
        "导入": (
            "import java.util.*;", "import java.io.*;", "import java.math.*;",
        ),
        "竞赛常用": (
            "Arrays.asList", "Arrays.binarySearch", "Arrays.copyOf",
            "Collections.max", "Collections.min", "Collections.frequency",
            "String.format", "String.join", "Integer.bitCount", "Long.bitCount",
            "Integer.toBinaryString", "Integer.toHexString", "Math.floorDiv",
            "Math.floorMod", "Objects.equals", "System.arraycopy",
            "HashMap.getOrDefault", "TreeSet.ceiling", "TreeSet.floor",
            "Deque.offerFirst", "Math.toIntExact",
        ),
    },
}

#: 代码片段：语言 -> ((触发名, 展开内容), ...)。
#: 名字刻意与关键字错开，避免"想打 for 却被展开成长模板"。
SNIPPETS: dict[str, tuple[tuple[str, str], ...]] = {
    "c": (
        ("main", "int main() {\n    $0\n    return 0;\n}"),
        ("fori", "for (int i = 0; i < n; i++) {\n    $0\n}"),
        ("forj", "for (int j = 0; j < m; j++) {\n    $0\n}"),
        ("forr", "for (int i = n - 1; i >= 0; i--) {\n    $0\n}"),
        ("scanint", 'scanf("%d", &n);'),
        ("printint", 'printf("%d\\n", $0);'),
        ("func", "int solve(int n) {\n    $0\n}"),
        ("structdef", "typedef struct {\n    int value;\n    $0\n} Node;"),
    ),
    "cpp": (
        ("main",
         "int main() {\n"
         "    ios::sync_with_stdio(false);\n"
         "    cin.tie(nullptr);\n"
         "    $0\n"
         "    return 0;\n"
         "}"),
        ("fori", "for (int i = 0; i < n; i++) {\n    $0\n}"),
        ("forj", "for (int j = 0; j < m; j++) {\n    $0\n}"),
        ("forr", "for (int i = n - 1; i >= 0; i--) {\n    $0\n}"),
        ("foreach", "for (auto &item : items) {\n    $0\n}"),
        ("func", "int solve() {\n    $0\n}"),
        ("cls", "struct Node {\n    int value;\n    $0\n};"),
        ("readn", "int n;\ncin >> n;"),
        ("readarray", "vector<int> a(n);\nfor (auto &x : a) cin >> x;"),
        ("usingns", "using namespace std;\n"),
        ("ll", "long long"),
    ),
    "python": (
        ("main",
         "def main():\n"
         "    $0\n"
         "\n"
         "\n"
         'if __name__ == "__main__":\n'
         "    main()"),
        ("fastread",
         "import sys\n"
         "\n"
         "data = sys.stdin.read().split()\n"
         "$0"),
        ("func", "def solve():\n    $0"),
        ("forn", "for i in range(n):\n    $0"),
        ("readsplit", "data = sys.stdin.readline().split()"),
        ("readints",
         "import sys\n"
         "\n"
         "n = int(sys.stdin.readline())\n"
         "a = list(map(int, sys.stdin.readline().split()))"),
    ),
    "java": (
        ("main",
         "public class Main {\n"
         "    public static void main(String[] args) {\n"
         "        $0\n"
         "    }\n"
         "}"),
        ("psvm",
         "public static void main(String[] args) {\n    $0\n}"),
        ("scanner", "Scanner sc = new Scanner(System.in);"),
        ("fori", "for (int i = 0; i < n; i++) {\n    $0\n}"),
        ("pr", "System.out.println($0);"),
        ("buffered",
         "BufferedReader br = new BufferedReader(new InputStreamReader(System.in));\n"
         "StringTokenizer st = new StringTokenizer(br.readLine());"),
    ),
}

#: 候选列表最短触发前缀长度（手动唤出不受此限）。
#:
#: 是 **1** —— 也就是"敲下第一个字母就自动弹"，这是学生的默认预期：
#: 要用的时候它得已经在，而不是先想起来有个快捷键再按。候选框本身不抢键，
#: 敲满一个词回车就是换行（见 :class:`SuggestionPopup`），所以早弹不会打断输入。
#: 觉得吵的话，真正该调的是 :func:`code_context`（注释和字符串里不弹），
#: 而不是把这个数字调回去 —— 调回 2 只会让"想用的时候它不在"。
MIN_PREFIX = 1

#: 动态词表上限：够用即可，避免超大文件把模型撑爆
MAX_DYNAMIC_WORDS = 600
#: 超过这个体积就不扫全文了
MAX_SCAN_CHARS = 400_000

#: 标识符（动态词表用）
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")

#: 光标前的前缀匹配规则。预处理指令要连 ``#`` 一起吃掉，
#: 否则 ``#inc`` 会退化成 ``inc``，替换后变成 ``##include``。
_PREPROC_PREFIX = re.compile(r"#[A-Za-z_][A-Za-z0-9_]*$")
_WORD_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def prefix_at(line: str) -> str:
    """取一行里光标处的待补全前缀。``line`` 应已截断到光标位置。"""
    if line.lstrip().startswith("#"):
        match = _PREPROC_PREFIX.search(line)
        if match:
            return match.group(0)
    match = _WORD_PREFIX.search(line)
    return match.group(0) if match else ""


#: 各语言的注释写法：``语言 -> (行注释前缀, 块注释起止)``。
#: ``None`` 表示这门语言没有块注释。
#:
#: **``#`` 只对 Python 是注释。** C / C++ 的 ``#`` 是预处理指令，把它也当注释，
#: ``#inc`` → ``#include <iostream>`` 这条提示就再也弹不出来了。
_COMMENT_SPEC: dict[str, tuple[str | None, tuple[str, str] | None]] = {
    "c": ("//", ("/*", "*/")),
    "cpp": ("//", ("/*", "*/")),
    "java": ("//", ("/*", "*/")),
    "python": ("#", None),
}

#: 往回翻多少行去找没闭合的 ``/*``。开了几十行还没关的块注释本来就少见，
#: 真那样的话用户看到的也是一整片注释色，不需要候选框。
BLOCK_LOOKBACK_LINES = 40


def code_context(text: str, language: str, *, block_open: bool = False) -> tuple[bool, bool]:
    """扫一遍 ``text``，判断它**末尾**是否落在注释或字符串里。

    返回 ``(末尾是否在注释/字符串中, 扫完之后块注释是否仍然开着)``。
    第二个值是为了跨行：调用方把上一行扫出来的结果回填给下一行的 ``block_open``，
    就能知道当前行是不是还在一个多行块注释里面。

    刻意只做"够用"的词法判断，不建 AST、不认原始字符串前缀（``R"(...)"``）。
    Python 的三引号也按普通引号处理：连着三个双引号会被读成"开一次、关一次、
    再开一次"，于是它之后的内容一律算作在字符串里 —— 方向是对的（多行字符串里
    确实不该弹候选框），只是顺便把整段 docstring 都算了进去。
    """
    spec = _COMMENT_SPEC.get(language)
    if spec is None:
        return False, block_open

    line_comment, block = spec
    index = 0
    end = len(text)
    quote = ""
    while index < end:
        if block_open:
            close = text.find(block[1], index)
            if close < 0:
                return True, True
            index = close + len(block[1])
            block_open = False
            continue
        if quote:
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if line_comment is not None and text.startswith(line_comment, index):
            return True, False
        if block is not None and text.startswith(block[0], index):
            block_open = True
            index += len(block[0])
            continue
        char = text[index]
        if char in "\"'":
            quote = char
        index += 1
    return bool(quote), block_open


def block_comment_is_open(document, block_number: int, language: str) -> bool:
    """当前这一行是否还处在一个跨行的块注释里。

    只往前翻有限行：把整篇文档正则扫一遍放到每次按键的路径上太贵，而"翻过
    40 行还没闭合的 ``/*``"在实际代码里基本不存在。
    """
    spec = _COMMENT_SPEC.get(language)
    if spec is None or spec[1] is None or block_number <= 0:
        return False
    start = max(0, block_number - BLOCK_LOOKBACK_LINES)
    lines: list[str] = []
    for number in range(start, block_number):
        block = document.findBlockByNumber(number)
        if not block.isValid():
            break
        lines.append(block.text())
    text = "\n".join(lines)
    return text.count(spec[1][0]) > text.count(spec[1][1])


def _static_entries(language: Language) -> list[CompletionEntry]:
    """按语言取静态候选，关键字优先、片段垫后。"""
    result: list[CompletionEntry] = []
    for kind, words in COMPLETION_WORDS.get(language.value, {}).items():
        result.extend(CompletionEntry(word, kind) for word in words)
    result.extend(
        CompletionEntry(name, "片段", payload)
        for name, payload in SNIPPETS.get(language.value, ())
    )
    # 同名去重：先出现的赢，所以关键字不会被片段顶掉
    seen: set[str] = set()
    unique: list[CompletionEntry] = []
    for entry in result:
        if entry.text not in seen:
            seen.add(entry.text)
            unique.append(entry)
    return unique


def _document_words(text: str, known: set[str]) -> list[str]:
    """从正文里收集标识符，作为动态候选。"""
    if len(text) > MAX_SCAN_CHARS:
        return []
    words: set[str] = set()
    for match in _IDENTIFIER.finditer(text):
        word = match.group(0)
        if word not in known:
            words.add(word)
            if len(words) >= MAX_DYNAMIC_WORDS:
                break
    return sorted(words, key=str.lower)


#: 候选里的"类型标签"存在这个自定义角色里。
#: 不能用"第二列"来放 —— QCompleter 的弹出列表是 QListView，一旦设了
#: ``modelColumn``，Qt 就会把其余列全部隐藏，所以类型只能自绘。
KIND_ROLE = Qt.ItemDataRole.UserRole + 1


class CompletionDelegate(QStyledItemDelegate):
    """候选列表自绘：左边名字、右边淡色类型标签。

    背景与文字都自己画，不交给 :class:`QStyle`。原因是 Windows 原生样式的
    选中态是"浅灰底 + 深字"，而这里要的是 Fluent 那种"强调色底 + 白字"，
    用样式表去改又会被 :class:`QCompleter` 内部重置，直接自绘最省事也最稳。
    """

    PAD = 10

    def __init__(self, palette: Palette, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette

    def color_for_kind(self, kind: str) -> QColor:
        """片段用强调色，其余（关键字 / 标准库 / 文中词）用弱化色。"""
        return QColor(self._palette.accent if kind == "片段" else self._palette.text_muted)

    def _kind_width(self, option, kind: str) -> int:
        if not kind:
            return 0
        return QFontMetrics(option.font).horizontalAdvance(kind) + 16

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt 命名
        # 已失效的 painter 不能碰：绘制事件有可能在控件销毁的间隙到达，
        # 那时 C++ 侧的 QPainter 已经析构，任何绘制调用都是访问违例。
        if not painter.isActive():
            return
        palette = self._palette
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        try:
            painter.setClipRect(option.rect)
            if selected:
                painter.fillRect(option.rect, QColor(palette.accent))
            elif hovered:
                painter.fillRect(option.rect, QColor(palette.selection))
            else:
                painter.fillRect(option.rect, QColor(palette.surface))

            text = str(index.data(Qt.DisplayRole) or "")
            kind = str(index.data(KIND_ROLE) or "")
            metrics = QFontMetrics(option.font)
            rect = option.rect.adjusted(self.PAD, 0, -self.PAD, 0)

            kind_width = self._kind_width(option, kind)
            if kind_width:
                color = QColor(palette.accent_text) if selected else self.color_for_kind(kind)
                if selected:
                    color.setAlpha(190)
                painter.setPen(color)
                painter.drawText(rect.adjusted(rect.width() - kind_width, 0, 0, 0),
                                 Qt.AlignRight | Qt.AlignVCenter, kind)

            painter.setPen(QColor(palette.accent_text) if selected
                           else QColor(palette.text))
            name_rect = rect.adjusted(0, 0, -kind_width, 0)
            painter.drawText(
                name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(text, Qt.ElideRight, name_rect.width()),
            )
        finally:
            # 必须成对 restore —— 否则 Qt 会在 paint 结束时报警并可能画花
            painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt 命名
        base = super().sizeHint(option, index)
        kind = str(index.data(KIND_ROLE) or "")
        base.setWidth(base.width() + self.PAD * 2 + self._kind_width(option, kind))
        base.setHeight(max(base.height() + 4, 22))
        return base


class SuggestionPopup(QListView):
    """候选列表：**用户没主动选之前，不许有任何"当前项"**。

    这一个小类修掉的是一类很难自己发现的行为 bug。``QCompleter`` 在弹出时
    （以及每次刷新模型时）会把第 0 项设成 ``currentIndex``，而编辑器的键盘处理
    里有一条"候选框开着时按回车就采纳当前项"。两者一凑就出事：

    ==================  ==========================================
    敲进去的            按下回车之后
    ==================  ==========================================
    ``for``             展开成整个 ``for (int i = 0; i < n; i++) {`` 模板
    ``int ma``          ``int map``（还把回车吃了）
    ``// hi``           ``// hix = 1;``（注释里回车也没了）
    ``pri``（Python）   ``printx = 1;``
    ==================  ==========================================

    也就是说：**只要敲满 2 个字符能匹配上候选，回车就不再是换行** ——
    而这个编辑器里写代码哪能不换行。它会让人以为"这编辑器有毛病"，
    却很难说出毛病在哪。

    修法不是"别让 Qt 预选"（挡不住，每次更新模型它都会重设），
    而是让候选列表**拒绝**程序化的选中，只认用户自己的操作：

    * 按方向键（↑↓ / PgUp / PgDn）或点鼠标之后，选中才生效；
    * 在那之前，列表就只是一份"可以看看"的建议，回车/Tab 照常走编辑器的逻辑
      （换行 / 缩进），不会被它截走。

    用户一旦真的选了一项，意图就明确了，这时回车和 Tab 都应该采纳它。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._user_engaged = False

    def reset_engagement(self) -> None:
        """把"用户选过了"这条记录清掉，并取消选中。

        每次重新挑候选（也就是每敲一个字符）都要清一次：上一轮按过 ↓ 不代表
        这一轮还想采纳，光标都已经移到别的词上面了。
        """
        self._user_engaged = False
        super().setCurrentIndex(QModelIndex())

    def has_choice(self) -> bool:
        """用户是不是**自己**在候选列表里选了一项。

        以这个标志为准，而不是 ``currentIndex().isValid()``：Qt 还有些路径
        （选中模型、键盘搜索等）能绕过 :meth:`setCurrentIndex` 把当前项设上，
        那些都不是用户的意图。这里只认"按了方向键"或"点了鼠标"。
        """
        return self._user_engaged

    def setCurrentIndex(self, index: QModelIndex) -> None:  # noqa: N802 - Qt 命名
        # 清空选中任何时候都放行；**选中**只有用户自己动过手才算数。
        if index.isValid() and not self._user_engaged:
            return
        super().setCurrentIndex(index)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
                           Qt.Key_Home, Qt.Key_End):
            self._user_engaged = True
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._user_engaged = True
        super().mousePressEvent(event)


class CodeCompleter(QCompleter):
    """前缀式代码补全。

    挂在 :class:`CodeEditor` 上，由编辑器负责触发时机，这里只管数据与弹出。
    """

    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor
        self._language = Language.CPP
        self._palette = resolve_palette("light")
        self._entries: dict[str, CompletionEntry] = {}
        self._dynamic_words: list[str] = []
        self._static_rows = 0
        self._dirty = True

        self._model = QStandardItemModel(0, 1, self)
        self.setModel(self._model)
        self.setCompletionColumn(0)
        self.setCompletionRole(Qt.DisplayRole)
        self.setCompletionMode(QCompleter.PopupCompletion)
        self.setCaseSensitivity(Qt.CaseInsensitive)
        self.setWrapAround(False)
        self.setMaxVisibleItems(14)
        self.setWidget(editor)

        # 换成我们自己的候选列表：默认那个会配合 Qt 的自动预选把回车吃掉。
        # ``setPopup`` 会接管所有权并销毁旧的那个，所以这一步是安全的替换。
        self.setPopup(SuggestionPopup())

        popup = self.popup()
        # QCompleter 默认建的弹出列表是**没有父对象的顶层窗口**，它会比编辑器活得久：
        # 编辑器（连同它持有的 completer）销毁后，popup 仍然挂在桌面上，
        # 于是延迟到达的绘制事件会打到一个已经失效的委托上 —— 实测表现为
        # `Windows fatal exception: access violation`，位置就在 CompletionDelegate.paint。
        # 显式认编辑器当父对象（Qt::Popup 的定位由 QCompleter 自己按全局坐标完成，
        # 与 QComboBox 的下拉列表是同一套机制），三者就一起生一起死。
        # 注意这行必须在 setPopup 之后：setPopup 会把父对象重置掉。
        popup.setParent(editor, Qt.Popup)
        popup.setFont(mono_font(10))
        popup.setUniformItemSizes(True)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._delegate = CompletionDelegate(self._palette, popup)
        popup.setItemDelegate(self._delegate)
        self.apply_palette(self._palette)

    # ---- 数据 -------------------------------------------------------------

    def mark_dirty(self) -> None:
        """正文变了，动态词表下次弹出时重算。"""
        self._dirty = True

    def set_language(self, language: Language, palette: Palette | None = None) -> None:
        if palette is not None:
            self._palette = palette
        self._language = language
        self._entries.clear()
        self._dynamic_words = []
        self._dirty = True
        self._model.removeRows(0, self._model.rowCount())
        for entry in _static_entries(language):
            self._entries[entry.text] = entry
            self._append_row(entry.text, entry.kind)
        self._static_rows = self._model.rowCount()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._delegate.apply_palette(palette)
        self.popup().setStyleSheet(
            f"QListView {{ background-color: {palette.surface}; color: {palette.text};"
            f" border: 1px solid {palette.border}; outline: none; padding: 2px; }}"
        )

    def _refresh_dynamic(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        words = _document_words(self._editor.toPlainText(), set(self._entries))
        if words == self._dynamic_words:
            return
        self._dynamic_words = words
        if self._model.rowCount() > self._static_rows:
            self._model.removeRows(
                self._static_rows, self._model.rowCount() - self._static_rows
            )
        for word in words:
            self._append_row(word, "文中")

    def _append_row(self, name: str, kind: str) -> None:
        item = QStandardItem(name)
        item.setEditable(False)
        item.setData(kind, KIND_ROLE)
        self._model.appendRow(item)

    # ---- 弹出与采纳 -------------------------------------------------------

    def try_complete(self, *, force: bool = False) -> None:
        """按当前光标前缀决定是否弹出候选。

        :param force: 手动唤出（``Ctrl+Space`` / ``Alt+/``）。手动是一次明确的
            请求，所以不受前缀长度与"注释里不弹"的限制 —— 用户按了就是想要。
        """
        cursor = self._editor.textCursor()
        prefix = prefix_at(cursor.block().text()[: cursor.positionInBlock()])
        popup = self.popup()

        if not force:
            if len(prefix) < MIN_PREFIX:
                popup.hide()
                return
            if self._suppressed():
                popup.hide()
                return

        self._refresh_dynamic()
        self.setCompletionPrefix(prefix)
        model = self.completionModel()
        if model.rowCount() == 0:
            popup.hide()
            return

        rect = self._editor.cursorRect()
        rect.setWidth(max(280, popup.sizeHintForColumn(0) + 24))
        # 每一次重挑候选都清掉上一轮的选中：上一轮按过 ↓ 不代表这一轮还想采纳。
        # **然后不预选任何一项** —— 列表在这里只是"给你看看"，回车该换行就换行。
        if isinstance(popup, SuggestionPopup):
            popup.reset_engagement()
        self.complete(rect)
        if isinstance(popup, SuggestionPopup):
            # complete() 内部会把第 0 项设成当前项，这里再清一次。
            popup.reset_engagement()

    def _suppressed(self) -> bool:
        """光标处落在注释或字符串里 —— 这时不该弹候选框。

        阈值降到 1 之后，每敲一个字母都会走到这里。注释里写的是自然语言，
        弹出来的候选框没有任何用，只会一直挡着正在写的字。
        """
        language = self._language.value
        if language not in _COMMENT_SPEC:
            return False
        cursor = self._editor.textCursor()
        block = cursor.block()
        before = block.text()[: cursor.positionInBlock()]
        if code_context(before, language)[0]:
            return True
        return block_comment_is_open(
            self._editor.document(), block.blockNumber(), language)

    def has_choice(self) -> bool:
        """用户是否**自己**在候选列表里选了一项。

        编辑器用它来决定回车/Tab 该换行还是该采纳（见
        :class:`SuggestionPopup` 的说明）。没有换成自定义列表时退回 Qt 的
        语义：只要有个有效的当前项就算选过。
        """
        popup = self.popup()
        if isinstance(popup, SuggestionPopup):
            return popup.has_choice()
        return popup.currentIndex().isValid()

    def accept_current(self) -> bool:
        """采纳高亮项；没有高亮项（或用户还没选过）时返回 False。"""
        popup = self.popup()
        if isinstance(popup, SuggestionPopup) and not popup.has_choice():
            return False
        index = popup.currentIndex()
        if not index.isValid():
            return False
        name = index.data(Qt.DisplayRole)
        if not name:
            return False
        self.insert(str(name))
        return True

    def insert(self, name: str) -> None:
        """把候选写入编辑器，替换掉已输入的前缀。"""
        editor = self._editor
        cursor = editor.textCursor()
        prefix = prefix_at(cursor.block().text()[: cursor.positionInBlock()])
        if prefix:
            cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(prefix))

        entry = self._entries.get(name)
        payload = entry.insert if entry and entry.insert else name
        if CARET_MARKER in payload:
            before, _, after = payload.partition(CARET_MARKER)
            cursor.insertText(before + after)
            end = cursor.position()
            cursor.setPosition(end - len(after))
        else:
            cursor.insertText(payload)
        editor.setTextCursor(cursor)
        self.popup().hide()


class CodeHighlighter(QSyntaxHighlighter):
    """轻量语法着色。

    不做完整语法分析（那需要真正的解析器），只按词法规则着色：注释、字符串、
    数字、关键字、预处理指令。对"读代码"这件事已经够用，而且开销可以忽略。
    """

    def __init__(self, document, palette: Palette, language: Language) -> None:
        super().__init__(document)
        self._palette = palette
        self._language = language
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._build()

    def set_language(self, language: Language) -> None:
        self._language = language
        self._build()
        self.rehighlight()

    def set_palette(self, palette: Palette) -> None:
        """就地换配色，重建规则后重排。

        不新建对象是有原因的：``QSyntaxHighlighter`` 会把 document 认成自己的
        父对象，Qt 的父子关系托住 C++ 对象，所以 ``self._highlighter = 新实例``
        这种"换掉"的写法**旧实例并不会消失** —— 实测换 5 次主题，文档上就挂了
        6 个高亮器，此后每敲一个键都要重排 6 遍，越用越卡。
        """
        self._palette = palette
        self._build()
        self.rehighlight()

    # ---- 内部 -------------------------------------------------------------

    def _build(self) -> None:
        p = self._palette
        self._rules.clear()

        def fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            result = QTextCharFormat()
            result.setForeground(QColor(color))
            if bold:
                result.setFontWeight(QFont.Bold)
            result.setFontItalic(italic)
            return result

        keyword_fmt = fmt("#0000c0" if not p.is_dark else "#7fb4ff", bold=True)
        number_fmt = fmt("#116644" if not p.is_dark else "#8ed4a0")
        string_fmt = fmt("#a31515" if not p.is_dark else "#e09a7c")
        comment_fmt = fmt(p.text_muted, italic=True)
        preproc_fmt = fmt("#808000" if not p.is_dark else "#d5c26a")

        keywords = KEYWORDS.get(self._language.value, ())
        if keywords:
            pattern = r"\b(" + "|".join(re.escape(word) for word in keywords) + r")\b"
            self._rules.append((QRegularExpression(pattern), keyword_fmt))

        self._rules.append((QRegularExpression(r"\b\d+(?:\.\d+)?[fFlLuU]*\b"), number_fmt))
        # 双引号字符串（含转义），不做跨行处理
        self._rules.append((QRegularExpression(r'"(\\.|[^"\\])*"'), string_fmt))
        self._rules.append((QRegularExpression(r"'(\\.|[^'\\])*'"), string_fmt))
        if self._language is not Language.PYTHON:
            self._rules.append((QRegularExpression(r"^\s*#\s*\w+"), preproc_fmt))

        prefix = re.escape(COMMENT_PREFIX.get(self._language.value, "//"))
        self._rules.append((QRegularExpression(f"{prefix}[^\n]*"), comment_fmt))
        if self._language is not Language.PYTHON:
            self._comment_fmt = comment_fmt
        else:
            self._comment_fmt = comment_fmt

    def highlightBlock(self, text: str) -> None:
        for expression, text_format in self._rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), text_format)

        # C 系语言的块注释 /* ... */ 需要跨行状态
        if self._language in (Language.C, Language.CPP, Language.JAVA):
            self._highlight_block_comments(text)

    def _highlight_block_comments(self, text: str) -> None:
        start_expression = QRegularExpression(r"/\*")
        end_expression = QRegularExpression(r"\*/")

        self.setCurrentBlockState(0)
        start = 0
        if self.previousBlockState() != 1:
            match = start_expression.match(text)
            start = match.capturedStart() if match.hasMatch() else -1

        while start >= 0:
            end_match = end_expression.match(text, start)
            if end_match.hasMatch():
                length = end_match.capturedEnd() - start
                self.setFormat(start, length, self._comment_fmt)
                next_match = start_expression.match(text, end_match.capturedEnd())
                start = next_match.capturedStart() if next_match.hasMatch() else -1
            else:
                self.setCurrentBlockState(1)
                self.setFormat(start, len(text) - start, self._comment_fmt)
                break


# ======================================================================
# 代码编辑器
# ======================================================================


class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """带行号的代码编辑区。

    :param line_numbers: 是否显示行号。写代码需要，"写 Markdown 描述"不需要 ——
        描述里的行号只是干扰。
    :param completion: 是否启用代码提示。纯文本编辑（如题目描述）不需要。
    """

    def __init__(self, palette: Palette | None = None, parent=None, *,
                 line_numbers: bool = True, completion: bool = True) -> None:
        super().__init__(parent)
        self._palette = palette or resolve_palette("light")
        self._language = Language.CPP
        self._show_line_numbers = line_numbers

        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabChangesFocus(False)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)

        self._line_numbers = _LineNumberArea(self)
        self._line_numbers.setVisible(line_numbers)
        self._highlighter = CodeHighlighter(self.document(), self._palette, self._language)

        self._completer: CodeCompleter | None = None
        if completion:
            self._completer = CodeCompleter(self)
            self._completer.set_language(self._language, self._palette)
            # 词表只跟正文有关，所以接"正文变了"而不是"看起来变了"，否则每次
            # 重排格式（换主题、切语言）都会白算一遍动态词
            self.connect_content_changed(self._completer.mark_dirty)
            self.setToolTip("Tab 缩进 · Shift+Tab 反缩进 · 敲下第一个字母就自动提示"
                            " · Ctrl+Space / Alt+/ 手动唤出")

        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_width()
        self._highlight_current_line()

    # ---- 对外 -------------------------------------------------------------

    def connect_content_changed(self, slot) -> None:
        """接"用户真的改了正文"，**不要**用 ``textChanged``。

        ``QPlainTextEdit.textChanged`` 背后是 ``QTextDocument.contentsChanged``，
        而语法高亮也会让它响：``QSyntaxHighlighter`` 调 ``setFormat()`` 时 Qt 只
        重排了格式、一个字都没增删，信号却照发。于是"换一次主题"在监听者眼里
        与"改了一次正文"完全等价 ——

        * 存题面板被标成"有未保存的修改"，关窗口时弹出保存确认；
        * 冒烟测试在离屏环境下没人点得到那个模态框，挂死 4 小时 33 分，
          最后靠 ``faulthandler`` 打出栈才认出是 ``_confirm_discard``。

        ``contentsChange(pos, removed, added)`` 只在真正增删字符时发出，
        格式重排不经过它，正好是要的判据。
        """

        def forward(_position: int, removed: int, added: int) -> None:
            if removed == 0 and added == 0:
                return
            slot()

        self.document().contentsChange.connect(forward)

    def set_language(self, language: Language) -> None:
        self._language = language
        self._highlighter.set_language(language)
        if self._completer is not None:
            self._completer.set_language(language, self._palette)

    def set_palette_theme(self, palette: Palette) -> None:
        self._palette = palette
        self._highlighter.set_palette(palette)
        if self._completer is not None:
            self._completer.set_language(self._language, palette)
            self._completer.apply_palette(palette)
        self._highlight_current_line()

    def position_info(self) -> str:
        cursor = self.textCursor()
        return f"第 {cursor.blockNumber() + 1} 行，第 {cursor.columnNumber() + 1} 列"

    # ---- 行号区 -----------------------------------------------------------

    def line_number_area_width(self) -> int:
        if not self._show_line_numbers:
            return 0
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self, _count: int = 0) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_area(self, rect, dy: int) -> None:
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._line_numbers.setGeometry(contents.left(), contents.top(),
                                       self.line_number_area_width(), contents.height())

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._line_numbers)
        painter.fillRect(event.rect(), QColor(self._palette.surface_alt))

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        current_line = self.textCursor().blockNumber()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                color = QColor(self._palette.accent if number == current_line
                               else self._palette.text_muted)
                painter.setPen(color)
                painter.drawText(0, int(top), self._line_numbers.width() - 6,
                                 self.fontMetrics().height(),
                                 Qt.AlignRight | Qt.AlignVCenter, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def _highlight_current_line(self) -> None:
        selections = []
        if not self.isReadOnly():
            # ExtraSelection 定义在 QTextEdit 上，QPlainTextEdit 自身没有这个嵌套类
            selection = QTextEdit.ExtraSelection()
            color = QColor(self._palette.selection)
            color.setAlpha(90)
            selection.format.setBackground(color)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            selections.append(selection)
        self.setExtraSelections(selections)

    # ---- 输入行为 ---------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        completer = self._completer
        key = event.key()

        # 候选框开着的时候，先处理它该管的键。
        #
        # 顺序是有讲究的：方向键**永远**交给它（按方向键就是"我要选一项"的意思），
        # 而回车与 Tab **只在用户真的选过之后**才交给它。
        #
        # 这条区分修的是一个很难自己发现的 bug：候选框每次弹出都会把第 0 项
        # 设成当前项，如果回车无条件"采纳当前项"，那么只要敲满 2 个字符能匹配上
        # 候选，回车就不再是换行 —— `for` + 回车会展开整个 for 模板，
        # `// hi` + 回车会把注释改成 `// hix = 1;`。写代码哪能不换行，
        # 于是整个编辑器都显得"有毛病"却说不出哪里怪。
        if completer is not None and completer.popup().isVisible():
            if key == Qt.Key_Escape:
                completer.popup().hide()
                return
            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                completer.popup().keyPressEvent(event)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab) and completer.has_choice():
                completer.accept_current()
                return
            # 没选过任何一项：不拦，往下走 —— 回车换行、Tab 缩进都照常。

        # 手动唤出（注释里、或者前缀太短时也能用：手动是一次明确的请求）
        if completer is not None and self._is_manual_trigger(event):
            completer.try_complete(force=True)
            return

        if key == Qt.Key_Tab and not event.modifiers():
            self.insertPlainText("    ")
            return
        if key == Qt.Key_Backtab:
            # Shift+Tab：整行左移一级缩进
            self._unindent()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            super().keyPressEvent(event)
            self._auto_indent()
            return

        super().keyPressEvent(event)

        # 打了一个标识符字符之后，按需把候选框调出来
        if completer is not None and self._triggered_by(event):
            completer.try_complete()

    @staticmethod
    def _is_manual_trigger(event) -> bool:
        """是否是要手动唤出候选框。

        Windows 中文环境下 ``Ctrl+Space`` 默认被输入法抢走，所以额外提供
        ``Alt+/`` 这条输入法不会拦截的通道。
        """
        modifiers = event.modifiers()
        if event.key() == Qt.Key_Slash and modifiers & Qt.AltModifier:
            return True
        return event.key() == Qt.Key_Space and bool(modifiers & Qt.ControlModifier)

    @staticmethod
    def _triggered_by(event) -> bool:
        """这个按键是否可能推进一个标识符。"""
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return False
        text = event.text()
        if len(text) != 1:
            return False
        return text.isalnum() or text in "_."

    def _auto_indent(self) -> None:
        cursor = self.textCursor()
        line = cursor.block().text()[: cursor.positionInBlock()]
        indent = re.match(r"[ \t]*", line).group(0)
        extra = "    " if line.rstrip().endswith(("{", ":", "(", "[")) else ""
        if indent or extra:
            cursor.insertText(indent + extra)
            self.setTextCursor(cursor)

    def _unindent(self) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        for _ in range(4):
            cursor.deleteChar()


# ======================================================================
# 路径选择
# ======================================================================


class PathPicker(QWidget):
    """一行路径选择器：输入框 + 浏览 + 状态徽标。"""

    changed = Signal(str)

    def __init__(self, *, caption: str = "选择文件", file_filter: str = "所有文件 (*)",
                 is_directory: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._caption = caption
        self._filter = file_filter
        self._is_directory = is_directory

        self.edit = QLineEdit()
        # 这里原来是个写死 "未配置" 的占位符，而右边紧邻的状态徽标本来也是
        # "未配置" —— 同一行上同一个词出现两次，只是字重不同。徽标才是状态
        # 的唯一出处（它会跟着变成 待验证 / 可用 / 无效 / 未找到），
        # 输入框留空就够了。
        self.edit.setPlaceholderText("尚未选择工具链")
        self.edit.textChanged.connect(self.changed)

        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse)

        self.status = QLabel("未配置")
        self.status.setMinimumWidth(72)
        self.status.setAlignment(Qt.AlignCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse_button)
        layout.addWidget(self.status)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, value: str) -> None:
        if value and value != self.edit.text():
            self.edit.setText(value)

    def set_status(self, text: str, color: str = "", *, tooltip: str = "") -> None:
        self.status.setText(text)
        self.status.setToolTip(tooltip or text)
        if color:
            self.status.setStyleSheet(
                f"color: {color}; border: 1px solid {color}; border-radius: 9px;"
                f" padding: 1px 8px; font-weight: 600;"
            )
        else:
            self.status.setStyleSheet("")

    def _browse(self) -> None:
        if self._is_directory:
            selected = QFileDialog.getExistingDirectory(self, self._caption, self.path() or os.getcwd())
        else:
            selected, _ = QFileDialog.getOpenFileName(
                self, self._caption, self.path() or os.getcwd(), self._filter
            )
        if selected:
            self.set_path(selected)
            self.changed.emit(selected)


# ======================================================================
# Markdown 预览
# ======================================================================


class MarkdownView(QTextBrowser):
    """题目描述预览，能把 ``![](a.png)`` 解析到本地资源目录。"""

    def __init__(self, palette: Palette | None = None, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette or resolve_palette("light")
        self.setOpenExternalLinks(True)
        self.setOpenLinks(False)
        self.setReadOnly(True)

    def set_palette_theme(self, palette: Palette) -> None:
        self._palette = palette
        self.setStyleSheet(
            f"QTextBrowser {{ background-color: {palette.surface};"
            f" border: 1px solid {palette.border}; border-radius: 5px; padding: 8px; }}"
        )

    def render_markdown(
        self,
        text: str,
        *,
        resource_dirs: list[str] | None = None,
        images_enabled: bool = True,
    ) -> None:
        """渲染 Markdown。``images_enabled=False`` 时退化为纯文本。"""
        content = text or ""
        if images_enabled:
            content = _resolve_images(content, resource_dirs or [])
            self.setMarkdown(content)
        else:
            self.setPlainText(content)


def _resolve_images(text: str, resource_dirs: list[str]) -> str:
    """把相对图片路径替换为绝对 file:// URL，让预览能显示本地图片。"""

    def replace(match: re.Match[str]) -> str:
        alt, source = match.group(1), match.group(2).strip()
        if source.lower().startswith(("http://", "https://", "data:", "file:")):
            return match.group(0)
        candidate = source.replace("\\", "/").lstrip("./")
        for directory in resource_dirs:
            path = Path(directory) / candidate
            if path.is_file():
                return f"![{alt}]({path.as_uri()})"
        return match.group(0)

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace, text)


# ======================================================================
# 结果输出
# ======================================================================


class OutputView(QPlainTextEdit):
    """判题/运行输出面板：等宽字体、只读、可着色追加。"""

    def __init__(self, palette: Palette | None = None, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette or resolve_palette("light")
        self.setReadOnly(True)
        self.setFont(mono_font(10))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setMaximumBlockCount(20000)   # 防止长时间评测把内存吃满
        self._palette_apply()

    def _palette_apply(self) -> None:
        self.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {self._palette.surface};"
            f" border: 1px solid {self._palette.border}; border-radius: 5px;"
            f" padding: 6px; }}"
        )

    def set_palette_theme(self, palette: Palette) -> None:
        self._palette = palette
        self._palette_apply()

    def append_line(self, text: str = "", color: str = "", *, bold: bool = False) -> None:
        """追加一行，结尾自动换行。"""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color or self._palette.text))
        fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
        cursor.setCharFormat(fmt)
        cursor.insertText(text + "\n")
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def append_block(self, text: str, color: str = "", *, bold: bool = False) -> None:
        for line in (text or "").splitlines():
            self.append_line(line, color, bold=bold)

    def clear_output(self) -> None:
        self.clear()


__all__ = [
    "COMPLETION_WORDS",
    "KEYWORDS",
    "SNIPPETS",
    "CodeCompleter",
    "CodeEditor",
    "CodeHighlighter",
    "CompletionDelegate",
    "CompletionEntry",
    "MarkdownView",
    "OutputView",
    "PathPicker",
    "TestCaseRows",
    "prefix_at",
]


# ======================================================================
# 测试点编辑区
# ======================================================================


class _TestCaseRow(QFrame):
    """单个测试点的输入 / 输出编辑框。"""

    removed = Signal(int)      # 行号（0 基）

    def __init__(self, index: int, parent=None) -> None:
        super().__init__(parent)
        self._index = index
        # 没有编辑控件的字段：``values()`` 会把它们原样带回去（见 load 的说明）
        self._name = ""
        self._sample = False
        self.setFrameShape(QFrame.StyledPanel)
        # 卡片外观（浅一档的底 + 边框 + 圆角）交给主题的 role="card"。
        # 原来是在这里内联写一段 QSS 把 palette 的三个色值抄进去，
        # 那样切主题之后这一行不会重刷 —— 同一屏里新旧两套配色并存。
        self.setProperty("role", "card")

        self.title = QLabel(f"测试点 {index + 1}")
        self.title.setProperty("role", "strong")

        self.remove_button = QPushButton("删除")
        self.remove_button.setProperty("flat", True)
        self.remove_button.setToolTip("删除该测试点")
        self.remove_button.clicked.connect(lambda: self.removed.emit(self._index))

        # 分值：NOI 是按测试点给分的，所以每个点自己带一个分值。
        # 放在标题行而不是输入输出那一排，是因为它是"这个点值多少分"这个
        # 属性，和"这个点是什么"（输入/期望输出）不是一类东西。
        self.points_spin = QSpinBox()
        self.points_spin.setRange(0, 1000)
        self.points_spin.setValue(DEFAULT_TESTCASE_POINTS)
        self.points_spin.setToolTip(
            f"这个测试点值多少分（默认 {DEFAULT_TESTCASE_POINTS} 分）。\n"
            "NOI 的给分方式：过几个测试点就拿几个点的分。\n"
            "10 个点的题满分正好 100；想让 5 个点的题也是满分 100，"
            "就把每个点设成 20。")

        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 4, 0)
        header.addWidget(self.title)
        header.addSpacing(12)
        header.addWidget(QLabel("分值"))
        header.addWidget(self.points_spin)
        header.addStretch(1)
        header.addWidget(self.remove_button)

        self.input_edit = QPlainTextEdit()
        self.output_edit = QPlainTextEdit()
        for editor, placeholder in (
            (self.input_edit, "标准输入（每行按回车分隔）"),
            (self.output_edit, "期望输出"),
        ):
            editor.setPlaceholderText(placeholder)
            editor.setFont(mono_font(10))
            editor.setLineWrapMode(QPlainTextEdit.NoWrap)
            editor.setMinimumHeight(76)
            # 刻意**不**加内联边框样式：让它跟全站输入框用同一条 QSS 规则。
            # 原先这里单独描了一遍颜色和圆角，结果测试点的输入框是全站唯一
            # 圆角 4px、内边距也不同的那种，切主题还不会跟着变。

        body = QHBoxLayout()
        body.setContentsMargins(8, 4, 8, 8)
        body.setSpacing(8)
        body.addLayout(_labeled("输入", self.input_edit), 1)
        body.addLayout(_labeled("期望输出", self.output_edit), 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addLayout(body)

    def renumber(self, index: int) -> None:
        self._index = index
        self.title.setText(f"测试点 {index + 1}")

    def values(self) -> TestCase:
        return TestCase(
            input=self.input_edit.toPlainText(),
            output=self.output_edit.toPlainText(),
            name=self._name,
            sample=self._sample,
            points=self.points_spin.value(),
        )

    def load(self, case: TestCase) -> None:
        self.input_edit.setPlainText(case.input)
        self.output_edit.setPlainText(case.output)
        self.points_spin.setValue(case.points)
        # 名字与"是否样例"这一行没有编辑控件，但必须原样带回去：``values()``
        # 是新建一个 TestCase，不记住就会把题目里的「样例」标记**静默清掉** ——
        # 表现是"改完一道题再开房间，学生看不到样例题面了"。
        self._name = case.name
        self._sample = case.sample


class TestCaseRows(QWidget):
    """测试点列表：支持增删、编号、取值、回填。"""

    changed = Signal()

    def __init__(self, palette: Palette | None = None, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette or resolve_palette("light")
        self._rows: list[_TestCaseRow] = []

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 6, 0)
        self._container_layout.setSpacing(8)
        self._container_layout.addStretch(1)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setWidget(self._container)
        self.area.setFrameShape(QFrame.NoFrame)

        add_button = QPushButton("＋ 添加测试点")
        add_button.clicked.connect(lambda: self.add_row())
        clear_button = QPushButton("清空全部")
        clear_button.setProperty("variant", "danger")
        clear_button.clicked.connect(lambda: self.set_count(1))

        # 这一行右边原来还有一个「当前 N 个测试点」。它和「存题模块」题目信息里
        # 的「测试点数」是同一个数字（两边双向同步），而且每点一次「＋ 添加测试点」
        # 上面就多出一个带编号的卡片，数量本来就一目了然。删掉。
        footer = QHBoxLayout()
        footer.addWidget(add_button)
        footer.addWidget(clear_button)
        footer.addStretch(1)
        # 合计满分。分值是逐个测试点填的，"这道题一共多少分"必须一眼看得到 ——
        # 点数一改，每题满分就跟着变，而它决定榜上所有分数的分母。
        self.total_label = QLabel()
        self.total_label.setProperty("muted", True)
        footer.addWidget(self.total_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.area, 1)
        layout.addLayout(footer)

        # 上面每一条改动路径（增行、删行、改分值）都会发 changed，合计算挂在
        # 它上面就够了，不必在每个改动点各调一次。
        self.changed.connect(self._update_total)
        self.set_count(1)

    def _update_total(self) -> None:
        total = sum(row.points_spin.value() for row in self._rows)
        self.total_label.setText(f"合计 {total} 分")

    # ---- 对外 -------------------------------------------------------------

    def count(self) -> int:
        return len(self._rows)

    def add_row(self, case: TestCase | None = None) -> _TestCaseRow:
        row = _TestCaseRow(len(self._rows))
        row.removed.connect(self._remove_row)
        row.input_edit.textChanged.connect(self.changed)
        row.output_edit.textChanged.connect(self.changed)
        row.points_spin.valueChanged.connect(self.changed)
        if case is not None:
            row.load(case)
        self._container_layout.insertWidget(self._container_layout.count() - 1, row)
        self._rows.append(row)
        self.changed.emit()
        return row

    def _remove_row(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        if len(self._rows) <= 1:
            # 至少保留一个测试点，清空内容而不是删掉控件
            self._rows[0].load(TestCase())
            self.changed.emit()
            return
        row = self._rows.pop(index)
        self._container_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        for position, item in enumerate(self._rows):
            item.renumber(position)
        self.changed.emit()

    def set_count(self, count: int, cases: list[TestCase] | None = None) -> None:
        """重建为指定数量的测试点。"""
        count = max(1, int(count))
        for row in self._rows:
            self._container_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for index in range(count):
            case = cases[index] if cases and index < len(cases) else None
            self.add_row(case)

    def values(self) -> list[TestCase]:
        return [row.values() for row in self._rows]

    def load(self, cases: list[TestCase]) -> None:
        self.set_count(max(1, len(cases)), cases)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette


def _labeled(title: str, widget: QWidget) -> QVBoxLayout:
    """给控件加一个上方小标题。

    颜色与字号来自主题的 ``role="caption"``，不在这里内联 —— 内联的色值
    在切主题时不会重刷，之前深色主题下这几个小标题一直留着浅色的灰。
    """
    label = QLabel(title)
    label.setProperty("role", "caption")
    layout = QVBoxLayout()
    layout.setSpacing(2)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(label)
    layout.addWidget(widget)
    return layout
