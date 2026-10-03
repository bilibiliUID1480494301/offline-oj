"""雷同检测的界面侧：结果对话框 + 「提交与代码」页的入口。

``core/similarity.py`` 的测试（``test_similarity.py``）保证**判断是对的**；
这里保证**老师看得到、看得懂、并且不会被这个数字误导**。两件事的失败方式
完全不同 —— 前者是"漏报/误报"，后者是"按钮点不动"或者"结论看起来像判决"。

整个界面唯一不能出错的一句话是「这只是线索」：雷同检测指向的是具体的
学生，一份措辞像判决的报告会被直接拿去处理人。所以有一条用例专门盯着
那条提示在不在屏幕上。
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

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.core import similarity as sim  # noqa: E402
from offline_oj.core import similarity_export as sx  # noqa: E402
from offline_oj.net.server import build_exam_problem  # noqa: E402
from offline_oj.net.session import ExamSession  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import TAB_EXAM, MainWindow  # noqa: E402
from offline_oj.ui.similarity_dialog import (  # noqa: E402
    DIFF_ROW_LIMIT,
    DEFAULT_EXPORT_STEM,
    FMT_CHOICES,
    KIND_EXACT,
    KIND_SIMILAR,
    REVIEW_NOTICE,
    SimilarityDialog,
    findings,
)
from offline_oj.ui.theme import LIGHT  # noqa: E402

#: 够长的骨架与解法（token 数要超过 k=15，否则走的是"整串一个指纹"的
#: 退化分支，测不到骨架扣除）。
TEMPLATE = """\
#include <bits/stdc++.h>
using namespace std;
int main() {
    int n;
    scanf("%d", &n);
    printf("%d\\n", solve(n));
    return 0;
}
"""

LOOP_SUM = """\
int solve(int n) {
    int total = 0;
    for (int i = 1; i <= n; i++) total += i;
    return total;
}
"""


def make_problem():
    from offline_oj.core.models import Problem, TestCase

    return Problem(id="P0001", title="两数求和", description="求和。",
                   time_limit=1000, memory_limit=128,
                   testcases=[TestCase(input="1 2\n", output="3\n")])


def rows_for(*codes: tuple[str, str]) -> list[dict]:
    """``(名字, 代码)`` → analyse 认识的记录。"""
    return [
        {"serial": index + 1, "device_id": f"DEV{index + 1:04d}",
         "username": name, "problem_id": "P0001", "language": "cpp",
         "code": code, "score": 100, "verdict": "AC"}
        for index, (name, code) in enumerate(codes)
    ]


# ======================================================================
# 装置
# ======================================================================


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeServer:
    """主机端视图要读的几个属性，不含端口与线程（与 test_exam_code_view 同款）。"""

    def __init__(self) -> None:
        self.port = 50900
        self.judged_count = 0

    def stop(self, timeout: float = 1.0) -> None:
        pass

    def lan_addresses(self) -> list[str]:
        return ["127.0.0.1:50900"]

    def connected_devices(self) -> list[str]:
        return []


@pytest.fixture
def window(qapp, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("simui")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    yield win
    if win.exam_panel._server is not None:
        win.exam_panel.close_room(quiet=True)
    win.problems_panel._set_dirty(False)
    win.close()


@pytest.fixture
def host(window):
    """切到考试页、装一台假主机、把模态提示拆掉，返回面板。

    拆 ``notify`` 是离屏测试的老规矩：它连着主窗口的提示框，而
    ``QMessageBox.exec()`` 在没人点的情况下会永久阻塞。
    """
    window.switch_tab(TAB_EXAM)
    panel = window.exam_panel
    try:
        panel.notify.disconnect()
    except RuntimeError:
        pass
    session = ExamSession("S-1", title="讲评", room_code="135790")
    session.set_problems([build_exam_problem(make_problem())])
    panel._server = _FakeServer()
    panel._session = session
    panel._refresh_host_views()
    return panel


def add_submission(panel, *, user: str, device: str, code: str) -> None:
    session = panel._session
    item = session.new_submission(device, user, "P0001", "cpp", code)
    item.verdict = "AC"
    item.score = 100
    session.record_submission(item)
    panel._fill_submissions(force=True)


# ======================================================================
# findings：报告 → 表格行
# ======================================================================


def test_duplicate_group_becomes_one_row():
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    rows = findings(report)
    assert len(rows) == 1
    assert rows[0].kind == KIND_EXACT
    assert rows[0].percent == 100.0


def test_duplicate_group_members_are_kept_for_the_tooltip():
    """三人以上一字不差时，一行代表整组，但完整名单不能丢 ——
    老师要照着它去找人。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM),
                                  ("丙", TEMPLATE + LOOP_SUM)))
    rows = findings(report)
    assert len(rows) == 1
    assert len(rows[0].members) == 3
    assert rows[0].roster_text == "甲、乙、丙"
    assert "3 人" in rows[0].note


