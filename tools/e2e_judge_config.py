"""文件输入输出 + 自定义校验器的真实端到端验收。

单元测试用的是 Python 校验器与合成数据，跑得快但不能证明"真实工具链下也对"。
这份脚本用机器上真正装着的编译器与解释器走完整链路：

1. **文件输入输出** —— 真编译 C++、真跑，读 ``in.txt`` 写 ``out.txt``；
   同时对照标准输入输出模式，证明这个开关确实改变了判题行为；
2. **自定义校验器** —— 校验器本身也是一份 **C++ 源码**，由评测机现场编译，
   按标准协议裁决。选"浮点容差"这个最典型的场景：
   同一个正确解，精确比对判 WA，挂了校验器就判 AC。
   退出码 0 / 1 / 2 三条出口各验一次。
3. **持久化** —— 判题方式存进题库文件，重新打开题库再判一遍，结果不变。

用法::

    python tools\\e2e_judge_config.py            # 用临时数据目录，跑完即弃
    python tools\\e2e_judge_config.py --keep     # 保留目录，方便打开界面看

退出码 0 表示全部符合预期，1 表示有断言不成立。
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from offline_oj.core.compilers import CompilerDetector  # noqa: E402
from offline_oj.core.judge import Judge  # noqa: E402
from offline_oj.core.models import (  # noqa: E402
    IOMode,
    JudgeConfig,
    Language,
    Problem,
    TestCase,
    Verdict,
)
from offline_oj.core.runners import make_runner  # noqa: E402

FILE_PROBLEM_ID = "F0001"
SPJ_PROBLEM_ID = "S0001"

# ---------------------------------------------------------------- 题目定义

#: 文件输入输出：读 in.txt，把和写入 out.txt
FILE_PROBLEM = Problem(
    id=FILE_PROBLEM_ID,
    title="文件输入输出 A+B",
    description="读入 in.txt 中的两个整数，把它们的和写入 out.txt。",
    time_limit=3000,
    memory_limit=256,
    judge=JudgeConfig(io_mode=IOMode.FILE),
    testcases=[TestCase(input="1 2\n", output="3\n"),
               TestCase(input="10 20\n", output="30\n")],
)

#: 浮点题：输入 n，输出 sqrt(n)，允许 1e-5 的误差
SPJ_PROBLEM = Problem(
    id=SPJ_PROBLEM_ID,
    title="两点距离（浮点容差）",
    description="输入一个整数 n，输出 sqrt(n)。允许 1e-5 的绝对误差。",
    time_limit=3000,
    memory_limit=256,
    judge=JudgeConfig(io_mode=IOMode.STDIO),
    testcases=[TestCase(input="2\n", output="1.414214\n"),
               TestCase(input="3\n", output="1.732051\n")],
)

#: 校验器：数值容差 1e-5；输出里出现数字之外的字符判格式错误
CHECKER_CPP = r"""
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>

static const double TOLERANCE = 1e-5;

static std::string trimmed(const std::string& text) {
    std::string::size_type begin = text.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return "";
    std::string::size_type end = text.find_last_not_of(" \t\r\n");
    return text.substr(begin, end - begin + 1);
}

