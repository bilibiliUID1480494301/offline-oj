"""代码提示（补全）的单元测试。

补全本身不依赖真实窗口，但需要 QApplication 才能构造控件，所以整份用例跑在
``QT_QPA_PLATFORM=offscreen`` 下，可以在 CI / 无桌面环境里执行。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offline_oj.core.models import Language  # noqa: E402
from offline_oj.ui.widgets import (  # noqa: E402
    COMPLETION_WORDS,
    KIND_ROLE,
    SNIPPETS,
    _document_words,
    _static_entries,
    code_context,
    prefix_at,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ======================================================================
# 纯函数：前缀提取
# ======================================================================

class TestPrefixAt:
    def test_plain_identifier(self):
        assert prefix_at("int x = cou") == "cou"

    def test_dot_member_only_takes_last_segment(self):
        assert prefix_at("obj.meth") == "meth"

    def test_scope_operator(self):
        assert prefix_at("std::cou") == "cou"

    def test_no_identifier(self):
        assert prefix_at("x + ") == ""
        assert prefix_at("    ") == ""
        assert prefix_at("") == ""

    def test_preprocessor_keeps_hash(self):
        """``#inc`` 必须连 ``#`` 一起返回，否则替换后会变成 ``##include``。"""
        assert prefix_at("#inc") == "#inc"
        assert prefix_at("    #inc") == "#inc"

    def test_hash_inside_a_line_still_yields_last_word(self):
        # 光标在 `<` 后面，此时取到的应该是 `<` 之后的片段
        assert prefix_at("    #include <ios") == "ios"

    def test_bare_hash_is_not_a_prefix(self):
        assert prefix_at("#") == ""

    def test_single_char(self):
        assert prefix_at("a") == "a"


# ======================================================================
# 纯函数：注释 / 字符串上下文
# ======================================================================

class TestCodeContext:
    """``code_context`` 决定"光标是不是落在注释或字符串里"。

    联想阈值降到 1 之后，每敲一个字母都会问它一次 —— 注释里写的是自然语言，
    弹出来的候选框只会挡住正在写的字，所以判定必须准。
    """

    def test_plain_code_is_counted_as_code(self):
        assert code_context("int x = 0;", "cpp") == (False, False)

    def test_line_comment_is_detected(self):
        assert code_context("// hello", "cpp")[0] is True

    def test_unterminated_string_is_detected(self):
        assert code_context('printf("hi', "cpp")[0] is True

    def test_closed_string_returns_to_code(self):
        assert code_context('printf("hi"); int a', "cpp")[0] is False

    def test_block_comment_reports_open(self):
        in_comment, still_open = code_context("/* hello", "cpp")
        assert in_comment is True
        assert still_open is True

    def test_closed_block_comment_returns_to_code(self):
        assert code_context("/* a */ int x", "cpp") == (False, False)

    def test_hash_is_not_a_comment_in_c_and_cpp(self):
        """``#`` 在 C/C++ 里是预处理指令。

        把它也当注释，``#inc`` → ``#include <iostream>`` 这条提示就再也弹不出来 ——
        而"敲第一个字母就联想"最有价值的场景之一恰好就是写 include。
        """
        assert code_context("#inc", "cpp")[0] is False
        assert code_context("#inc", "c")[0] is False

    def test_hash_is_a_comment_in_python(self):
        assert code_context("# hi", "python")[0] is True

    def test_unknown_language_is_never_suppressed(self):
        """没登记的语言一律不静音 —— 宁可多弹，不可该弹不弹。"""
        assert code_context("// x", "brainfuck") == (False, False)


# ======================================================================
# 纯函数：静态词表与动态词表
# ======================================================================

