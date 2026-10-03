"""高级设置面板。

把"这个程序把数据放在哪"直接摆出来，是 Windows 应用的基本礼貌：
用户要备份题库、要清缓存、要排查问题，都需要知道路径。所以存储路径全部可见可打开，
而不是藏在某个注册表键里。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ...win32 import process as win_process
from ..theme import (
    COLUMN_SPACING,
    GROUP_MARGINS,
    PAGE_MARGINS,
    ROW_SPACING,
    SECTION_SPACING,
)
from .base import Panel

THEME_OPTIONS = (
    ("light", "浅色"),
    ("dark", "深色"),
    ("system", "跟随系统"),
)

#: 存储路径条目：(键, 标签, 是否目录)
STORAGE_ITEMS = (
    ("data_root", "数据根目录", True),
    ("problems_file", "题库文件", False),
    ("resources_dir", "题目资源（图片）", True),
    ("log_file", "运行日志", False),
    ("work_dir", "编译工作区", True),
    ("submissions_dir", "提交历史", True),
)


class SettingsPanel(Panel):
    """设置与数据维护。"""

    #: 主题等界面设置变更，主窗口需要立即重新应用
    settings_applied = Signal()

    def _build(self) -> None:
        settings = self.ctx.settings

        # ---- 判题选项 ----
        judging_group = QGroupBox("判题选项")
        judging_layout = QHBoxLayout(judging_group)
        judging_layout.setContentsMargins(*GROUP_MARGINS)
        judging_layout.setSpacing(COLUMN_SPACING)

        self.security_check = QCheckBox("提交前检查危险操作（推荐开启）")
        self.security_check.setToolTip(
            "对 system()/subprocess/socket 等调用给出提示，确认后才执行。\n"
            "这是提示性护栏，不是沙箱：被测代码仍然以你的用户权限运行。"
        )
        self.security_check.setChecked(settings.bool("security_mode"))

        self.o2_check = QCheckBox("C / C++ 编译时开启 -O2 优化")
        self.o2_check.setToolTip("关闭后按 -O0 编译，便于对照优化对运行时间的影响")
        self.o2_check.setChecked(settings.bool("o2_optimization"))

        # 两个开关横排。竖着叠是两行、横排是一行 —— 这里一共就两个复选框，
        # 合起来不到四百像素，何必各占一整行，把下面的路径表往下推。
        judging_layout.addWidget(self.security_check)
        judging_layout.addWidget(self.o2_check)
        judging_layout.addStretch(1)

        # ---- 界面 ----
        ui_group = QGroupBox("界面")
        ui_layout = QHBoxLayout(ui_group)
        ui_layout.setContentsMargins(*GROUP_MARGINS)
        ui_layout.setSpacing(COLUMN_SPACING)

        # 三项都是一眼能读完的短设置（主题三选一、字号两位数、一个复选框），
        # 原来一项一行、还撑满整行宽：「主题 [浅色                          ]」
        # 这种比例既难看，也让人以为要往里填很长一串东西。并成一行，
        # 控件按自身尺寸显示。
        self.theme_combo = QComboBox()
        for value, label in THEME_OPTIONS:
            self.theme_combo.addItem(label, value)
        current_theme = str(settings.get("theme", "light"))
        index = self.theme_combo.findData(current_theme)
        self.theme_combo.setCurrentIndex(max(0, index))
        # 自持尺寸下给一个下限，免得"跟随系统"这种长一点的选项被挤成省略号
        self.theme_combo.setMinimumWidth(110)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 24)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setMinimumWidth(96)
        self.font_size_spin.setValue(int(settings.get("editor_font_size", 11) or 11))

        self.markdown_check = QCheckBox("题目描述使用 Markdown 渲染")
        self.markdown_check.setChecked(settings.bool("markdown_render"))

        ui_layout.addWidget(QLabel("主题"))
        ui_layout.addWidget(self.theme_combo)
        ui_layout.addSpacing(COLUMN_SPACING)
        ui_layout.addWidget(QLabel("代码字号"))
        ui_layout.addWidget(self.font_size_spin)
        ui_layout.addSpacing(COLUMN_SPACING)
        ui_layout.addWidget(self.markdown_check)
        ui_layout.addStretch(1)

        # ---- 存储位置 ----
        storage_group = QGroupBox("数据存储位置（%LOCALAPPDATA%）")
        storage_grid = QGridLayout(storage_group)
        storage_grid.setContentsMargins(*GROUP_MARGINS)
        storage_grid.setHorizontalSpacing(ROW_SPACING)
        storage_grid.setVerticalSpacing(ROW_SPACING)

        self._path_fields: dict[str, QLineEdit] = {}
        for row, (key, label, is_dir) in enumerate(STORAGE_ITEMS):
            name = QLabel(label)
            name.setProperty("role", "strong")

            field = QLineEdit(self._resolve_path(key))
            field.setReadOnly(True)
            field.setCursorPosition(0)
            field.setToolTip(self._resolve_path(key))
            self._path_fields[key] = field

            open_button = QPushButton("打开")
            open_button.setProperty("flat", True)
            open_button.clicked.connect(lambda _checked=False, k=key: self._open_path(k))

            storage_grid.addWidget(name, row, 0)
            storage_grid.addWidget(field, row, 1)
            storage_grid.addWidget(open_button, row, 2)
        storage_grid.setColumnStretch(1, 1)

        # ---- 维护 ----
        maintenance_group = QGroupBox("维护")
        maintenance_layout = QHBoxLayout(maintenance_group)
        maintenance_layout.setContentsMargins(*GROUP_MARGINS)
        maintenance_layout.setSpacing(ROW_SPACING)

        orphan_button = QPushButton("清理未引用的图片")
        orphan_button.setToolTip("删除没有任何题目描述引用的资源文件")
        orphan_button.clicked.connect(self._clean_orphans)

        history_button = QPushButton("清空提交历史")
        history_button.setProperty("variant", "danger")
        history_button.setToolTip("只删除提交记录，不影响题库")
        history_button.clicked.connect(self._clear_history)

        reset_button = QPushButton("恢复默认设置")
        reset_button.setProperty("variant", "danger")
        reset_button.clicked.connect(self._reset_settings)

        maintenance_layout.addWidget(orphan_button)
        maintenance_layout.addWidget(history_button)
        maintenance_layout.addStretch(1)
        maintenance_layout.addWidget(reset_button)

        # ---- 保存按钮 ----
        self.apply_button = QPushButton("保存并应用")
        self.apply_button.setProperty("variant", "primary")
        self.apply_button.clicked.connect(self.apply)

        self.hint = QLabel("设置在点击「保存并应用」后生效；主题会立即切换。")
        self.hint.setProperty("muted", True)

        footer = QHBoxLayout()
        footer.setSpacing(ROW_SPACING)
        footer.addWidget(self.hint, 1)
        footer.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(SECTION_SPACING)
        layout.addWidget(judging_group)
        layout.addWidget(ui_group)
        layout.addWidget(storage_group)
        layout.addWidget(maintenance_group)
        layout.addStretch(1)
        layout.addLayout(footer)

    # ---- 路径 -------------------------------------------------------------

    def _resolve_path(self, key: str) -> str:
        mapping = {
            "data_root": self.ctx.paths.data_root,
            "problems_file": self.ctx.paths.problems_file,
            "resources_dir": self.ctx.paths.resources_dir,
            "log_file": self.ctx.paths.log_file,
            "work_dir": self.ctx.paths.work_dir,
            "submissions_dir": self.ctx.paths.submissions_dir,
        }
        return str(mapping.get(key, ""))

    def _open_path(self, key: str) -> None:
        from pathlib import Path

        target = Path(self._resolve_path(key))
        if not target.exists():
            self.notify.emit("warning", f"路径尚不存在：\n{target}")
            return
        if target.is_dir():
            win_process.reveal_in_explorer(target)
        else:
            win_process.open_path(target)

    def refresh_paths(self) -> None:
        for key, field in self._path_fields.items():
            field.setText(self._resolve_path(key))

    # ---- 保存 -------------------------------------------------------------

    def apply(self) -> None:
        settings = self.ctx.settings
        settings.set("security_mode", self.security_check.isChecked())
        settings.set("o2_optimization", self.o2_check.isChecked())
        settings.set("theme", self.theme_combo.currentData())
        settings.set("markdown_render", self.markdown_check.isChecked())
        settings.set("editor_font_size", self.font_size_spin.value())
        settings.save()

        self.status("设置已保存")
        self.settings_applied.emit()

    def on_settings_changed(self) -> None:
        settings = self.ctx.settings
        self.security_check.setChecked(settings.bool("security_mode"))
        self.o2_check.setChecked(settings.bool("o2_optimization"))
        index = self.theme_combo.findData(str(settings.get("theme", "light")))
        self.theme_combo.setCurrentIndex(max(0, index))
        self.markdown_check.setChecked(settings.bool("markdown_render"))
        self.font_size_spin.setValue(int(settings.get("editor_font_size", 11) or 11))
        self.refresh_paths()

    # ---- 维护 -------------------------------------------------------------

    def _clean_orphans(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        orphans = self.ctx.repository.list_orphan_resources()
        if not orphans:
            self.notify.emit("info", "没有发现未被引用的图片。")
            return
        preview = "\n".join(orphans[:10])
        more = f"\n… 另有 {len(orphans) - 10} 个" if len(orphans) > 10 else ""
        answer = QMessageBox.question(
            self,
            "清理未引用的图片",
            f"找到 {len(orphans)} 个未被任何题目引用的文件：\n\n{preview}{more}\n\n"
            "确认删除？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        removed = 0
        for name in orphans:
            try:
                (self.ctx.paths.resources_dir / name).unlink(missing_ok=True)
                removed += 1
            except Exception:
                continue
        self.status(f"已清理 {removed} 个资源文件")
        self.notify.emit("info", f"已清理 {removed} 个未引用的图片。")
        self.refresh_paths()

    def _clear_history(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, "清空提交历史",
            "确认清空所有提交记录？题库与题目内容不受影响。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.ctx.submission_log.clear()
        self.status("提交历史已清空")

    def _reset_settings(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            self, "恢复默认设置",
            "将把判题选项与界面设置恢复为默认值（题库、编译器路径不受影响）。\n"
            "确认继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.ctx.settings.reset()
        self.ctx.settings.save()
        self.on_settings_changed()
        self.settings_applied.emit()
        self.status("已恢复默认设置")
