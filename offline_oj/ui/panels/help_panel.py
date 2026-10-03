"""帮助与关于面板。

"复制诊断信息"是这个面板最实用的一按钮：出问题时用户只要把它粘到聊天窗口，
维护者就能拿到版本、路径、日志尾部，省掉来回问"你装在哪、什么版本"。
"""

from __future__ import annotations

import platform
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ... import APP_DISPLAY_NAME, __version__
from ...win32 import process as win_process
from ..theme import (
    COLUMN_SPACING,
    GROUP_MARGINS,
    PAGE_MARGINS,
    ROW_SPACING,
    SECTION_SPACING,
    TIGHT_SPACING,
)
from .base import Panel

USAGE_STEPS = (
    ("1", "配置工具链", "进入「编译器配置」→ 点「自动检测」→ 点「验证可用性」→ 保存。"),
    ("2", "建题库", "进入「存题模块」→ 新建题目 → 填写描述与测试点 → 保存。"),
    ("3", "刷题判题", "进入「写题模块」→ 左侧选题 → 写代码 → 「提交代码」查看评测结果。"),
    ("4", "交换题目", "存题模块左列底部的「导入 / 导出」各带一个菜单，"
                      "单题与 ZIP / 文件夹批量都在里面。"),
    ("5", "课堂测验", "「局域网测验 → 主机端」勾题目、设时长，点「开启房间」，"
                      "把大字的房间号报给学生；收上来的每份代码在「提交与代码」页里逐份看。"),
)

SHORTCUTS = (
    ("Ctrl + N", "新建题目"),
    ("Ctrl + S", "保存当前题目"),
    ("Ctrl + O", "打开代码文件"),
    ("Ctrl + Shift + S", "代码另存为文件"),
    ("Ctrl + Enter", "提交代码并评测"),
    ("Ctrl + R", "测试运行（自定义输入）"),
    ("Ctrl + L", "清空代码"),
    ("Ctrl + B", "编译并验证工具链"),
    ("Ctrl + Space / Alt + /", "手动唤出代码提示"),
    ("Tab / Shift + Tab", "缩进 / 反缩进"),
    ("F1", "打开帮助"),
    ("Ctrl + F", "聚焦到题目搜索框"),
)

EDITOR_FEATURES = (
    ("语法着色", "注释、字符串、数字、关键字与预处理指令按语言着色，内置浅色 / 深色两套配色。"),
    ("代码提示", "敲下第一个字母就自动弹出候选，无需任何快捷键；注释和字符串里不弹，"
                 "需要时用 Ctrl + Space 或 Alt + / 手动唤出。"
                 "↑ ↓ 选择，Enter / Tab 采纳，Esc 关闭。"),
    ("候选来源", "按语言预置的关键字、类型、标准库 API，加上当前文件里出现过的标识符。"
                 "提示是「前缀式」的 —— 没有编译器前端就没有类型信息，"
                 "所以不支持 obj. 之后列出成员。"),
    ("代码片段", "如 main、fori、readarray，展开成多行模板并把光标停在待填写的位置。"),
    ("自动缩进", "回车时沿用上一行缩进，行尾是 { ( [ 或 : 时自动再加一级。"),
    ("代码文件", "编辑器上方的「打开…」「另存为…」把代码存成文件或从文件读入，"
                 "后缀按当前语言给。文件一律按 UTF-8 读写；记事本存的带 BOM 的 "
                 "UTF-8 也能正确读入。有未保存的改动时会先问一句。"),
    ("O2 优化", "「写题模块」编辑器上方的「O2 优化」决定 C / C++ 提交按 -O2 还是 -O0 编译。"
                "「测试运行」用同一个值，所以自测的耗时与判题一致；"
                "默认值跟随「高级设置」，局域网测验里由主机统一决定。"),
)

TECH_STACK = (
    "界面：PySide6 (Qt 6)  ·  平台：Windows 10 / 11",
    "评测：进程级沙箱（时间 / 内存限制，超限终止进程树）",
    "工具链：C / C++（GCC 系 · Clang 系 · MSVC）、Python、Java —— 自动探测，也可手动指定",
    "存储：JSON（原子写入 + 滚动备份）位于 %LOCALAPPDATA%\\OfflineOJ",
)