class TestStaticEntries:
    @pytest.mark.parametrize("language", list(Language))
    def test_names_are_unique(self, language):
        entries = _static_entries(language)
        names = [entry.text for entry in entries]
        assert len(names) == len(set(names)), f"{language} 词表有重名条目"

    @pytest.mark.parametrize("language", list(Language))
    def test_no_duplicate_word_declarations(self, language):
        """同一个词不许在两个分组里各写一遍。

        :func:`_static_entries` 是"先出现的赢"，所以重写一遍不会报错，
        只会让后写的那条**静默消失** —— 看起来加了词，其实没生效。
        这条用例专门盯这种"以为加了"的情况。
        """
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for kind, words in COMPLETION_WORDS.get(language.value, {}).items():
            for word in words:
                if word in seen:
                    duplicates.append(f"{word}（{seen[word]} 与 {kind}）")
                else:
                    seen[word] = kind
        assert not duplicates, f"{language} 词表有重复声明: {duplicates}"

    @pytest.mark.parametrize("language", list(Language))
    def test_every_language_has_words(self, language):
        assert len(_static_entries(language)) >= 40

    def test_keyword_wins_over_snippet(self):
        """同名时关键字应当保留，片段不能把关键字顶掉。"""
        cpp = {entry.text: entry for entry in _static_entries(Language.CPP)}
        assert cpp["using"].kind == "关键字"

    def test_snippet_is_present_under_its_own_name(self):
        cpp = {entry.text: entry for entry in _static_entries(Language.CPP)}
        assert cpp["main"].kind == "片段"
        assert "$0" in cpp["main"].insert

    def test_snippet_names_are_not_keywords(self):
        """片段名不能撞关键字 —— 否则想打关键字就被展开成长模板。"""
        for language, snippets in SNIPPETS.items():
            keywords = set(COMPLETION_WORDS.get(language, {}).get("关键字", ()))
            collisions = {name for name, _ in snippets} & keywords
            assert not collisions, f"{language} 片段名与关键字冲突: {collisions}"

    def test_snippet_caret_marker_is_single(self):
        for language, snippets in SNIPPETS.items():
            for name, payload in snippets:
                assert payload.count("$0") <= 1, f"{language}.{name} 有多个光标标记"


class TestDocumentWords:
    def test_collects_identifiers(self):
        words = _document_words("int total = 0; total += value;", set())
        assert set(words) == {"int", "total", "value"}

    def test_excludes_known(self):
        words = _document_words("int total = 0;", {"int"})
        assert "int" not in words
        assert "total" in words

    def test_ignores_single_char(self):
        assert _document_words("a b c ab", set()) == ["ab"]

    def test_sorted_case_insensitively(self):
        words = _document_words("zeta Alpha beta", set())
        assert words == ["Alpha", "beta", "zeta"]

    def test_skips_huge_documents(self):
        assert _document_words("x" * 500_000, set()) == []


# ======================================================================
# 编辑器集成
# ======================================================================

def make_editor(language: Language, text: str = "", *, show: bool = False):
    """建一个编辑区，正文设为 ``text``，光标停在末尾。

    ``show=True`` 会真的把窗口显示出来。只有当定位/几何这类问题的答案取决于
    "窗口在屏幕上哪儿"时才需要 —— 未显示的顶层窗口，Qt 给它算出来的全局坐标
    是虚构的，拿它做比较等于在比两个想象出来的数字。
    """
    from offline_oj.ui.widgets import CodeEditor

    editor = CodeEditor()
    editor.set_language(language)
    editor.setPlainText(text)
    if show:
        editor.resize(900, 500)
        editor.show()
        QApplication.processEvents()
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    editor.setTextCursor(cursor)
    editor._completer.try_complete()
    if show:
        QApplication.processEvents()
    return editor


def candidate_names(editor) -> list[str]:
    model = editor._completer.completionModel()
    return [model.index(row, 0).data(Qt.DisplayRole) for row in range(model.rowCount())]


