"""代码文件的打开 / 另存为。

这一组盯的是三类**只有真的读写文件才会暴露**的问题，它们在界面上都表现成
同一句"莫名其妙"：

* **BOM** —— 记事本「另存为 UTF-8」默认带 BOM。按 ``utf-8`` 读进来，第一行
  会多一个看不见的字符，编译器报的错指在别处；
* **不是 UTF-8 的文件** —— 中文 Windows 上 GBK 存的源码很常见，报错必须是人话；
* **未保存的内容被无声覆盖** —— 打开另一个文件前要问一句。

对话框一律换成替身：离屏环境里 ``exec()`` 是永久阻塞（没人点得到那个框），
真开对话框的话这一整轮测试会挂死。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.core.models import Language  # noqa: E402
from offline_oj.settings import AppSettings  # noqa: E402
from offline_oj.ui import codefile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ======================================================================
# 纯函数：后缀、过滤器、起始目录
# ======================================================================

class TestNaming:
    def test_each_language_knows_its_own_suffixes(self):
        assert ".cpp" in codefile.suffixes(Language.CPP)
        assert ".py" in codefile.suffixes(Language.PYTHON)
        assert ".java" in codefile.suffixes(Language.JAVA)

    def test_default_name_matches_the_language(self):
        assert codefile.default_name(Language.CPP) == "main.cpp"
        assert codefile.default_name(Language.JAVA) == "main.java"

    def test_filter_puts_the_current_language_first(self):
        """当前语言必须排第一 —— 排在后面等于每次都要手动挑一次。"""
        text = codefile.file_filter(Language.PYTHON)
        assert text.startswith("Python 源文件")
        assert "*.py" in text

    def test_filter_always_offers_all_files(self):
        """学生手里的文件名五花八门，只给当前语言一种会让"打开"看起来没反应。"""
        for language in Language:
            assert codefile.ALL_FILES in codefile.file_filter(language)

    @pytest.mark.parametrize("name, expected", [
        ("a.cpp", Language.CPP),
        ("main.cc", Language.CPP),
        ("x.c", Language.C),
        ("solve.py", Language.PYTHON),
        ("Main.java", Language.JAVA),
        ("MAIN.CPP", Language.CPP),
        ("no_extension", None),
        ("weird.xyz", None),
    ])
    def test_language_is_guessed_from_the_suffix(self, name, expected):
        assert codefile.language_for_path(name) is expected

    def test_directory_of_a_file_is_its_parent(self, tmp_path):
        target = tmp_path / "a.cpp"
        target.write_text("", encoding="utf-8")
        assert codefile.initial_directory(str(target)) == str(tmp_path)

    def test_directory_of_a_directory_is_itself(self, tmp_path):
        assert codefile.initial_directory(str(tmp_path)) == str(tmp_path)

    def test_directory_of_a_missing_path_falls_back(self, tmp_path):
        """上次那个目录被删掉时不能把对话框停在一个空目录里。

        Windows 上那看起来像"我的文件都不见了"。
        """
        ghost = tmp_path / "gone" / "a.cpp"
        assert codefile.initial_directory(str(ghost)) == str(tmp_path)

    def test_empty_hint_gives_the_fallback(self):
        assert codefile.initial_directory("", fallback="D:/x") == "D:/x"

    def test_file_label_is_blank_without_a_file(self):
        assert codefile.file_label("", dirty=False) == ""

    def test_file_label_marks_unsaved(self):
        assert codefile.file_label("D:/x/main.cpp", dirty=False) == "main.cpp"
        assert codefile.file_label("D:/x/main.cpp", dirty=True) == "main.cpp・未保存"


# ======================================================================
# 读写：BOM、编码、换行
# ======================================================================

class TestReadWrite:
    def test_written_file_has_no_bom(self, tmp_path):
        """写出去不许带 BOM。

        判题工具链按 UTF-8 读源码（MSVC 那边还显式加了 ``/utf-8``），
        主动写 BOM 是给自己找麻烦。
        """
        target = tmp_path / "a.cpp"
        codefile.write_code(target, "int main(){}")
        assert not target.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_written_file_uses_crlf(self, tmp_path):
        """物理写的是 ``\\r\\n``。

        Windows 记事本对只有 ``\\n`` 的文件会当成"一整行"显示，而学生双击
        打开自己存的代码是常事。判题工具链两者都吃，所以按好看的那个来。

        断言必须落在**字节**上：``str`` 读法默认把 ``\\r\\n`` 归一成 ``\\n``，
        拿 ``read_text()`` 比会得到一个假的通过。
        """
        target = tmp_path / "a.cpp"
        codefile.write_code(target, "// 中文注释\nint x;")
        assert target.read_bytes() == "// 中文注释\r\nint x;".encode("utf-8")

    def test_reading_normalises_the_newlines(self, tmp_path):
        """读回来一律是 ``\\n`` —— 编辑器与判题都按 ``\\n`` 处理。"""
        target = tmp_path / "a.cpp"
        codefile.write_code(target, "int a;\nint b;")
        assert codefile.read_code(target) == "int a;\nint b;"

    def test_round_trip_survives_chinese(self, tmp_path):
        target = tmp_path / "a.cpp"
        text = "// 第一行\n// 第二行"
        codefile.write_code(target, text)
        assert codefile.read_code(target) == text

    def test_reading_a_bom_file_strips_it(self, tmp_path):
        """回归：带 BOM 的文件读进来，第一个字符必须是 ``/`` 而不是 ``\\ufeff``。

        这个字符在编辑器里看不见，而编译器会在第 1 行报一个毫不相干的错。
        """
        target = tmp_path / "a.cpp"
        target.write_bytes("\ufeff#include <iostream>\n".encode("utf-8"))
        text = codefile.read_code(target)
        assert not text.startswith("\ufeff")
        assert text.startswith("#include")

    def test_reading_a_gbk_file_says_so_plainly(self, tmp_path):
        """GBK 存的源码在中文 Windows 上很常见，报错要能指导下一步。"""
        target = tmp_path / "a.cpp"
        target.write_bytes("// 中文注释\n".encode("gbk"))
        with pytest.raises(codefile.CodeFileError) as excinfo:
            codefile.read_code(target)
        message = str(excinfo.value)
        assert "UTF-8" in message
        assert "另存为" in message

    def test_reading_a_missing_file_does_not_explode(self, tmp_path):
        with pytest.raises(codefile.CodeFileError):
            codefile.read_code(tmp_path / "nope.cpp")

    def test_reading_is_tolerant_of_plain_utf8_too(self, tmp_path):
        """没有 BOM 的文件当然也要照读 —— ``utf-8-sig`` 不要求 BOM 存在。"""
        target = tmp_path / "a.py"
        target.write_bytes("print('hi')\n".encode("utf-8"))
        assert codefile.read_code(target).startswith("print")


# ======================================================================
# 控制器：接到编辑器上之后的行为
# ======================================================================

class _Notify:
    """收集 ``emit`` 调用的替身。

    用真的 ``Signal`` 会连到 ``MainWindow.notify`` 上，而它对 info 级也是
    **模态对话框** —— 离屏环境下没人点得到，整轮测试挂死。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def emit(self, level: str, message: str) -> None:
        self.messages.append((level, message))


