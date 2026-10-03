"""快捷键不能撞车 —— 这类问题在界面上完全看不出来。

真踩到的一次：写题面板的 ``Ctrl+L``（清空代码）和"测验"菜单里的
``Ctrl+L``（跳到局域网测验页）是同一个键序列。两个都是窗口级快捷键，
按下去到底执行哪个由 Qt 判歧义，实测会打
``Ambiguous shortcut overload`` 并且**两个都不一定生效** ——
用户只会觉得"这个键有时候管用、有时候不管用"。

这种冲突靠人工走查很难发现（要一个个菜单点开对），但机器一扫就出来了。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QAction, QShortcut
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(tmp_path_factory, monkeypatch) -> MainWindow:
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("keys")))
    return MainWindow(AppContext.create(build_paths().ensure_layout()))


@pytest.fixture(scope="module")
def window(qapp, tmp_path_factory):
    """整轮共用同一个窗口。

    快捷键是窗口装配时就绑死的静态属性，装一次和装三次扫出来的东西一样 ——
    而这台机器上装一个 ``MainWindow`` 要好几秒。以前每个用例各装一个，
    光这一个文件就吃掉全量跑的一大块时间。

    ``pytest.MonkeyPatch()`` 直接实例化（而不是用 ``monkeypatch`` 夹具）是
    因为后者是函数级作用域，配不了模块级夹具。**收尾必须 ``undo()``** ——
    ``OFFLINE_OJ_HOME`` 是进程级环境变量，留着会把同一轮里的其他测试模块
    指到这个临时目录上。
    """
    patch = pytest.MonkeyPatch()
    win = None
    try:
        win = _make_window(tmp_path_factory, patch)
        yield win
    finally:
        if win is not None:
            # 关窗口前清掉"会弹模态框"的状态，否则离屏环境挂死
            if win.exam_panel._server is not None:
                win.exam_panel.close_room(quiet=True)
            win.problems_panel._set_dirty(False)
            win.close()
        patch.undo()


def collect_shortcuts(window) -> dict[str, list[str]]:
    """把所有非空快捷键收成 {键序列: [出处...]}。

    两个来源都要看：菜单/工具栏用 ``QAction``，面板内部用 ``QShortcut``。
    撞车往往就发生在"一边在菜单、一边在面板"这种跨层的地方。
    """
    found: dict[str, list[str]] = {}

    def add(sequence: str, where: str) -> None:
        if sequence:
            found.setdefault(sequence, []).append(where)

    for action in window.findChildren(QAction):
        add(action.shortcut().toString(), f"QAction({action.text() or '无标题'})")
    for shortcut in window.findChildren(QShortcut):
        add(shortcut.key().toString(), f"QShortcut({shortcut.parent().__class__.__name__})")
    return found


class TestNoShortcutCollisions:
    def test_every_shortcut_is_bound_once(self, window):
        found = collect_shortcuts(window)
        clashing = {key: places for key, places in found.items() if len(places) > 1}
        assert not clashing, "这些快捷键被绑了多次（按下去由 Qt 判歧义）：\n" + "\n".join(
            f"  {key}: " + " / ".join(places) for key, places in clashing.items())

    def test_the_check_actually_sees_shortcuts(self, window):
        """防止上面的断言因为"一个都没扫到"而永远通过。"""
        found = collect_shortcuts(window)
        assert len(found) >= 8, f"只扫到 {len(found)} 个快捷键，检查可能没生效：{found}"

    def test_clear_code_and_the_quiz_tab_do_not_share_a_key(self, window):
        """点出这一组的具体名字，免得以后有人"顺手"把它改回去。"""
        found = collect_shortcuts(window)
        assert "Ctrl+L" in found, "Ctrl+L（清空代码）不见了"
        assert all("清空代码" in place or "QShortcut" in place
                   for place in found["Ctrl+L"]), \
            f"Ctrl+L 被别的东西占了：{found['Ctrl+L']}"
