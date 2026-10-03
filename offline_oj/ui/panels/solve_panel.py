"""写题模块。

左选题目、右写代码，提交后逐测试点流式显示结果。

流程上的三个要点：

1. **提交前校验** —— 没选题目、代码为空、工具链没配好，都在本地先拦住，
   而不是等编译失败再报错；
2. **静态检查先行** —— 命中危险调用时先弹确认框（可在设置里关掉）；
3. **判题在后台** —— 判题线程只发事件，界面负责渲染。点「停止」会中止后续测试点，
   已经在跑的当前测试点会跑完后退出，不会硬杀线程留下僵尸进程。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core import security
from ...core.judge import (
    KIND_ABORTED,
    KIND_CASE,
    KIND_CHECKER,
    KIND_COMPILED,
    KIND_ENVIRONMENT,
    KIND_FINISH,
    KIND_START,
    JudgeReport,
)
from ...core.models import Language, Problem, Verdict
from ...core.repository import SubmissionRecord
from ..codefile import CodeFileController
from ..theme import (
    DIALOG_MARGINS,
    HEADER_SPACING,
    PAGE_MARGINS,
    ROW_SPACING,
    TIGHT_SPACING,
    Palette,
    apply_verdict_badge,
)
from ..widgets import CodeEditor, MarkdownView, OutputView
from ..workers import JudgeWorker, TestRunWorker
from .base import Panel

ID_ROLE = Qt.UserRole + 1

EXAMPLE_CODE: dict[str, str] = {
    "cpp": """#include <iostream>
using namespace std;

int main() {
    int a, b;
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}
""",
    "c": """#include <stdio.h>

int main() {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a + b);
    return 0;
}
""",
    "python": """a, b = map(int, input().split())
print(a + b)
""",
    "java": """import java.util.Scanner;

