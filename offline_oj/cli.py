"""命令行入口（无界面）。

给自动化用：批量判题、CI 里跑回归、或者只是想在不打开窗口的情况下看题库。
刻意不导入 PySide6 —— 服务器上没有图形环境也能跑。

示例::

    offline-oj-cli list
    offline-oj-cli detect
    offline-oj-cli judge P0001 solution.cpp --language cpp
    offline-oj-cli judge P0001 sol.py -l python --quiet
    offline-oj-cli export ./backup --format folder
    offline-oj-cli import ./submissions.zip --strategy rename
    offline-oj-cli doctor
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import APP_DISPLAY_NAME, __version__
from .context import AppContext
from .core.archive import ProblemArchive
from .core.compilers import KEY_LABELS, CompilerDetector
from .core.judge import Judge
from .core.models import Language
from .core.repository import OVERWRITE, RENAME, SKIP
from .core.runners import make_runner
from .logging_setup import setup_logging
from .paths import build_paths

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_ACCEPTED = 0
EXIT_NOT_ACCEPTED = 2

STRATEGIES = {SKIP: SKIP, OVERWRITE: OVERWRITE, RENAME: RENAME}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offline-oj-cli",
        description=f"{APP_DISPLAY_NAME} 命令行工具 v{__version__}",
    )
    parser.add_argument("--data-dir", default="", metavar="DIR",
                        help="覆盖数据目录（默认 %%LOCALAPPDATA%%\\OfflineOJ）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    list_parser = sub.add_parser("list", help="列出题库中的题目")
    list_parser.add_argument("--search", default="", help="按题号 / 标题过滤")

    # detect
    detect_parser = sub.add_parser("detect", help="检测本机编译器")
    detect_parser.add_argument("--save", action="store_true", help="把检测结果写入设置")

    # judge
    judge_parser = sub.add_parser("judge", help="评测一个源文件")
    judge_parser.add_argument("problem_id", help="题目 ID")
    judge_parser.add_argument("source", help="源代码文件路径")
    judge_parser.add_argument("-l", "--language", default="cpp",
                              choices=[item.value for item in Language],
                              help="语言（默认 cpp）")
    judge_parser.add_argument("--quiet", action="store_true", help="只输出结论")
    judge_parser.add_argument("--no-security-check", action="store_true",
                              help="跳过危险调用静态检查")
    judge_parser.add_argument("--save-submission", action="store_true",
                              help="把本次结果写入提交历史")

    # export / import
    export_parser = sub.add_parser("export", help="导出题库")
    export_parser.add_argument("target", help="目标路径（.zip 或目录）")
    export_parser.add_argument("--format", default="auto", choices=["auto", "zip", "folder"])

    import_parser = sub.add_parser("import", help="导入题目包")
    import_parser.add_argument("source", help="ZIP 文件或目录")
    import_parser.add_argument("--strategy", default=RENAME, choices=list(STRATEGIES))

    sub.add_parser("doctor", help="打印诊断信息")
    return parser


# ----------------------------------------------------------------------
# 各子命令
# ----------------------------------------------------------------------


def cmd_list(ctx: AppContext, args: argparse.Namespace) -> int:
    problems = ctx.repository.search(args.search)
    if not problems:
        print("题库为空，或没有匹配的题目。")
        return EXIT_OK
    width = max((len(item.id) for item in problems), default=4)
    for problem in problems:
        print(f"{problem.id:<{width}}  {problem.title}  "
              f"[{problem.testcase_count} 点, {problem.time_limit}ms, {problem.memory_limit}MB]")
    print(f"\n共 {len(problems)} 道题 · 数据目录 {ctx.paths.data_root}")
    return EXIT_OK


def cmd_detect(ctx: AppContext, args: argparse.Namespace) -> int:
    found = 0
    for key, label in KEY_LABELS.items():
        info = CompilerDetector.detect(key)
        if info is None:
            print(f"✗ {label}: 未找到")
            continue
        found += 1
        print(f"✓ {label}: {info.path}")
        print(f"    {info.version}")
        if args.save:
            ctx.settings.set_compiler_path(key, info.path)
    if args.save:
        ctx.settings.save()
        print(f"\n已保存到 {ctx.settings.file}")
    print(f"\n找到 {found}/{len(KEY_LABELS)} 个工具链")
    return EXIT_OK if found else EXIT_FAILURE


def cmd_judge(ctx: AppContext, args: argparse.Namespace) -> int:
    problem = ctx.repository.get(args.problem_id)
    if problem is None:
        print(f"错误：题库中不存在题目 {args.problem_id}", file=sys.stderr)
        return EXIT_FAILURE

    source_path = Path(args.source)
    if not source_path.is_file():
        print(f"错误：源文件不存在 {source_path}", file=sys.stderr)
        return EXIT_FAILURE
    code = source_path.read_text(encoding="utf-8", errors="replace")

    language = Language.from_value(args.language)
    if language is None:
        print(f"错误：不支持的语言 {args.language}", file=sys.stderr)
        return EXIT_FAILURE

    if not args.no_security_check:
        from .core import security

        findings = security.scan(code, language)
        if findings:
            print("安全提示（不影响执行）：")
            for item in findings:
                print(f"  · {item}")

    paths = ctx.compiler_paths()
    # 题目可能挂着自定义校验器，它的工具链同样要就位 —— 否则整题会因为
    # "校验器不可用"判成评测机内部错误，而报错时机在编译之后，看着很费解
    required = list(language.key_paths)
    checker_language = problem.judge.checker_language
    if problem.judge.uses_checker and checker_language is not None:
        required += [key for key in checker_language.key_paths if key not in required]

    if not all(paths.get(key) for key in required):
        print("部分工具链未配置，尝试自动检测…")
        for key in required:
            if not paths.get(key):
                info = CompilerDetector.detect(key)
                if info is not None:
                    paths[key] = info.path

    runner = make_runner(language, paths, ctx.paths.work_dir,
                         optimize=ctx.settings.bool("o2_optimization"))
    report = Judge(runner).judge(problem, code)

    if args.quiet:
        print(f"{report.verdict.value} {report.passed}/{report.total}")
    else:
        print(report.text_report())

    if args.save_submission:
        from .core.repository import SubmissionRecord

        ctx.submission_log.append(SubmissionRecord.from_report(report, language))
        print(f"\n已写入提交历史 {ctx.paths.submissions_dir / 'history.jsonl'}")

    return EXIT_ACCEPTED if report.accepted else EXIT_NOT_ACCEPTED


def cmd_export(ctx: AppContext, args: argparse.Namespace) -> int:
    problems = ctx.repository.all()
    if not problems:
        print("题库为空，没有可导出的题目。", file=sys.stderr)
        return EXIT_FAILURE

    target = Path(args.target)
    style = args.format
    if style == "auto":
        style = "zip" if target.suffix.lower() == ".zip" else "folder"

    archive = ProblemArchive(ctx.repository)
    if style == "zip":
        count = archive.export_zip(target, problems)
    else:
        count = archive.export_folder(target, problems)
    print(f"已导出 {count} 道题到 {target}")
    return EXIT_OK


def cmd_import(ctx: AppContext, args: argparse.Namespace) -> int:
    source = Path(args.source)
    if not source.exists():
        print(f"错误：路径不存在 {source}", file=sys.stderr)
        return EXIT_FAILURE

    archive = ProblemArchive(ctx.repository)
    if source.is_dir():
        report = archive.import_folder(source, args.strategy)
    else:
        report = archive.import_zip(source, args.strategy)

    print(report.text())
    ctx.repository.save()
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_doctor(_ctx: AppContext, args: argparse.Namespace) -> int:
    import platform

    print(f"{APP_DISPLAY_NAME} {__version__}")
    print(f"Python {platform.python_version()} · {platform.platform()}")
    paths = build_paths()
    for key, value in paths.as_dict().items():
        print(f"{key}: {value}")
    print(f"日志: {paths.log_file}")
    return EXIT_OK


# ----------------------------------------------------------------------


HANDLERS = {
    "list": cmd_list,
    "detect": cmd_detect,
    "judge": cmd_judge,
    "export": cmd_export,
    "import": cmd_import,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "data_dir", ""):
        import os

        os.environ["OFFLINE_OJ_HOME"] = str(Path(args.data_dir).expanduser().resolve())

    paths = build_paths().ensure_layout()
    setup_logging(paths, level=logging.DEBUG if args.verbose else logging.WARNING)

    ctx = AppContext.create(paths)
    try:
        return HANDLERS[args.command](ctx, args)
    finally:
        ctx.shutdown()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