def make_controller(qapp, tmp_path, *, text: str = "", language: Language = Language.CPP):
    from offline_oj.ui.widgets import CodeEditor

    editor = CodeEditor()
    editor.set_language(language)
    if text:
        editor.setPlainText(text)
    notify = _Notify()
    controller = codefile.CodeFileController(
        widget=None,
        editor=editor,
        settings=AppSettings(tmp_path / "settings.json"),
        notify=notify,
        current_language=lambda: language,
    )
    # 起手就该是"干净"的：占位文本不算用户的改动
    controller._saved_text = editor.toPlainText()
    return controller, editor, notify


class TestControllerState:
    def test_fresh_controller_is_clean(self, qapp, tmp_path):
        controller, _, _ = make_controller(qapp, tmp_path, text="int x;")
        assert not controller.is_dirty()
        assert controller.path == ""
        assert controller.label_text() == ""

    def test_editing_marks_it_dirty(self, qapp, tmp_path):
        controller, editor, _ = make_controller(qapp, tmp_path, text="int x;")
        editor.insertPlainText("\nint y;")
        assert controller.is_dirty()

    def test_adopting_a_file_leaves_it_clean(self, qapp, tmp_path):
        """回归：刚打开的文件不能一进来就显示成"未保存"。

        ``setPlainText`` 会**同步**触发内容变化回调，文件关联必须在那之后落定。
        """
        controller, editor, _ = make_controller(qapp, tmp_path)
        target = tmp_path / "a.cpp"
        target.write_text("int main(){}", encoding="utf-8")
        controller.adopt(str(target), "int main(){}")
        assert editor.toPlainText() == "int main(){}"
        assert not controller.is_dirty()
        assert controller.label_text() == "a.cpp"

    def test_forget_drops_the_association(self, qapp, tmp_path):
        controller, _, _ = make_controller(qapp, tmp_path)
        controller.adopt(str(tmp_path / "a.cpp"), "int x;")
        controller.forget()
        assert controller.path == ""
        assert controller.label_text() == ""

    def test_editing_after_adopt_shows_unsaved(self, qapp, tmp_path):
        controller, editor, _ = make_controller(qapp, tmp_path)
        controller.adopt(str(tmp_path / "a.cpp"), "int x;")
        editor.insertPlainText(" // 改了")
        assert controller.is_dirty()
        assert controller.label_text() == "a.cpp・未保存"


