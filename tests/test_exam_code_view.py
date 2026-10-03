"""主机端「提交与代码」页：老师看得到学生交上来的源码。

这个功能有两半，"学生只有榜单"那一半在 net 层就锁死了 ——
``Submission.to_dict()`` 不含 ``code``，源码在进入密文之前就没了，
真链路的证据在 ``test_net_lan.py`` 里。这里管的是另一半：源码到了主机内存
之后，界面怎么把它安稳地交到老师手里。

"安稳"是这一页唯一的技术难点。提交列表每几秒就会因为别人的提交而重排，
而重填表格会把选中清掉、把滚动位置打回开头 —— 老师正盯着一份代码的时候
页面自己跳走，那这一页在活跃的考场里就没法用。所以下面的用例集中盯三件事：

* 选中跟着**提交编号**走，不跟着行号（新提交插到最前面会把行号整体推下去）；
* 内容没变就不重灌（源码重灌一次，光标和滚动位置就回开头）；
* 关房间就把缓存丢掉（下一场是另一批学生）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.core.models import Language  # noqa: E402
from offline_oj.net.server import build_exam_problem  # noqa: E402
from offline_oj.net.session import ExamSession  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import TAB_EXAM, MainWindow  # noqa: E402
from offline_oj.ui.panels.exam_panel import (  # noqa: E402
    HOST_TAB_ARCHIVE,
    HOST_TAB_CODE,
    HOST_TAB_LOG,
    HOST_TAB_OVERALL,
    HOST_TAB_PROBLEM,
    HOST_TAB_ROSTER,
    STUDENT_TAB_BOARD,
    STUDENT_TAB_LOG,
    STUDENT_TAB_MINE,
    SUBMISSION_COLUMNS,
)

CODE_TAB_NAME = "提交与代码"


def make_problem():
    """题目在函数里 import：``TestCase`` 这个名字会被 pytest 当测试类收集。"""
    from offline_oj.core.models import Problem, TestCase

    return Problem(
        id="P0001",
        title="两数求和",
        description="读入两个整数，输出它们的和。",
        time_limit=1000,
        memory_limit=128,
        testcases=[TestCase(input="1 2\n", output="3\n")],
    )


# ======================================================================
# 装置
# ======================================================================


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("codeview")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    yield win
    # 两处会弹模态框的状态必须清掉：离屏环境下没人点得到那个框，
    # 整轮测试就挂在那里了。
    if win.exam_panel._server is not None:
        win.exam_panel.close_room(quiet=True)
    win.problems_panel._set_dirty(False)
    win.close()


class _FakeServer:
    """主机端视图要读的几个属性，不含端口与线程。

    这一页要验的是"提交列表里的那份源码有没有变成老师眼前的代码"，
    与端口监听无关 —— 真实的主机链路在 ``test_net_lan.py`` 里已经跑过了。
    """

    def __init__(self) -> None:
        self.port = 50800
        self.judged_count = 0
        self.stopped = False

    def stop(self, timeout: float = 1.0) -> None:
        self.stopped = True

    def lan_addresses(self) -> list[str]:
        return ["127.0.0.1:50800"]

    def connected_devices(self) -> list[str]:
        return []


def attach_room(panel, *, room_code: str = "246810", title: str = "讲评"):
    """装上一台主机和一个全新的会话，返回那个会话。

    真实路径是「开启房间」构造 :class:`ExamServer`，这里把端口与线程省掉：
    这一页关心的是"会话里的提交怎么变成老师眼前的源码"。
    """
    session = ExamSession(f"S-{room_code}", title=title, room_code=room_code)
    session.set_problems([build_exam_problem(make_problem())])
    panel._server = _FakeServer()
    panel._session = session
    panel._refresh_host_views()
    return session


@pytest.fixture
def host(window):
    """切到考试页并装上一台假主机，返回面板。"""
    window.switch_tab(TAB_EXAM)
    panel = window.exam_panel
    attach_room(panel)
    return panel


def add_submission(panel, *, user: str, device: str, code: str,
                   language: str = "cpp", verdict: str = "AC", score: int = 100,
                   minutes_ago: int = 0, message: str = ""):
    """往主机的会话里放一次提交（走真实的 ``new_submission``，次数由它算）。"""
    session = panel._session
    item = session.new_submission(device, user, "P0001", language, code)
    item.submitted_at = datetime.now() - timedelta(minutes=minutes_ago)
    item.verdict = verdict
    item.score = score
    item.judged_at = item.submitted_at + timedelta(seconds=2)
    item.message = message or f"全部通过 · {verdict} · 1/1 测试点通过"
    session.record_submission(item)
    return item


def cell(panel, row: int, column: int) -> str:
    item = panel.host_code_table.item(row, column)
    return item.text() if item is not None else ""


# ======================================================================
# 列表
# ======================================================================


class TestSubmissionList:
    def test_lists_every_submission_newest_first(self, host):
        old = add_submission(host, user="张三", device="DEV00001",
                             code="// 张三的", minutes_ago=5)
        new = add_submission(host, user="李四", device="DEV00002", code="// 李四的")

        host._refresh_host_views()

        table = host.host_code_table
        assert table.rowCount() == 2
        assert cell(host, 0, 0) == str(new.serial), "最新的一份该在最上面"
        assert cell(host, 1, 0) == str(old.serial)

    def test_rows_carry_what_the_teacher_needs_to_pick(self, host):
        add_submission(host, user="张三", device="DEV00001", code="int main(){}",
                       language="python")
        host._refresh_host_views()

        assert host.host_code_table.columnCount() == len(SUBMISSION_COLUMNS)
        # 列序以 SUBMISSION_COLUMNS 为准，这里只挑几列验内容 —— 全列硬编码
        # 一遍的话，将来调整列序就得改两处，而其中一处是测试。
        index = {name: position for position, name in enumerate(SUBMISSION_COLUMNS)}
        assert cell(host, 0, index["用户名"]) == "张三"
        assert cell(host, 0, index["设备 ID"]) == "DEV00001"
        assert cell(host, 0, index["题目"]) == "P0001"
        assert cell(host, 0, index["语言"]) == "Python"
        assert cell(host, 0, index["结论"]) == "AC"
        assert cell(host, 0, index["得分"]) == "100"

    def test_a_pending_submission_shows_a_blank_verdict(self, host):
        """还没判完的提交不能被显示成"通过了"。"""
        add_submission(host, user="张三", device="DEV00001", code="int main(){}",
                       verdict="", score=0)
        host._refresh_host_views()

        assert cell(host, 0, SUBMISSION_COLUMNS.index("结论")) == "…"

    def test_the_list_grows_when_someone_else_submits(self, host):
        add_submission(host, user="张三", device="DEV00001", code="// 1")
        host._refresh_host_views()
        assert host.host_code_table.rowCount() == 1

        add_submission(host, user="李四", device="DEV00002", code="// 2")
        host._refresh_host_views()
        assert host.host_code_table.rowCount() == 2


# ======================================================================
# 读代码
# ======================================================================


class TestReadingCode:
    def test_opens_the_newest_submission_by_default(self, host):
        """第一次进这一页就有东西看，而不是一块"请先选择"的空白。"""
        add_submission(host, user="张三", device="DEV00001", code="// 旧的",
                       minutes_ago=3)
        add_submission(host, user="李四", device="DEV00002", code="// 新的")

        host._refresh_host_views()

        assert host.host_code_view.toPlainText() == "// 新的"
        assert "李四" in host.host_code_caption.text()
        assert host.copy_code_button.isEnabled(), "有东西在看就该能复制"

    def test_picking_another_row_swaps_the_code(self, host):
        old = add_submission(host, user="张三", device="DEV00001",
                             code="// 张三的", minutes_ago=5)
        add_submission(host, user="李四", device="DEV00002", code="// 李四的")
        host._refresh_host_views()
        assert host.host_code_view.toPlainText() == "// 李四的"

        host._select_serial(old.serial)

        assert host.host_code_view.toPlainText() == "// 张三的", \
            "点另一行之后源码区没跟着换"
        assert "张三" in host.host_code_caption.text()

    def test_the_editor_follows_the_submitted_language(self, host):
        add_submission(host, user="张三", device="DEV00001",
                       code="print(1)", language="python")
        host._refresh_host_views()
        assert host.host_code_view._language is Language.PYTHON

        other = add_submission(host, user="李四", device="DEV00002",
                               code="int main(){}", language="cpp")
        host._refresh_host_views()
        host._select_serial(other.serial)
        assert host.host_code_view._language is Language.CPP, \
            "换一份代码要换高亮语言，否则关键字按错误的语言着色"

    def test_the_verdict_note_shows_the_judge_message(self, host):
        add_submission(host, user="张三", device="DEV00001", code="int main(){}",
                       verdict="CE", score=0,
                       message="编译失败 · CE Compile Error\nmain.cpp:1: 错误：缺少分号")
        host._refresh_host_views()

        note = host.host_verdict_view.toPlainText()
        assert "编译失败" in note
        assert "缺少分号" in note, "编译错误的多行内容要被完整带出来"

    def test_copy_puts_the_code_on_the_clipboard(self, host):
        add_submission(host, user="张三", device="DEV00001",
                       code="// 讲评用的代码")
        host._refresh_host_views()

        host.copy_selected_code()

        assert QApplication.clipboard().text() == "// 讲评用的代码"


# ======================================================================
# 刷新不打断
# ======================================================================


class TestRefreshDoesNotInterrupt:
    def test_a_new_submission_does_not_kick_you_off_the_one_you_are_reading(self, host):
        """这条是整页的关键：别人的提交把行号整体推下去，老师看的那份不能变。"""
        mine = add_submission(host, user="张三", device="DEV00001",
                              code="// 老师正在看这一份", minutes_ago=5)
        add_submission(host, user="李四", device="DEV00002", code="// 李四的")
        host._refresh_host_views()
        host._select_serial(mine.serial)
        assert host.host_code_view.toPlainText() == "// 老师正在看这一份"

        add_submission(host, user="王五", device="DEV00003", code="// 王五的")
        host._refresh_host_views()

        assert host.host_code_table.rowCount() == 3
        assert host.host_code_view.toPlainText() == "// 老师正在看这一份", \
            "刷新把老师正在看的那份换掉了"
        assert host._selected_serial() == mine.serial, \
            "选中要跟着提交编号走；跟行号走的话，新提交一来就指到别人身上了"
        assert host.host_code_view._language is Language.CPP

    def test_refresh_never_reloads_the_same_code(self, host, monkeypatch):
        """同一份不重灌文本。

        重灌一次 ``setPlainText`` 就把光标和滚动位置打回开头，而列表每几秒
        就会重填一次。这里不靠"看滚动条有没有跳"来验 —— 离屏环境下视口尺寸
        不可靠，滚动条可能压根滚不动，那样的断言会变成永真的假证据。
        """
        add_submission(host, user="张三", device="DEV00001", code="// 张三的")
        host._refresh_host_views()

        reloads: list[str] = []
        original = host.host_code_view.setPlainText
        monkeypatch.setattr(host.host_code_view, "setPlainText",
                            lambda text: (reloads.append(text), original(text))[1])

        assert host._code_serial is not None, "还没选中任何一份，这个用例就白跑了"
        host._refresh_host_views()
        host._refresh_host_views()
        assert reloads == [], "内容没变却重灌了源码，老师的阅读位置会丢"

        # 换一份时当然要重灌，否则上一条就退化成"永远不刷新"了
        other = add_submission(host, user="李四", device="DEV00002",
                               code="// 李四的")
        host._refresh_host_views()
        assert reloads == [], "新提交本身不该打断正在看的那一份"
        host._select_serial(other.serial)
        assert reloads == ["// 李四的"]

    def test_a_verdict_landing_updates_the_row_without_reloading_the_code(
            self, host, monkeypatch):
        """判定结果落下来时列表要跟着变，但那不算"换了一份"，不该重灌源码。"""
        item = add_submission(host, user="张三", device="DEV00001",
                              code="// 张三的", verdict="", score=0)
        host._refresh_host_views()
        assert cell(host, 0, SUBMISSION_COLUMNS.index("结论")) == "…"

        reloads: list[str] = []
        original = host.host_code_view.setPlainText
        monkeypatch.setattr(host.host_code_view, "setPlainText",
                            lambda text: (reloads.append(text), original(text))[1])

        item.verdict = "AC"
        item.score = 100
        host._refresh_host_views()

        assert cell(host, 0, SUBMISSION_COLUMNS.index("结论")) == "AC"
        assert cell(host, 0, SUBMISSION_COLUMNS.index("得分")) == "100"
        assert reloads == []


# ======================================================================
# 收尾
# ======================================================================


class TestCleanup:
    def test_closing_the_room_forgets_the_code(self, host):
        add_submission(host, user="张三", device="DEV00001", code="// 张三的")
        host._refresh_host_views()
        assert host.host_code_view.toPlainText() == "// 张三的"

        host.close_room(quiet=True)

        assert host.host_code_table.rowCount() == 0
        assert host.host_code_view.toPlainText() == ""
        assert host.host_verdict_view.toPlainText() == ""
        assert host.host_code_caption.text() == ""
        assert host._submission_by_serial == {}, "上一场的代码不该留到下一场"
        assert not host.copy_code_button.isEnabled()

    def test_next_room_starts_from_a_clean_page(self, host):
        """关掉再开一场，界面不会还翻得出上一场的提交。

        ``close_room`` 会把会话整个丢掉，所以列表空掉是"没有数据可填"，
        而不是"碰巧被清了一遍"。
        """
        add_submission(host, user="张三", device="DEV00001", code="// 上一场")
        host._refresh_host_views()
        host.close_room(quiet=True)

        attach_room(host, room_code="135790", title="第二场")
        add_submission(host, user="李四", device="DEV00004", code="// 这一场")
        host._refresh_host_views()

        assert host.host_code_table.rowCount() == 1
        assert host.host_code_view.toPlainText() == "// 这一场"


# ======================================================================
# 从哪一份开始看
# ======================================================================


class TestWhichOneYouGet:
    """列表是逐条攒起来的，所以"最新"在测验刚开始时指的是第 1 条。

    老师十分钟后才切进这一页，看到的该是那一刻最新的那份，而不是最早到达的。
    """

    def test_switching_into_the_page_lands_on_the_newest_submission(self, host):
        first = add_submission(host, user="张三", device="DEV00001", code="// 最早的")
        host._refresh_host_views()
        assert host._code_serial == first.serial, "第一条到达时它就是最新的"

        newest = add_submission(host, user="李四", device="DEV00002", code="// 最新的")
        host._refresh_host_views()
        assert host.host_code_view.toPlainText() == "// 最早的", \
            "这一条只管「别人提交不抢焦点」，和切页签是两件事"

        host.host_tabs.setCurrentIndex(HOST_TAB_CODE)   # 老师这才切到这一页

        assert host.host_code_view.toPlainText() == "// 最新的", \
            "切进来时该停在最新那份上，而不是最早到达的那份"
        assert host._selected_serial() == newest.serial

    def test_a_row_the_teacher_picked_is_never_taken_away(self, host):
        first = add_submission(host, user="张三", device="DEV00001", code="// 最早的")
        add_submission(host, user="李四", device="DEV00002", code="// 最新的")
        host._refresh_host_views()

        host._select_serial(first.serial)        # 老师亲手点了下面那一行

        host.host_tabs.setCurrentIndex(HOST_TAB_PROBLEM)
        host.host_tabs.setCurrentIndex(HOST_TAB_CODE)

        assert host.host_code_view.toPlainText() == "// 最早的", \
            "老师自己选过的，切页签不该给他换掉"


# ======================================================================
# 学生端
# ======================================================================


class TestTabIndexes:
    """页签下标一律用名字。

    ``host_tabs`` / ``student_tabs`` 以前是裸数字（``setCurrentIndex(2)``），
    往中间插一页之后那些数字会**静默**指到隔壁：不报错、不提示，只是跳到
    别的页上 —— 截图脚本拍到的就不是它以为的那页。这两条把常量和顺序钉在一起。
    """

    def test_host_tab_constants_match_the_page_order(self, host):
        tabs = host.host_tabs
        assert tabs.tabText(HOST_TAB_OVERALL) == "总分榜"
        assert tabs.tabText(HOST_TAB_PROBLEM) == "单题榜"
        assert tabs.tabText(HOST_TAB_CODE) == CODE_TAB_NAME
        assert tabs.tabText(HOST_TAB_ROSTER) == "名单"
        assert tabs.tabText(HOST_TAB_LOG) == "现场记录"
        assert tabs.tabText(HOST_TAB_ARCHIVE) == "历史场次"
        assert tabs.count() == 6, "多出来的页没有常量管着，等于又回到裸数字"

    def test_student_tab_constants_match_the_page_order(self, host):
        tabs = host.student_tabs
        assert tabs.tabText(STUDENT_TAB_MINE) == "我的提交"
        assert tabs.tabText(STUDENT_TAB_BOARD) == "排行榜"
        assert tabs.tabText(STUDENT_TAB_LOG) == "连接记录"
        assert tabs.count() == 3


class TestStudentSide:
    def test_only_the_host_side_has_this_page(self, host):
        panel = host
        host_titles = [panel.host_tabs.tabText(index)
                       for index in range(panel.host_tabs.count())]
        student_titles = [panel.student_tabs.tabText(index)
                          for index in range(panel.student_tabs.count())]

        assert CODE_TAB_NAME in host_titles
        assert CODE_TAB_NAME not in student_titles, \
            "学生端只有榜单；能读代码的地方一处都不该有"

    def test_the_student_editor_is_not_a_window_onto_other_peoples_code(self, host):
        """主机端这一页要看代码，但别顺手把那份代码塞进学生端共用的编辑器。"""
        add_submission(host, user="张三", device="DEV00001", code="// 别人的代码")
        host._refresh_host_views()

        assert host.code_editor.toPlainText() == ""
