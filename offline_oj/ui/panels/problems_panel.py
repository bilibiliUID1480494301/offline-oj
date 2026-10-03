"""存题模块。

左侧是题库浏览（搜索、新建、删除、随机抽题、导入导出），
右侧是当前题目的编辑区（ID / 标题 / 限制 / Markdown 描述 / 测试点 / 判题方式）。

「判题方式」是后加的：输入输出可以走标准流也可以走题目指定的文件，判定可以交给
一道自定义校验器（用于答案不唯一、允许浮点误差的题）。两项都默认关闭，因此
不碰它们的题目存出来的 JSON 与老版本逐字节一致。

原实现把"编辑器内容"和"当前题目"直接绑在控件上，切换题目时会丢未保存的修改。
这里引入了脏标记与保存确认，避免辛苦写的题被一次误点冲掉。
"""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.models import (
    CHECKER_LANGUAGES,
    DEFAULT_INPUT_FILE,
    DEFAULT_OUTPUT_FILE,
    IOMode,
    JudgeConfig,
    Language,
    Problem,
    TASK_NAME_PLACEHOLDER,
    sanitize_slug,
)
from ...core.repository import OVERWRITE, RENAME, SKIP
from ...win32 import process as win_process
from ..theme import (
    DIALOG_MARGINS,
    GROUP_MARGINS,
    PAGE_MARGINS,
    ROW_SPACING,
    Palette,
    set_dynamic_property,
)
from ..widgets import CodeEditor, MarkdownView, TestCaseRows
from ..workers import ExportWorker, ImportWorker
from .base import Panel

TITLE_ROLE = Qt.UserRole + 1


def _friendly_time(stamp: str) -> str:
    """把落盘的 ISO 时间戳改成人看的样子。

    ``Problem.updated_at`` 存的是 ``2026-09-14T22:13:17`` —— 直接显示出来，
    那个 ``T`` 是 ISO 8601 的分隔符，只在机器之间有意义。界面上把它换成空格。
    """
    return stamp[:19].replace("T", " ")