class TestOpenFile:
    def test_open_reads_the_file(self, qapp, tmp_path, monkeypatch):
        target = tmp_path / "a.cpp"
        target.write_text("int main(){}", encoding="utf-8")
        controller, editor, _ = make_controller(qapp, tmp_path)
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        assert controller.open_file()
        assert editor.toPlainText() == "int main(){}"
        assert controller.path == str(target)

    def test_cancelling_changes_nothing(self, qapp, tmp_path, monkeypatch):
        controller, editor, _ = make_controller(qapp, tmp_path, text="keep me")
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: ("", "")))
        assert not controller.open_file()
        assert editor.toPlainText() == "keep me"

    def test_a_broken_file_is_reported_not_crashed(self, qapp, tmp_path, monkeypatch):
        target = tmp_path / "a.cpp"
        target.write_bytes("// 中文\n".encode("gbk"))
        controller, editor, notify = make_controller(qapp, tmp_path, text="keep me")
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        assert not controller.open_file()
        assert editor.toPlainText() == "keep me", "读失败不该把编辑器清掉"
        assert notify.messages and notify.messages[0][0] == "error"

    def test_unsaved_work_asks_before_being_replaced(self, qapp, tmp_path, monkeypatch):
        """编辑器里还有没存过的代码时，"打开"必须先问一句。

        不问的后果是无声地删掉学生刚写的代码 —— 而且他多半会先怪程序。
        """
        target = tmp_path / "a.cpp"
        target.write_text("other", encoding="utf-8")
        controller, editor, _ = make_controller(qapp, tmp_path, text="我写的代码")
        editor.insertPlainText(" // 改动")     # 变成"有未保存内容"

        asked: list[str] = []
        monkeypatch.setattr(controller, "confirm_discard",
                            lambda action: asked.append(action) or False)
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        assert not controller.open_file()
        assert asked, "没有问就往下走了"
        assert "改动" in editor.toPlainText()

    def test_clean_editor_is_not_asked(self, qapp, tmp_path):
        """没什么可丢的时候不许弹框 —— 每次都问会让这个提示失去意义。"""
        controller, _, _ = make_controller(qapp, tmp_path, text="int x;")
        assert controller.confirm_discard("打开另一个文件")

    def test_empty_editor_is_not_asked(self, qapp, tmp_path):
        controller, _, _ = make_controller(qapp, tmp_path)
        assert controller.confirm_discard("打开另一个文件")

    def test_opening_a_py_file_switches_the_language(self, qapp, tmp_path, monkeypatch):
        """用 C++ 的规则给 .py 着色，关键字一条都不对。"""
        target = tmp_path / "a.py"
        target.write_text("print('hi')", encoding="utf-8")
        switched: list[Language] = []
        controller, editor, _ = make_controller(qapp, tmp_path)
        controller._select_language = switched.append
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        assert controller.open_file()
        assert switched == [Language.PYTHON]

    def test_without_a_switcher_it_at_least_tells_the_user(self, qapp, tmp_path,
                                                           monkeypatch):
        target = tmp_path / "a.py"
        target.write_text("print('hi')", encoding="utf-8")
        controller, _, notify = make_controller(qapp, tmp_path)
        monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        controller.open_file()
        assert notify.messages, "换了语言却一声不响"


