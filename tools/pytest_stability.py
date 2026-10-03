"""反复跑同一组 pytest 目标，把"偶发崩溃 / 挂死"变成可观测的结果。

这个脚本来自一次真实的排查：加了两个用例之后，``pytest tests`` 开始不稳定 ——
单独跑全过，凑到一起必崩，而且崩法有两种（有时访问违例秒退，有时直接挂死）。
用 shell 判断是行不通的：``for`` 循环里 ``$?`` 会被自己的 ``echo`` 覆盖，
而且"崩溃"和"挂死被超时杀掉"在退出码上长得一模一样。

所以这里统一按子进程跑，每次带超时，并报告：

* **退出码原文** —— Windows 上 ``3221225477``（= ``0xC0000005``）就是访问违例；
* **耗时** —— 用来区分"秒退"和"卡住很久才结束"；
* **卡死判定** —— 超过 ``--timeout`` 仍在跑，就记为挂死并说明是哪一档；
* **崩溃块的开头** —— faulthandler 打印的最内层帧在**块的最前面**，
  只 tail 尾部只会看到 pytest 自己的 ``runpy`` / ``_console_main``，一点用没有。

于是"必崩"这种说法才有意义：跑 N 遍，任何一遍出问题都算没过。

    # 全套跑 5 遍（默认离屏平台）
    python tools\\pytest_stability.py tests --repeat 5

    # 盯住某个可疑组合：把两类用例放一起
    python tools\\pytest_stability.py tests\\test_completion.py::TestPopupLifetime tests\\test_core.py --repeat 3

    # 换成真实 Windows 平台跑（会真的弹窗，需要桌面会话）
    python tools\\pytest_stability.py tests --platform windows
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

#: 崩溃特征。faulthandler 的输出里这些词只在真出事时出现。
MARKERS = ("access violation", "Windows fatal exception", "Fatal Python error")

#: Windows 的访问违例退出码（0xC0000005）
ACCESS_VIOLATION = 3221225477

ROOT = Path(__file__).resolve().parent.parent


def split_verdict(code: int | None, killed: bool, timeout: int) -> str:
    if killed:
        return f"挂死（超过 {timeout}s 仍在运行）"
    if code == 0:
        return "OK"
    if code == ACCESS_VIOLATION:
        return "崩溃：access violation"
    return f"退出码 {code}"


def fault_block(text: str, limit: int = 14) -> list[str]:
    """截出崩溃块的开头若干行 —— 真正出事的地方在这儿。"""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if any(marker in line for marker in MARKERS)), None)
    if start is None:
        return [line for line in lines if line.strip()][-6:]
    end = start
    for i in range(start, min(start + limit, len(lines))):
        if i > start and not lines[i].strip():
            break
        end = i
    return [line for line in lines[start:end + 1] if line.strip()]


def run_once(targets: list[str], *, timeout: int, platform: str,
             extra: list[str], log_dir: Path | None, tag: str) -> tuple[bool, float]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = platform
    env["PYTHONFAULTHANDLER"] = "1"
    cmd = [sys.executable, "-u", "-X", "faulthandler", "-m", "pytest",
           *targets, *extra, "-o", "addopts=", "-q", "-p", "no:cacheprovider"]

    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
        code, killed = proc.returncode, False
        text = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, killed = None, True
        text = "".join(
            part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
            for part in (exc.stdout, exc.stderr)
        )
    elapsed = time.monotonic() - started

    summary = next((line.strip() for line in reversed(text.splitlines())
                    if "passed" in line or "failed" in line or "error" in line.lower()), "")
    print(f"  [{tag}] {split_verdict(code, killed, timeout)} | {elapsed:.1f}s | {summary}")
    for line in fault_block(text):
        print(f"      | {line}")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{tag}.log").write_text(text, encoding="utf-8")
    return code == 0 and not killed, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="反复跑 pytest 目标，暴露偶发崩溃/挂死")
    parser.add_argument("targets", nargs="*", default=["tests"],
                        help="pytest 目标（默认 tests）")
    parser.add_argument("--repeat", type=int, default=3, help="重复次数（默认 3）")
    parser.add_argument("--timeout", type=int, default=180,
                        help="单次超时秒数，超过即判定挂死（默认 180）")
    parser.add_argument("--platform", default="offscreen",
                        help="QT_QPA_PLATFORM（默认 offscreen；用 windows 跑真实桌面）")
    parser.add_argument("--log-dir", default=None,
                        help="把每次的完整输出写到这里（默认 build/pytest-stability）")
    parser.add_argument("--extra", nargs="*", default=[],
                        help="透传给 pytest 的其它参数")
    args = parser.parse_args()

    targets = args.targets or ["tests"]
    log_dir = Path(args.log_dir) if args.log_dir else ROOT / "build" / "pytest-stability"

    print(f"目标：{' '.join(targets)}")
    print(f"平台：{args.platform} · 重复 {args.repeat} 次 · 单次超时 {args.timeout}s")
    print(f"日志：{log_dir}")
    print()

    failures = 0
    slowest = 0.0
    for index in range(1, args.repeat + 1):
        ok, elapsed = run_once(targets, timeout=args.timeout, platform=args.platform,
                               extra=args.extra, log_dir=log_dir, tag=f"第{index}遍")
        slowest = max(slowest, elapsed)
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"结论：{args.repeat} 遍里有 {failures} 遍没过 —— 不稳定（最慢 {slowest:.1f}s）")
        return 1
    print(f"结论：{args.repeat} 遍全部通过（最慢 {slowest:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