class TestAutoPopup:
    def test_first_letter_already_pops_up(self, qapp):
        """敲下第一个字母就该有联想 —— 这是默认行为，不需要任何快捷键。

        历史行为是"满 2 个字符才弹"，理由是"一个字母候选太多"。但真正的问题
        不是候选多，而是**回车被候选框吃掉**（见
        :class:`TestEnterNeverEatenByThePopup`）：修好那个之后，阈值就可以放到 1。
        """
        editor = make_editor(Language.CPP, "v")
        assert editor._completer.popup().isVisible()
        assert "vector" in candidate_names(editor)

    def test_no_prefix_does_not_popup(self, qapp):
        """光标前面没有半个词（空格、标点之后）不弹。"""
        editor = make_editor(Language.CPP, "int x = ")
        assert not editor._completer.popup().isVisible()

    def test_no_match_does_not_popup(self, qapp):
        """一个字母都不匹配时，没有可弹的东西。

        用单字符是因为**正文里的词自己也是一条候选**：敲 ``zzz`` 的话，
        ``zzz`` 本身就进了动态词表并匹配上自己，候选框当然会弹。
        单字符不会进动态词表，所以这里问的是纯粹的"静态词表里有没有"。
        """
        editor = make_editor(Language.CPP, "z")
        assert not editor._completer.popup().isVisible()

    def test_two_chars_popup(self, qapp):
        editor = make_editor(Language.CPP, "vec")
        assert editor._completer.popup().isVisible()
        assert "vector" in candidate_names(editor)

    def test_prefix_filters_candidates(self, qapp):
        """静态词排在前面，正文里的动态词垫后。"""
        editor = make_editor(Language.CPP, "retu")
        names = candidate_names(editor)
        assert names[0] == "return"
        assert "retu" in names
        assert all(name.lower().startswith("retu") for name in names)

    def test_case_insensitive(self, qapp):
        editor = make_editor(Language.PYTHON, "PRIN")
        assert "print" in candidate_names(editor)

    def test_preprocessor_prefix(self, qapp):
        editor = make_editor(Language.CPP, "#inc")
        names = candidate_names(editor)
        assert "#include <iostream>" in names

    def test_nothing_is_preselected(self, qapp):
        """候选框弹出时**不许**预选任何一项。

        这是"回车被吃掉"那个 bug 的根源：一旦有当前项，回车就会被当成
        "采纳当前项"，于是 ``for`` + 回车不再换行。所以这里把"没有当前项"
        钉死 —— 想采纳，必须先自己按方向键（见 :class:`TestSuggestionPopup`）。
        """
        editor = make_editor(Language.CPP, "retu")
        assert editor._completer.popup().isVisible()
        assert not editor._completer.popup().currentIndex().isValid()

    def test_comment_disables_popup(self, qapp):
        """行注释里不弹候选框 —— 那里写的是自然语言。"""
        editor = make_editor(Language.CPP, "// int ma")
        assert not editor._completer.popup().isVisible()

    def test_string_literal_disables_popup(self, qapp):
        editor = make_editor(Language.CPP, 'printf("int ma')
        assert not editor._completer.popup().isVisible()

    def test_manual_trigger_works_inside_a_comment(self, qapp):
        """手动唤出（``Ctrl+Space`` / ``Alt+/``）是一次明确请求，不受静音规则限制。"""
        editor = make_editor(Language.CPP, "// int ma")
        editor._completer.try_complete(force=True)
        assert editor._completer.popup().isVisible()

    @pytest.mark.parametrize("letter, expected", [
        ("v", "vector"),
        ("s", "sort"),
        ("m", "memset"),
        ("p", "printf"),
        ("q", "queue"),
        ("h", "hypot"),
        ("n", "nth_element"),
        ("l", "long long"),
    ])
    def test_one_letter_reaches_the_common_entries(self, qapp, letter, expected):
        """一个字母就要能摸到竞赛里最常用的那些。

        这几条同时也是"词表真的扩进去了"的凭据 —— 尤其 ``memset`` / ``printf``
        这类 C 风格函数，写 C++ 竞赛的人用得比 ``std::`` 那几个还多。
        """
        editor = make_editor(Language.CPP, letter)
        assert expected in candidate_names(editor)


class TestInsertion:
    def test_replaces_prefix(self, qapp):
        editor = make_editor(Language.CPP, "vec")
        editor._completer.insert("vector")
        assert editor.toPlainText() == "vector"

    def test_replaces_only_the_prefix(self, qapp):
        editor = make_editor(Language.CPP, "std::vec")
        editor._completer.insert("vector")
        assert editor.toPlainText() == "std::vector"

    def test_preprocessor_does_not_double_hash(self, qapp):
        """回归：``#`` 没被算进前缀时会出现 ``##include``。"""
        editor = make_editor(Language.CPP, "#inc")
        editor._completer.insert("#include <iostream>")
        assert editor.toPlainText() == "#include <iostream>"

    def test_insert_appends_when_no_prefix(self, qapp):
        editor = make_editor(Language.CPP, "int x = ")
        editor._completer.insert("vector")
        assert editor.toPlainText() == "int x = vector"

    def test_popup_hides_after_insert(self, qapp):
        editor = make_editor(Language.CPP, "vec")
        editor._completer.insert("vector")
        assert not editor._completer.popup().isVisible()


