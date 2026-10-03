"""跑任意脚本，并在 N 秒后把**所有线程的调用栈**打到 stderr。

挂死最难查的地方是它"看起来像还在跑"：进程活着、CPU 不动、日志停在某一行，
你没法判断它是慢还是死。这个工具用 ``faulthandler.dump_traceback_later``
定时把每个线程的栈全部倒出来 —— "谁在等谁"一眼可见，不用一轮轮加打印。

拿本项目真发生过的事举例（存题面板那句保存提示）：

    python tools\\watchdog_run.py --after 45 tests\\smoke_gui.py

栈里直接出现::

    problems_panel.py:996 in _confirm_discard
    problems_panel.py:1061 in on_closing
    main_window.py:438 in closeEvent
    smoke_gui.py:557 in main

于是真相是"离屏环境下 QMessageBox.exec() 没人点得到"，而不是任何网络或线程问题 ——
在拿到这段栈之前，这一轮已经在猜"是不是等待没上限"上花了几个小时。

用法::

    python tools\\watchdog_run.py tests\\smoke_gui.py            # 默认 60 秒后倒栈
    python tools\\watchdog_run.py --after 30 tools\\lan_e2e.py
    python tools\\watchdog_run.py --every 20 tests\\smoke_gui.py  # 反复倒，看是否在推进
    python tools\\watchdog_run.py --hard-exit 120 ...            # 到点连进程一起收掉

脚本在**本进程**里用 ``runpy`` 执行（子进程里的栈父进程看不到）。因此脚本的
``sys.argv`` / ``__name__`` 与直接跑一致，退出码会原样透出。
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import runpy
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="带看门狗地跑一个脚本，挂死时自动倒出全线程栈")
    parser.add_argument("target", help="要跑的脚本路径；配 --module 时是模块名")
    parser.add_argument("--module", action="store_true",
                        help="把 target 当模块名，等价于 python -m（例如 pytest）")
    parser.add_argument("--after", type=float, default=60.0,
                        help="多少秒后第一次倒栈（默认 60）")
    parser.add_argument("--every", type=float, default=0.0,
                        help="每隔多少秒重复倒一次；0 表示只倒一次")
    parser.add_argument("--hard-exit", type=float, default=0.0,
                        help="超过多少秒仍未结束就强杀进程；0 表示不杀")
    parser.add_argument("--windowed", action="store_true",
                        help="不强制离屏（默认会把 QT_QPA_PLATFORM 设成 offscreen）")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="透传给脚本的参数")
    args = parser.parse_args()

    if args.module:
        entry = args.target
        how = "模块"
    else:
        target = Path(args.target)
        if not target.is_absolute():
            target = (ROOT / target).resolve()
        if not target.exists():
            print(f"找不到脚本：{target}", file=sys.stderr)
            return 2
        entry = str(target)
        how = "脚本"

    if not args.windowed:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    started = time.monotonic()

    if args.every > 0:
        # 要"先等 after 秒，之后每 every 秒一次"，dump_traceback_later 的
        # repeat=True 只能做到固定间隔，所以这条自己起线程
        def keep_dumping() -> None:
            time.sleep(args.after)
            while True:
                print(f"\n[看门狗] 已过 {time.monotonic() - started:.0f}s，"
                      f"再次倒栈：", file=sys.stderr)
                faulthandler.dump_traceback(file=sys.stderr)
                time.sleep(args.every)

        threading.Thread(target=keep_dumping, daemon=True).start()
    else:
        faulthandler.dump_traceback_later(args.after, repeat=False)

    if args.hard_exit > 0:
        def hard_stop() -> None:
            time.sleep(args.hard_exit)
            print(f"\n[看门狗] 超过 {args.hard_exit:.0f}s 仍未结束，强制退出",
                  file=sys.stderr)
            os._exit(124)

        threading.Thread(target=hard_stop, daemon=True).start()

    # 让被包装者看到的 argv 与直接运行时一致
    sys.argv = [entry, *args.rest]
    code = 0
    try:
        if args.module:
            # run_module 实现了 python -m 的语义（包会去找 __main__ 子模块）
            runpy.run_module(entry, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(entry, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:                       # noqa: BLE001 - 原样透出
        faulthandler.cancel_dump_traceback_later()
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()

    print(f"\n[看门狗] {how} {entry} 正常结束，退出码 {code}，"
          f"用时 {time.monotonic() - started:.1f}s", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
