"""临时探针：真实按键路径下，补全弹窗会不会把回车吃掉。

用法: QT_QPA_PLATFORM=offscreen python tools/_probe_complete.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from offline_oj.core.models import Language
from offline_oj.ui.widgets import CodeEditor


def typed(keys: str, tail: str = "x = 1;", language: Language = Language.CPP) -> None:
    editor = CodeEditor()
    editor.set_language(language)
    editor.show()
    QTest.keyClicks(editor, keys)
    popup_shown = editor._completer.popup().isVisible()
    QTest.keyClick(editor, Qt.Key_Return)
    QTest.keyClicks(editor, tail)
    print(f"  敲 {keys!r} （弹窗={popup_shown}）回车后 => {editor.toPlainText()!r}")
    editor.hide()


def nav_then_enter(keys: str, language: Language = Language.CPP) -> None:
    """按方向键选中之后再回车 —— 这时才应该采纳。"""
    editor = CodeEditor()
    editor.set_language(language)
    editor.show()
    QTest.keyClicks(editor, keys)
    QTest.keyClick(editor, Qt.Key_Down)
    QTest.keyClick(editor, Qt.Key_Return)
    print(f"  敲 {keys!r} + ↓ + 回车 => {editor.toPlainText()!r}")
    editor.hide()


if __name__ == "__main__":
    QApplication.instance() or QApplication([])
    print("== 回车必须始终是换行 ==")
    typed("for")
    typed("int ma")
    typed("// hi")
    typed("pri", language=Language.PYTHON)
    typed("#inc")
    print("== 主动选了才采纳 ==")
    nav_then_enter("vecto")
    nav_then_enter("mai")