static bool numeric(const std::string& text) {
    for (std::string::size_type i = 0; i < text.size(); ++i) {
        char c = text[i];
        bool ok = (c >= '0' && c <= '9') || c == '.' || c == '-' || c == '+'
                  || c == 'e' || c == 'E';
        if (!ok) return false;
    }
    return true;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr, "usage: checker <input> <output> <answer>\n");
        return 3;
    }

    std::ifstream got_file(argv[2]);
    std::string got;
    std::getline(got_file, got);
    got = trimmed(got);

    std::ifstream want_file(argv[3]);
    std::string want;
    std::getline(want_file, want);
    want = trimmed(want);

    if (!numeric(got)) {
        std::printf("输出里出现了数字之外的字符：%s\n", got.c_str());
        return 2;
    }

    double mine = std::atof(got.c_str());
    double expected = std::atof(want.c_str());
    double error = std::fabs(mine - expected);
    if (error <= TOLERANCE) {
        std::printf("误差 %.3e 在容差 1e-5 以内\n", error);
        return 0;
    }
    std::printf("误差 %.3e 超过容差 1e-5（期望 %.10f，实际 %.10f）\n",
                error, expected, mine);
    return 1;
}
"""

# ---------------------------------------------------------------- 选手提交

#: 文件模式：正确读文件、写文件
CPP_FILE_SOLUTION = r"""
#include <fstream>
int main() {
    long long a = 0, b = 0;
    std::ifstream in("in.txt");
    in >> a >> b;
    std::ofstream out("out.txt");
    out << a + b << "\n";
    return 0;
}
"""

PY_FILE_SOLUTION = (
    "a, b = map(int, open('in.txt').read().split())\n"
    "open('out.txt', 'w').write(str(a + b) + '\\n')\n"
)

#: 输出 6 位小数 —— 与标准答案逐字节一致
PY_SQRT_SIX = "import math\nprint(f'{math.sqrt(int(input())):.6f}')\n"

#: 输出 10 位小数 —— 数值正确、文本不同，正是校验器要救的那一类
CPP_SQRT_TEN = r"""
#include <cmath>
#include <cstdio>
#include <iostream>
int main() {
    long long n = 0;
    std::cin >> n;
    std::printf("%.10f\n", std::sqrt((double)n));
    return 0;
}
"""

#: 数值错了 1e-2，远超容差
CPP_SQRT_OFF = r"""
#include <cmath>
#include <cstdio>
#include <iostream>
int main() {
    long long n = 0;
    std::cin >> n;
    std::printf("%.10f\n", std::sqrt((double)n) + 0.01);
    return 0;
}
"""

#: 数值没问题，但输出里混了说明文字 —— 应当判格式错误
PY_SQRT_CHATTY = ("import math\n"
                  "print(f'sqrt = {math.sqrt(int(input())):.6f}')\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="文件模式与自定义校验器验收")
    parser.add_argument("--keep", action="store_true", help="保留数据目录")
    parser.add_argument("--workdir", default="", help="指定数据目录")
    args = parser.parse_args()

    if args.workdir:
        data_dir = Path(args.workdir).resolve()
        temporary = None
    else:
        temporary = tempfile.TemporaryDirectory(prefix="oj_judgecfg_")
        data_dir = Path(temporary.name)
    os.environ["OFFLINE_OJ_HOME"] = str(data_dir)

    from offline_oj.context import AppContext
    from offline_oj.paths import build_paths

    ctx = AppContext.create(build_paths().ensure_layout())
    for key in ("cpp", "c", "python", "javac", "java"):
        info = CompilerDetector.detect(key)
        if info:
            ctx.settings.set_compiler_path(key, info.path)
    ctx.settings.save()
    paths = ctx.settings.compiler_paths()

    print(f"数据目录: {data_dir}")
    for key in sorted(paths):
        print(f"  {key:8s} {paths[key] or '（未检测到）'}")

    failures: list[str] = []
    skipped: list[str] = []

    def run(problem: Problem, language: Language, code: str):
        missing = [key for key in language.key_paths if not paths.get(key)]
        if missing:
            skipped.append(f"{language.value}（缺 {'/'.join(missing)}）")
            return None
        runner = make_runner(language, paths, ctx.paths.work_dir, optimize=True)
        return Judge(runner).judge(problem, code)

    def check(label: str, report, expected: Verdict, *, contains: str = "") -> None:
        if report is None:
            return
        ok = report.verdict is expected
        detail = ""
        if not ok:
            failures.append(f"{label} 期望 {expected.value}，实际 {report.verdict.value}")
        elif contains and report.outcomes:
            message = report.outcomes[0].message
            if contains not in message:
                ok = False
                failures.append(f"{label} 的说明里应当出现 {contains!r}，实际 {message!r}")
            detail = f" · {message[:60]}"
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label:36s} {report.verdict.value:4s} "
              f"{report.passed}/{report.total}  {report.max_time_ms:6.1f}ms{detail}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    print("一、文件输入输出")
    print("=" * 74)
    print(f"题目 {FILE_PROBLEM_ID}：{FILE_PROBLEM.judge_note().lstrip(' · ')}")

    check("C++ 读 in.txt / 写 out.txt", run(FILE_PROBLEM, Language.CPP,
                                          CPP_FILE_SOLUTION), Verdict.AC)
    check("Python 读 in.txt / 写 out.txt", run(FILE_PROBLEM, Language.PYTHON,
                                             PY_FILE_SOLUTION), Verdict.AC)

    # 读对了输入、算对了答案，但把结果打印到了标准输出
    prints_instead = ("a, b = map(int, open('in.txt').read().split())\n"
                      "print(a + b)\n")
    check("答案打印到标准输出 → WA（写错了地方）",
          run(FILE_PROBLEM, Language.PYTHON, prints_instead),
          Verdict.WA, contains="没有生成输出文件 out.txt")

    # 对照组一：标准流的解，在标准输入输出模式下一切正常
    stdio_twin = dataclasses.replace(FILE_PROBLEM, id="F0001-stdio",
                                     judge=JudgeConfig())
    stdio_solution = "a, b = map(int, input().split())\nprint(a + b)\n"
    check("对照：标准流的解 + 标准输入输出模式",
          run(stdio_twin, Language.PYTHON, stdio_solution), Verdict.AC)

    # 对照组二：同一个"读 in.txt"的解放进标准输入输出模式，连输入都读不到。
    # 这说明 io_mode 真的改变了程序运行时的环境，而不只是换个说法。
    check("对照：读 in.txt 的解 + 标准输入输出模式",
          run(stdio_twin, Language.PYTHON, PY_FILE_SOLUTION), Verdict.RE)

    # ---- 持久化：存盘、重开题库、再判 ----
    from offline_oj.core.repository import ProblemRepository

    ctx.repository.put(FILE_PROBLEM)
    ctx.repository.save()
    reopened = ProblemRepository(data_dir / "problems.json",
                                data_dir / "resources").load()
    restored = reopened.get(FILE_PROBLEM_ID)
    if restored is None:
        failures.append("题目没有写进题库文件")
    else:
        same = restored.judge.to_dict() == FILE_PROBLEM.judge.to_dict()
        check("重新打开题库后再判一次",
              run(restored, Language.PYTHON, PY_FILE_SOLUTION), Verdict.AC)
        if not same:
            failures.append(f"判题方式没有原样落盘: {restored.judge.to_dict()}")
        print(f"  {'✓' if same else '✗'} 判题方式原样落盘              "
              f"{restored.judge.to_dict()}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    print("二、自定义校验器（C++ 源码，现场编译）")
    print("=" * 74)

    judged = dataclasses.replace(
        SPJ_PROBLEM,
        judge=JudgeConfig(checker=CHECKER_CPP, checker_language=Language.CPP),
    )
    print(f"题目 {SPJ_PROBLEM_ID}：{judged.judge_note().lstrip(' · ')}"
          " · 容差 1e-5")

    # 先给一个"对照"：同一个 10 位小数答案，不挂校验器就是 WA
    check("对照：同一份解 + 精确比对", run(SPJ_PROBLEM, Language.CPP,
                                        CPP_SQRT_TEN), Verdict.WA)
    check("挂了校验器：数值在容差内 → AC",
          run(judged, Language.CPP, CPP_SQRT_TEN), Verdict.AC)
    check("逐字节相同的解当然也 AC",
          run(judged, Language.PYTHON, PY_SQRT_SIX), Verdict.AC)
    check("误差 1e-2 超出容差 → WA",
          run(judged, Language.CPP, CPP_SQRT_OFF), Verdict.WA, contains="超过容差")
    check("数值对但格式不合要求 → PE",
          run(judged, Language.PYTHON, PY_SQRT_CHATTY), Verdict.PE,
          contains="数字之外的字符")

    # ---- 校验器自己坏了：算评测机内部错误 ----
    broken = dataclasses.replace(
        SPJ_PROBLEM, id="S0001-broken",
        judge=JudgeConfig(checker="int main( {}\n", checker_language=Language.CPP),
    )
    report = run(broken, Language.PYTHON, PY_SQRT_SIX)
    if report is not None:
        label = "校验器编译失败 → IE（选手代码未被误判为 CE）"
        if report.verdict is not Verdict.IE:
            failures.append(f"校验器编译失败应当判 IE，实际 {report.verdict.value}")
            print(f"  ✗ {label}：实际 {report.verdict.value}")
        elif not report.compile_ok:
            failures.append("校验器编译失败被当成了选手的编译错误")
            print(f"  ✗ {label}：compile_ok 被污染")
        else:
            print(f"  ✓ {label}  {report.verdict.value:4s} "
                  f"{report.passed}/{report.total}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    print("三、判题方式也随题目一起导出 / 导入")
    print("=" * 74)
    from offline_oj.core.archive import ProblemArchive
    from offline_oj.core.repository import RENAME

    target = data_dir / "judge_config.zip"
    ProblemArchive(ctx.repository).export_zip(target, [judged])
    fresh = ProblemRepository(data_dir / "imported" / "problems.json",
                              data_dir / "imported" / "resources").load()
    ProblemArchive(fresh).import_zip(target, strategy=RENAME)
    imported = fresh.get(SPJ_PROBLEM_ID)
    same = imported is not None and imported.judge.to_dict() == judged.judge.to_dict()
    if not same:
        failures.append("判题方式没有随题目包往返")
    print(f"  {'✓' if same else '✗'} 校验器源码随题目包往返        "
          f"{'一致' if same else imported.judge.to_dict() if imported else '题目丢失'}")
    check("用导入回来的题目再判一次",
          run(imported, Language.CPP, CPP_SQRT_TEN) if imported else None, Verdict.AC)

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    if skipped:
        print("跳过的用例：" + "、".join(sorted(set(skipped))))
    if failures:
        print(f"不符合预期 {len(failures)} 项：")
        for item in failures:
            print(f"  · {item}")
        code = 1
    else:
        print("全部符合预期。")
        code = 0

    if temporary is not None:
        if code == 0:
            temporary.cleanup()
        else:
            # 失败时把数据目录留下来，方便直接打开界面复盘
            print(f"\n数据目录保留在 {data_dir}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