class TestSaveAs:
    def test_save_writes_the_editor_text(self, qapp, tmp_path, monkeypatch):
        target = tmp_path / "out.cpp"
        controller, editor, _ = make_controller(qapp, tmp_path, text="int main(){}")
        monkeypatch.setattr(codefile.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        assert controller.save_as()
        assert target.read_text(encoding="utf-8") == "int main(){}"
        assert not controller.is_dirty()
        assert controller.label_text() == "out.cpp"

    def test_cancelling_writes_nothing(self, qapp, tmp_path, monkeypatch):
        controller, _, _ = make_controller(qapp, tmp_path, text="int x;")
        monkeypatch.setattr(codefile.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: ("", "")))
        assert not controller.save_as()
        assert list(tmp_path.glob("*.cpp")) == []

    def test_suggestion_is_the_current_file(self, qapp, tmp_path, monkeypatch):
        """已经关联了文件时，"另存为"默认还是它，而不是回到 main.cpp。"""
        seen: list[str] = []
        controller, _, _ = make_controller(qapp, tmp_path)
        controller.adopt(str(tmp_path / "mine.cpp"), "int x;")

        def fake(_parent, _caption, suggested, _filter):
            seen.append(suggested)
            return "", ""

        monkeypatch.setattr(codefile.QFileDialog, "getSaveFileName", staticmethod(fake))
        controller.save_as()
        assert seen == [str(tmp_path / "mine.cpp")]

    def test_write_failure_is_reported(self, qapp, tmp_path, monkeypatch):
        controller, _, notify = make_controller(qapp, tmp_path, text="int x;")
        monkeypatch.setattr(codefile.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(tmp_path / "no" / "x.cpp"), "")))
        assert not controller.save_as()
        assert notify.messages and notify.messages[0][0] == "error"

    def test_directory_is_remembered(self, qapp, tmp_path, monkeypatch):
        """下次对话框要还从这个目录开始 —— 否则每次都得从头翻目录树。"""
        target = tmp_path / "out.cpp"
        controller, _, _ = make_controller(qapp, tmp_path, text="int x;")
        monkeypatch.setattr(codefile.QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(target), "")))
        controller.save_as()
        assert controller._settings.get("code_directory") == str(tmp_path)


# ======================================================================
# 接到真实面板上
# ======================================================================

class TestWiredIntoThePanels:
    def _window(self, tmp_path_factory, monkeypatch):
        from offline_oj.context import AppContext
        from offline_oj.paths import build_paths
        from offline_oj.ui.main_window import MainWindow

        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("codefile")))
        return MainWindow(AppContext.create(build_paths().ensure_layout()))

    def test_solve_panel_has_the_buttons(self, qapp, tmp_path_factory, monkeypatch):
        window = self._window(tmp_path_factory, monkeypatch)
        try:
            assert window.solve_panel.open_code_button.text() == "打开…"
            assert window.solve_panel.save_code_button.text() == "另存为…"
        finally:
            window.problems_panel._set_dirty(False)
            window.close()

    def test_menus_offer_both_actions(self, qapp, tmp_path_factory, monkeypatch):
        from PySide6.QtGui import QAction

        window = self._window(tmp_path_factory, monkeypatch)
        try:
            labels = {action.text() for action in window.findChildren(QAction)}
            assert "打开代码文件…" in labels
            assert "代码另存为…" in labels
        finally:
            window.problems_panel._set_dirty(False)
            window.close()

    def test_opening_through_the_panel_reaches_the_editor(self, qapp, tmp_path_factory,
                                                          monkeypatch):
        target = tmp_path_factory.mktemp("code") / "ansi.cpp"
        target.write_bytes("\ufeffint main(){}".encode("utf-8"))
        window = self._window(tmp_path_factory, monkeypatch)
        try:
            panel = window.solve_panel
            monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                                staticmethod(lambda *a, **k: (str(target), "")))
            panel.open_code_file()
            assert panel.code_editor.toPlainText() == "int main(){}"
            assert panel.code_file_label.text() == "ansi.cpp"
        finally:
            window.problems_panel._set_dirty(False)
            window.close()

    def test_inserting_the_template_forgets_the_file(self, qapp, tmp_path_factory,
                                                     monkeypatch):
        """模板把正文整体换掉了，就不该再声称"我打开的是 a.cpp"。"""
        target = tmp_path_factory.mktemp("code2") / "a.cpp"
        target.write_text("int main(){}", encoding="utf-8")
        window = self._window(tmp_path_factory, monkeypatch)
        try:
            panel = window.solve_panel
            monkeypatch.setattr(codefile.QFileDialog, "getOpenFileName",
                                staticmethod(lambda *a, **k: (str(target), "")))
            panel.open_code_file()
            assert panel.code_files.path
            panel.insert_example()
            assert panel.code_files.path == ""
            assert panel.code_file_label.text() == ""
        finally:
            window.problems_panel._set_dirty(False)
            window.close()
