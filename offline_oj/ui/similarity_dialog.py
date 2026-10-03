"""「雷同检测」结果对话框。

一次分析给出的是**线索，不是结论** —— 这一点要在整个界面上体现出来，
而不只是写在某句小字里：

* 标题里不出现"抄袭""作弊"这类词。这个工具不判案，它只把"值得看一眼的
  几对"摆到老师面前；
* 顶部常驻一条复核提示（红色），**不可折叠**；
* 「完全重复」与「高度相似」分开列，措辞不同：前者是确定性的，
  后者只是启发式；
* 每一行都带具体数字（相似度 + 共享指纹数），老师能自己判断"这个 62% 是
  因为都用了快读，还是解题思路真的撞了"；
* 口径说明（分析时扣了什么、为什么有些题没扣）**就摆在结果下面**，
  老师不用点开菜单去找。

布局是"上面一张表（谁和谁像）、下面并排 diff（哪里像）"。选中一行就出 diff ——
这一步是从"数字"走到"证据"的唯一路径，所以它必须是一次点击的距离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core import similarity as sim
from ..core import similarity_export as sx
from .theme import GROUP_MARGINS, ROW_SPACING, mono_font

#: 结果表的列。顺序就是"老师念出来"的顺序：多像 → 什么性质 → 哪道题 → 谁。
COLUMNS = ("相似度", "性质", "题目", "选手 A", "选手 B", "备注")

#: 并排 diff 的列。
DIFF_COLUMNS = ("行", "选手 A", "行", "选手 B")

#: 行性质。这两个词要一直用到界面文案里，别写成同义词 ——
#: 「完全重复」是确定性的结论，「高度相似」是启发式的线索。
KIND_EXACT = "完全重复"
KIND_SIMILAR = "高度相似"

#: 复核提示。**不是免责声明**：同一道水题的朴素解法本来就长得一样，
#: 不加这句，老师会拿着 85% 去找一个没抄的人。
REVIEW_NOTICE = ("相似度是线索，不是结论：同一道题的正确解法本来就容易写得像。"
                 "请打开源码人工复核后再做判断。")

#: 并排 diff 最多渲染多少行。
#:
#: 抄来的代码动辄两三百行，全渲染既没人看、又会让对话框开一次卡一下。
#: 超出的部分明确写出来"仅显示前 N 行"，而不是悄悄截断。
DIFF_ROW_LIMIT = 400

#: 导出格式选项（与 ``ExportDialog.EXPORT_CHOICES`` 同风格：显示名里把"用途"
#: 也写进去，老师不是天天跟扩展名打交道的人）。CSV 一个报告出两个文件，所以
#: 没有"单文件 CSV"的说法，但后缀识别照常走 ``similarity_export.format_from_path``。
#: 每一项：``(键, 显示名, 文件筛选器)``。
FMT_CHOICES = (
    ("txt", "txt（纯文本，直接看）", "文本文件 (*.txt)"),
    ("csv", "csv（表格，Excel 可直接打开）", "CSV 表格 (*.csv)"),
    ("xlsx", "xlsx（Excel 工作簿）", "Excel 工作簿 (*.xlsx)"),
    ("docx", "docx（Word 文档）", "Word 文档 (*.docx)"),
    ("pdf", "pdf（PDF）", "PDF 文件 (*.pdf)"),
)

#: 导出对话框的默认文件名（不含后缀，后缀由所选格式决定）。
DEFAULT_EXPORT_STEM = "雷同检测报告"


@dataclass
class Finding:
    """结果表里的一行 —— 一次"两方对比"。

    完全重复组有三人以上时，这一行代表组内**前两位**，其余人数写在备注里；
    完整名单放在 :attr:`members` 里（鼠标悬停可以看到）。
    """

    kind: str
    percent: float
    problem_title: str
    left: sim.Person
    right: sim.Person
    band: str = "low"
    note: str = ""
    members: list[sim.Person] = field(default_factory=list)

    @property
    def percent_text(self) -> str:
        return f"{self.percent:.0f}%"

    @property
    def roster_text(self) -> str:
        """组内全部成员的名字（给 tooltip 用）。"""
        return "、".join(person.display for person in self.members)


def findings(report: sim.Report) -> list[Finding]:
    """把报告摊成结果表的行。

    **完全重复优先**，且完全重复的对不再在「高度相似」里重复出现 ——
    同一件事说两遍，会让人以为有两批人。
    """
    rows: list[Finding] = []
    for group in report.exact_groups:
        members = list(group.members)
        if len(members) < 2:
            continue
        rows.append(Finding(
            kind=KIND_EXACT, percent=100.0, problem_title=group.problem_title,
            left=members[0], right=members[1], band="high",
            note=(f"共 {len(members)} 人一字不差"
                  if len(members) > 2 else "两人一字不差"),
            members=members))

    for pair in report.pairs:
        if pair.exact:
            continue          # 已在完全重复组里说过
        rows.append(Finding(
            kind=KIND_SIMILAR, percent=pair.percent,
            problem_title=pair.problem_title, left=pair.left, right=pair.right,
            band=pair.band,
            note=f"共享 {pair.shared} 个指纹",
            members=[pair.left, pair.right]))
    return rows


class SimilarityDialog(QDialog):
    """分析结果。``report`` 是 :func:`offline_oj.core.similarity.analyse` 的产出。"""

    def __init__(self, report: sim.Report, *, palette=None, parent=None,
                 default_directory: str | Path = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("雷同检测（供人工复核）")
        self.resize(1180, 800)
        self._report = report
        self._palette = palette
        self._findings = findings(report)
        self._directory = Path(default_directory) if default_directory else Path.cwd()
        self._build()
        self._fill_table()

    # -- 构建 ---------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*GROUP_MARGINS)
        layout.setSpacing(ROW_SPACING)

        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "strong")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        notice = QLabel(REVIEW_NOTICE)
        notice.setProperty("role", "danger")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table, 1)

        self.notes_label = QLabel()
        self.notes_label.setProperty("muted", True)
        self.notes_label.setWordWrap(True)
        layout.addWidget(self.notes_label)

        self.diff_table = QTableWidget(0, len(DIFF_COLUMNS))
        self.diff_table.setHorizontalHeaderLabels(DIFF_COLUMNS)
        self.diff_table.verticalHeader().setVisible(False)
        self.diff_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.diff_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.diff_table.setFont(mono_font())
        diff_header = self.diff_table.horizontalHeader()
        diff_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        diff_header.setSectionResizeMode(1, QHeaderView.Stretch)
        diff_header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.diff_table, 2)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.buttons.button(QDialogButtonBox.Close).setText("关闭")
        self.buttons.rejected.connect(self.reject)
        self.export_button = QPushButton("导出…")
        self.export_button.clicked.connect(self._export)
        self.buttons.addButton(self.export_button, QDialogButtonBox.ActionRole)
        layout.addWidget(self.buttons)

    # -- 填充 ---------------------------------------------------------------

    def _fill_table(self) -> None:
        table = self.table
        table.setRowCount(0)
        for finding in self._findings:
            row = table.rowCount()
            table.insertRow(row)
            first = finding.left.display
            second = (finding.right.display if len(finding.members) <= 2
                      else f"{finding.right.display} 等 {len(finding.members)} 人")
            cells = (
                finding.percent_text, finding.kind, finding.problem_title,
                first, second, finding.note,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(row, column, item)
            self._tint(table, row, finding)
            if len(finding.members) > 2:
                table.item(row, 4).setToolTip(finding.roster_text)

        summary = self._report_summary()
        self.summary_label.setText(summary)
        self.notes_label.setText("\n".join(f"· {note}" for note in self._report.notes))

        if self._findings:
            table.selectRow(0)
        else:
            self._fill_diff(None)

    def _tint(self, table: QTableWidget, row: int, finding: Finding) -> None:
        """按档位给整行上色。**色值一律取自主题调色板**，不写内联色号。"""
        if self._palette is None:
            return
        if finding.kind == KIND_EXACT:
            color = self._palette.danger
        elif finding.band == "high":
            color = self._palette.danger
        elif finding.band == "medium":
            color = self._palette.warning
        else:
            color = self._palette.text_muted
        brush = QBrush(QColor(color))
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                item.setForeground(brush)

    def _report_summary(self) -> str:
        report = self._report
        problems = len({person.problem_id for person in report.analysed})
        return (f"比对 {len(report.analysed)} 份提交 · {problems} 道题 · "
                f"完全重复 {len(report.exact_groups)} 组 · "
                f"高度相似 {sum(1 for f in self._findings if f.kind == KIND_SIMILAR)} 对")

    # -- 交互 ---------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self._fill_diff(None)
            return
        index = rows[0].row()
        if 0 <= index < len(self._findings):
            self._fill_diff(self._findings[index])
        else:
            self._fill_diff(None)

    def _fill_diff(self, finding: Finding | None) -> None:
        table = self.diff_table
        table.setRowCount(0)
        if finding is None:
            return
        left_code = self._report.codes.get(finding.left.serial, "")
        right_code = self._report.codes.get(finding.right.serial, "")
        language = finding.left.language or finding.right.language
        rows = sim.diff_lines(left_code, right_code, language=language, context=1)

        # 只留前若干行：diff 可能是几百行，全塞进去没人看，还拖慢渲染。
        truncated = len(rows) > DIFF_ROW_LIMIT
        for line in rows[:DIFF_ROW_LIMIT]:
            row = table.rowCount()
            table.insertRow(row)
            cells = (
                "" if line.left_no is None else str(line.left_no),
                line.left_text,
                "" if line.right_no is None else str(line.right_no),
                line.right_text,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(self._diff_brush(line.tag))
                table.setItem(row, column, item)
        if truncated:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 1, QTableWidgetItem("… 差异过长，仅显示前 "
                                                   f"{DIFF_ROW_LIMIT} 行"))
        table.resizeRowsToContents()

    def _diff_brush(self, tag: str) -> QBrush:
        """diff 行的前景色：相同的行用弱化色，不同的行用主题色。"""
        if self._palette is None:
            return QBrush()
        color = {
            sim.DIFF_EQUAL: self._palette.text_muted,
            sim.DIFF_CHANGED: self._palette.warning,
            sim.DIFF_LEFT_ONLY: self._palette.danger,
            sim.DIFF_RIGHT_ONLY: self._palette.success,
        }.get(tag, self._palette.text)
        return QBrush(QColor(color))

    # -- 取值（给测试与调用方）----------------------------------------------

    def row_count(self) -> int:
        return self.table.rowCount()

    def current_finding(self) -> Finding | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = rows[0].row()
        return self._findings[index] if 0 <= index < len(self._findings) else None

    def diff_row_count(self) -> int:
        return self.diff_table.rowCount()

    def select_row(self, index: int) -> None:
        """程序化选中一行（离屏测试点不到鼠标，只能这么验接线）。"""
        if 0 <= index < self.table.rowCount():
            self.table.selectRow(index)

    # -- 导出 ---------------------------------------------------------------

    def export_to(self, path: str | Path, fmt: str) -> list[Path]:
        """把当前报告导出成 ``fmt``（txt/csv/xlsx/docx/pdf），返回写出的文件列表。

        异常**直接冒泡**给调用方 —— 这里不吞错误，真机导失败要让人看见，
        而不是静悄悄地什么都没写。``pdf`` 走 ``ui/export_pdf`` 渲染，其余四种
        交给 ``core/similarity_export``（零依赖、纯 Python）。
        """
        if fmt == "pdf":
            from .export_pdf import render_pdf
            return [render_pdf(sx.html_report(self._report), path)]
        return sx.export(path, self._report, fmt)

    def _export(self) -> None:
        """「导出…」按钮：问清格式与落盘位置，再调 :meth:`export_to`。

        离屏测试点不到按钮，所以测试直接调 ``export_to`` 验接线，这里只负责
        "弹框 + 选格式 + 报错" 这几件 GUI 才需要的活。
        """
        filters = ";;".join(choice[2] for choice in FMT_CHOICES)
        default_path = str(self._directory / DEFAULT_EXPORT_STEM)
        chosen, selected_filter = QFileDialog.getSaveFileName(
            self, "导出雷同检测报告", default_path, filters)
        if not chosen:
            return
        fmt = self._fmt_from_dialog(chosen, selected_filter)
        try:
            out = self.export_to(chosen, fmt)
        except Exception as exc:  # noqa: BLE001 让用户在界面上看到可读的错误
            QMessageBox.warning(self, "导出失败", f"{type(exc).__name__}：{exc}")
            return
        QMessageBox.information(
            self, "导出完成",
            "已导出：\n" + "\n".join(str(p) for p in out))

    @staticmethod
    def _fmt_from_dialog(chosen: str, selected_filter: str) -> str:
        """从文件框返回值推出格式键。

        大多数平台会按"选中的筛选器"自动补后缀，但万一没补（用户手打了全名、
        或平台没补），就**先用后缀判、再用筛选器兜底**，两种都认。
        """
        try:
            return sx.format_from_path(chosen)
        except ValueError:
            pass
        for key, _label, filt in FMT_CHOICES:
            if filt == selected_filter:
                return key
        # 筛选器也没匹配上（极少见）：按后缀硬判一次，再不行就抛给 export 去报错
        suffix = Path(chosen).suffix.lower().lstrip(".")
        if suffix in ("txt", "csv", "xlsx", "docx", "pdf"):
            return suffix
        raise ValueError(f"无法识别导出格式：{chosen!r} / {selected_filter!r}")


__all__ = ["COLUMNS", "DEFAULT_EXPORT_STEM", "DIFF_COLUMNS", "DIFF_ROW_LIMIT",
           "FMT_CHOICES", "Finding", "KIND_EXACT", "KIND_SIMILAR",
           "REVIEW_NOTICE", "SimilarityDialog", "findings"]
