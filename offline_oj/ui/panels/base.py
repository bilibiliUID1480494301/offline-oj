"""面板基类。

统一三件事，避免每个面板各写一套：

1. **上下文注入** —— 面板通过 ``self.ctx`` 拿到 paths / settings / repository；
2. **状态上报** —— ``status_message`` 信号统一送到主窗口状态栏；
3. **主题切换** —— ``apply_palette`` 在浅色/深色切换时被主窗口调用。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ...context import AppContext
from ..theme import Palette


class Panel(QWidget):
    """所有功能面板的基类。"""

    #: 请求主窗口在状态栏显示一条消息
    status_message = Signal(str)

    #: 请求主窗口弹出提示
    notify = Signal(str, str)      # level(info/warning/error), 正文

    def __init__(self, ctx: AppContext, palette: Palette, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.palette = palette
        self._build()
        self.apply_palette(palette)

    # ---- 子类实现 ---------------------------------------------------------

    def _build(self) -> None:
        """构造界面。子类必须实现。"""

    def apply_palette(self, palette: Palette) -> None:
        """主题切换钩子，默认只更新字段。"""
        self.palette = palette

    # ---- 生命周期钩子 -----------------------------------------------------

    def on_activated(self) -> None:
        """面板被切到前台时调用，可用于懒刷新。"""

    def on_settings_changed(self) -> None:
        """设置被保存后调用。"""

    def on_closing(self) -> bool:
        """主窗口关闭前调用，返回 ``False`` 可阻止关闭（例如有未保存内容）。"""
        return True

    # ---- 便捷方法 ---------------------------------------------------------

    def status(self, text: str) -> None:
        self.status_message.emit(text)
