"""``CodeEditor`` 的信号契约。

这一组守的是一件很反直觉、又确实害过我们的事：

**语法高亮会让 ``textChanged`` 响，尽管一个字都没改。**

``QPlainTextEdit.textChanged`` 背后是 ``QTextDocument.contentsChanged``，而
``QSyntaxHighlighter`` 在 ``setFormat()`` 时只重排了格式，Qt 依旧发出这个信号。
于是所有拿 ``textChanged`` 当"用户改过内容"的监听者，都会把**换主题**、
**切语言**这种纯显示变更误认成用户编辑 —— 表现是存题面板被标成"有未保存的
修改"，关窗口时弹出保存确认。这个缺陷在冒烟测试里更狠：离屏环境下没人点得到
那个模态框，整个测试挂死 4 小时 33 分，最后靠 ``faulthandler`` 打栈才认出
凶手是 ``problems_panel._confirm_discard``。

``contentsChange(pos, removed, added)`` 只在真正增删字符时发出，格式重排不经过
它，所以 ``connect_content_changed()`` 走的是这条路。下面是这条契约的守卫：
既防"把显示变更当成编辑"，也防"过滤过头把真编辑漏掉"。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offline_oj.core.models import Language  # noqa: E402
from offline_oj.ui.theme import DARK, LIGHT, resolve_palette  # noqa: E402
from offline_oj.ui.widgets import CodeEditor, CodeHighlighter  # noqa: E402

SAMPLE = (
    "#include <iostream>\n"
    "int main() {\n"
    "    int n = 0;  // 读入\n"
    "    return 0;\n"
    "}\n"
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Spy:
    """同时盯着 Qt 的原始信号和我们自己的"正文变了"回调。"""

    def __init__(self, editor: CodeEditor) -> None:
        self.raw_text_changed = 0
        self.content_changed = 0
        editor.textChanged.connect(self._on_raw)
        editor.connect_content_changed(self._on_content)

    def _on_raw(self) -> None:
        self.raw_text_changed += 1

    def _on_content(self) -> None:
        self.content_changed += 1


def make_editor(qapp) -> CodeEditor:
    editor = CodeEditor(resolve_palette("light"), line_numbers=True, completion=True)
    editor.setPlainText(SAMPLE)
    qapp.processEvents()
    return editor


class TestDisplayChangesAreNotEdits:
    """换主题、切语言都是纯显示变更，不算改内容。"""

    def test_theme_switch_does_not_report_a_content_change(self, qapp):
        editor = make_editor(qapp)
        spy = _Spy(editor)

        editor.set_palette_theme(DARK)
        editor.set_palette_theme(LIGHT)
        qapp.processEvents()

        assert spy.content_changed == 0, (
            "换主题被当成了改正文 —— 存题面板会被标成有未保存的修改，"
            "关窗口时弹出保存确认"
        )
        # 原始信号确实会响，这正是不能用它的原因；这条断言也是"上面那个 0
        # 不是因为它压根没做事"的证据
        assert spy.raw_text_changed > 0, (
            "textChanged 没响，说明高亮压根没重排 —— 那这条用例就没在测东西"
        )

    def test_language_switch_does_not_report_a_content_change(self, qapp):
        editor = make_editor(qapp)
        spy = _Spy(editor)

        editor.set_language(Language.PYTHON)
        qapp.processEvents()

        assert spy.content_changed == 0, "切语言不该被当成改正文"


class TestRealEditsStillCount:
    """反向守卫：过滤不能过滤过头，真编辑必须报出来。"""

    def test_typing_reports_a_content_change(self, qapp):
        editor = make_editor(qapp)
        spy = _Spy(editor)

        editor.textCursor().insertText("x")
        qapp.processEvents()

        assert spy.content_changed > 0, "真的敲了字却没报出来，脏标记会失灵"

    def test_deleting_reports_a_content_change(self, qapp):
        from PySide6.QtGui import QTextCursor

        editor = make_editor(qapp)
        spy = _Spy(editor)

        # 光标默认停在第 0 位，直接 deletePreviousChar() 什么都删不掉 ——
        # 那样测出来的是"没改内容"，而不是"删字没被漏掉"。先挪到末尾。
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.End)
        editor.setTextCursor(cursor)
        editor.textCursor().deletePreviousChar()
        qapp.processEvents()

        assert spy.content_changed > 0, "删字也该算改内容"

    def test_deleting_nothing_reports_nothing(self, qapp):
        """对照组：光标停在开头，删不动 —— 不该报"内容变了"。"""
        editor = make_editor(qapp)
        spy = _Spy(editor)

        editor.textCursor().deletePreviousChar()
        qapp.processEvents()

        assert spy.content_changed == 0, "什么都没删掉却报了内容变更"

    def test_setting_plain_text_reports_a_content_change(self, qapp):
        editor = make_editor(qapp)
        spy = _Spy(editor)

        editor.setPlainText("int main() { return 1; }\n")
        qapp.processEvents()

        assert spy.content_changed > 0, "整体替换文本当然算改内容"


class TestHighlighterLifecycle:
    """换主题不能越换越多高亮器。

    ``QSyntaxHighlighter`` 会把 document 认成父对象，Qt 的父子关系托住 C++ 对象，
    所以 ``self._highlighter = 新实例`` 并不会让旧实例消失。实测换 5 次主题，
    文档上挂了 6 个高亮器 —— 此后每敲一个键都要重排 6 遍，越用越卡。
    """

    @staticmethod
    def _count(editor: CodeEditor) -> int:
        return sum(1 for child in editor.document().children()
                   if isinstance(child, CodeHighlighter))

    def test_repeated_theme_switches_keep_exactly_one_highlighter(self, qapp):
        editor = make_editor(qapp)
        assert self._count(editor) == 1, "构造后本该只有一个高亮器"

        for round_index in range(6):
            editor.set_palette_theme(DARK if round_index % 2 else LIGHT)
            qapp.processEvents()
            assert self._count(editor) == 1, (
                f"第 {round_index + 1} 次换主题后高亮器变成了 {self._count(editor)} 个 "
                f"—— 每多一个，每次重排就多跑一遍"
            )