class HelpPanel(Panel):
    """使用说明、快捷键与诊断信息。"""

    def _build(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        # 右侧留 8px 是给滚动条让位 —— 滚动区里内容会铺满视口宽度，
        # 不留这一条的话文字会钻到滚动条底下。
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(SECTION_SPACING)

        layout.addWidget(self._build_about())
        layout.addWidget(self._build_usage())
        layout.addWidget(self._build_editor())
        layout.addWidget(self._build_shortcuts())
        layout.addWidget(self._build_diagnostics())
        layout.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(container)

        root = QVBoxLayout(self)
        root.setContentsMargins(*PAGE_MARGINS)
        root.addWidget(area)

    # ---- 各区块 -----------------------------------------------------------

    def _build_about(self) -> QGroupBox:
        group = QGroupBox("关于")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(*GROUP_MARGINS)
        layout.setSpacing(TIGHT_SPACING)

        title = QLabel(APP_DISPLAY_NAME)
        title.setProperty("role", "h1")

        version = QLabel(f"版本 {__version__}　·　构建于 Python {platform.python_version()}")
        version.setProperty("muted", True)

        layout.addWidget(title)
        layout.addWidget(version)
        for line in TECH_STACK:
            item = QLabel("· " + line)
            item.setProperty("muted", True)
            item.setWordWrap(True)
            layout.addWidget(item)

        note = QLabel(
            "本程序在本机直接编译并运行你提交的代码。请只运行自己信任的代码 —— "
            "这里没有虚拟机或容器级隔离。"
        )
        note.setWordWrap(True)
        # 用语义角色而不是内联色值：内联写死的颜色切主题后不会重刷，
        # 深色主题下会留着浅色主题的那个红。见 theme.stylesheet 的说明。
        note.setProperty("role", "danger")
        layout.addWidget(note)
        # 存成属性：冒烟测试会断言这个角色在换主题之后仍然在
        # （角色丢了外观会静默退回默认，界面上看不出来）。
        self.note_label = note
        return group

    def _build_usage(self) -> QGroupBox:
        group = QGroupBox("快速上手")
        grid = QGridLayout(group)
        grid.setContentsMargins(*GROUP_MARGINS)
        grid.setHorizontalSpacing(SECTION_SPACING)
        grid.setVerticalSpacing(ROW_SPACING)
        for row, (number, title, detail) in enumerate(USAGE_STEPS):
            badge = QLabel(number)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(22, 22)
            badge.setProperty("role", "step")
            heading = QLabel(title)
            heading.setProperty("role", "strong")
            text = QLabel(detail)
            text.setProperty("muted", True)
            text.setWordWrap(True)
            grid.addWidget(badge, row, 0, Qt.AlignTop)
            grid.addWidget(heading, row, 1, Qt.AlignTop)
            grid.addWidget(text, row, 2)
        grid.setColumnStretch(2, 1)
        return group

    def _build_editor(self) -> QGroupBox:
        group = QGroupBox("代码编辑器")
        grid = QGridLayout(group)
        grid.setContentsMargins(*GROUP_MARGINS)
        grid.setHorizontalSpacing(COLUMN_SPACING)
        grid.setVerticalSpacing(ROW_SPACING)
        for row, (title, detail) in enumerate(EDITOR_FEATURES):
            heading = QLabel(title)
            heading.setProperty("role", "strong")
            text = QLabel(detail)
            text.setProperty("muted", True)
            text.setWordWrap(True)
            grid.addWidget(heading, row, 0, Qt.AlignTop)
            grid.addWidget(text, row, 1)
        # 这里原来还单独挂了一句"提示是「前缀式」的…"的说明。它排在最后一行之后，
        # 而最后一行后来变成了「O2 优化」—— 一句话紧跟在一个不相干的条目下面，
        # 读起来像是 O2 的补充。那句话本来就是讲代码提示的，已经并进「候选来源」了。
        grid.setColumnStretch(1, 1)
        return group

    def _build_shortcuts(self) -> QGroupBox:
        group = QGroupBox("快捷键")
        grid = QGridLayout(group)
        grid.setContentsMargins(*GROUP_MARGINS)
        grid.setHorizontalSpacing(COLUMN_SPACING)
        grid.setVerticalSpacing(TIGHT_SPACING)
        for index, (keys, description) in enumerate(SHORTCUTS):
            key_label = QLabel(keys)
            key_label.setProperty("role", "code")
            text = QLabel(description)
            grid.addWidget(key_label, index // 2, (index % 2) * 2)
            grid.addWidget(text, index // 2, (index % 2) * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return group

    def _build_diagnostics(self) -> QGroupBox:
        group = QGroupBox("诊断")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(*GROUP_MARGINS)
        layout.setSpacing(ROW_SPACING)

        copy_button = QPushButton("复制诊断信息")
        copy_button.setProperty("variant", "primary")
        copy_button.clicked.connect(self._copy_diagnostics)

        log_button = QPushButton("打开日志文件")
        log_button.clicked.connect(lambda: win_process.open_path(self.ctx.paths.log_file))

        data_button = QPushButton("打开数据目录")
        data_button.clicked.connect(
            lambda: win_process.reveal_in_explorer(self.ctx.paths.data_root)
        )

        layout.addWidget(copy_button)
        layout.addWidget(log_button)
        layout.addWidget(data_button)
        layout.addStretch(1)

        self.diagnostic_view = QLabel("")
        self.diagnostic_view.setProperty("muted", True)
        self.diagnostic_view.setWordWrap(True)
        layout.addWidget(self.diagnostic_view, 1)
        return group

    # ---- 行为 -------------------------------------------------------------

    def _copy_diagnostics(self) -> None:
        lines = ["=== 离线 OJ 系统 诊断信息 ==="]
        for key, value in self.ctx.diagnostics().items():
            lines.append(f"{key}: {value}")
        lines.append(f"Python: {sys.version.split()[0]}")
        lines.append(f"平台: {platform.platform()}")
        lines.append("--- 日志尾部 ---")
        lines.extend(self._tail_log(30))

        text = "\n".join(lines)
        QApplication.clipboard().setText(text)

        preview = "已复制诊断信息到剪贴板，可直接粘贴给维护人员。"
        self.diagnostic_view.setText(preview)
        self.status(preview)

    def _tail_log(self, count: int) -> list[str]:
        log_file = self.ctx.paths.log_file
        if not log_file.is_file():
            return ["（暂无日志）"]
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return [f"（读取日志失败: {exc}）"]
        return content.splitlines()[-count:]
