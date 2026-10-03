"""「导出成绩单 / 提交明细」对话框。

一次导出要问清三件事：**导什么**（成绩单 / 提交明细）、**导成什么格式**、
**导到哪个文件**。三件都问完才让点「导出」。

几个刻意的选择
--------------
* **格式选项是"格式 + 用途"写在一起**（``xlsx（Excel 工作簿）``），不是光秃秃的
  ``xlsx``：这个程序的用户是老师，不是每天跟文件扩展名打交道的人。
* **「完整档案包」是单独一项**，不是"再勾一个复选框"。它产出的不是一份表格，
  而是一个 zip（表格 + 每人一个源码文件），把它混在格式列表里和别的东西并列
  会让人以为"选了 zip 就没有表格了"。
* 默认横向——成绩单的列数随题目数增长，纵向装不下。
* **文件名默认填好**（场次时间 + 场次名），老师改都不用改就能导。这是
  "少一次输入"与"少一次导错"两件事里性价比最高的一处。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import export as ex
from ..core.export import ExportData
from .theme import ROW_SPACING, TIGHT_SPACING

#: 下拉框里的每一项：``(显示名, 键, 说明)``。
#: ``键`` 只用来说明产物 —— ``bundle`` 走档案包，其余走 ``ex.export`` 的格式名。
EXPORT_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("txt（纯文本，直接看）", "txt", "txt"),
    ("csv（表格，Excel 可直接打开）", "csv", "csv"),
    ("xlsx（Excel 工作簿）", "xlsx", "xlsx"),
    ("docx（Word 文档）", "docx", "docx"),
    ("pdf（PDF，横向 A4）", "pdf", "pdf"),
    ("zip（完整档案包：表格 + 每人源码）", "bundle", "zip"),
)

#: 每个键对应的文件后缀与筛选器（档案包单独一个）。
SUFFIX = {"txt": ".txt", "csv": ".csv", "xlsx": ".xlsx", "docx": ".docx",
          "pdf": ".pdf", "bundle": ".zip"}
FILTERS = {
    "txt": "文本文件 (*.txt)", "csv": "CSV 表格 (*.csv)",
    "xlsx": "Excel 工作簿 (*.xlsx)", "docx": "Word 文档 (*.docx)",
    "pdf": "PDF 文件 (*.pdf)", "bundle": "ZIP 压缩包 (*.zip)",
}


class ExportDialog(QDialog):
    """问清「导什么 / 导成什么 / 导到哪」。"""

    def __init__(self, data: ExportData, *, default_directory: str | Path = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出")
        self.setMinimumWidth(520)
        self._data = data

        layout = QVBoxLayout(self)
        layout.setSpacing(TIGHT_SPACING)

        # -- 一句话说清导的是哪一场 --------------------------------
        # 回看两年前的档案时，这个框是唯一提示"你现在导的不是刚考完那场"的地方
        which = QLabel(f"{data.title or '未命名场次'}"
                       f" · {len(data.submissions)} 份提交")
        which.setProperty("role", "strong")
        which.setWordWrap(True)
        layout.addWidget(which)

        if not data.keep_code:
            note = QLabel("这份档案没有保存源码，导出内容里不会包含代码。")
            note.setProperty("muted", True)
            note.setWordWrap(True)
            layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(ROW_SPACING)

        section_box = QWidget()
        section_layout = QVBoxLayout(section_box)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(2)
        self.scores_check = QCheckBox("成绩单（一行一人，含各题得分与名次）")
        self.scores_check.setChecked(True)
        self.submissions_check = QCheckBox("提交明细（一行一次提交）")
        self.submissions_check.setChecked(True)
        section_layout.addWidget(self.scores_check)
        section_layout.addWidget(self.submissions_check)
        form.addRow("导出内容", section_box)

        self.format_combo = QComboBox()
        for label, key, _hint in EXPORT_CHOICES:
            self.format_combo.addItem(label, key)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("格式", self.format_combo)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("导出到哪个文件")
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(TIGHT_SPACING)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.browse_button)
        form.addRow("保存到", path_row)

        layout.addLayout(form)

        self.hint = QLabel()
        self.hint.setProperty("muted", True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("导出")
        self.buttons.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._directory = Path(default_directory) if default_directory else Path.cwd()
        self._refresh_default_path()
        self._on_format_changed()

    # -- 取值 -------------------------------------------------------------

    def selected_sections(self) -> list[str]:
        """勾了哪几项。**一项都没勾时返回空列表**，由调用方拦下。"""
        picked = []
        if self.scores_check.isChecked():
            picked.append(ex.SECTION_SCORES)
        if self.submissions_check.isChecked():
            picked.append(ex.SECTION_SUBMISSIONS)
        return picked

    def selected_format(self) -> str:
        return str(self.format_combo.currentData())

    def target(self) -> Path:
        return Path(self.path_edit.text().strip())

    # -- 交互 -------------------------------------------------------------

    def _on_format_changed(self) -> None:
        key = self.selected_format()
        if key == "bundle":
            self.hint.setText("档案包里有一份表格、一份纯文本，"
                              "以及 sources/ 下每人一个源码文件。")
        elif key == "csv":
            self.hint.setText("CSV 每个内容一项一个文件 —— "
                              "两张表塞进一个文件里，就没有一列是机器读得懂的。")
        elif key == "pdf":
            self.hint.setText("PDF 用系统字体渲染，中文不需要额外装字体。")
        else:
            self.hint.setText("")
        # 「完整档案包」本身就含两张表，内容勾选对它没有意义
        for widget in (self.scores_check, self.submissions_check):
            widget.setEnabled(key != "bundle")
        self._refresh_default_path()

    def _refresh_default_path(self) -> None:
        """按当前格式把默认文件名补/改后缀。用户改过路径就只换后缀。"""
        suffix = SUFFIX[self.selected_format()]
        current = self.path_edit.text().strip()
        stem = self._data and ex.default_stem(self._data)
        if not current:
            self.path_edit.setText(str(self._directory / f"{stem}{suffix}"))
            return
        path = Path(current)
        if path.suffix.lower() != suffix:
            self.path_edit.setText(str(path.with_suffix(suffix)))

    def _browse(self) -> None:
        key = self.selected_format()
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出到", str(self.target() or self._directory), FILTERS[key])
        if chosen:
            self.path_edit.setText(chosen)

    def accept(self) -> None:
        """没勾内容、没填路径都不许过 —— 早点说，别等导出完才发现是空表。"""
        if self.selected_format() != "bundle" and not self.selected_sections():
            self.hint.setText("至少要勾一项导出内容。")
            return
        if not self.path_edit.text().strip():
            self.hint.setText("请先选一个保存位置。")
            return
        target = self.target()
        if target.is_dir():
            self.hint.setText("保存位置是一个目录，得写到具体文件。")
            return
        super().accept()


__all__ = ["EXPORT_CHOICES", "FILTERS", "SUFFIX", "ExportDialog"]
