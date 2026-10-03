"""应用启动装配。

Windows 桌面应用的启动顺序是有讲究的，顺序错了会出现"设置不生效"的怪现象：

1. **进程级设置**（DPI 感知、AppUserModelID）必须在创建 ``QApplication`` 之前完成，
   一旦窗口创建就无法再改；
2. **日志**要尽早装好，后面任何一步失败都能留下线索；
3. **单实例检查**要在主窗口创建之前，避免两个进程争夺同一个 ``problems.json``；
4. 最后才装异常兜底钩子并显示窗口。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_DISPLAY_NAME, APP_NAME, APP_ORGANIZATION, __version__
from .context import AppContext
from .logging_setup import install_excepthook, setup_logging
from .paths import build_paths
from .win32 import appid as win_appid
from .win32 import dpi as win_dpi

log = logging.getLogger("offline_oj.app")

WELCOME_TEXT = """欢迎使用离线 OJ 系统。

上手三步：
  1. 进入「编译器配置」，点「自动检测」找到本机编译器，再点「验证可用性」确认能真正编译运行；
  2. 进入「存题模块」，新建题目并填写描述与测试点；
  3. 进入「写题模块」写代码，按 Ctrl+Enter 提交评测。

数据目录：{data_dir}
按 F1 可随时查看帮助与快捷键。
"""


def _prepare_process() -> None:
    """创建 QApplication 之前必须完成的进程级设置。"""
    win_dpi.enable_high_dpi_quietly()
    win_appid.set_app_user_model_id()


def _create_application(argv: list[str]) -> QApplication:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_ORGANIZATION)
    # 小数倍缩放（125% / 150%）不做取整，避免界面被拉扯变形
    app.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    return app


def _install_crash_dialog(window) -> None:
    """未捕获异常时先记日志，再给用户一个能截图的对话框。"""

    def report(title: str, error: BaseException, traceback_text: str) -> None:
        if not _can_show_dialogs():
            return
        box = QMessageBox(window)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("错误")
        box.setText(title)
        box.setInformativeText(
            f"{error}\n\n详细信息已写入日志文件。可以把「帮助与关于 → 复制诊断信息」"
            "发给维护人员。"
        )
        box.setDetailedText(traceback_text)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    install_excepthook(report)


def _maybe_show_welcome(window, ctx: AppContext) -> None:
    if ctx.settings.bool("welcome_shown"):
        return
    if not _can_show_dialogs():
        # 离屏环境（自动化测试）不弹窗，也不写标记 —— 真实用户下次启动仍能看到欢迎提示
        return
    box = QMessageBox(window)
    box.setWindowTitle("欢迎")
    box.setIcon(QMessageBox.Information)
    box.setText(APP_DISPLAY_NAME)
    box.setInformativeText(WELCOME_TEXT.format(data_dir=ctx.paths.data_root))
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()
    ctx.settings.set("welcome_shown", True)
    ctx.settings.save()


def _can_show_dialogs() -> bool:
    """判断当前是否有真正的图形环境。

    离屏/最小平台（``offscreen``）与 CI 环境下，模态对话框会永远等不到人点确定，
    把进程卡死。启动失败时宁可只写日志也不要卡住。
    """
    platform_name = os.environ.get("QT_QPA_PLATFORM", "").lower()
    return platform_name not in ("offscreen", "minimal")


def _report_startup_failure() -> None:
    """启动失败时的兜底提示：先写日志与 stderr，再（有图形环境时）弹窗。"""
    details = traceback.format_exc()
    log.error("启动失败:\n%s", details)
    if sys.stderr is not None:
        try:
            print(details, file=sys.stderr)
        except Exception:
            pass

    if not _can_show_dialogs():
        return
    try:
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("启动失败")
        box.setText("程序无法启动。")
        box.setInformativeText("详细信息已写入日志文件，可查看「帮助与关于 → 复制诊断信息」。")
        box.setDetailedText(details)
        box.exec()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """程序入口。返回进程退出码。"""
    arguments = list(sys.argv if argv is None else argv)

    _prepare_process()

    paths = build_paths().ensure_layout()
    setup_logging(paths)
    log.info("%s %s 启动，数据目录 %s", APP_DISPLAY_NAME, __version__, paths.data_root)

    app = _create_application(arguments)

    # 单实例：已有实例则唤出它并退出
    from .ui.single_instance import SingleInstanceGuard

    guard = SingleInstanceGuard()
    if not guard.acquire():
        log.info("已有实例在运行，尝试唤出已有窗口后退出")
        guard.notify_existing()
        return 0

    try:
        ctx = AppContext.create(paths)

        # 清理上次异常退出留下的编译残留（只针对本程序自己的工作区）
        from .core.runners import kill_all

        try:
            kill_all(paths.work_dir)
        except Exception:
            log.debug("清理工作区残留失败", exc_info=True)

        from .ui.main_window import MainWindow

        window = MainWindow(ctx)
        guard.activation_requested.connect(window.activate_from_other_instance)
        _install_crash_dialog(window)
        window.show()
        _maybe_show_welcome(window, ctx)

        exit_code = app.exec()
        log.info("正常退出，退出码 %s", exit_code)
        return exit_code
    except Exception:
        _report_startup_failure()
        return 1
    finally:
        guard.close()


def run() -> None:
    """控制台脚本入口。"""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