class TestPopupLifetime:
    """候选列表的定位与生命周期。

    ``QCompleter`` 自己建的那个弹出列表**默认是没有父对象的顶层窗口**，
    实测它会比编辑器活得久：编辑器（连同持有它的 completer）销毁之后，
    popup 还挂在桌面上，于是延迟到达的绘制事件会打到一个已经失效的委托上 ——
    表现为 ``Windows fatal exception: access violation``，崩溃位置在
    ``CompletionDelegate.paint``，跟代码毫无逻辑关联，极难排查。

    修法是显式让编辑器当它的父对象。这两条用例把"改好之后仍然对"钉住：
    定位必须还贴着光标（不能因为变成子窗口就跑到窗口内部），
    宿主真正析构时它必须跟着走（不能再留游离窗口）。
    """

    def test_popup_sits_under_the_caret(self, qapp):
        """弹窗必须贴着光标。

        位置只能在**真的显示过**的编辑器上量：编辑器没 ``show()`` 时，
        Qt 给未显示顶层窗口算出来的全局坐标是虚构的，量出来的"光标在哪"没有意义。
        真实使用中编辑器一定已经显示，用例照做即可。

        比较的两个点取自同一个窗口，所以窗口本身被系统摆在哪里都不影响结论 ——
        这也正是这条断言能同时适应离屏平台和真实桌面的原因。
        """
        editor = make_editor(Language.CPP, "vec", show=True)
        try:
            popup = editor._completer.popup()
            assert popup.isVisible()
            assert popup.isWindow(), "弹出列表必须是独立窗口，不能变成内嵌子控件"

            caret = editor.mapToGlobal(editor.cursorRect().bottomLeft())
            # 问"弹窗在屏幕上的哪个位置"要用 mapToGlobal，**不要**读 geometry()：
            # 弹窗一旦有了父对象，即使带着 Qt::Window 标志、isWindow() 也为 True，
            # geometry() 仍然是**父控件坐标系**里的值，两者相差一个父窗口的左上角。
            origin = popup.mapToGlobal(QPoint(0, 0))
            # 允许一点点横向偏差（边框 / 内边距），但必须出现在光标正下方
            assert abs(origin.x() - caret.x()) <= 24, (
                f"水平定位异常：弹窗 x={origin.x()} 光标 x={caret.x()}")
            assert 0 <= origin.y() - caret.y() < 40, (
                f"垂直定位异常：弹窗 y={origin.y()} 光标 y={caret.y()}")
        finally:
            # 用例不留窗口在屏幕上
            editor.hide()

    def test_popup_is_owned_by_the_editor(self, qapp):
        """弹出列表必须挂在编辑器名下 —— 这是它「随宿主一起销毁」的唯一保证。

        ``QCompleter`` 默认建出来的弹出列表是**没有父对象**的顶层窗口，
        所以必须由我们显式认领（``CodeCompleter.__init__`` 里的 ``setParent``）。

        为什么不直接把编辑器拆掉来验证：pytest 里所有用例共用一个
        ``QApplication``，强行析构控件会连累后面每一个用例（实测整个测试进程直接挂死）。
        父对象关系正是 Qt 管理生命周期的方式，断言它比事后拆一遍更稳，
        也更能说明问题出在哪。

        顺便记一笔：这里**不要**为了"对照"去建一个临时的
        ``QCompleter()`` 并访问它的 ``popup()`` —— 那正好又造出一个永不回收的
        游离弹窗，实测会让整个测试进程崩掉。坑就是这么踩出来的。
        """
        editor = make_editor(Language.CPP, "vec")
        popup = editor._completer.popup()
        assert popup.parent() is editor, (
            "弹出列表没有挂在编辑器名下，宿主销毁后它会变成游离的顶层窗口，"
            "并让延迟到达的绘制事件打到已失效的委托上（access violation）")


class TestSnippets:
    def test_caret_lands_on_marker(self, qapp):
        editor = make_editor(Language.CPP, "mai")
        editor._completer.insert("main")
        text = editor.toPlainText()
        assert text.startswith("int main() {")
        # 光标之前是模板前半段，之后应恰好是 $0 之后的尾巴
        tail = text[editor.textCursor().position():]
        assert "return 0;" in tail
        assert tail.startswith("\n    return 0;")

    def test_python_main_guard(self, qapp):
        editor = make_editor(Language.PYTHON, "mai")
        editor._completer.insert("main")
        text = editor.toPlainText()
        assert text.startswith("def main():")
        assert '__name__ == "__main__"' in text
        assert text.rstrip().endswith("main()")

    def test_java_main_is_a_full_class(self, qapp):
        editor = make_editor(Language.JAVA, "mai")
        editor._completer.insert("main")
        text = editor.toPlainText()
        assert text.startswith("public class Main {")
        assert text.count("public static void main") == 1
        assert text.rstrip().endswith("}")

    def test_c_snippet_expands(self, qapp):
        editor = make_editor(Language.C, "fori")
        editor._completer.insert("fori")
        assert editor.toPlainText().startswith("for (int i = 0; i < n; i++) {")


