"""MSVC（cl.exe）端到端验收。

验收目标不是"能编译"，而是**换一套工具链后判定结论不变**：

1. 能定位 VS 的 ``cl.exe`` 并组装出可用的编译环境（INCLUDE / LIB / PATH）；
2. 真的编译 + 真的运行一个最小程序（自检）；
3. 把内置的「两数求和」语料（自创题面 + 6 个测试点）走 MSVC 与现有工具链
   （GCC/clang）各判一遍，逐条比对判定结论；
4. 打印 MSVC 实际采用的标准参数，确认它是按工具集版本算出来的。

这个脚本会明确区分三类结果，因为它们的处置方式完全不同：

* **代码问题** —— 编译不过、答案算错。那是缺陷，脚本会失败退出。
* **已知的工具链差异** —— 例如 MSVC 没有 ``bits/stdc++.h``。
  记录在 :data:`KNOWN_DIVERGENCES` 里，附原因，不算失败。
* **环境问题** —— 判题机上的安全软件拦截了刚编译出的程序（见 ``--help``）。
  编译与代码都没问题，只是产物起不来。会明确提示怎么加信任区，不算失败。

    python tools/msvc_e2e.py
    python tools/msvc_e2e.py -v          # 打印编译错误与测试点差异详情
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from offline_oj.core.compilers import CompilerDetector             # noqa: E402
from offline_oj.core.judge import Judge                            # noqa: E402
from offline_oj.core.models import (                               # noqa: E402
    Language,
    Problem,
    TestCase,
    Verdict,
)
from offline_oj.core.runners import (                              # noqa: E402
    _STANDARD_CACHE,
    _cache_key,
    make_runner,
    profile_for,
    self_test,
)
from offline_oj.win32 import msvc                                  # noqa: E402

PROBLEM_ID = "P0001"

# 内置语料的题面。自创内容，表述与任何在线评测的现有题目无关。
DESCRIPTION = """# 两数求和

## 题目描述

输入两个整数，输出它们的和。

## 输入格式

一行，两个以空格分隔的整数 a 与 b。

## 输出格式

一行，一个整数：a 与 b 的和。

## 提示