def test_duplicate_pair_is_not_listed_twice():
    """同一件事说两遍，会让人以为有两批人。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    rows = findings(report)
    assert [row.kind for row in rows] == [KIND_EXACT]
    assert not any(row.kind == KIND_SIMILAR for row in rows)


def test_similar_pair_appears_as_its_own_row():
    report = sim.analyse(rows_for(
        ("甲", TEMPLATE + LOOP_SUM),
        ("乙", TEMPLATE + LOOP_SUM.replace("solve", "calc").replace("total", "sum")),
        ("丙", TEMPLATE + "int w(int n) { while (n > 0) n--; return n; }"),
    ), min_similarity=0.0)
    rows = findings(report)
    assert rows, "改过名字的抄袭要能出现在表里"
    assert rows[0].kind == KIND_SIMILAR


# ======================================================================
# 对话框
# ======================================================================


@pytest.fixture
def dialog(qapp):
    made: list[SimilarityDialog] = []

    def build(report):
        one = SimilarityDialog(report, palette=LIGHT)
        made.append(one)
        return one

    yield build
    for one in made:
        one.close()
        one.deleteLater()


def test_the_dialog_says_this_is_only_a_clue(dialog):
    """**最重要的一条**：报告指向具体的人，措辞像判决就会被直接拿去处理人。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    one = dialog(report)
    labels = [child.text() for child in one.findChildren(type(one.summary_label))]
    assert any(REVIEW_NOTICE == text for text in labels)
    assert "人工复核" in REVIEW_NOTICE
    assert "抄袭" not in REVIEW_NOTICE


def test_dialog_rows_match_the_findings(dialog):
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    one = dialog(report)
    assert one.row_count() == len(findings(report)) == 1


def test_dialog_prints_the_report_notes_on_screen(dialog):
    """口径说明不能藏在菜单里 —— 老师要能当场看到"扣了什么、为什么有些题没扣"。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM),
                                  ("丙", TEMPLATE + LOOP_SUM)))
    one = dialog(report)
    text = one.notes_label.text()
    assert "同一道题" in text
    assert "人工复核" in text


def test_dialog_summary_counts_are_visible(dialog):
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM),
                                  ("丙", TEMPLATE + "int w(int n) { return n; }")))
    one = dialog(report)
    summary = one.summary_label.text()
    assert "3 份提交" in summary
    assert "完全重复 1 组" in summary


def test_selecting_a_row_fills_the_side_by_side_diff(dialog):
    """从"数字"走到"证据"必须是一次点击的距离。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    one = dialog(report)
    assert one.diff_row_count() > 0, "选中第一行后应当已经铺好 diff"
    before = one.diff_row_count()
    one.select_row(0)
    assert one.diff_row_count() == before