class ProblemsPanel(Panel):
    """题目管理。"""

    #: 题库发生变化（增删改导入），其它面板需要刷新
    problems_changed = Signal()

    #: 请求切换到写题模块并打开某道题
    request_solve = Signal(str)

    def _build(self) -> None:
        self.current_id: str | None = None
        self._dirty = False
        self._loading = False
        self._import_worker: ImportWorker | None = None
        self._export_worker: ExportWorker | None = None

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_browser())
        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 980])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.addWidget(splitter)

        self.refresh_list()
        self.new_problem(confirm=False)

    # ------------------------------------------------------------------
    # 左侧：题库列表
    # ------------------------------------------------------------------

    def _build_browser(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(ROW_SPACING)

        title = QLabel("题库")
        title.setProperty("role", "h2")

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索题号 / 标题 / 来源")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.refresh_list)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_double_clicked)

        # 这里原来还有一个「共 N 道题 · M 个测试点 · 最近更新 …」的统计标签，
        # 和状态栏右下角那个一模一样 —— 同一个字符串一屏上出现两次。
        # 状态栏那个每页都在、随时可见，所以留下它，删掉这一个；
        # 顺便左列也少一行，按钮不必再往窗口底部挤。

        # 三行按钮各管一件事，不再和别处重复：
        #   增删题目 / 练习取题 / 与外部交换题目。
        # 原来第一行是「新建 / 保存 / 删除」，而编辑器右下角已经有一个更显眼的
        # 主按钮「保存题目」，同一个动作在一屏上有两个入口，删掉左列那个。
        row1 = QHBoxLayout()
        for text, slot, tip in (
            ("新建", lambda: self.new_problem(), "新建一道题 (Ctrl+N)"),
            ("删除", lambda: self.delete_problem(), "删除当前题目"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            if text == "删除":
                button.setProperty("variant", "danger")
            row1.addWidget(button)

        row2 = QHBoxLayout()
        random_button = QPushButton("随机抽题")
        random_button.setToolTip("在题库中随机选一道题，用于抽题练习")
        random_button.clicked.connect(self.random_problem)
        solve_button = QPushButton("去解题")
        solve_button.setToolTip("切到「写题模块」用当前题目练习")
        solve_button.clicked.connect(self._open_in_solver)
        row2.addWidget(random_button)
        row2.addWidget(solve_button)

        # 导入 / 导出各挂一个菜单：原来"导入题目…/批量导入…"四个按钮铺成两行，
        # 而这一列本身很窄，"批量导入…" 被挤到换行，看着像放错了地方。
        # 单题与批量本来就是同一个动作的两种范围，收进一个按钮里更清楚。
        def menu_button(text: str, tip: str, entries) -> QPushButton:
            button = QPushButton(text)
            button.setToolTip(tip)
            menu = QMenu(button)
            for label, slot in entries:
                menu.addAction(label, slot)
            button.setMenu(menu)
            return button

        row3 = QHBoxLayout()
        row3.addWidget(menu_button("导入", "把题目从文件或压缩包读进来", (
            ("导入单题…", self.import_single),
            ("批量导入（ZIP / 目录）…", self.batch_import),
        )))
        row3.addWidget(menu_button("导出", "把题目导出成文件或压缩包", (
            ("导出当前题目…", self.export_current),
            ("批量导出…", self.batch_export),
        )))

        layout.addWidget(title)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        return container

    # ------------------------------------------------------------------
    # 右侧：编辑区
    # ------------------------------------------------------------------

    def _build_editor(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)

        # ---- 基本信息 ----
        info_group = QGroupBox("题目信息")
        form = QFormLayout(info_group)
        form.setContentsMargins(*GROUP_MARGINS)
        form.setSpacing(ROW_SPACING)

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("例如 P0001（字母 / 数字 / 下划线 / 中文）")
        self.id_edit.textChanged.connect(self._mark_dirty)
        # 英文名留空时是按题目 ID 推导的，改 ID 得同步刷新那个提示
        self.id_edit.textChanged.connect(self._refresh_name_hint)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("题目标题")
        self.title_edit.textChanged.connect(self._mark_dirty)

        # 题目英文名（CCF 规约）：决定源程序名、可执行文件名、数据文件名与每题的子目录名。
        # 它旁边挂一个实时提示，告诉用户"实际会用哪个名字"——留空时是按题目 ID 推导的，
        # 而中文题目 ID 洗出来只剩兜底值 task，不提示的话根本看不出来。
        self.slug_edit = QLineEdit()
        self.slug_edit.setPlaceholderText("例如 poker（留空按题目 ID 推导）")
        self.slug_edit.setToolTip(
            "CCF 规约：小写英文字母 / 数字 / 下划线。\n"
            "题目 poker 对应源程序 poker.cpp、可执行文件 poker、数据文件 poker.in / poker.out。"
        )
        self.slug_edit.textChanged.connect(self._on_slug_changed)

        self.slug_hint = QLabel()
        self.slug_hint.setProperty("muted", True)

        slug_row = QWidget()
        slug_layout = QHBoxLayout(slug_row)
        slug_layout.setContentsMargins(0, 0, 0, 0)
        slug_layout.setSpacing(ROW_SPACING)
        slug_layout.addWidget(self.slug_edit, 3)
        slug_layout.addWidget(self.slug_hint, 2)

        self.time_spin = QSpinBox()
        self.time_spin.setRange(100, 60000)
        self.time_spin.setSingleStep(100)
        self.time_spin.setSuffix(" ms")
        self.time_spin.setValue(1000)
        self.time_spin.valueChanged.connect(self._mark_dirty)

        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(16, 4096)
        self.memory_spin.setSingleStep(16)
        self.memory_spin.setSuffix(" MB")
        self.memory_spin.setValue(256)
        self.memory_spin.valueChanged.connect(self._mark_dirty)

        # 时间与内存并成一行。两个都是"数字 + 单位"的小框，各占一行会把
        # 「题目信息」撑成 7 行 —— 而它上面每一行都在和下面的描述区、测试点抢
        # 高度。并成一行后整个编辑区顶部矮一档，描述区能多显示三四行正文。
        limits_row = QWidget()
        limits_layout = QHBoxLayout(limits_row)
        limits_layout.setContentsMargins(0, 0, 0, 0)
        limits_layout.setSpacing(ROW_SPACING)
        limits_layout.addWidget(self.time_spin, 1)
        limits_layout.addWidget(self.memory_spin, 1)

        self.case_count_spin = QSpinBox()
        self.case_count_spin.setRange(1, 100)
        self.case_count_spin.setValue(1)
        self.case_count_spin.setToolTip("调整测试点数量（已有内容会尽量保留）")
        self.case_count_spin.valueChanged.connect(self._on_case_count_changed)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("可选：题目来源")
        self.source_edit.textChanged.connect(self._mark_dirty)

        form.addRow("题目 ID", self.id_edit)
        form.addRow("标题", self.title_edit)
        form.addRow("英文名", slug_row)
        form.addRow("时间 / 内存限制", limits_row)
        form.addRow("测试点数", self.case_count_spin)
        form.addRow("来源", self.source_edit)

        # ---- 描述 ----
        description_group = QGroupBox("题目描述（支持 Markdown）")
        description_layout = QVBoxLayout(description_group)
        description_layout.setContentsMargins(*GROUP_MARGINS)
        description_layout.setSpacing(ROW_SPACING)

        toolbar = QHBoxLayout()
        for text, slot, tip in (
            ("插入图片", self.add_image, "把图片复制到资源目录并插入引用"),
            ("代码块", self.add_code_block, "插入 ``` 代码块模板"),
            ("数学公式", self.add_formula, "插入公式语法示例"),
            ("预览", self.preview_description, "以 Markdown 渲染预览"),
        ):
            button = QPushButton(text)
            button.setProperty("flat", True)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        toolbar.addStretch(1)

        self.description_editor = CodeEditor(
            self.palette, line_numbers=False, completion=False
        )
        # 用 connect_content_changed 而不是 textChanged：后者在语法高亮重排
        # （换主题、切语言）时也会响，等于"换一次主题 = 改一次正文"，
        # 面板会被标成有未保存的修改，关窗口时弹出保存确认
        self.description_editor.connect_content_changed(self._mark_dirty)
        self.description_editor.setMinimumHeight(140)

        description_layout.addLayout(toolbar)
        description_layout.addWidget(self.description_editor, 1)

        # ---- 测试点 ----
        cases_group = QGroupBox("测试点")
        cases_layout = QVBoxLayout(cases_group)
        cases_layout.setContentsMargins(*GROUP_MARGINS)
        self.cases = TestCaseRows(self.palette)
        self.cases.changed.connect(self._on_cases_changed)
        cases_layout.addWidget(self.cases)

        # ---- 校验器源码（没勾校验器时整块收起来） ----
        checker_group = self._build_checker_group()

        vertical = QSplitter(Qt.Vertical)
        vertical.addWidget(description_group)
        vertical.addWidget(cases_group)
        vertical.addWidget(checker_group)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        # 校验器源码按自身高度显示即可，不参与拉伸；
        # 它是隐藏的时候 QSplitter 会把这一栏压成 0，不占地方
        vertical.setStretchFactor(2, 0)
        self.editor_splitter = vertical

        # ---- 底部操作 ----
        self.hint_label = QLabel("尚未保存")
        self.hint_label.setProperty("muted", True)

        reset_button = QPushButton("撤销修改")
        reset_button.setToolTip("放弃未保存的修改，重新载入")
        reset_button.clicked.connect(self._reload_current)

        self.save_button = QPushButton("保存题目")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save_problem)

        footer = QHBoxLayout()
        footer.addWidget(self.hint_label, 1)
        footer.addWidget(reset_button)
        footer.addWidget(self.save_button)

        # 题目信息与判题方式并排：两边都是"几个下拉框和输入框"，
        # 叠起来会把描述区挤没，横着分两栏高度刚好齐平
        #
        # 判题方式那栏要**包一层再撑开**：直接放进横向布局的话，QGroupBox
        # 会被拉到和「题目信息」一样高，而它内容少一行 —— 于是框里空出
        # 一大片灰底，看着像"这里本来该有东西但没加载出来"。
        # 用一层纵向布局加 addStretch，框保持自身高度，剩下的留白落在框外。
        judge_column = QWidget()
        judge_column_layout = QVBoxLayout(judge_column)
        judge_column_layout.setContentsMargins(0, 0, 0, 0)
        judge_column_layout.setSpacing(ROW_SPACING)
        judge_column_layout.addWidget(self._build_judge_group())
        judge_column_layout.addStretch(1)

        top = QHBoxLayout()
        top.setSpacing(ROW_SPACING)
        top.addWidget(info_group, 3)
        top.addWidget(judge_column, 2)

        layout.addLayout(top)
        layout.addWidget(vertical, 1)
        layout.addLayout(footer)

        # 判题方式那栏已经建好了，这时才能刷新数据文件名的占位符
        self._refresh_name_hint()

        self._sync_judge_widgets()
        return container

    # ------------------------------------------------------------------
    # 题目英文名（CCF 规约）
    # ------------------------------------------------------------------

    def effective_name(self) -> str:
        """当前表单实际会用的题目英文名：填了就用，留空按题目 ID 推导。"""
        return sanitize_slug(self.slug_edit.text().strip() or self.id_edit.text().strip())

    def _on_slug_changed(self) -> None:
        self._refresh_name_hint()
        self._mark_dirty()

    def _refresh_name_hint(self) -> None:
        """更新「英文名」右侧的提示，以及文件模式那两个输入框的占位符。

        占位符跟着题目名走是有意义的：留空时输入框里显示 ``poker.in`` 而不是
        ``{name}.in``，用户能一眼确认"数据文件到底叫什么"。
        """
        if not hasattr(self, "input_file_edit"):
            return  # 构建期：判题方式那一栏还没建出来
        raw = self.slug_edit.text().strip()
        name = self.effective_name()
        if not raw:
            text = f"留空，按题目 ID 推导为 {name}"
        elif raw != name:
            text = f"含非法字符，将清洗为 {name}"
        else:
            text = f"源程序 {name}.cpp · 数据文件 {name}.in / {name}.out"
        self.slug_hint.setText(text)
        self.slug_hint.setToolTip(f"实际使用的题目英文名：{name}")
        self.input_file_edit.setPlaceholderText(
            DEFAULT_INPUT_FILE.replace(TASK_NAME_PLACEHOLDER, name))
        self.output_file_edit.setPlaceholderText(
            DEFAULT_OUTPUT_FILE.replace(TASK_NAME_PLACEHOLDER, name))

    def _build_judge_group(self) -> QGroupBox:
        """判题方式：输入输出走哪里、用不用自定义校验器。

        这两项都做成**可选**的，默认值与老题库完全一致（标准输入输出 + 精确比对）。
        校验器源码不在这里 —— 它需要的是纵向空间，放在下面的分栏里。
        """
        group = QGroupBox("判题方式")
        form = QFormLayout(group)
        form.setContentsMargins(*GROUP_MARGINS)
        form.setSpacing(ROW_SPACING)

        self.io_mode_combo = QComboBox()
        for mode in IOMode:
            self.io_mode_combo.addItem(mode.display, mode.value)
        self.io_mode_combo.setToolTip(
            "标准输入输出：数据走 stdin/stdout（多数题目的做法）\n"
            "文件输入输出：题目指定文件名，程序读写工作目录下的文件"
        )
        self.io_mode_combo.currentIndexChanged.connect(self._on_io_mode_changed)

        self.input_file_edit = QLineEdit()
        self.input_file_edit.setPlaceholderText(DEFAULT_INPUT_FILE)
        self.input_file_edit.setToolTip("程序要读取的输入文件名（相对运行目录，不要带路径）")
        self.input_file_edit.textChanged.connect(self._mark_dirty)

        self.output_file_edit = QLineEdit()
        self.output_file_edit.setPlaceholderText(DEFAULT_OUTPUT_FILE)
        self.output_file_edit.setToolTip("程序要写入的输出文件名（相对运行目录，不要带路径）")
        self.output_file_edit.textChanged.connect(self._mark_dirty)

        self.checker_check = QCheckBox("使用自定义校验器（特殊判题）")
        self.checker_check.setToolTip(
            "答案不唯一、允许浮点误差、与顺序无关的题目需要挂一个校验器程序。\n"
            "不勾选时校验器内容不会随题目保存。"
        )
        self.checker_check.toggled.connect(self._on_checker_toggled)

        self.checker_language_combo = QComboBox()
        for language in CHECKER_LANGUAGES:
            self.checker_language_combo.addItem(language.display, language.value)
        self.checker_language_combo.setToolTip("校验器用什么语言编写")
        self.checker_language_combo.currentIndexChanged.connect(
            self._on_checker_language_changed)

        form.addRow("输入输出方式", self.io_mode_combo)
        form.addRow("输入文件名", self.input_file_edit)
        form.addRow("输出文件名", self.output_file_edit)
        form.addRow("", self.checker_check)
        form.addRow("校验器语言", self.checker_language_combo)
        return group

    def _build_checker_group(self) -> QGroupBox:
        """校验器源码 + 协议说明；整块随复选框显示 / 隐藏。"""
        group = QGroupBox("校验器源码")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(*GROUP_MARGINS)
        layout.setSpacing(ROW_SPACING)

        self.checker_editor = CodeEditor(self.palette, line_numbers=True, completion=True)
        self.checker_editor.setMinimumHeight(90)
        self.checker_editor.connect_content_changed(self._mark_dirty)
        layout.addWidget(self.checker_editor, 1)

        protocol = QLabel(
            "校验器按顺序接收三个命令行参数：输入文件、选手输出、标准答案。"
            "退出码 0 = 答案正确，1 = 答案错误，2 = 格式错误；"
            "其它退出码、运行超时或编译失败一律记为评测机内部错误，不会算到选手头上。"
        )
        protocol.setProperty("muted", True)
        protocol.setWordWrap(True)
        layout.addWidget(protocol)

        self.checker_group = group
        return group

    # ------------------------------------------------------------------
    # 判题方式：状态同步
    # ------------------------------------------------------------------

    def _sync_judge_widgets(self) -> None:
        """按当前选择启用 / 显示相关控件（只有这一处改可见性）。"""
        file_mode = self.io_mode_combo.currentData() == IOMode.FILE.value
        checked = self.checker_check.isChecked()
        self.input_file_edit.setEnabled(file_mode)
        self.output_file_edit.setEnabled(file_mode)
        self.checker_language_combo.setEnabled(checked)
        self.checker_group.setVisible(checked)

    def _on_io_mode_changed(self) -> None:
        self._sync_judge_widgets()
        if self.io_mode_combo.currentData() == IOMode.FILE.value and not self._loading:
            # 切到文件模式时补上默认名：空着容易让人以为"这一栏不用填"
            if not self.input_file_edit.text().strip():
                self.input_file_edit.setText(DEFAULT_INPUT_FILE)
            if not self.output_file_edit.text().strip():
                self.output_file_edit.setText(DEFAULT_OUTPUT_FILE)
        self._mark_dirty()

    def _on_checker_toggled(self, checked: bool) -> None:
        self._sync_judge_widgets()
        if checked and not self._loading:
            self._grow_judge_pane()
            self.checker_editor.setFocus()
        self._mark_dirty()

    def _on_checker_language_changed(self) -> None:
        language = Language.from_value(str(self.checker_language_combo.currentData()))
        if language is not None:
            self.checker_editor.set_language(language)
        self._mark_dirty()

    def _grow_judge_pane(self) -> None:
        """刚显示出来的校验器编辑框，如果分栏还是塌着的就顺手撑开。

        否则用户勾上复选框后看到的是一条缝，还得自己去拖分栏。
        """
        sizes = self.editor_splitter.sizes()
        if len(sizes) != 3 or sizes[2] >= 220:
            return
        delta = 220 - sizes[2]
        sizes[0] = max(140, sizes[0] - delta // 2)
        sizes[1] = max(140, sizes[1] - delta // 2)
        sizes[2] = 220
        self.editor_splitter.setSizes(sizes)

    # ------------------------------------------------------------------
    # 列表与选中
    # ------------------------------------------------------------------

    def refresh_list(self) -> None:
        """按搜索框内容重建列表，并尽量保持当前选中的题目。"""
        keyword = self.search_edit.text() if hasattr(self, "search_edit") else ""
        problems = self.ctx.repository.search(keyword)

        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for problem in problems:
            item = QListWidgetItem(f"{problem.id}  ·  {problem.title}")
            item.setData(TITLE_ROLE, problem.id)
            item.setToolTip(problem.summary())
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

        # 恢复选中
        target = self.current_id
        for row in range(self.list_widget.count()):
            if self.list_widget.item(row).data(TITLE_ROLE) == target:
                self.list_widget.setCurrentRow(row)
                break

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        problem_id = current.data(TITLE_ROLE)
        if problem_id == self.current_id:
            return
        if not self._confirm_discard():
            # 用户取消：把选中状态退回去
            self.refresh_list()
            return
        self.load_problem(problem_id)

    def _on_double_clicked(self, item: QListWidgetItem) -> None:
        self.request_solve.emit(item.data(TITLE_ROLE))

    # ------------------------------------------------------------------
    # 题目操作
    # ------------------------------------------------------------------

    def new_problem(self, *, confirm: bool = True) -> None:
        if confirm and not self._confirm_discard():
            return
        new_id = self.ctx.repository.next_id()
        self.current_id = None
        self._loading = True
        self.id_edit.setText(new_id)
        self.title_edit.setText("")
        self.slug_edit.setText("")
        self.description_editor.setPlainText("")
        self.source_edit.setText("")
        self.time_spin.setValue(1000)
        self.memory_spin.setValue(256)
        self.cases.set_count(1)
        self.case_count_spin.setValue(1)
        self.io_mode_combo.setCurrentIndex(0)
        self.input_file_edit.setText(DEFAULT_INPUT_FILE)
        self.output_file_edit.setText(DEFAULT_OUTPUT_FILE)
        self.checker_check.setChecked(False)
        self.checker_language_combo.setCurrentIndex(0)
        self.checker_editor.setPlainText("")
        self._sync_judge_widgets()
        self._loading = False
        self._set_dirty(True)
        self.hint_label.setText("新题目，尚未保存")
        self.list_widget.clearSelection()
        self.status(f"已新建题目 {new_id}，填写后点「保存题目」")

    def load_problem(self, problem_id: str) -> None:
        problem = self.ctx.repository.get(problem_id)
        if problem is None:
            self.status(f"题目不存在: {problem_id}")
            return

        self._loading = True
        self.current_id = problem_id
        self.id_edit.setText(problem.id)
        self.title_edit.setText(problem.title)
        self.slug_edit.setText(problem.slug)
        self.source_edit.setText(problem.source)
        self.description_editor.setPlainText(problem.description)
        self.time_spin.setValue(problem.time_limit)
        self.memory_spin.setValue(problem.memory_limit)
        self.cases.load(problem.testcases)
        self.case_count_spin.setValue(self.cases.count())

        # 先定输入输出方式，再填文件名 —— 反过来的话，切到文件模式时的
        # "补默认名"会盖掉刚填进去的题目配置
        judge = problem.judge
        self.io_mode_combo.setCurrentIndex(
            max(0, self.io_mode_combo.findData(judge.io_mode.value)))
        self.input_file_edit.setText(judge.input_file)
        self.output_file_edit.setText(judge.output_file)
        self.checker_check.setChecked(judge.uses_checker)
        configured = judge.checker_language or CHECKER_LANGUAGES[0]
        self.checker_language_combo.setCurrentIndex(
            max(0, self.checker_language_combo.findData(configured.value)))
        self.checker_editor.set_language(configured)
        self.checker_editor.setPlainText(judge.checker)
        self._sync_judge_widgets()
        self._loading = False

        self._set_dirty(False)
        self.hint_label.setText(
            f"已载入 {problem.id}（更新于 {_friendly_time(problem.updated_at)}）")
        self.status(f"已载入题目 {problem.id}")

    def _collect_judge(self) -> JudgeConfig:
        """把「判题方式」一栏收成配置。

        没勾校验器时，语言与源码一起丢掉：:meth:`JudgeConfig.to_dict` 靠
        "是否全为默认"来决定要不要落盘，留下半截配置只会让题目 JSON 多出空壳字段。
        """
        use_checker = self.checker_check.isChecked()
        language = Language.from_value(str(self.checker_language_combo.currentData()))
        return JudgeConfig(
            io_mode=IOMode.from_value(self.io_mode_combo.currentData()),
            input_file=self.input_file_edit.text().strip() or DEFAULT_INPUT_FILE,
            output_file=self.output_file_edit.text().strip() or DEFAULT_OUTPUT_FILE,
            checker_language=language if use_checker else None,
            checker=self.checker_editor.toPlainText() if use_checker else "",
        )

    def _collect(self) -> Problem:
        return Problem(
            id=self.id_edit.text().strip(),
            title=self.title_edit.text().strip(),
            description=self.description_editor.toPlainText(),
            time_limit=self.time_spin.value(),
            memory_limit=self.memory_spin.value(),
            testcases=self.cases.values(),
            source=self.source_edit.text().strip(),
            slug=self.slug_edit.text().strip(),
            judge=self._collect_judge(),
        )

    def save_problem(self) -> None:
        problem = self._collect()
        issues = problem.validate()
        if issues:
            QMessageBox.warning(
                self, "无法保存",
                "请先修正以下问题：\n\n" + "\n".join(f"· {item}" for item in issues),
            )
            return

        previous_id = self.current_id
        exists = problem.id in self.ctx.repository

        if exists and problem.id != previous_id:
            answer = QMessageBox.question(
                self, "题目 ID 冲突",
                f"题目 {problem.id} 已存在，是否覆盖它？\n\n"
                "选择「否」将自动改用一个新的 ID 保存。",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.No,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.No:
                problem.id = self.ctx.repository.next_id()
                self.id_edit.setText(problem.id)

        # ID 被改过：先删掉旧键，避免留下孤儿题
        if previous_id and previous_id != problem.id:
            self.ctx.repository.remove(previous_id)

        try:
            self.ctx.repository.save()  # 先落盘旧状态，再改内存
            final_id = self.ctx.repository.put(problem, strategy=OVERWRITE)
            self.ctx.repository.save()
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"写入题库失败：\n{exc}")
            return

        self.current_id = final_id
        self._set_dirty(False)
        self.refresh_list()
        self.problems_changed.emit()
        self.hint_label.setText(f"已保存 {final_id} · {len(problem.testcases)} 个测试点")
        self.status(f"题目 {final_id} 已保存")

    def delete_problem(self) -> None:
        if not self.current_id:
            self.notify.emit("info", "当前没有选中已保存的题目。")
            return
        problem = self.ctx.repository.get(self.current_id)
        if problem is None:
            return
        answer = QMessageBox.question(
            self, "删除题目",
            f"确认删除题目「{problem.id} · {problem.title}」？\n\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.ctx.repository.remove(self.current_id)
        self.ctx.repository.save()
        self.status(f"已删除题目 {self.current_id}")
        self.current_id = None
        self.refresh_list()
        self.problems_changed.emit()
        if self.ctx.repository:
            first = self.ctx.repository.all()[0]
            self.load_problem(first.id)
        else:
            self.new_problem(confirm=False)

    def random_problem(self) -> None:
        problems = self.ctx.repository.all()
        if not problems:
            self.notify.emit("info", "题库还是空的，先新建一道题吧。")
            return
        if not self._confirm_discard():
            return
        picked = random.choice(problems)
        self.load_problem(picked.id)
        for row in range(self.list_widget.count()):
            if self.list_widget.item(row).data(TITLE_ROLE) == picked.id:
                self.list_widget.setCurrentRow(row)
                break
        self.status(f"随机抽到 {picked.id} · {picked.title}")

    def _reload_current(self) -> None:
        if not self.current_id:
            self.new_problem(confirm=False)
            return
        self.load_problem(self.current_id)

    def _open_in_solver(self) -> None:
        if not self.current_id:
            self.notify.emit("info", "先保存题目，再去解题。")
            return
        self.request_solve.emit(self.current_id)

    # ------------------------------------------------------------------
    # 描述编辑辅助
    # ------------------------------------------------------------------

    def add_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp *.svg);;所有文件 (*)"
        )
        if not filename:
            return
        try:
            resource_name = self.ctx.repository.import_resource(filename)
        except Exception as exc:
            QMessageBox.critical(self, "插入图片失败", str(exc))
            return
        self.description_editor.insertPlainText(f"![图片]({resource_name})")
        self.status(f"图片已复制到资源目录：{resource_name}")

    def add_code_block(self) -> None:
        self.description_editor.insertPlainText("\n```cpp\n// 在此输入代码\n```\n")

    def add_formula(self) -> None:
        self.description_editor.insertPlainText(
            "\n行内公式：$a^2 + b^2 = c^2$\n\n块级公式：\n$$\n\\sum_{i=1}^{n} a_i\n$$\n"
        )

    def preview_description(self) -> None:
        text = self.description_editor.toPlainText()
        if not text.strip():
            self.notify.emit("info", "描述还是空的。")
            return

        dialog = _preview_widget(
            self,
            text,
            self.ctx.paths.image_search_paths(),
            self.ctx.settings.bool("markdown_render"),
        )
        dialog.exec()

    # ------------------------------------------------------------------
    # 单题导入导出
    # ------------------------------------------------------------------

    def import_single(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "导入题目", str(self.ctx.settings.get("last_import_dir", "") or ""),
            "JSON 题目文件 (*.json);;所有文件 (*)"
        )
        if not filename:
            return
        self.ctx.settings.set("last_import_dir", str(Path(filename).parent))

        ok, message = self.ctx.archive.import_single(filename, strategy=RENAME)
        if not ok:
            QMessageBox.critical(self, "导入失败", message)
            return
        self.ctx.repository.save()
        self.refresh_list()
        self.problems_changed.emit()
        self.status("题目已导入")

        imported_id = self._guess_imported_id(filename)
        if imported_id:
            self.load_problem(imported_id)

    def export_current(self) -> None:
        if not self.current_id:
            self.notify.emit("info", "请先选择一道已保存的题目。")
            return
        problem = self.ctx.repository.get(self.current_id)
        if problem is None:
            return

        default_dir = str(self.ctx.settings.get("last_export_dir", "") or "")
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出题目", str(Path(default_dir) / f"{problem.id}.json"),
            "JSON 题目文件 (*.json)"
        )
        if not filename:
            return
        self.ctx.settings.set("last_export_dir", str(Path(filename).parent))
        try:
            self.ctx.archive.export_single(problem, filename)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.status(f"已导出到 {filename}")
        self.notify.emit("info", f"题目已导出：\n{filename}")

    # ------------------------------------------------------------------
    # 批量导入导出
    # ------------------------------------------------------------------

    def batch_import(self) -> None:
        if self._busy():
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("批量导入")
        choice.setText("从 ZIP 压缩包还是文件夹导入？\n\n"
                       "包内结构应为 problems/*.json，图片放在 resources/ 下。")
        zip_button = choice.addButton("选择 ZIP", QMessageBox.AcceptRole)
        folder_button = choice.addButton("选择文件夹", QMessageBox.ActionRole)
        choice.addButton("取消", QMessageBox.RejectRole)
        choice.exec()

        clicked = choice.clickedButton()
        if clicked is zip_button:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择 ZIP 题目包", str(self.ctx.settings.get("last_import_dir", "") or ""),
                "ZIP 压缩包 (*.zip)")
            is_zip = True
        elif clicked is folder_button:
            path = QFileDialog.getExistingDirectory(
                self, "选择题目文件夹", str(self.ctx.settings.get("last_import_dir", "") or ""))
            is_zip = False
        else:
            return
        if not path:
            return

        strategy = self._ask_strategy()
        if strategy is None:
            return
        self.ctx.settings.set("last_import_dir", str(Path(path).parent if is_zip else path))

        self.status("正在批量导入…")
        self._set_busy(True)
        worker = ImportWorker(self.ctx.archive, path, is_zip=is_zip, strategy=strategy, parent=self)
        worker.progress.connect(lambda done, total, message: self.status(
            f"导入进度 {done}/{total}：{message}"))
        worker.done.connect(self._on_import_done)
        worker.crashed.connect(lambda message: QMessageBox.critical(self, "导入失败", message))
        worker.finished.connect(lambda: self._set_busy(False))
        self._import_worker = worker
        worker.start()

    def _on_import_done(self, report) -> None:
        self.ctx.repository.save()
        self.refresh_list()
        self.problems_changed.emit()
        self.status(f"批量导入完成：新增 {report.imported}，覆盖 {report.overwritten}")
        _ReportDialog(self, "批量导入结果", report.text()).exec()

    def batch_export(self) -> None:
        if self._busy():
            return
        problems = self.ctx.repository.all()
        if not problems:
            self.notify.emit("info", "题库为空，没有可导出的题目。")
            return

        choice = QMessageBox(self)
        choice.setWindowTitle("批量导出")
        choice.setText(f"将导出全部 {len(problems)} 道题。\n\n打包成单个 ZIP，还是写成文件夹？")
        zip_button = choice.addButton("导出为 ZIP", QMessageBox.AcceptRole)
        folder_button = choice.addButton("导出为文件夹", QMessageBox.ActionRole)
        choice.addButton("取消", QMessageBox.RejectRole)
        choice.exec()

        clicked = choice.clickedButton()
        if clicked is zip_button:
            path, _ = QFileDialog.getSaveFileName(
                self, "导出为 ZIP", str(Path(self.ctx.settings.get("last_export_dir", "") or "")
                                     / "problems_export.zip"), "ZIP 压缩包 (*.zip)")
            is_zip = True
        elif clicked is folder_button:
            path = QFileDialog.getExistingDirectory(
                self, "选择导出文件夹", str(self.ctx.settings.get("last_export_dir", "") or ""))
            is_zip = False
        else:
            return
        if not path:
            return
        self.ctx.settings.set("last_export_dir", str(Path(path).parent if is_zip else path))

        self.status("正在批量导出…")
        self._set_busy(True)
        worker = ExportWorker(self.ctx.archive, path, problems, is_zip=is_zip, parent=self)
        worker.progress.connect(lambda done, total, message: self.status(
            f"导出进度 {done}/{total}：{message}"))
        worker.done.connect(self._on_export_done)
        worker.crashed.connect(lambda message: QMessageBox.critical(self, "导出失败", message))
        worker.finished.connect(lambda: self._set_busy(False))
        self._export_worker = worker
        worker.start()

    def _on_export_done(self, count: int) -> None:
        self.status(f"已导出 {count} 道题目")
        target = getattr(self._export_worker, "_target", "") if self._export_worker else ""
        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setText(f"已导出 {count} 道题目。")
        box.setInformativeText(str(target))
        open_button = box.addButton("打开所在位置", QMessageBox.AcceptRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_button and target:
            win_process.reveal_in_explorer(target)

    def _ask_strategy(self) -> str | None:
        box = QMessageBox(self)
        box.setWindowTitle("ID 冲突处理")
        box.setText("导入时遇到已存在的题目 ID，希望怎么处理？")
        skip = box.addButton("跳过已存在", QMessageBox.AcceptRole)
        overwrite = box.addButton("覆盖已有", QMessageBox.DestructiveRole)
        rename = box.addButton("重命名新题目", QMessageBox.ActionRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            return SKIP
        if clicked is overwrite:
            return OVERWRITE
        if clicked is rename:
            return RENAME
        return None

    # ------------------------------------------------------------------
    # 内部状态
    # ------------------------------------------------------------------

    def _on_case_count_changed(self, value: int) -> None:
        if self._loading:
            return
        current = self.cases.values()
        preserved = current[:value] if value < len(current) else current
        self.cases.set_count(value, preserved)
        self._mark_dirty()

    def _on_cases_changed(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.case_count_spin.setValue(self.cases.count())
        self._loading = False
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        if self._loading:
            return
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        if dirty:
            self.hint_label.setText("● 有未保存的修改")
        # 颜色交给主题的 role="warn"，这里只管开关这个角色。
        # 以前是在这里和 apply_palette 里各写一遍 setStyleSheet —— 两处同步，
        # 改一处忘一处就会出现"切了主题颜色还是旧的"。
        set_dynamic_property(self.hint_label, "role", "warn" if dirty else None)

    def _confirm_discard(self) -> bool:
        """有未保存修改时询问。返回 ``True`` 表示可以继续。"""
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self, "尚未保存",
            "当前题目有未保存的修改，是否先保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            self.save_problem()
            # 保存过程可能因为校验失败而中断
            return not self._dirty
        return True

    def _guess_imported_id(self, filename: str) -> str | None:
        """导入单题后尽力找出落库的 ID。"""
        import json

        try:
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
            candidate = str(data.get("id", ""))
        except Exception:
            return None
        if candidate and candidate in self.ctx.repository:
            return candidate
        # 被重命名了：找最近更新的那道
        problems = sorted(self.ctx.repository.all(), key=lambda p: p.updated_at, reverse=True)
        return problems[0].id if problems else None

    def _busy(self) -> bool:
        for worker in (self._import_worker, self._export_worker):
            if worker is not None and worker.isRunning():
                self.status("已有导入/导出任务在进行，请稍候…")
                return True
        return False

    def _set_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def on_activated(self) -> None:
        self.refresh_list()

    def apply_palette(self, palette: Palette) -> None:
        super().apply_palette(palette)
        if hasattr(self, "description_editor"):
            self.description_editor.set_palette_theme(palette)
        if hasattr(self, "checker_editor"):
            self.checker_editor.set_palette_theme(palette)
        if hasattr(self, "cases"):
            self.cases.apply_palette(palette)
        # hint_label 不需要在这里重刷：它的颜色来自动态属性，
        # setStyleSheet 全量重刷时属性选择器会自己重新求值。

    def on_closing(self) -> bool:
        for worker in (self._import_worker, self._export_worker):
            if worker is not None and worker.isRunning():
                worker.wait(5000)
        return self._confirm_discard()


class _ReportDialog:
    """带滚动区域的批量操作报告。"""

    def __init__(self, parent, title: str, text: str) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        self._dialog = QDialog(parent)
        self._dialog.setWindowTitle(title)
        self._dialog.resize(720, 520)

        view = MarkdownView()
        view.setPlainText(text)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._dialog.accept)

        layout = QVBoxLayout(self._dialog)
        layout.setContentsMargins(*DIALOG_MARGINS)
        layout.addWidget(view, 1)
        layout.addWidget(buttons)

    def exec(self) -> None:
        self._dialog.exec()


def _preview_widget(parent, text: str, resource_dirs: list[str], enabled: bool):
    """构造一个独立的 Markdown 预览窗口。"""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox

    dialog = QDialog(parent)
    dialog.setWindowTitle("题目预览")
    dialog.resize(860, 640)

    view = MarkdownView()
    view.render_markdown(text, resource_dirs=resource_dirs, images_enabled=enabled)

    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(*DIALOG_MARGINS)
    layout.addWidget(view, 1)
    layout.addWidget(buttons)
    return dialog
