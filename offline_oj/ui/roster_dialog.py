"""选手名单编辑器：**在 APP 里直接建名单**，不必先去 Excel 里画一张表。

入口在主机端「进场方式」选了「账号进场」之后的「名单…」按钮。对话框里：

* 直接逐行敲账号、姓名、口令、座位（表格就地编辑）；
* 「生成口令」给还没口令的人补一条 6 位数字口令 —— 首位不为 0，
  照着纸条念不会把前导零念丢；
* 名单存进 ``%LOCALAPPDATA%\\OfflineOJ\\rosters\\<名称>.json``，
  下次开房直接取用；「导出打印条」另存一份 CSV，考前提早发给学生。

为什么不用 QMessageBox 弹"哪几行被丢掉了"
------------------------------------------
导入 CSV 时少收的行要**当场、逐条**说出来，但模态框会把"改完再看"变成
"关掉就再也看不见"。所以丢弃原因写进对话框底部那行状态文字，一直留在
屏幕上直到下一次操作把它顶掉 —— 老师可以照着它去改表。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import GROUP_MARGINS, ROW_SPACING, TIGHT_SPACING
from ..core.roster import (PASSCODE_NOTICE, Roster, RosterError,
                           generate_passcode, safe_roster_name)

log = logging.getLogger(__name__)

__all__ = ["RosterDialog"]

#: 表格列。**导出用 ``CSV_HEADERS``**（core/roster.py），这里只管"屏幕上长什么样"。
COLUMNS = ("账号", "姓名", "口令", "座位")


class RosterDialog(QDialog):
    """一份名单的就地编辑。``roster()`` 在 ``accept()`` 之后拿到结果。"""

    def __init__(self, ctx, *, roster: Roster | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选手名单")
        self._ctx = ctx
        self._roster: Roster | None = None
        self._source = roster

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*GROUP_MARGINS)
        layout.setSpacing(ROW_SPACING)

        # ---- 名称 ----
        name_row = QHBoxLayout()
        name_row.setSpacing(TIGHT_SPACING)
        name_row.addWidget(QLabel("名单名称"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如「高一(3)班」")
        if roster is not None:
            self.name_edit.setText(roster.title)
        self.name_edit.textEdited.connect(self._refresh_title_hint)
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)

        # ---- 表格 ----
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        # ---- 行操作 ----
        row_buttons = QHBoxLayout()
        row_buttons.setSpacing(TIGHT_SPACING)
        add_row = QPushButton("添加一行")
        add_row.setProperty("flat", True)
        add_row.clicked.connect(self.add_row)
        row_buttons.addWidget(add_row)

        remove_row = QPushButton("删除选中")
        remove_row.setProperty("flat", True)
        remove_row.clicked.connect(self.remove_selected)
        row_buttons.addWidget(remove_row)

        fill_codes = QPushButton("生成口令")
        fill_codes.setToolTip("给还没有口令的人补一条 6 位数字口令；已有口令的不动")
        fill_codes.setProperty("flat", True)
        fill_codes.clicked.connect(self.fill_passcodes)
        row_buttons.addWidget(fill_codes)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)

        # ---- 文件操作 ----
        file_buttons = QHBoxLayout()
        file_buttons.setSpacing(TIGHT_SPACING)
        import_csv = QPushButton("导入 CSV…")
        import_csv.setToolTip("把一张已有的表合并进来；表头认「学号/账号/姓名/密码」等常见写法")
        import_csv.setProperty("flat", True)
        import_csv.clicked.connect(self.import_csv)
        file_buttons.addWidget(import_csv)

        export_csv = QPushButton("导出打印条…")
        export_csv.setToolTip("存成一份 CSV（带口令），考前提早发给学生或打印")
        export_csv.setProperty("flat", True)
        export_csv.clicked.connect(self.export_csv)
        file_buttons.addWidget(export_csv)
        file_buttons.addStretch(1)
        layout.addLayout(file_buttons)

        # ---- 提示与状态 ----
        notice = QLabel(PASSCODE_NOTICE)
        notice.setProperty("muted", True)
        notice.setWordWrap(True)
        layout.addWidget(notice)

        self.status_label = QLabel()
        self.status_label.setProperty("muted", True)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存名单")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if roster is not None:
            self._load(roster)
        self._refresh_title_hint()

    # ------------------------------------------------------------------
    # 数据进出
    # ------------------------------------------------------------------

    def roster(self) -> Roster | None:
        return self._roster

    def _table_rows(self) -> list[dict[str, str]]:
        """把表格读成 ``Roster.from_rows`` 认的那一行 dict。"""
        rows: list[dict[str, str]] = []
        for row in range(self.table.rowCount()):
            def cell(column: int) -> str:
                item = self.table.item(row, column)
                return (item.text() if item is not None else "").strip()
            rows.append({"账号": cell(0), "姓名": cell(1),
                         "密码": cell(2), "座位": cell(3)})
        return rows

    def _collect(self) -> tuple[Roster, list[str]]:
        roster, notes = Roster.from_rows(self._table_rows(),
                                         title=self.name_edit.text())
        roster.saved_at = ""
        return roster, notes

    def _load(self, roster: Roster) -> None:
        self.table.setRowCount(0)
        for item in roster:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(
                    (item.account, item.name, item.passcode, item.seat)):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def _refresh_title_hint(self, *_args) -> None:
        name = safe_roster_name(self.name_edit.text())
        self.setWindowTitle(f"选手名单 · {name}")

    # ------------------------------------------------------------------
    # 行操作
    # ------------------------------------------------------------------

    def add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self._say("先选中一行再删。")
            return
        self.table.removeRow(row)

    def fill_passcodes(self) -> None:
        """给空口令的行补生成。已有口令的不动 —— 重发一遍口令是现场事故。"""
        filled = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            if item is not None and item.text().strip():
                continue
            self.table.setItem(row, 2, QTableWidgetItem(generate_passcode()))
            filled += 1
        self._say("已生成 {} 条口令。".format(filled) if filled
                  else "每一行都已经有口令了。")

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------

    def import_csv(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "导入名单 CSV", "", "CSV / 文本 (*.csv *.txt);;所有文件 (*)")
        if not path:
            return
        try:
            incoming, notes = Roster.load_csv(path)
        except RosterError as exc:
            self._say(str(exc))
            return
        merged = 0
        existing = {self.table.item(row, 0).text().strip(): row
                    for row in range(self.table.rowCount())
                    if self.table.item(row, 0) is not None}
        for person in incoming:
            key = person.account
            if key in existing:
                row = existing[key]
                for column, value in ((1, person.name), (2, person.passcode),
                                      (3, person.seat)):
                    cell = self.table.item(row, column)
                    if cell is None or not cell.text().strip():
                        self.table.setItem(row, column, QTableWidgetItem(value))
            else:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for column, value in enumerate(
                        (person.account, person.name, person.passcode,
                         person.seat)):
                    self.table.setItem(row, column, QTableWidgetItem(value))
                existing[key] = row
            merged += 1
        summary = f"已合并 {merged} 人（相同账号只补空格，不覆盖已填的值）。"
        if notes:
            summary += " 有 " + str(len(notes)) + " 行没收进来：" + "；".join(notes)
        self._say(summary)

    def export_csv(self) -> None:
        roster, notes = self._collect()
        if not len(roster):
            self._say("名单还是空的，先加几行再导出。")
            return
        name = safe_roster_name(self.name_edit.text()) + "-打印条.csv"
        target, _filter = QFileDialog.getSaveFileName(
            self, "导出打印条", str(Path.home() / "Documents" / name),
            "CSV (*.csv)")
        if not target:
            return
        try:
            roster.save_csv(target)
        except RosterError as exc:
            self._say(str(exc))
            return
        if notes:
            self._say(f"已导出，但有 {len(notes)} 行被丢掉：" + "；".join(notes))
        else:
            self._say(f"已导出 {len(roster)} 人 → {target}")

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save(self) -> None:
        """收进表格、落盘，然后关闭。

        名单文件落在 ``paths.rosters_dir``，名字由「名单名称」来 —— 与
        ``ExamPanel`` 下次开房时取用的是同一个文件。
        """
        roster, notes = self._collect()
        if not len(roster):
            self._say("名单还是空的。要改用「房间号进场」的话，"
                      "把上面的进场方式换回去即可。")
            return
        name = roster.title
        path = self._ctx.paths.roster_file(name)
        try:
            roster.save(path)
        except RosterError as exc:
            self._say(str(exc))
            return
        self._roster = roster
        # 表格按"收下来"的样子重灌一遍：老师敲的「ZhangSan 」此刻变成
        # 统一后的「zhangsan」，他看到的就是主机将来认的那个写法。
        self.name_edit.setText(roster.title)
        self._load(roster)
        if notes:
            self._say("已保存，但有行被合并或丢掉：" + "；".join(notes))
        else:
            self._say(f"已保存 {len(roster)} 人 → {path.name}")
        self.accept()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _say(self, text: str) -> None:
        """把要说的话留在对话框底部，而不是弹一个要点掉的框。"""
        self.status_label.setText(text)


def show_roster_dialog(ctx, *, roster: Roster | None = None,
                       parent: QWidget | None = None) -> Roster | None:
    """打开编辑器，返回（可能更新的）名单；取消则返回 ``None``。"""
    dialog = RosterDialog(ctx, roster=roster, parent=parent)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.roster()