def test_diff_shows_both_sides_indented(dialog):
    """并排 diff 要保留缩进 —— 去掉缩进两段代码摆在一起就读不懂了。

    刻意造一对**有一行差异**的代码：两边完全相同时 diff 会把中间折叠成
    ``…``，屏幕上只剩首尾两行，验不到缩进。
    """
    left = TEMPLATE + LOOP_SUM
    right = TEMPLATE + LOOP_SUM.replace("total += i;", "total -= i;")
    report = sim.analyse(rows_for(("甲", left), ("乙", right)))
    one = dialog(report)
    texts = [one.diff_table.item(row, 1).text()
             for row in range(one.diff_table.rowCount())
             if one.diff_table.item(row, 1) is not None]
    assert any(text.startswith("    ") for text in texts), texts[:5]


def test_dialog_without_findings_is_still_usable(dialog):
    """一潭死水（没有任何相似对）也是合法结果 —— 界面不能崩，也不能空着不说话。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM)))
    one = dialog(report)
    assert one.row_count() == 0
    assert one.diff_row_count() == 0
    assert one.summary_label.text()


def test_dialog_works_without_a_palette(qapp):
    """主题调色板没传进来时也不能崩（着色是装饰，不是功能）。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    one = SimilarityDialog(report)
    assert one.row_count() == 1
    one.close()
    one.deleteLater()


def test_very_long_diff_is_announced_not_silently_cut(dialog):
    """抄来的代码可能有几百行；超出渲染上限要**写出来**，不能悄悄截断 ——
    悄悄截断会让人以为"后面都一样"。"""
    long_left = TEMPLATE + "\n".join(f"int v{i} = {i};" for i in range(DIFF_ROW_LIMIT + 60))
    long_right = TEMPLATE + "\n".join(f"int v{i} = {i + 1};"
                                      for i in range(DIFF_ROW_LIMIT + 60))
    report = sim.analyse(rows_for(("甲", long_left), ("乙", long_right)))
    one = dialog(report)
    texts = [one.diff_table.item(row, column).text()
             for row in range(one.diff_table.rowCount())
             for column in (1, 3)
             if one.diff_table.item(row, column) is not None]
    assert any("仅显示前" in text for text in texts)


# ======================================================================
# 「提交与代码」页的入口
# ======================================================================


def test_button_has_a_glyph_hint_and_is_flat(host):
    """入口在「提交与代码」页顶部，带省略号（点了会开对话框）。"""
    assert host.similarity_button.text().endswith("…")
    assert host.similarity_button.toolTip()


def test_button_is_disabled_until_there_is_something_to_compare(host):
    host._fill_submissions(force=True)
    assert not host.similarity_button.isEnabled()


def test_button_turns_on_once_a_submission_arrives(host):
    add_submission(host, user="甲", device="AAAA", code=TEMPLATE + LOOP_SUM)
    assert host.similarity_button.isEnabled()


def test_button_is_disabled_again_after_the_room_closes(host):
    add_submission(host, user="甲", device="AAAA", code=TEMPLATE + LOOP_SUM)
    assert host.similarity_button.isEnabled()
    host._clear_host_views()
    assert not host.similarity_button.isEnabled()


def test_rows_carry_the_source_code(host):
    """``core`` 不 import ``net``，所以界面这侧要把 Submission 转成 dict ——
    漏了 ``code`` 的话，分析能跑但每条提交都没源码，报告永远是空的。"""
    add_submission(host, user="甲", device="AAAA", code=TEMPLATE + LOOP_SUM)
    rows = host._similarity_rows()
    assert rows and rows[0]["code"] == TEMPLATE + LOOP_SUM
    assert rows[0]["device_id"] == "AAAA"
    assert rows[0]["problem_id"] == "P0001"


def test_problem_titles_come_from_the_session(host):
    assert host._problem_titles() == {"P0001": "两数求和"}


