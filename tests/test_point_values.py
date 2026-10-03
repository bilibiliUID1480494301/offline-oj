"""测试点分值：从写题面板的那个数字框一路走到题库文件。

NOI 的给分方式里，**分值是逐个测试点填的**，题目满分是它们的和。这带来两个
容易出错的点，这一组用例就盯这两个：

* 分值必须**存得下来**。它是 ``TestCase`` 上新增的字段，落盘时又遵循"默认值
  不写"的约定（见 ``to_dict``）—— 少写一次不会报错，只会在下次打开题目时
  悄悄变回 10 分，而榜上所有分数的分母都跟着错。
* 合计要好算。分值是"一个一个填"的，出题人不可能自己心算"这道题一共多少分"，
  所以编辑区底下必须有一个跟着变的合计。

刻意**不建 ``MainWindow``**：装一个主窗口要好几秒，而这里验的是控件的取值与
回填，和主窗口的接线无关。面板那一层由 ``smoke_gui.py`` 端到端走一遍
（它打印的「满分 20」「20/60 分」就是这条链路的结果）。
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

# 别名不是洁癖：``TestCase`` 与 ``TestCaseRows`` 都以 ``Test`` 开头且带 ``__init__``，
# 模块级导入会被 pytest 当成本文件里的测试类，报 ``PytestCollectionWarning``。
# 就地导入也能避开，但这一个文件里它们出现几十次，别名更省事。
from offline_oj.core.models import (  # noqa: E402
    DEFAULT_TESTCASE_POINTS,
    Problem,
    TestCase as CaseModel,
)
from offline_oj.core.repository import ProblemRepository  # noqa: E402
from offline_oj.ui.widgets import TestCaseRows as PointRows  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def rows(qapp):
    widget = PointRows()
    yield widget
    widget.deleteLater()


# ======================================================================
# 控件本身
# ======================================================================


class TestTheEditor:
    def test_a_new_point_starts_at_the_default(self, rows):
        assert rows.count() == 1
        assert rows.values()[0].points == DEFAULT_TESTCASE_POINTS

    def test_the_spinner_value_is_what_gets_collected(self, rows):
        rows._rows[0].points_spin.setValue(37)
        assert rows.values()[0].points == 37

    def test_the_spinner_takes_the_whole_range(self, rows):
        """0 是合法的：一个点值 0 分是"这个点不计分"，不是"填错了"。

        上限给到 1000 而不是 100，是为了让"每题满分 100"这个约定能被凑出来
        （例如 100 个点各 1 分，或 4 个点各 25 分）。
        """
        spin = rows._rows[0].points_spin
        spin.setValue(0)
        assert rows.values()[0].points == 0
        spin.setValue(1000)
        assert rows.values()[0].points == 1000

    def test_load_backfills_the_spinner(self, rows):
        """回填是"打开一道存过的题"那条路，填不回去就成了静默重置。"""
        rows.load([CaseModel(input="a", output="1", points=25),
                   CaseModel(input="b", output="2", points=5)])
        assert [case.points for case in rows.values()] == [25, 5]

    def test_load_keeps_the_sample_flag(self, rows):
        """这一行没有编辑控件，但必须原样带回去 —— 丢了样例标记，学生就看不到样例。

        和分值是同一类问题：``values()`` 是新建一个 ``TestCase``，不记住就没了。
        """
        rows.load([CaseModel(input="a", output="1", sample=True, points=20)])
        case = rows.values()[0]
        assert case.sample is True
        assert case.points == 20


class TestTheTotal:
    """底下的「合计 N 分」。"""

    def test_it_sums_the_points(self, rows):
        rows.load([CaseModel(points=50), CaseModel(points=30), CaseModel(points=20)])
        assert rows.total_label.text() == "合计 100 分"

    def test_it_follows_an_edit(self, rows):
        rows._rows[0].points_spin.setValue(70)
        assert rows.total_label.text() == "合计 70 分"

    def test_it_follows_adding_a_point(self, rows):
        rows.add_row()
        assert rows.total_label.text() == f"合计 {DEFAULT_TESTCASE_POINTS * 2} 分"

    def test_it_follows_removing_a_point(self, rows):
        rows.load([CaseModel(points=50), CaseModel(points=30)])
        assert rows.total_label.text() == "合计 80 分"
        rows._remove_row(1)
        assert rows.total_label.text() == "合计 50 分"

    def test_it_counts_zero_point_rows_as_zero(self, rows):
        """0 分的点要真的算成 0，不能"当成没填过"回落到默认值。"""
        rows.load([CaseModel(points=0), CaseModel(points=40)])
        assert rows.total_label.text() == "合计 40 分"


# ======================================================================
# 落盘
# ======================================================================


class TestItSurvivesTheDisk:
    def test_the_point_values_come_back(self, tmp_path):
        """存一道题、换个仓库再读回来，分值必须逐个对得上。"""
        problems_file = tmp_path / "problems.json"
        resources = tmp_path / "res"
        problem = Problem(
            id="P0001", title="三点的题", description="",
            time_limit=1000, memory_limit=128,
            testcases=[CaseModel(input="1", output="1", points=50),
                       CaseModel(input="2", output="2", points=30),
                       CaseModel(input="3", output="3", points=20)],
        )
        repo = ProblemRepository(problems_file, resources).load()
        repo.put(problem)
        repo.save()
        assert problems_file.exists(), "题库没落盘，后面的读回全是假象"

        again = ProblemRepository(problems_file, resources).load().get("P0001")
        assert again is not None
        assert [case.points for case in again.testcases] == [50, 30, 20]
        assert again.total_points == 100

    def test_a_default_valued_case_writes_nothing_extra(self, tmp_path):
        """默认 10 分不写进文件 —— 老题库的字节不该因为这次改动而变化。"""
        import json

        problems_file = tmp_path / "problems.json"
        problem = Problem(id="P0001", title="默认的题", description="",
                          time_limit=1000, memory_limit=128,
                          testcases=[CaseModel(input="1", output="1")])
        repo = ProblemRepository(problems_file, tmp_path / "res").load()
        repo.put(problem)
        repo.save()

        payload = json.loads(problems_file.read_text(encoding="utf-8"))
        case = payload["P0001"]["testcases"][0]
        assert "points" not in case, case
        # 但读回来仍要是 10：不写在盘上不等于"没有分值"
        again = ProblemRepository(
            problems_file, tmp_path / "res").load().get("P0001")
        assert again.testcases[0].points == DEFAULT_TESTCASE_POINTS

    def test_a_problems_max_follows_its_points(self):
        """题目满分是各点之和，不再恒定 100。"""
        problem = Problem(id="P0001", title="t", description="",
                          time_limit=1000, memory_limit=128,
                          testcases=[CaseModel(points=25)] * 4)
        assert problem.total_points == 100


# ======================================================================
# 界面上的那一行文本
# ======================================================================


class TestScoreText:
    """``_fmt_score``：知道满分就写"37/50"，不知道就只写"37"。

    榜上的数字脱离满分读不出含义 —— "10 分"到底是满分还是刚及格，取决于这道题
    值多少分。但分母也不是总有：老载荷与外部判题器都可能不带 ``possible``，
    那时硬凑一个"/100"反而是错的。
    """

    @staticmethod
    def _fmt(score, possible=0):
        from offline_oj.ui.panels.exam_panel import _fmt_score

        return _fmt_score(score, possible)

    def test_with_a_denominator(self):
        assert self._fmt(37, 50) == "37/50"

    def test_without_a_denominator(self):
        assert self._fmt(37) == "37"
        assert self._fmt(37, 0) == "37"

    def test_zero_is_shown_as_zero(self):
        """0 分要显示成 0，不能变成空白 —— 空白看起来像"还没判"。"""
        assert self._fmt(0, 50) == "0/50"
        assert self._fmt(0) == "0"

    def test_a_missing_score_falls_back_to_zero(self):
        """载荷里没有 ``score``（畸形数据）时当 0 处理，不抛异常也不显示空白。

        正常路径走不到这里：一份都没交的人，榜上那一行的 ``score`` 是 0 而不是
        缺失（"考了 0 分"和"榜上无名"是两件事，见 ``test_net_session``）。
        """
        assert self._fmt(None, 50) == "0/50"
        assert self._fmt("", 50) == "0/50"
        assert self._fmt(None) == "0"
