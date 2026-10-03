"""主窗口。

装配层的职责就三件事，业务逻辑一律不放在这里：

1. **搭骨架** —— 菜单栏、选项卡、状态栏；
2. **接线** —— 把面板之间的信号连起来（存题面板改了题库 → 写题面板刷新列表）；
3. **管生命周期** —— 主题切换、窗口状态持久化、关闭前落盘。

**这里为什么没有全局工具栏。** 曾经有一条平铺 7 个按钮的工具栏，后来按选项卡
收起过一轮，再后来整条删掉了。原因是它不携带任何自己的信息：每一个按钮都能在
菜单里找到同名的入口（多数还同键），而各面板本来就有就地的一排按钮 —— 写题页的
「提交代码 / 测试运行」、存题页的「保存题目」、编译器页的「验证可用性」。
同一条命令在一屏上出现两三次，用户要分辨的就不再是"这个按钮干什么"，而是
"这两个按钮是不是不一样"；那份认知成本换不来任何便利，只换来视觉噪音。
**命令入口的唯一真源是菜单，面板按钮是就地快捷方式，只有这两层。**
"""

from __future__ import annotations

import base64
import logging

from PySide6.QtCore import QByteArray, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..context import AppContext
from ..win32 import process as win_process
from .panels import (
    CompilersPanel,
    ExamPanel,
    HelpPanel,
    ProblemsPanel,
    SettingsPanel,
    SolvePanel,
)
from .theme import Palette, apply_palette, resolve_palette, stylesheet, ui_font

log = logging.getLogger(__name__)

#: 选项卡位置。**用具名常量而不是裸数字** —— 插一个新面板就会把后面所有
#: ``switch_tab(2)`` 之类的调用指到隔壁去，而这种错不会报错，只会跳错页。
(
    TAB_PROBLEMS,
    TAB_SOLVE,
    TAB_EXAM,
    TAB_COMPILERS,
    TAB_SETTINGS,
    TAB_HELP,
) = range(6)

TAB_NAMES = ("存题模块", "写题模块", "局域网测验", "编译器配置", "高级设置", "帮助与关于")