class _Pickup:
    """替身对话框：只记下报告，不进事件循环（离屏没人点得动模态框）。

    它同时是"对话框"（有 ``exec``）和"构造函数"（可调用）—— 这样把它替到
    ``_similarity_dialog`` 上之后，调用点与真实情形一模一样。
    """

    def __init__(self) -> None:
        self.report = None
        self.shown = 0

    def __call__(self, report):
        self.report = report
        return self

    def exec(self) -> int:
        self.shown += 1
        return 0


def test_check_runs_on_the_current_source_and_opens_the_dialog(host, monkeypatch):
    add_submission(host, user="甲", device="AAAA", code=TEMPLATE + LOOP_SUM)
    add_submission(host, user="乙", device="BBBB", code=TEMPLATE + LOOP_SUM)
    picked = _Pickup()
    monkeypatch.setattr(host, "_similarity_dialog", picked)
    host.run_similarity_check()
    assert picked.shown == 1
    assert picked.report is not None
    assert picked.report.exact_groups, "两份一模一样的代码要被认出来"


def test_check_reports_when_there_is_nothing_to_compare(host, monkeypatch):
    """没有提交时点按钮要说话，而不是弹一个空报告。"""
    picked = _Pickup()
    monkeypatch.setattr(host, "_similarity_dialog", picked)
    notices: list[tuple[str, str]] = []
    host.notify.connect(lambda level, text: notices.append((level, text)))
    host.run_similarity_check()
    assert picked.shown == 0
    assert notices and notices[0][0] == "warning"


def test_check_reports_when_no_submission_has_code(host, monkeypatch):
    """档案可以勾「只存成绩不存代码」，那时提交在、源码没了。"""
    add_submission(host, user="甲", device="AAAA", code="int main(){}")
    host._session.submissions[0].code = ""
    picked = _Pickup()
    monkeypatch.setattr(host, "_similarity_dialog", picked)
    notices: list[tuple[str, str]] = []
    host.notify.connect(lambda level, text: notices.append((level, text)))
    host.run_similarity_check()
    assert picked.shown == 0
    assert notices and "源码" in notices[0][1]


def test_button_click_is_wired_to_the_handler(host, monkeypatch):
    add_submission(host, user="甲", device="AAAA", code=TEMPLATE + LOOP_SUM)
    calls: list[int] = []
    monkeypatch.setattr(host, "run_similarity_check", lambda: calls.append(1))
    host.similarity_button.click()
    assert calls == [1]


# ======================================================================
# 导出按钮：对话框自己要能导出
# ======================================================================

def test_dialog_exposes_export_button(qapp):
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    dialog = SimilarityDialog(report, palette=LIGHT)
    assert dialog.export_button is not None
    assert dialog.export_button.text() == "导出…"
    # 默认导出文件名（不含后缀）要填好，老师改都不用改就能导
    assert DEFAULT_EXPORT_STEM
    assert FMT_CHOICES


def test_export_to_writes_a_file(qapp, tmp_path):
    """按钮接线用的最直接证明：调 export_to 真写出文件、且带声明。"""
    report = sim.analyse(rows_for(("甲", TEMPLATE + LOOP_SUM),
                                  ("乙", TEMPLATE + LOOP_SUM)))
    dialog = SimilarityDialog(report, palette=LIGHT)
    target = tmp_path / "雷同检测报告.txt"
    out = dialog.export_to(target, "txt")
    assert out and out[0].exists()
    assert sx.EXPORT_NOTICE in out[0].read_text(encoding="utf-8-sig")


def test_fmt_from_dialog_suffix_wins_over_filter(qapp):
    """用户在 txt 筛选器下手打了一个 .pdf 名，按后缀认成 pdf。"""
    assert SimilarityDialog._fmt_from_dialog("report.pdf", "文本文件 (*.txt)") == "pdf"


def test_fmt_from_dialog_falls_back_to_selected_filter(qapp):
    """用户没打后缀、平台也没补：按选中的筛选器兜底成 xlsx。"""
    assert (SimilarityDialog._fmt_from_dialog("report", "Excel 工作簿 (*.xlsx)")
            == "xlsx")
