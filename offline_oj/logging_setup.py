"""日志与未捕获异常兜底。

Windows GUI 程序（``console=False`` 的 PyInstaller 产物）没有控制台，
``print`` 出来的信息会凭空消失。因此：

* 所有诊断信息写入 ``%LOCALAPPDATA%\\OfflineOJ\\logs\\app.log``，单文件 1 MB 滚动保留 5 份；
* ``sys.excepthook`` / ``threading.excepthook`` 接管未捕获异常，先落盘再弹窗，
  避免用户看到"程序突然消失"而没有任何线索。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from typing import Callable

from .paths import AppPaths

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5

#: 未捕获异常的回调签名，由 UI 层注入（弹窗提示）
ErrorReporter = Callable[[str, BaseException, str], None]


def setup_logging(paths: AppPaths, level: int = logging.INFO) -> logging.Logger:
    """装配根日志器，返回应用日志对象。重复调用不会叠加 handler。"""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.handlers.RotatingFileHandler(
        paths.log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    root.addHandler(file_handler)

    # 有控制台时（源码运行 / 调试版）同时输出到 stderr，方便开发
    if sys.stderr is not None and getattr(sys.stderr, "write", None):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
        root.addHandler(console)

    logging.getLogger("offline_oj").info("日志已启动，文件: %s", paths.log_file)
    return logging.getLogger("offline_oj")


def install_excepthook(reporter: ErrorReporter | None = None) -> None:
    """接管未捕获异常：写日志 + 可选弹窗。

    :param reporter: 接收 ``(标题, 异常, 回溯文本)`` 的回调；为 ``None`` 时只写日志。
    """
    log = logging.getLogger("offline_oj.crash")

    def _report(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical("未捕获异常:\n%s", text)
        if reporter is not None:
            try:
                reporter("程序发生未处理的错误", exc_value, text)
            except Exception:
                log.exception("异常提示回调自身失败")

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        _report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _report
    threading.excepthook = _thread_hook  # Python 3.8+