def engage_popup(editor) -> None:
    """模拟"我要选一项"：按一下 ↓。

    采纳只认用户自己的操作（见 :class:`SuggestionPopup`），所以任何想走
    "选中并采纳"路径的用例都得先做这一步。走编辑器的按键入口而不是直接给
    popup 发事件 —— 那条路径上还挂着"方向键永远转交候选框"的转发逻辑。
    """
    from PySide6.QtTest import QTest

    QTest.keyClick(editor, Qt.Key_Down)


class TestAcceptCurrent:
    def test_returns_false_until_the_user_chooses(self, qapp):
        """弹窗开着但用户没选过 —— 不许采纳。

        这就是回车不被吃掉的那条线：没有"用户的选择"，就没有可以采纳的对象。
        """
        editor = make_editor(Language.CPP, "vecto")
        assert editor._completer.popup().isVisible()
        assert not editor._completer.accept_current()
        assert editor.toPlainText() == "vecto"

    def test_accepts_after_the_user_chooses(self, qapp):
        editor = make_editor(Language.CPP, "vecto")
        engage_popup(editor)
        assert editor._completer.accept_current()
        assert editor.toPlainText() == "vector"

    def test_returns_false_when_popup_never_opened(self, qapp):
        # 单字符 "z" 在 C++ 静态词表里没有任何匹配，候选框根本不会弹
        editor = make_editor(Language.CPP, "z")
        assert not editor._completer.popup().isVisible()
        assert not editor._completer.accept_current()


class TestSuggestionPopup:
    """候选列表：用户没主动选之前，不许有任何"当前项"。"""

    def _popup(self, editor):
        return editor._completer.popup()

    def test_refuses_programmatic_selection(self, qapp):
        """程序化的选中要被拒绝 —— Qt 每次刷新模型都会这么干一次。"""
        editor = make_editor(Language.CPP, "vec")
        popup = self._popup(editor)
        model = editor._completer.completionModel()
        popup.setCurrentIndex(model.index(0, 0))
        assert not popup.currentIndex().isValid()
        assert not popup.has_choice()

    def test_arrow_key_marks_engagement(self, qapp):
        editor = make_editor(Language.CPP, "vec")
        popup = self._popup(editor)
        engage_popup(editor)
        assert popup.has_choice()
        assert popup.currentIndex().isValid()

    def test_retyping_clears_the_previous_choice(self, qapp):
        """上一轮按过 ↓ 不代表这一轮还想采纳 —— 每敲一个字符都要清零。"""
        editor = make_editor(Language.CPP, "vec")
        engage_popup(editor)
        assert self._popup(editor).has_choice()
        editor._completer.try_complete()
        assert not self._popup(editor).has_choice()
        assert not self._popup(editor).currentIndex().isValid()

    def test_escape_closes_the_popup(self, qapp):
        from PySide6.QtTest import QTest

        editor = make_editor(Language.CPP, "vec")
        QTest.keyClick(editor, Qt.Key_Escape)
        assert not self._popup(editor).isVisible()


class TestEnterNeverEatenByThePopup:
    """回归：候选框开着时，回车**必须**还是换行。

    历史 bug 的完整形状：``QCompleter`` 一弹出（以及每次刷新模型）就把第 0 项
    设成当前项，而编辑器的键盘处理里有一条"候选框开着时按回车就采纳当前项"。
    两者一凑，只要敲满 2 个字符能匹配上候选，回车就不再是换行：
    ``for`` + 回车展开成整个 for 模板，``// hi`` + 回车把注释改成 ``// hix = 1;``，
    ``int ma`` + 回车变成 ``int map``。而写代码哪能不换行 —— 于是整个编辑器
    都显得"有毛病"，却很难说出毛病在哪。

    这几条用例把"回车照常"钉住，是这次改动的核心验收项。
    """

    @pytest.mark.parametrize("typed, language", [
        ("for", Language.CPP),
        ("int ma", Language.CPP),
        ("pri", Language.PYTHON),
        ("#inc", Language.CPP),
    ])
    def test_enter_inserts_a_newline(self, qapp, typed, language):
        from PySide6.QtTest import QTest

        editor = make_editor(language, typed)
        # 前提：这时候候选框是开着的，回车确实处于"可能被抢"的处境
        assert editor._completer.popup().isVisible()
        QTest.keyClick(editor, Qt.Key_Return)
        QTest.keyClicks(editor, "x")
        assert editor.toPlainText() == typed + "\nx"

    def test_enter_in_a_comment_inserts_a_newline(self, qapp):
        from PySide6.QtTest import QTest

        editor = make_editor(Language.CPP, "// hi")
        assert not editor._completer.popup().isVisible()
        QTest.keyClick(editor, Qt.Key_Return)
        QTest.keyClicks(editor, "y")
        assert editor.toPlainText() == "// hi\ny"

    def test_tab_still_indents(self, qapp):
        """Tab 同样只在用户选过之后才交给候选框，否则照常缩进。"""
        from PySide6.QtTest import QTest

        editor = make_editor(Language.CPP, "vec")
        QTest.keyClick(editor, Qt.Key_Tab)
        assert editor.toPlainText() == "vec    "

    def test_enter_adopts_only_after_an_arrow_key(self, qapp):
        from PySide6.QtTest import QTest

        editor = make_editor(Language.CPP, "vecto")
        QTest.keyClick(editor, Qt.Key_Down)
        QTest.keyClick(editor, Qt.Key_Return)
        assert editor.toPlainText() == "vector"