C/C++ 请使用 ``int main()`` 并返回 0。评测忽略行尾空格、末尾空行与 CRLF 差异。
"""

TESTCASES = [
    TestCase(input="1 2\n", output="3\n"),
    TestCase(input="10 20\n", output="30\n"),
    TestCase(input="-5 8\n", output="3\n"),
    TestCase(input="0 0\n", output="0\n"),
    TestCase(input="1000000 2000000\n", output="3000000\n"),
    TestCase(input="-1000000000 1000000000\n", output="0\n"),
]

#: 应当 AC 的实现。
GOOD = [
    ("基础读入 · C", Language.C,
     '#include <stdio.h>\nint main(void) { int a, b; '
     'if (scanf("%d %d", &a, &b) != 2) return 0; printf("%d\\n", a + b); return 0; }\n'),
    ("基础读入 · C++", Language.CPP,
     '#include <iostream>\nint main() { std::ios::sync_with_stdio(false); '
     'int a, b; std::cin >> a >> b; std::cout << a + b << std::endl; return 0; }\n'),
    ("快速 IO · C++", Language.CPP,
     '#include <bits/stdc++.h>\nint main() { int a, b; '
     'std::cin >> a >> b; std::cout << a + b << "\\n"; return 0; }\n'),
]

#: 靠判题宽松规则才应当 AC 的实现（行尾空格 / 末尾空行 / CRLF）。
TOLERANT = [
    ("行尾空格 · C", Language.C,
     '#include <stdio.h>\nint main(void) { int a, b; '
     'if (scanf("%d %d", &a, &b) != 2) return 0; printf("%d \\n", a + b); return 0; }\n'),
    ("末尾空行 · C++", Language.CPP,
     '#include <iostream>\nint main() { int a, b; '
     'std::cin >> a >> b; std::cout << a + b << "\\n\\n"; return 0; }\n'),
    ("CRLF 输出 · C", Language.C,
     '#include <stdio.h>\nint main(void) { int a, b; '
     'if (scanf("%d %d", &a, &b) != 2) return 0; printf("%d\\r\\n", a + b); return 0; }\n'),
]

#: 不应当 AC 的写法，元组为 (名称, 语言, 期望判定, 代码)。
BAD = [
    ("答案算错 · C", Language.C, Verdict.WA,
     '#include <stdio.h>\nint main(void) { int a, b; '
     'if (scanf("%d %d", &a, &b) != 2) return 0; printf("%d\\n", a - b); return 0; }\n'),
    ("语法错误 · C++", Language.CPP, Verdict.CE,
     '#include <iostream>\nint main() { int a, b; '
     'std::cin >> a >> b std::cout << a + b; return 0; }\n'),
    ("main 声明为 void（题面要求 int main）", Language.CPP, Verdict.CE,
     '#include <iostream>\nvoid main() { int a, b; '
     'std::cin >> a >> b; std::cout << a + b; }\n'),
]

#: 已知且可解释的工具链差异：语料名 -> 原因。
#: 这里每一条都是实测确认过的，不是"先记下来糊弄过去"。
KNOWN_DIVERGENCES = {
    "快速 IO · C++": (
        "MSVC 不提供 bits/stdc++.h。这是 MSVC 与 GCC 的既有差别，"
        "在线评测用的也是 GCC，所以用该头文件的题解在 MSVC 上会 CE —— 属预期行为。"
    ),
    "main 声明为 void（题面要求 int main）": (
        "这条陷阱在 MSVC 上抓不到：GCC 报 error: '::main' must return 'int'，"
        "而 MSVC 对 void main **连警告都不发**（/W4、/WX 都试过，一律放过，"
        "所以也没有可提级的警告码可用），于是这个「错误写法」反而拿到了 AC。"
        "也就是说：要复现在线评测的判定，必须用 GCC 系编译器。"
        "工具链选择器正是基于这类原因把 GCC 排在 MSVC 前面。"
    ),
}

#: 这些字样说明"程序启动被系统拦下了"，而不是代码有问题
LAUNCH_BLOCKED_MARKERS = (
    "无法启动编译产物",
    "无法启动程序",
    "拒绝访问",
    "access is denied",
)

_ANSI = sys.stdout.isatty() and os.environ.get("TERM") not in (None, "", "dumb")
GREEN = "\033[32m" if _ANSI else ""
RED = "\033[31m" if _ANSI else ""
YELLOW = "\033[33m" if _ANSI else ""
DIM = "\033[2m" if _ANSI else ""
RESET = "\033[0m" if _ANSI else ""


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def looks_blocked(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in LAUNCH_BLOCKED_MARKERS)


def detect_paths() -> dict[str, str]:
    """跑一遍编译器探测，返回 {配置键: 路径}。"""
    return {key: info.path
            for key, info in CompilerDetector().detect_all().items()
            if info is not None}


def problem() -> Problem:
    """内存里造出内置语料题，与真实入库时用的是同一份题面与测试点。"""
    return Problem(
        id=PROBLEM_ID,
        title="两数求和",
        description=DESCRIPTION,
        time_limit=3000,          # 在线评测常见为 1s，离线判题放宽到 3s 更稳
        memory_limit=128,
        testcases=list(TESTCASES),
    )


def judge_with(language: Language, code: str, paths: dict[str, str],
               work_root: Path, task: Problem):
    """用指定工具链判一遍，返回 JudgeReport。"""
    runner = make_runner(language, paths, work_root, optimize=True)
    return Judge(runner).judge(task, code)


def launch_blocked(report) -> bool:
    """这次评测是不是因为"程序起不来"而失败的。"""
    if report.verdict is Verdict.CE:
        return False
    if report.compile_message and looks_blocked(report.compile_message):
        return True
    for outcome in report.outcomes:
        message = getattr(outcome, "message", "") or ""
        if looks_blocked(message):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MSVC 端到端验收",
        epilog=("若判题机上装了 360 / 火绒这类安全软件，它可能拦截刚编译出的程序"
                "（编译正常但一运行就报拒绝访问，产物随即被删除）。"
                "这种情况下本次只做编译期验收，脚本会提示需要加白名单的目录。"))
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印编译错误与测试点差异详情")
    args = parser.parse_args()

    banner("1. 定位 MSVC 与组装编译环境")
    cl = msvc.find_compiler()
    if not cl:
        print("  没有找到 Visual Studio 的 cl.exe，跳过本次验收。")
        print("  安装 VS / Build Tools 时勾选「使用 C++ 的桌面开发」即可。")
        return 0
    print(f"  cl.exe      : {cl}")

    environment = msvc.build_environment(cl)
    if environment is None:
        print(f"{RED}  编译环境组装失败（INCLUDE / LIB 缺失）{RESET}")
        return 1
    print(f"  版本        : {msvc.version_text(cl)}")
    print(f"  工具集版本  : {msvc.toolset_version(cl)}")
    print(f"  目标架构    : {msvc.architecture(cl)}")
    print(f"  INCLUDE 段数: {len(environment.get('INCLUDE', '').split(';'))}"
          f"   LIB 段数: {len(environment.get('LIB', '').split(';'))}")
    print(f"  按版本选的标准参数: C++ -> {msvc.standard_flag(cl, True) or '(不加)'}"
          f"   C -> {msvc.standard_flag(cl, False) or '(不加)'}")

    with tempfile.TemporaryDirectory(prefix="oj_msvc_e2e_") as folder:
        work = Path(folder)

        banner("2. 工具链自检（真的编译并运行）")
        blocked_langs: list[str] = []
        for language in (Language.CPP, Language.C):
            passed, message = self_test(language, {language.value: cl}, work)
            head = message.splitlines()[0]
            if passed:
                print(f"  {language.value:5s} {GREEN}通过{RESET}  {head}")
            elif looks_blocked(message):
                blocked_langs.append(language.value)
                print(f"  {language.value:5s} {YELLOW}启动被拦截{RESET}  {head}")
            else:
                print(f"  {language.value:5s} {RED}失败{RESET}")
                print(f"        {message[:600]}")
                return 1

        if blocked_langs:
            banner("注意：安全软件拦住了编译产物的启动")
            print("  编译是成功的，但一执行程序就被系统拒绝，随后产物还会被删除。")
            print("  实测特征：产物编译出来放着不动一直在，一尝试启动就消失。")
            print("  这是环境问题，不是代码问题。相关语料本次只能验到「编译期」。")
            print("  要恢复完整验收，请把下面这个目录加入杀毒软件的信任区")
            print("  （360：安全防护中心 → 信任区；火绒：设置 → 信任区 → 添加目录）：")
            print(f"    {work}")

        banner("3. 现有工具链（判定基线）")
        paths = detect_paths()
        for key, path in sorted(paths.items()):
            # "编译器家族"只对 C/C++ 有意义，脚本语言没有这个概念
            family = f"[{profile_for(path).name:4s}] " if key in ("c", "cpp") else ""
            print(f"  {key:6s} {family}{path}")
        baseline_ready = "c" in paths and "cpp" in paths
        if not baseline_ready:
            print(f"{YELLOW}  缺少 C/C++ 基线编译器，本次只做 MSVC 单边验收{RESET}")

        task = problem()
        print(f"\n  题库: {task.id} {task.title} · {len(task.testcases)} 个测试点 · "
              f"{task.time_limit}ms / {task.memory_limit}MB")

        banner("4. 内置语料：MSVC 与基线逐条比对")
        # 只取 C / C++ 语料。Python、Java 根本不经过 cl.exe，放进来只是重复验证
        # 解释器本身，还会把解释器自己的问题混进 MSVC 的结论里。
        corpus = (
            [(name, lang, code, Verdict.AC) for name, lang, code in GOOD
             if lang in (Language.C, Language.CPP)]
            + [(name, lang, code, Verdict.AC) for name, lang, code in TOLERANT
               if lang in (Language.C, Language.CPP)]
            # BAD 的元组顺序是 (名称, 语言, 期望判定, 代码)
            + [(name, lang, code, expect) for name, lang, expect, code in BAD
               if lang in (Language.C, Language.CPP)]
        )

        wrong: list[str] = []
        diverged: list[str] = []
        env_blocked: list[str] = []
        seen_known: set[str] = set()

        print(f"\n  {'语料':34s} {'语言':5s} {'MSVC':6s} {'基线':6s} {'期望':6s} 结论")
        print("  " + "-" * 68)
        for name, language, code, expected in corpus:
            key = language.value

            msvc_paths = dict(paths)
            msvc_paths[key] = cl
            report = judge_with(language, code, msvc_paths, work, task)
            verdict = report.verdict

            if baseline_ready:
                base_report = judge_with(language, code, paths, work, task)
                base_verdict = base_report.verdict
            else:
                base_verdict = verdict

            known = name in KNOWN_DIVERGENCES
            environment_issue = launch_blocked(report)

            if environment_issue:
                # 该编过的确编过了，只是跑不起来 —— 归到环境问题
                compile_ok = not (verdict is Verdict.CE
                                  or looks_blocked(report.compile_message))
                good = compile_ok if expected is not Verdict.CE else not compile_ok
                mark = f"{DIM}仅编译期{'OK' if good else '不符'}{RESET}"
                env_blocked.append(name)
                if not good:
                    wrong.append(f"{name}: MSVC 编译{'通过' if compile_ok else '失败'}，"
                                 f"期望{'通过' if expected is not Verdict.CE else '失败'}")
            elif verdict is expected:
                good = True
                mark = f"{GREEN}OK{RESET}"
            elif known:
                good = True
                mark = f"{YELLOW}已知差异{RESET}"
                seen_known.add(name)
            else:
                good = False
                mark = f"{RED}不符{RESET}"
                wrong.append(f"{name}: MSVC={verdict.text} 期望={expected.text}")

            if verdict is not base_verdict and not known and not environment_issue:
                diverged.append(f"{name}: MSVC={verdict.text} 基线={base_verdict.text}")
            note = "" if verdict is base_verdict else f" {DIM}(基线 {base_verdict.text}){RESET}"

            label = name if len(name) <= 34 else name[:32] + ".."
            print(f"  {label:34s} {key:5s} {verdict.text:6s} {base_verdict.text:6s} "
                  f"{expected.text:6s} {mark}{note}")
            if not good and args.verbose:
                detail = report.compile_message if not report.compile_ok else report.summary()
                for line in (detail or "").splitlines()[:4]:
                    print(f"      {DIM}{line}{RESET}")

        banner("5. 结论")
        resolved = _STANDARD_CACHE.get(("cpp", _cache_key(cl)))
        print(f"  MSVC 实际采用的 C++ 标准参数 : {resolved or '(不加)'}")
        print(f"  MSVC 实际采用的 C 标准参数   : "
              f"{_STANDARD_CACHE.get(('c', _cache_key(cl))) or '(不加)'}")
        print(f"  参与比对的语料              : {len(corpus)} 条"
              f"（C++ {sum(1 for c in corpus if c[1] is Language.CPP)} / "
              f"C {sum(1 for c in corpus if c[1] is Language.C)}）")

        if env_blocked:
            print(f"{YELLOW}  环境阻断（安全软件），以下 {len(env_blocked)} 条只验到编译期:{RESET}")
            for name in env_blocked:
                print(f"    - {name}")
            print(f"    被拦的语言: {', '.join(blocked_langs)}；处理办法见第 2 节。")
        elif not wrong:
            print(f"{GREEN}  全部语料判定与期望一致{RESET}")

        if seen_known:
            print(f"{YELLOW}  已知工具链差异（非缺陷）:{RESET}")
            for name in sorted(seen_known):
                print(f"    - {name}")
                print(f"      {KNOWN_DIVERGENCES[name]}")

        if wrong:
            print(f"{RED}  判定不符 {len(wrong)} 项:{RESET}")
            for item in wrong:
                print(f"    - {item}")
        if diverged:
            print(f"{RED}  未预期的工具链差异:{RESET}")
            for item in diverged:
                print(f"    - {item}")

        # 安全软件拦截属环境条件，不算验收失败
        return 1 if (wrong or diverged) else 0


if __name__ == "__main__":
    raise SystemExit(main())
