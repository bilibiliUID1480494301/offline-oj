"""同一屏上不该把同一件事显示两遍。

用户连着两轮反馈都是同一类毛病：先是"UI 还是混乱"，再是"UI 重复以及拥挤"。
两次的根子都是**同一个东西在一屏上出现了两三次**：

* 全局工具栏的按钮 = 菜单项 + 面板自己的按钮（写题页一屏两个"提交代码"）；
* 题库统计"共 N 道题 · M 个测试点 · 最近更新 …"在状态栏和题目列表下各一份，
  一字不差；
* "当前 N 个测试点"（测试点栏脚）和"测试点数"（题目信息栏）是同一个数字；
* 编译器路径行的空输入框占位符和右侧状态徽标都写着"未配置"。

这类问题**在代码里逐处看都挺合理** —— 状态栏本来就该常显、面板里本来就该有按钮、
每一行路径后面本来就该有个状态。只有把它们同时渲染出来才看得出重复，
所以这里就用机器渲染、机器比对。

判定用的是一条朴素的规则：**字符串相同，就算重复；不看语义。**
因为两个地方同时显示同一件事时，用户要花时间确认"它们是不是一样的"，
而那份确认永远没有收益。留信息量更大、或者位置更顺的那一个。

唯一的例外是"重复的表格行"：设置页六行路径各带一个"打开"、编译器页五个
``PathPicker`` 各带一个"浏览…"。它们文本相同但**属于同一种行的同类部件**，
是在说"每一行都可以这么做"，不是在说两件不同的事。所以按
``(文本, 父控件类名)`` 分组 —— 只有跨了不同的父类还撞文本，才算重复。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QToolBar

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import TAB_NAMES, MainWindow  # noqa: E402

#: 短得像噪音的文本不参与比对。刻度、序号、单位这类重复是正常的。
MIN_LENGTH = 4


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(tmp_path_factory, monkeypatch) -> MainWindow:
    """建一个能用的主窗口，带两道题。"""
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("dedup")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    seed_repository(win)
    return win


@pytest.fixture(scope="module")
def window(qapp, tmp_path_factory):
    """整轮共用同一个窗口。

    这台机器上一个 ``MainWindow`` 要好几秒才装配得起来，而这里量的是"界面上
    有没有重复的字符串" —— 一个装配完就定住了的属性，装一次和装三次结论一样。
    以前每个用例各装一个，光这一个文件就吃掉全量跑的一大块时间。

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
            # 收尾的两条纪律：不清掉"会弹模态框"的状态，离屏环境没人点得到
            # 那个框，整轮测试就挂死了。
            if win.exam_panel._server is not None:
                win.exam_panel.close_room(quiet=True)
            win.problems_panel._set_dirty(False)
            win.close()
        patch.undo()


def seed_repository(window: MainWindow) -> None:
    """放两道题进去。

    题库为空时统计标签显示的是"题库为空"，那个字符串太短、也看不出重复；
    而且存题面板载入题目需要真的有一道题（空目录上 ``load_problem`` 会静默失败）。
    """
    from offline_oj.core.models import Problem, TestCase

    for index, title in enumerate(("两数求和", "回文判断"), start=1):
        window.ctx.repository.put(Problem(
            id=f"P000{index}",
            title=title,
            description=f"# {title}\n\n这是一道示例题。",
            time_limit=1000,
            memory_limit=128,
            testcases=[TestCase(input="1 2\n", output="3\n")],
        ))
    # 直接往 repository 里塞是绕过了界面的：状态栏那条统计是 _on_problems_changed
    # 刷新的，不发这个信号就会留着构造时的"题库为空"，断言 0 次出现必然失败
    # —— 而且失败得像是"统计没重复"，方向完全反了。
    window.problems_panel.problems_changed.emit()


def visible_labels(window: MainWindow) -> list[QLabel]:
    """当前这一页上真正看得见的标签。

    ``isVisibleTo`` 而不是 ``isVisible``：不需要真的 ``show()`` 窗口
    （离屏环境里把窗口显示出来只会多出"收尾要关掉什么"的麻烦），
    它只是问"如果我把窗口显示出来，这个控件会不会露出来"。
    QTabWidget 内部是 QStackedWidget，非当前页会被 ``hide()``，
    所以这样拿到的正好是当前选项卡上的那一份。
    """
    return [label for label in window.findChildren(QLabel) if label.isVisibleTo(window)]


def walk_tabs(window: MainWindow):
    """逐页走一遍，产出 (页名, 该页可见的标签)。"""
    for index, name in enumerate(TAB_NAMES):
        window.switch_tab(index)
        QApplication.processEvents()
        yield name, visible_labels(window)


# ======================================================================
# 没有第二条命令面
# ======================================================================


class TestNoSecondCommandSurface:
    def test_there_is_no_global_toolbar(self, window):
        """命令入口只有两层：菜单 + 面板就地按钮。

        这里曾经是一条工具栏，删过两次才删干净：先按选项卡把按钮收起，
        再整条去掉。它不携带任何自己的信息 —— 每个按钮都在菜单里有同键入口，
        而各面板本来就有就地的那一个。
        """
        toolbars = window.findChildren(QToolBar)
        assert not toolbars, (
            "主窗口又出现了工具栏：" +
            " / ".join(tb.windowTitle() or "无标题" for tb in toolbars) +
            "\n每个按钮都和菜单 + 面板按钮重复，别再把它加回来。"
        )


# ======================================================================
# 同一个字符串不在两个地方各显示一遍
# ======================================================================


class TestNoDuplicatedStrings:
    def test_repository_summary_appears_once_on_every_tab(self, window):
        """题库统计是"每页都能看到"的那一条，所以它只该出现在状态栏。"""
        window.problems_panel.load_problem("P0001")
        QApplication.processEvents()
        summary = window.ctx.repository.stats().as_text()
        assert summary and summary != "题库为空", f"统计文本没准备好：{summary!r}"

        for name, labels in walk_tabs(window):
            hits = [label for label in labels if label.text() == summary]
            assert len(hits) == 1, (
                f"「{name}」页上题库统计出现了 {len(hits)} 次：{summary!r}\n"
                "状态栏已经常显这一条了，面板里再来一份就是重复。"
            )

    def test_no_label_is_shown_in_two_different_places(self, window):
        """跨"地方"的重复标签：同一个字符串出现在两类不同的父控件下。"""
        window.problems_panel.load_problem("P0001")
        QApplication.processEvents()

        offenders: list[str] = []
        for name, labels in walk_tabs(window):
            buckets: dict[str, set[str]] = {}
            for label in labels:
                text = label.text().strip()
                if len(text) < MIN_LENGTH:
                    continue
                parent = label.parent()
                buckets.setdefault(text, set()).add(
                    type(parent).__name__ if parent is not None else "None")
            for text, parents in buckets.items():
                if len(parents) > 1:
                    offenders.append(f"「{name}」页 {text!r} 同时出现在 {sorted(parents)}")

        assert not offenders, (
            "同一个字符串在两个地方各显示了一遍：\n  " + "\n  ".join(offenders) +
            "\n\n留信息量更大、或者位置更顺的那个；"
            "如果它们本来就是「每行一个」的同类控件，见本文件开头的例外说明。"
        )