class TestDynamicWords:
    def test_local_identifier_becomes_candidate(self, qapp):
        source = "int counter = 0;\nint x = coun"
        editor = make_editor(Language.CPP, source)
        assert "counter" in candidate_names(editor)

    def test_known_words_not_duplicated(self, qapp):
        editor = make_editor(Language.CPP, "int x = int")
        names = candidate_names(editor)
        assert names.count("int") == 1


class TestKindLabel:
    """类型标签走自定义角色 —— QCompleter 的弹出列表只有一列，放不下第二列。"""

    def test_kind_is_stored_in_custom_role(self, qapp):
        editor = make_editor(Language.CPP, "retu")
        model = editor._completer.completionModel()
        kinds = {model.index(r, 0).data(Qt.DisplayRole): model.index(r, 0).data(KIND_ROLE)
                 for r in range(model.rowCount())}
        assert kinds["return"] == "关键字"

    def test_snippet_kind(self, qapp):
        editor = make_editor(Language.CPP, "mai")
        model = editor._completer.completionModel()
        kinds = {model.index(r, 0).data(Qt.DisplayRole): model.index(r, 0).data(KIND_ROLE)
                 for r in range(model.rowCount())}
        assert kinds["main"] == "片段"

    def test_dynamic_word_kind(self, qapp):
        editor = make_editor(Language.CPP, "int counter = 0; int coun")
        model = editor._completer.completionModel()
        kinds = {model.index(r, 0).data(Qt.DisplayRole): model.index(r, 0).data(KIND_ROLE)
                 for r in range(model.rowCount())}
        assert kinds["counter"] == "文中"

    def test_popup_exposes_exactly_one_column(self, qapp):
        """这是"类型必须自绘"的原因，写成用例免得以后有人改回两列。"""
        editor = make_editor(Language.CPP, "vec")
        assert editor._completer.popup().model().columnCount() == 1


class TestLanguageSwitch:
    def test_switch_updates_candidates(self, qapp):
        """切语言后候选要跟着换 —— 用 C 有、C++ 没有的 stdio 家族来验证。"""
        editor = make_editor(Language.CPP, "snpr")
        assert "snprintf" not in candidate_names(editor)
        editor.set_language(Language.C)
        editor._completer.try_complete()
        assert editor._completer.popup().isVisible()
        assert "snprintf" in candidate_names(editor)

    def test_no_stale_words_after_switch(self, qapp):
        """切语言后不能残留上一门语言的词。"""
        editor = make_editor(Language.PYTHON, "vec")
        editor.set_language(Language.CPP)
        editor._completer.try_complete()
        assert "vector" in candidate_names(editor)
        editor.set_language(Language.JAVA)
        editor._completer.try_complete()
        assert "vector" not in candidate_names(editor)


class TestCompletionDisabled:
    def test_plain_editor_has_no_completer(self, qapp):
        from offline_oj.ui.widgets import CodeEditor

        editor = CodeEditor(line_numbers=False, completion=False)
        assert editor._completer is None

    def test_typing_still_works(self, qapp):
        from offline_oj.ui.widgets import CodeEditor

        editor = CodeEditor(line_numbers=False, completion=False)
        editor.setPlainText("# 题目描述")
        assert editor.toPlainText() == "# 题目描述"