public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
        int a = sc.nextInt();
        int b = sc.nextInt();
        System.out.println(a + b);
        sc.close();
    }
}
""",
}


class SolvePanel(Panel):
    """刷题与判题。"""

    #: 一次评测完成，携带用于写入提交历史的记录
    submission_done = Signal(object)      # SubmissionRecord

    def _build(self) -> None:
        self.current_problem_id: str | None = None
        self._judge_worker: JudgeWorker | None = None
        self._last_report: JudgeReport | None = None
        self._last_record: SubmissionRecord | None = None

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_problem_side())
        splitter.addWidget(self._build_code_side())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 940])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.addWidget(splitter)

        self._install_shortcuts()
        self.refresh_problem_list()

    # ------------------------------------------------------------------
    # 左侧：题目
    # ------------------------------------------------------------------

    def _build_problem_side(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(ROW_SPACING)

        title = QLabel("选题")
        title.setProperty("role", "h2")

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索题目")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.refresh_problem_list)

        self.problem_list = QListWidget()
        self.problem_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.problem_list.currentItemChanged.connect(self._on_problem_selected)

        self.problem_title = QLabel("未选择题目")
        self.problem_title.setProperty("role", "h2")
        self.problem_title.setWordWrap(True)

        self.problem_meta = QLabel("从上方列表中选择一道题开始练习")
        self.problem_meta.setProperty("muted", True)
        self.problem_meta.setWordWrap(True)

        self.problem_view = MarkdownView(self.palette)

        splitter = QSplitter(Qt.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(TIGHT_SPACING)
        top_layout.addWidget(title)
        top_layout.addWidget(self.search_edit)
        top_layout.addWidget(self.problem_list, 1)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(TIGHT_SPACING)
        bottom_layout.addWidget(self.problem_title)
        bottom_layout.addWidget(self.problem_meta)
        bottom_layout.addWidget(self.problem_view, 1)

        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setSizes([260, 480])
        layout.addWidget(splitter)
        return container

    # ------------------------------------------------------------------
    # 右侧：代码与结果
    # ------------------------------------------------------------------

    def _build_code_side(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)

        # ---- 工具栏 ----
        self.language_combo = QComboBox()
        for language in Language:
            self.language_combo.addItem(language.display, language.value)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)

        example_button = QPushButton("插入示例模板")
        example_button.setProperty("flat", True)
        example_button.setToolTip("插入该语言的 A+B 求和示例代码框架")
        example_button.clicked.connect(self.insert_example)

        self.submit_button = QPushButton("提交代码")
        self.submit_button.setProperty("variant", "primary")
        self.submit_button.setToolTip("对所有测试点评测 (Ctrl+Enter)")
        self.submit_button.clicked.connect(self.submit_code)

        self.test_button = QPushButton("测试运行")
        self.test_button.setToolTip("用自定义输入跑一次，不比对答案 (Ctrl+R)")
        self.test_button.clicked.connect(self.test_run)

        self.stop_button = QPushButton("停止")
        self.stop_button.setToolTip("中止本次评测")
        self.stop_button.setEnabled(False)
        self.stop_button.setProperty("variant", "danger")
        self.stop_button.clicked.connect(self.stop_judging)

        clear_button = QPushButton("清空代码")
        clear_button.setProperty("flat", True)
        clear_button.setToolTip("清空编辑器 (Ctrl+L)")
        clear_button.clicked.connect(self.clear_code)

        # 提交级的编译优化开关。默认取「高级设置」里的全局值，但放在这里是因为
        # 它直接影响"自测挺稳、判题 TLE"这类结论 —— 就地对照一次就能看出优化
        # 对耗时的影响，不必来回翻设置页。
        self.o2_check = QCheckBox("O2 优化")
        self.o2_check.setToolTip(
            "C / C++ 编译时加 -O2（MSVC 为 /O2）。\n"
            "默认跟随「高级设置」里的全局开关；提交与「测试运行」都用这个值，\n"
            "所以自测看到的耗时与真实判题一致。"
        )
        self.o2_check.setChecked(self.ctx.settings.bool("o2_optimization"))

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("语言"))
        toolbar.addWidget(self.language_combo)
        toolbar.addWidget(example_button)
        toolbar.addWidget(self.o2_check)
        toolbar.addStretch(1)
        toolbar.addWidget(self.test_button)
        toolbar.addWidget(self.submit_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(clear_button)

        # ---- 编辑器 ----
        self.code_editor = CodeEditor(self.palette)
        # 代码提示的键位提示原来是一个常驻的小字标签，挤在标题行右端、
        # 紧挨着光标位置显示，读起来像一句话："代码提示 第 1 行，第 1 列"。
        # 移到占位符里 —— 编辑器空着的时候它本来就该说这句话，写了代码之后
        # 也不再占位置。悬停提示保留完整说明。
        self.code_editor.setPlaceholderText("在这里写代码…　敲下字母即联想")
        self.code_editor.setToolTip(
            "敲下第一个字母就自动弹出候选；注释与字符串里不弹"
            "（Ctrl+Space / Alt+/ 可手动唤出）。\n"
            "候选框里用 ↑ ↓ 选择，Enter / Tab 采纳，Esc 关闭。"
        )
        self.code_editor.cursorPositionChanged.connect(self._update_cursor_label)
        # 只有**真的改了正文**才会走到这里（`connect_content_changed` 用的是
        # `contentsChange`，换主题之类的纯格式重排不会触发），正好用来更新
        # "当前文件・未保存"那行小字。
        self.code_editor.connect_content_changed(self._on_code_changed)

        self.cursor_label = QLabel("第 1 行，第 1 列")
        self.cursor_label.setProperty("muted", True)

        # 代码文件：打开 / 另存为。放在编辑器标题行是刻意的 —— 这两个动作作用在
        # **编辑器里的这份代码**上，和上面那排"提交/测试运行/清空"不是一类。
        self.open_code_button = QPushButton("打开…")
        self.open_code_button.setProperty("flat", True)
        self.open_code_button.setToolTip(
            "从文件读一份代码到编辑器 (Ctrl+O)\n"
            "记事本存的 UTF-8 带 BOM 也能正确读入。")
        self.open_code_button.clicked.connect(self.open_code_file)

        self.save_code_button = QPushButton("另存为…")
        self.save_code_button.setProperty("flat", True)
        self.save_code_button.setToolTip("把编辑器里的代码存成文件 (Ctrl+Shift+S)")
        self.save_code_button.clicked.connect(self.save_code_as)

        # 当前文件 + 有没有改动。空着的时候不占地方（文本为空即不显示）。
        self.code_file_label = QLabel("")
        self.code_file_label.setProperty("role", "caption")

        editor_header = QHBoxLayout()
        editor_header.addWidget(QLabel("代码"))
        editor_header.addWidget(self.open_code_button)
        editor_header.addWidget(self.save_code_button)
        editor_header.addWidget(self.code_file_label)
        editor_header.addStretch(1)
        editor_header.addWidget(self.cursor_label)

        self.code_files = CodeFileController(
            widget=self,
            editor=self.code_editor,
            settings=self.ctx.settings,
            notify=self.notify,
            label=self.code_file_label,
            current_language=self.current_language,
            select_language=self._select_language,
        )

        # ---- 结果 ----
        self.verdict_label = QLabel("尚未提交")
        self.verdict_label.setProperty("role", "badge")

        result_header = QHBoxLayout()
        result_header.addWidget(QLabel("评测结果"))
        result_header.addWidget(self.verdict_label)
        result_header.addStretch(1)

        copy_button = QPushButton("复制报告")
        copy_button.setProperty("flat", True)
        copy_button.clicked.connect(self.copy_report)
        result_header.addWidget(copy_button)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(False)

        self.result_view = OutputView(self.palette)

        code_splitter = QSplitter(Qt.Vertical)
        editor_box = QWidget()
        editor_layout = QVBoxLayout(editor_box)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(HEADER_SPACING)
        editor_layout.addLayout(editor_header)
        editor_layout.addWidget(self.code_editor, 1)

        result_box = QWidget()
        result_layout = QVBoxLayout(result_box)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(HEADER_SPACING)
        result_layout.addLayout(result_header)
        result_layout.addWidget(self.progress)
        result_layout.addWidget(self.result_view, 1)

        code_splitter.addWidget(editor_box)
        code_splitter.addWidget(result_box)
        code_splitter.setStretchFactor(0, 3)
        code_splitter.setStretchFactor(1, 2)

        layout.addLayout(toolbar)
        layout.addWidget(code_splitter, 1)
        return container

    # ------------------------------------------------------------------
    # 快捷键
    # ------------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        """只装"菜单里没有"的那几个键。

        提交 / 测试运行 / 聚焦搜索在菜单里已经各有一条窗口级 QAction
        （``Ctrl+Return`` / ``Ctrl+R`` / ``Ctrl+F``），面板里再装一遍就是
        同一个键序列绑两次 —— 实测 Qt 会报 ``Ambiguous shortcut overload``，
        按下去哪个生效看运气。菜单那几条跨选项卡都能用，这里就不重复了。

        留下的两个是有原因的：

        * ``Ctrl+Enter``：小键盘回车。Windows 上很多人的回车习惯是它，
          而它和 ``Ctrl+Return`` 在 Qt 里是两个不同的键，不会撞。
        * ``Ctrl+L``：清空代码。菜单里没有对应项，只在编辑器里才有意义。
        """
        QShortcut(QKeySequence("Ctrl+Enter"), self, self.submit_code)
        QShortcut(QKeySequence("Ctrl+L"), self, self.clear_code)

    def focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    # ------------------------------------------------------------------
    # 题目列表
    # ------------------------------------------------------------------

    def refresh_problem_list(self) -> None:
        if not hasattr(self, "problem_list"):
            return
        keyword = self.search_edit.text()
        problems = self.ctx.repository.search(keyword)

        self.problem_list.blockSignals(True)
        self.problem_list.clear()
        for problem in problems:
            item = QListWidgetItem(f"{problem.id}  ·  {problem.title}")
            item.setData(ID_ROLE, problem.id)
            self.problem_list.addItem(item)
        self.problem_list.blockSignals(False)

        if self.current_problem_id:
            for row in range(self.problem_list.count()):
                if self.problem_list.item(row).data(ID_ROLE) == self.current_problem_id:
                    self.problem_list.setCurrentRow(row)
                    break

    def _on_problem_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        self.load_problem(current.data(ID_ROLE))

    def load_problem(self, problem_id: str) -> None:
        problem = self.ctx.repository.get(problem_id)
        if problem is None:
            return

        self.current_problem_id = problem_id
        self.problem_title.setText(f"{problem.id}　{problem.title}")
        # 判题方式也摆在题头：文件输入输出 / 特殊判题会改变"怎么交才对"，
        # 等出了 WA 才发现就太晚了
        self.problem_meta.setText(
            f"时间限制 {problem.time_limit} ms　·　内存限制 {problem.memory_limit} MB　·　"
            f"{problem.testcase_count} 个测试点" + problem.judge_note().replace(" · ", "　·　")
        )
        self.problem_view.render_markdown(
            problem.description,
            resource_dirs=self.ctx.paths.image_search_paths(),
            images_enabled=self.ctx.settings.bool("markdown_render"),
        )

        # 换题时若编辑器还是上一题的示例模板，顺手换成新题的空模板
        if not self.code_editor.toPlainText().strip():
            self.insert_example()

        self.result_view.clear_output()
        self._set_verdict(None)
        self.status(f"已载入题目 {problem_id}")

    # ------------------------------------------------------------------
    # 代码编辑
    # ------------------------------------------------------------------

    def _on_language_changed(self) -> None:
        language = self.current_language()
        self.code_editor.set_language(language)
        self.ctx.settings.set("last_language", language.value)

        content = self.code_editor.toPlainText()
        previous_examples = set(EXAMPLE_CODE.values())
        if not content.strip() or content in previous_examples:
            # 只有"空"或"仍是示例模板"时才替换，避免冲掉用户正在写的代码
            self.insert_example()

    def current_language(self) -> Language:
        value = self.language_combo.currentData()
        return Language.from_value(str(value)) or Language.CPP

    def _select_language(self, language: Language) -> None:
        """切换语言下拉框（会连带换编辑器语言与自动插入示例）。

        打开 ``.py`` 时用得上：拿 C++ 的规则给 Python 着色，关键字一条都不对。
        """
        if self.current_language() is language:
            return
        index = self.language_combo.findData(language.value)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)

    def insert_example(self) -> None:
        # 内容被整体换掉了，就不再是"那个文件"的内容
        self.code_files.forget()
        self.code_editor.setPlainText(EXAMPLE_CODE.get(self.current_language().value, ""))

    def clear_code(self) -> None:
        self.code_files.forget()
        self.code_editor.setPlainText("")
        self.status("代码编辑器已清空")

    # ------------------------------------------------------------------
    # 代码文件（打开 / 另存为）
    # ------------------------------------------------------------------
    #
    # 一整套流程（BOM、编码报错、上次目录、未保存确认）都在 CodeFileController
    # 里，测验面板的学生端用同一份 —— 见 offline_oj/ui/codefile.py。

    def open_code_file(self) -> None:
        """从文件读一份代码进来。"""
        if self.code_files.open_file():
            self.status(f"已打开 {os.path.basename(self.code_files.path)}")

    def save_code_as(self) -> None:
        """把编辑器里的代码存成文件。"""
        if self.code_files.save_as():
            self.status(f"已保存到 {os.path.basename(self.code_files.path)}")

    def _on_code_changed(self) -> None:
        """正文真的改了 —— 更新"当前文件・未保存"那行小字。"""
        self.code_files.refresh_label()

    def _update_cursor_label(self) -> None:
        self.cursor_label.setText(self.code_editor.position_info())

    # ------------------------------------------------------------------
    # 判题
    # ------------------------------------------------------------------

    def submit_code(self) -> None:
        if self._judging():
            self.status("正在评测中，请稍候…")
            return
        problem = self.ctx.repository.get(self.current_problem_id or "")
        if problem is None:
            self.notify.emit("info", "请先在左侧选择一道题目。")
            return

        code = self.code_editor.toPlainText()
        if not code.strip():
            self.notify.emit("warning", "代码是空的，先写点什么吧。")
            return

        language = self.current_language()
        ok, message = self._check_environment(language)
        if not ok:
            QMessageBox.warning(self, "运行环境未就绪", message)
            return

        if self.ctx.settings.bool("security_mode"):
            findings = security.scan(code, language)
            if findings:
                if not self._confirm_risky_code(findings):
                    self.status("已取消提交")
                    return

        self._start_judge(problem, code, language)

    def _check_environment(self, language: Language) -> tuple[bool, str]:
        paths = self.ctx.compiler_paths()
        missing: list[str] = []
        from ...core.compilers import KEY_LABELS
        from ...core.validation import PathValidator

        for key in language.key_paths:
            valid, reason = PathValidator.validate_executable(paths.get(key, ""))
            if not valid:
                missing.append(f"· {KEY_LABELS.get(key, key)}：{reason}")
        if missing:
            return False, ("以下工具链未配置或路径无效：\n\n" + "\n".join(missing)
                           + "\n\n请到「编译器配置」选项卡完成配置。")
        return True, ""

    def _confirm_risky_code(self, findings: list[str]) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("安全提示")
        box.setText("代码中包含可能具有风险的操作")
        box.setInformativeText(security.format_findings(findings))
        run_button = box.addButton("仍然运行", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        return box.clickedButton() is run_button

    def _start_judge(self, problem, code: str, language: Language) -> None:
        self.result_view.clear_output()
        self._set_verdict(None)
        self.progress.setRange(0, max(1, problem.testcase_count))
        self.progress.setValue(0)
        self.progress.setVisible(True)

        self.submit_button.setEnabled(False)
        self.submit_button.setText("评测中…")
        self.stop_button.setEnabled(True)
        self.status("正在评测…")

        worker = JudgeWorker(
            problem, code, language, self.ctx.compiler_paths(), self.ctx.paths.work_dir,
            optimize=self.o2_check.isChecked(), parent=self,
        )
        worker.event.connect(self._on_judge_event)
        worker.crashed.connect(self._on_judge_crashed)
        worker.finished.connect(self._on_judge_finished)
        self._judge_worker = worker
        worker.start()

    def _on_judge_event(self, event) -> None:
        p = self.palette

        if event.kind == KIND_START:
            self.result_view.append_line(
                f"题目 {event.message}　语言 {self.current_language().short}",
                p.text_muted)
            self.result_view.append_line("─" * 42, p.border)

        elif event.kind == KIND_ENVIRONMENT:
            self.result_view.append_line("运行环境未就绪", p.danger, bold=True)
            self.result_view.append_block(event.message, p.text)

        elif event.kind == KIND_COMPILED:
            if event.message:
                self.result_view.append_line("编译失败", p.danger, bold=True)
                self.result_view.append_block(event.message, p.text)
            else:
                self.result_view.append_line("编译通过", p.success)

        elif event.kind == KIND_CASE and event.outcome is not None:
            outcome = event.outcome
            color = p.verdict_color(outcome.verdict.value)
            mark = "●" if outcome.passed else "■"
            self.result_view.append_line(
                f"{mark} 测试点 {outcome.index}/{outcome.total}　{outcome.verdict.text}",
                color, bold=not outcome.passed)
            for line in outcome.detail_lines():
                self.result_view.append_line(line, p.text_muted)
            self.progress.setValue(outcome.index)

        elif event.kind == KIND_CHECKER:
            # 只有"校验器不能用了"才会走到这里，而且这道题根本判不了。
            # 说清是题目配置的问题，免得选手对着自己的代码找原因。
            self.result_view.append_line("自定义校验器不可用", p.danger, bold=True)
            self.result_view.append_block(
                f"{event.message}\n请在「存题模块」检查这道题的校验器，"
                "或先取消勾选「使用自定义校验器」。", p.text)

        elif event.kind == KIND_ABORTED:
            self.result_view.append_line("已中止评测", p.warning, bold=True)

        elif event.kind == KIND_FINISH and event.report is not None:
            self._on_report(event.report)

    def _on_report(self, report: JudgeReport) -> None:
        p = self.palette
        self._last_report = report
        self.result_view.append_line("─" * 42, p.border)
        # 编译优化放在结论**上**一行：TLE 的成因里"忘了开优化"和"算法确实慢"
        # 是两件完全不同的事，结论一出来就先把这个前提交代掉。
        if report.optimization_text:
            self.result_view.append_line(report.optimization_text, p.text_muted)
        self.result_view.append_line(report.summary(),
                                     p.verdict_color(report.verdict.value), bold=True)
        self._set_verdict(report.verdict)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)

        record = SubmissionRecord.from_report(report, report.language or self.current_language())
        self._last_record = record
        self.submission_done.emit(record)

        if report.accepted:
            self.status(f"{report.problem_id} 全部通过（{report.passed}/{report.total}）")
        else:
            self.status(f"{report.problem_id} 评测完成：{report.summary()}")

    def _on_judge_crashed(self, message: str) -> None:
        self.result_view.append_line(f"评测机异常：{message}", self.palette.danger, bold=True)
        self.status("评测机异常，详见日志")

    def _on_judge_finished(self) -> None:
        self.submit_button.setEnabled(True)
        self.submit_button.setText("提交代码")
        self.stop_button.setEnabled(False)

    def stop_judging(self) -> None:
        if self._judge_worker is not None and self._judge_worker.isRunning():
            self._judge_worker.stop()
            self.status("已请求停止，等待当前测试点结束…")

    def _judging(self) -> bool:
        return self._judge_worker is not None and self._judge_worker.isRunning()

    def _set_verdict(self, verdict: Verdict | None) -> None:
        self.verdict_label.setText("尚未提交" if verdict is None else verdict.text)
        # 配色交给主题：内联写死 color: #ffffff 在深色主题下会挑到白字压亮色，
        # 对比度只有 2.3:1。现在由 _readable_on 按亮度决定前景色。
        apply_verdict_badge(self.verdict_label, None if verdict is None else verdict.value)

    def copy_report(self) -> None:
        if self._last_report is None:
            self.notify.emit("info", "还没有可复制的评测报告。")
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._last_report.text_report())
        self.status("评测报告已复制到剪贴板")

    # ------------------------------------------------------------------
    # 测试运行
    # ------------------------------------------------------------------

    def test_run(self) -> None:
        if self._judging():
            self.status("评测进行中，请稍候…")
            return
        code = self.code_editor.toPlainText()
        if not code.strip():
            self.notify.emit("warning", "代码是空的，先写点什么吧。")
            return

        language = self.current_language()
        ok, message = self._check_environment(language)
        if not ok:
            QMessageBox.warning(self, "运行环境未就绪", message)
            return

        dialog = _TestRunDialog(self, code, language, self.ctx.repository.get(
            self.current_problem_id or ""), self)
        dialog.exec()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_activated(self) -> None:
        self.refresh_problem_list()

    def on_settings_changed(self) -> None:
        if self.current_problem_id:
            problem = self.ctx.repository.get(self.current_problem_id)
            if problem is not None:
                self.problem_view.render_markdown(
                    problem.description,
                    resource_dirs=self.ctx.paths.image_search_paths(),
                    images_enabled=self.ctx.settings.bool("markdown_render"),
                )
        size = int(self.ctx.settings.get("editor_font_size", 11) or 11)
        from ..theme import mono_font

        self.code_editor.setFont(mono_font(size))
        self.code_editor.setTabStopDistance(
            self.code_editor.fontMetrics().horizontalAdvance(" ") * 4)
        # 全局开关是"默认值"，改了就同步过来；本地面板上的勾选是提交级覆盖
        self.o2_check.setChecked(self.ctx.settings.bool("o2_optimization"))

    def apply_palette(self, palette: Palette) -> None:
        super().apply_palette(palette)
        for widget in ("code_editor", "result_view", "problem_view"):
            target = getattr(self, widget, None)
            if target is not None and hasattr(target, "set_palette_theme"):
                target.set_palette_theme(palette)
        if hasattr(self, "verdict_label"):
            self._set_verdict(self._last_report.verdict if self._last_report else None)

    def on_closing(self) -> bool:
        if self._judge_worker is not None and self._judge_worker.isRunning():
            self._judge_worker.stop()
            self._judge_worker.wait(5000)
        # 测试运行的工作线程由 _TestRunDialog 自己持有并在关闭时等待
        return True


class _TestRunDialog(QDialog):
    """自定义输入的单次运行对话框。

    它不只是"随便喂点输入跑一遍"：题目若配了文件输入输出，这里会**照样**把输入
    写成 ``<题目英文名>.in``、从 ``<题目英文名>.out`` 取输出，并把工作目录切成
    以题目英文名命名的那一层。否则选手在自测里看到的输出和真实判题完全不是一回事。
    """

    def __init__(self, owner: SolvePanel, code: str, language: Language,
                 problem: Problem | None = None, parent=None) -> None:
        super().__init__(parent or owner)
        self._owner = owner
        self._code = code
        self._language = language
        self._problem = problem
        self._judge = problem.judge if problem is not None else None
        self._task_name = problem.english_name if problem is not None else ""
        self._worker: TestRunWorker | None = None

        self.setWindowTitle(f"测试运行 · {language.short}")
        self.resize(760, 560)

        file_mode = self._judge is not None and self._judge.uses_files
        input_label = QLabel()
        if file_mode:
            input_name, output_name = problem.io_files()
            input_label.setText(f"输入文件 {input_name} 的内容"
                                f"（程序会从当前目录的 {input_name} 读取，"
                                f"写入 {output_name}）")
        else:
            input_label.setText("标准输入")
        self.input_label = input_label

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText(
            "程序从这个文件读取的内容。留空表示空文件。"
            if file_mode else
            "程序的标准输入内容。留空表示没有输入。"
        )
        self.input_edit.setFont(owner.code_editor.font())

        self.run_button = QPushButton("运行")
        self.run_button.setProperty("variant", "primary")
        self.run_button.clicked.connect(self._run)

        self.status_label = QLabel("准备就绪")
        self.status_label.setProperty("muted", True)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        header = QHBoxLayout()
        header.addWidget(self.run_button)
        header.addWidget(self.status_label, 1)

        self.output = OutputView(owner.palette)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*DIALOG_MARGINS)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(input_label)
        layout.addWidget(self.input_edit, 1)
        layout.addLayout(header)
        layout.addWidget(QLabel("运行输出" if not file_mode else "运行输出（文件模式以输出文件为准）"))
        layout.addWidget(self.output, 2)
        layout.addWidget(buttons)

    def _run(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.run_button.setEnabled(False)
        self.status_label.setText("编译并运行中…")
        self.output.clear_output()

        worker = TestRunWorker(
            self._code,
            self._language,
            self._owner.ctx.compiler_paths(),
            self._owner.ctx.paths.work_dir,
            self.input_edit.toPlainText(),
            judge=self._judge,
            name=self._task_name,
            # 与提交保持同一套编译参数，否则自测的耗时没有参考价值
            optimize=self._owner.o2_check.isChecked(),
            parent=self,
        )
        worker.done.connect(self._on_done)
        worker.crashed.connect(self._on_crashed)
        worker.finished.connect(lambda: self.run_button.setEnabled(True))
        self._worker = worker
        worker.start()

    def _on_done(self, result) -> None:
        p = self._owner.palette
        color = p.verdict_color(result.verdict.value)
        self.output.append_line(f"状态：{result.verdict.text}", color, bold=True)
        detail = result.format_detail()
        if detail:
            self.output.append_block(detail, p.text_muted)
        self.output.append_line("─" * 40, p.border)
        if result.stdout:
            self.output.append_block(result.stdout, p.text)
        elif result.ok:
            self.output.append_line("（程序没有输出）", p.text_muted)

        metrics = []
        if result.time_ms:
            metrics.append(f"{result.time_ms:.1f} ms")
        if result.memory_mb:
            metrics.append(f"{result.memory_mb:.1f} MB")
        self.status_label.setText("运行完成 · " + " / ".join(metrics) if metrics else "运行完成")

    def _on_crashed(self, message: str) -> None:
        self.output.append_line(f"运行失败：{message}", self._owner.palette.danger, bold=True)
        self.status_label.setText("运行失败")

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        super().reject()