class MainWindow(QMainWindow):
    """应用主窗口。"""

    #: 主题发生变化（供需要重绘的自绘控件使用）
    theme_changed = Signal(object)

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.palette_theme: Palette = resolve_palette(str(ctx.settings.get("theme", "light")))

        self.setWindowTitle(f"{APP_DISPLAY_NAME}  {__version__}")
        self.resize(1400, 860)
        self.setMinimumSize(1040, 680)
        self._apply_window_icon()

        self._build_panels()
        self._build_menu()
        self._build_status_bar()
        self._wire()
        self.apply_theme(self.palette_theme, persist=False)
        self._restore_state()

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_counts()
        self.set_status(f"就绪 · 数据目录 {self.ctx.paths.data_root}")

    # ------------------------------------------------------------------
    # 骨架
    # ------------------------------------------------------------------

    def _build_panels(self) -> None:
        self.problems_panel = ProblemsPanel(self.ctx, self.palette_theme)
        self.solve_panel = SolvePanel(self.ctx, self.palette_theme)
        self.exam_panel = ExamPanel(self.ctx, self.palette_theme)
        self.compilers_panel = CompilersPanel(self.ctx, self.palette_theme)
        self.settings_panel = SettingsPanel(self.ctx, self.palette_theme)
        self.help_panel = HelpPanel(self.ctx, self.palette_theme)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for name, panel in zip(TAB_NAMES, self._panels()):
            self.tabs.addTab(panel, name)
        self.setCentralWidget(self.tabs)

    def _panels(self) -> tuple[QWidget, ...]:
        return (self.problems_panel, self.solve_panel, self.exam_panel,
                self.compilers_panel, self.settings_panel, self.help_panel)

    def _build_menu(self) -> None:
        bar = self.menuBar()

        # 文件
        file_menu = bar.addMenu("文件(&F)")
        self._add_action(file_menu, "新建题目", "Ctrl+N",
                         lambda: (self.switch_tab(TAB_PROBLEMS), self.problems_panel.new_problem()))
        self._add_action(file_menu, "导入题目…", "",
                         lambda: (self.switch_tab(TAB_PROBLEMS), self.problems_panel.import_single()))
        self._add_action(file_menu, "批量导入…", "",
                         lambda: (self.switch_tab(TAB_PROBLEMS), self.problems_panel.batch_import()))
        self._add_action(file_menu, "批量导出…", "",
                         lambda: (self.switch_tab(TAB_PROBLEMS), self.problems_panel.batch_export()))
        file_menu.addSeparator()
        # 这一组作用在"编辑器里的这份代码"上，和上面的题目导入导出是两回事，
        # 所以隔一条分界线。面板里也有同名的就地按钮，这里提供快捷键与发现入口。
        self._add_action(file_menu, "打开代码文件…", "Ctrl+O",
                         lambda: (self.switch_tab(TAB_SOLVE), self.solve_panel.open_code_file()))
        self._add_action(file_menu, "代码另存为…", "Ctrl+Shift+S",
                         lambda: (self.switch_tab(TAB_SOLVE), self.solve_panel.save_code_as()))
        file_menu.addSeparator()
        self._add_action(file_menu, "打开数据目录", "",
                         lambda: win_process.reveal_in_explorer(self.ctx.paths.data_root))
        self._add_action(file_menu, "重新加载题库", "Ctrl+Shift+R", self.reload_repository)
        file_menu.addSeparator()
        self._add_action(file_menu, "退出", "Alt+F4", self.close)

        # 评测
        judge_menu = bar.addMenu("评测(&J)")
        self._add_action(judge_menu, "提交代码", "Ctrl+Return",
                         lambda: (self.switch_tab(TAB_SOLVE), self.solve_panel.submit_code()))
        self._add_action(judge_menu, "测试运行", "Ctrl+R",
                         lambda: (self.switch_tab(TAB_SOLVE), self.solve_panel.test_run()))
        self._add_action(judge_menu, "停止评测", "",
                         self.solve_panel.stop_judging)
        judge_menu.addSeparator()
        self._add_action(judge_menu, "提交记录", "", self.show_submissions)

        # 测验
        exam_menu = bar.addMenu("测验(&E)")
        # 这里原来是 Ctrl+L，和写题面板的"清空代码"撞了同一个键 —— 两个都是
        # 窗口级快捷键，在写题页按下去到底清空代码还是跳到测验页，取决于 Qt
        # 怎么判歧义（实测会报 Ambiguous shortcut overload，两个都不一定生效）。
        # 清空代码是老绑定、帮助页也写着它，所以让后来者挪开。
        #
        # 文案从"开房"改成了"开启房间"：开房在中文里有个和课堂毫不相干的常用义，
        # 老师把这句话念给学生听或者投到投影上都不合适。按钮、菜单、提示一起改，
        # 别只改按钮 —— 一半"开启房间"一半"开房"比原来还乱。
        self._add_action(exam_menu, "局域网测验（开启 / 加入房间）", "Ctrl+Shift+L",
                         lambda: self.switch_tab(TAB_EXAM))
        self._add_action(exam_menu, "开启 / 关闭房间", "",
                         lambda: (self.switch_tab(TAB_EXAM),
                                  self.exam_panel.set_role("host"),
                                  self.exam_panel.toggle_room()))
        self._add_action(exam_menu, "提前收卷", "",
                         lambda: (self.switch_tab(TAB_EXAM),
                                  self.exam_panel.set_role("host"),
                                  self.exam_panel.collect_now()))
        exam_menu.addSeparator()
        self._add_action(exam_menu, "复制房间信息", "",
                         lambda: self.exam_panel.copy_room_info())

        # 工具
        tool_menu = bar.addMenu("工具(&T)")
        self._add_action(tool_menu, "自动检测编译器", "",
                         lambda: (self.switch_tab(TAB_COMPILERS), self.compilers_panel.auto_detect()))
        self._add_action(tool_menu, "验证工具链可用性", "Ctrl+B",
                         lambda: (self.switch_tab(TAB_COMPILERS), self.compilers_panel.verify()))
        tool_menu.addSeparator()
        self._add_action(tool_menu, "打开日志文件", "",
                         lambda: win_process.open_path(self.ctx.paths.log_file))

        # 视图
        view_menu = bar.addMenu("视图(&V)")
        theme_menu = view_menu.addMenu("主题")
        group = QActionGroup(self)
        group.setExclusive(True)
        current = str(self.ctx.settings.get("theme", "light"))
        for value, label in (("light", "浅色"), ("dark", "深色"), ("system", "跟随系统")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(value == current)
            action.triggered.connect(lambda _checked=False, v=value: self.set_theme(v))
            group.addAction(action)
            theme_menu.addAction(action)
        view_menu.addSeparator()
        self._add_action(view_menu, "聚焦题目搜索", "Ctrl+F",
                         lambda: self._current_panel().focus_search()
                         if hasattr(self._current_panel(), "focus_search") else None)

        # 帮助
        help_menu = bar.addMenu("帮助(&H)")
        self._add_action(help_menu, "使用说明", "F1", lambda: self.switch_tab(TAB_HELP))
        self._add_action(help_menu, "复制诊断信息", "",
                         lambda: (self.switch_tab(TAB_HELP), self.help_panel._copy_diagnostics()))
        help_menu.addSeparator()
        self._add_action(help_menu, "关于", "", self.show_about)

    def _add_action(self, menu, text: str, shortcut: str, slot) -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        return action

    # ---- 状态栏 ---------------------------------------------------------
    #
    # 这里曾经有过一条全局工具栏。它的历史值得留着：最早是 7 个按钮一条平铺
    # 到底，而每一项在菜单里都有同名同键的入口，却不管你在哪一页 —— 切到
    # "帮助与关于"时，"提交代码""验证工具链"照样杵在最显眼的位置。
    # 按页收起之后每页只剩两三个，但真正的问题没解决：剩下的几个仍然和面板
    # 自己的按钮重复（写题页一屏上就有两个"提交代码"）。
    # 所以整条工具栏删掉了 —— 命令入口只剩两层：菜单（唯一真源）+ 面板就地按钮。
    # 删掉之后状态栏顺便少了一行高度，也算顺手赚的。
    def _build_status_bar(self) -> None:
        from PySide6.QtWidgets import QLabel

        bar = QStatusBar()
        self.setStatusBar(bar)

        self.status_label = QLabel("就绪")
        bar.addWidget(self.status_label, 1)

        self.count_label = QLabel()
        self.count_label.setToolTip("题库统计")
        bar.addPermanentWidget(self.count_label)

        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setProperty("muted", True)
        bar.addPermanentWidget(self.version_label)

    def _wire(self) -> None:
        """把面板之间的联动接起来。"""
        self.problems_panel.problems_changed.connect(self._on_problems_changed)
        self.problems_panel.request_solve.connect(self._open_in_solver)
        self.problems_panel.status_message.connect(self.set_status)
        self.solve_panel.status_message.connect(self.set_status)
        self.solve_panel.submission_done.connect(self._on_submission_done)
        self.exam_panel.status_message.connect(self.set_status)
        self.compilers_panel.status_message.connect(self.set_status)
        self.settings_panel.status_message.connect(self.set_status)
        self.settings_panel.settings_applied.connect(self._on_settings_applied)
        self.help_panel.status_message.connect(self.set_status)

        for panel in self._panels():
            panel.notify.connect(self.notify)

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------

    def set_theme(self, theme: str) -> None:
        self.ctx.settings.set("theme", theme)
        self.ctx.settings.save()
        self.apply_theme(resolve_palette(theme))

    def apply_theme(self, palette: Palette, *, persist: bool = True) -> None:
        self.palette_theme = palette
        app = QApplication.instance()
        if app is not None:
            apply_palette(app, palette)
            app.setFont(ui_font(9))
            # 只有 Fusion 才会完整遵守 QSS；Windows 原生样式会忽略大部分规则
            app.setStyle("Fusion")
            app.setStyleSheet(stylesheet(palette))

        for panel in self._panels():
            panel.apply_palette(palette)

        if persist:
            self.theme_changed.emit(palette)

    def _apply_window_icon(self) -> None:
        icon_path = self.ctx.paths.existing_asset("oj_icon.ico")
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

    # ------------------------------------------------------------------
    # 状态与入口
    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def notify(self, level: str, message: str) -> None:
        """统一的提示出口。"""
        if level == "warning":
            QMessageBox.warning(self, "提示", message)
        elif level == "error":
            QMessageBox.critical(self, "错误", message)
        else:
            QMessageBox.information(self, "提示", message)

    def switch_tab(self, index: int) -> None:
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _current_panel(self) -> QWidget:
        return self.tabs.currentWidget()

    def _on_tab_changed(self, index: int) -> None:
        panel = self.tabs.widget(index)
        if hasattr(panel, "on_activated"):
            panel.on_activated()
        self.ctx.settings.set("active_tab", index)

    def _on_problems_changed(self) -> None:
        self.solve_panel.refresh_problem_list()
        self.exam_panel.refresh_problem_choices()
        self._refresh_counts()

    def _on_settings_applied(self) -> None:
        for panel in self._panels():
            panel.on_settings_changed()
        self.apply_theme(resolve_palette(str(self.ctx.settings.get("theme", "light"))),
                         persist=False)
        self.solve_panel.code_editor.set_language(self.solve_panel.current_language())

    def _on_submission_done(self, record) -> None:
        """把提交写进历史（异步，避免磁盘抖动影响手感）。"""
        from .workers import SubmissionSaveWorker

        # 交给 Qt 父对象托管生命周期，跑完自动回收
        self._submission_worker = SubmissionSaveWorker(
            self.ctx.paths.submissions_dir / "history.jsonl", record, self
        )
        self._submission_worker.start()

    def _open_in_solver(self, problem_id: str) -> None:
        self.switch_tab(TAB_SOLVE)
        self.solve_panel.load_problem(problem_id)

    def _refresh_counts(self) -> None:
        stats = self.ctx.repository.stats()
        self.count_label.setText(stats.as_text())

    def reload_repository(self) -> None:
        self.ctx.repository.load()
        self.problems_panel.refresh_list()
        self.solve_panel.refresh_problem_list()
        self._refresh_counts()
        self.set_status(f"题库已重新加载：{len(self.ctx.repository)} 道题")

    def show_submissions(self) -> None:
        records = self.ctx.submission_log.recent(50)
        if not records:
            self.notify("info", "还没有提交记录。")
            return
        lines = [f"{item.get('at', '')[:19]}  {item.get('problem_id', '')}  "
                 f"{item.get('verdict', '')}  {item.get('passed', 0)}/{item.get('total', 0)}"
                 for item in records]
        box = QMessageBox(self)
        box.setWindowTitle("最近提交")
        box.setText(f"最近 {len(records)} 次提交（新的在前）")
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    def show_about(self) -> None:
        from PySide6.QtWidgets import QMessageBox as Box

        diagnostics = "\n".join(f"{key}：{value}"
                               for key, value in self.ctx.diagnostics().items())
        box = Box(self)
        box.setWindowTitle("关于")
        box.setIconPixmap(self.windowIcon().pixmap(64, 64))
        box.setText(f"<b>{APP_DISPLAY_NAME}</b><br>版本 {__version__}")
        box.setInformativeText(
            "面向 Windows 10 / 11 的本地代码评测客户端。\n"
            "支持 C / C++ / Python / Java，题库与提交记录保存在用户数据目录。"
        )
        box.setDetailedText(diagnostics)
        box.setStandardButtons(Box.Ok)
        box.exec()

    # ------------------------------------------------------------------
    # 窗口状态持久化
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        geometry = self.ctx.settings.get("window_geometry", "")
        if geometry:
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(geometry)))
            except Exception:
                log.debug("恢复窗口尺寸失败", exc_info=True)
        state = self.ctx.settings.get("window_state", "")
        if state:
            try:
                self.restoreState(QByteArray(base64.b64decode(state)))
            except Exception:
                log.debug("恢复窗口布局失败", exc_info=True)

        tab = int(self.ctx.settings.get("active_tab", TAB_PROBLEMS) or TAB_PROBLEMS)
        if 0 <= tab < self.tabs.count():
            self.tabs.setCurrentIndex(tab)

        last_language = str(self.ctx.settings.get("last_language", "cpp"))
        index = self.solve_panel.language_combo.findData(last_language)
        if index >= 0:
            self.solve_panel.language_combo.setCurrentIndex(index)

    def _save_state(self) -> None:
        try:
            self.ctx.settings.set(
                "window_geometry",
                base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
            )
            self.ctx.settings.set(
                "window_state",
                base64.b64encode(bytes(self.saveState())).decode("ascii"),
            )
            self.ctx.settings.set("active_tab", self.tabs.currentIndex())
            self.ctx.settings.save()
        except Exception:
            log.debug("保存窗口状态失败", exc_info=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def activate_from_other_instance(self) -> None:
        """另一个实例启动时唤出本窗口（Windows 单实例惯例）。"""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        for panel in self._panels():
            try:
                if not panel.on_closing():
                    event.ignore()
                    return
            except Exception:
                log.exception("面板关闭钩子异常")

        self._save_state()
        try:
            self.ctx.shutdown()
        except Exception:
            log.exception("退出前保存数据失败")
        log.info("主窗口已关闭")
        super().closeEvent(event)
