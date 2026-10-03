"""考场身份与管控的界面层：进场方式、名单编辑器、离场锁屏盖板。

逻辑层（``core/roster.py``、``net/session.py``、真 TCP 的握手与锁屏）在
``test_roster.py`` / ``test_net_session.py`` / ``test_net_lan.py`` 里已经测穿，
这里只测**界面把它接对了没有**：

* 名单能**在 APP 里直接建**（不必先去画一张 Excel 表），保存后下次开面板还在；
* 选了「账号进场」却没名单时，开房要被**挡在开房这一步**——那时老师还看得见提示；
* 学生端两种进场方式只露出一套输入框，账号进场时**没有用户名可填**；
* 「离开一下」盖住的是整个学生区，口令**发到主机核对**（本机不比对），
  而且在本机连一个可能的口令都没有时，这个按钮不许把屏幕盖上。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QTableWidgetItem

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.context import AppContext  # noqa: E402
from offline_oj.core.roster import Contestant, Roster  # noqa: E402
from offline_oj.net.session import EntryMode, ExamSession  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import TAB_EXAM, MainWindow  # noqa: E402
from offline_oj.ui.roster_dialog import RosterDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_window(tmp_path_factory) -> MainWindow:
    patch = pytest.MonkeyPatch()
    patch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("access")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    for index in (1, 2):
        from offline_oj.core.models import Problem, TestCase
        win.ctx.repository.put(Problem(
            id=f"P000{index}",
            title=f"题目 {index}",
            description=f"# 题目 {index}",
            time_limit=1000,
            memory_limit=128,
            testcases=[TestCase(input="1 2\n", output="3\n")],
        ))
    win.problems_panel.problems_changed.emit()
    # 收尾挂在窗口上，夹具 finally 里用得到
    win._test_patch = patch
    return win


@pytest.fixture(scope="module")
def window(qapp, tmp_path_factory):
    win = None
    try:
        win = _make_window(tmp_path_factory)
        yield win
    finally:
        if win is not None:
            panel = win.exam_panel
            if panel._server is not None:
                panel.close_room(quiet=True)
            if panel._client is not None or panel._worker is not None:
                panel._teardown_client()
            win.problems_panel._set_dirty(False)
            win.close()
            win._test_patch.undo()


@pytest.fixture
def panel(window):
    """切到考试页并**拆掉 notify 的模态接收者**。

    ``MainWindow.notify`` 对 info 级也弹模态框，离屏环境里 ``exec()`` 会永久
    阻塞（这条纪律是拿一次 10 分钟挂死换来的）。用例想收提示就自己接槽。
    """
    window.switch_tab(TAB_EXAM)
    panel = window.exam_panel
    try:
        panel.notify.disconnect()
    except RuntimeError:
        pass                                   # 已经拆过了
    seen: list[tuple[str, str]] = []
    panel.notify.connect(lambda level, text: seen.append((level, text)))
    panel._notes = seen
    # 每条用例都从"干净的开房前状态"出发：上一个用例留下的名单与进场方式
    # 不能漏到这里，否则"没有名单就开不了房"这条守卫会被悄悄跳过。
    panel._roster = None
    panel.entry_mode_combo.setCurrentIndex(0)
    yield panel
    if panel._server is not None:
        panel.close_room(quiet=True)
    panel._client = None
    panel._roster = None
    panel.entry_mode_combo.setCurrentIndex(0)
    panel._apply_lock_state()


def a_roster() -> Roster:
    return Roster(title="高一(3)班", contestants=[
        Contestant(account="zhangsan", name="张三", passcode="135790",
                   seat="A-01"),
        Contestant(account="lisi", name="李四", passcode="246810", seat="A-02"),
    ])


# ======================================================================
# 建场：进场方式与名单
# ======================================================================


class TestEntryModeSetup:
    def test_the_default_is_still_the_room_code(self, panel):
        assert panel._entry_mode is EntryMode.ROOM_CODE
        assert panel.roster_label.text() == "不需要名单"
        assert not panel.roster_button.isEnabled()

    def test_switching_to_accounts_lights_up_the_roster_row(self, panel):
        panel.entry_mode_combo.setCurrentIndex(1)
        assert panel.roster_label.text() == "未选择名单"
        assert panel.roster_button.isEnabled()
        panel.entry_mode_combo.setCurrentIndex(0)
        assert panel.roster_label.text() == "不需要名单"

    def test_a_chosen_roster_shows_its_name_and_size(self, panel):
        panel._roster = a_roster()
        panel.entry_mode_combo.setCurrentIndex(1)
        assert panel.roster_label.text() == "高一(3)班 · 2 人"
        panel.entry_mode_combo.setCurrentIndex(0)
        assert panel.roster_label.text() == "不需要名单"


class TestOpenRoomGuard:
    def test_account_mode_without_a_roster_is_blocked_before_opening(self, panel):
        panel.entry_mode_combo.setCurrentIndex(1)
        panel._set_all_checked(True)
        panel.open_room()
        assert panel._server is None
        assert any("名单" in text for _level, text in panel._notes)

    def test_account_mode_opens_with_the_roster_loaded(self, panel):
        panel._roster = a_roster()
        panel.entry_mode_combo.setCurrentIndex(1)
        panel._set_all_checked(True)
        panel.open_room()
        assert panel._server is not None
        session = panel._session
        assert session.entry_mode is EntryMode.ACCOUNT
        assert set(session.contestants) == {"zhangsan", "lisi"}
        # 开房期间名单按钮被冻结：主机已经按这份名单在认人
        assert not panel.roster_button.isEnabled()

    def test_room_code_mode_does_not_need_a_roster(self, panel):
        panel.entry_mode_combo.setCurrentIndex(0)
        panel._set_all_checked(True)
        panel.open_room()
        assert panel._server is not None
        assert panel._session.entry_mode is EntryMode.ROOM_CODE
        assert panel._session.contestants == {}


class TestRosterButtonFreezing:
    def test_closing_the_room_hands_the_button_back(self, panel):
        panel._roster = a_roster()
        panel.entry_mode_combo.setCurrentIndex(1)
        panel._set_all_checked(True)
        panel.open_room()
        assert not panel.roster_button.isEnabled()
        panel.close_room(quiet=True)
        assert panel.roster_button.isEnabled()


# ======================================================================
# 名单编辑器：APP 内直接创建
# ======================================================================


class TestRosterDialog:
    def test_rows_can_be_typed_straight_into_the_app(self, panel):
        dialog = RosterDialog(panel.ctx)
        dialog.add_row()
        dialog.table.setItem(0, 0, QTableWidgetItem(" ZhangSan "))
        dialog.table.setItem(0, 1, QTableWidgetItem("张三"))
        dialog.name_edit.setText("高一(3)班")
        dialog.fill_passcodes()
        code = dialog.table.item(0, 2).text()
        assert len(code) == 6 and code.isdigit() and code[0] != "0"

        dialog.save()
        assert dialog._roster is not None
        # 保存后表格重灌成"主机将来认的那个写法"
        assert dialog.table.item(0, 0).text() == "zhangsan"
        assert dialog._roster.accounts() == ["zhangsan"]
        target = panel.ctx.paths.roster_file("高一(3)班")
        assert target.exists()
        dialog.deleteLater()

    def test_an_existing_roster_comes_back_prefilled(self, panel):
        roster = a_roster()
        dialog = RosterDialog(panel.ctx, roster=roster)
        assert dialog.table.rowCount() == 2
        assert dialog.table.item(0, 0).text() == "zhangsan"
        assert dialog.name_edit.text() == "高一(3)班"
        dialog.deleteLater()

    def test_duplicates_are_merged_and_disclosed(self, panel):
        dialog = RosterDialog(panel.ctx)
        dialog.name_edit.setText("重复")
        for _ in range(2):
            dialog.add_row()
        dialog.table.setItem(0, 0, QTableWidgetItem("a1"))
        dialog.table.setItem(1, 0, QTableWidgetItem("A1"))
        dialog.save()
        assert len(dialog._roster) == 1
        assert "合并" in dialog.status_label.text() or "丢" in dialog.status_label.text()
        # 存下来的文件里也只有一个人
        assert len(Roster.load(panel.ctx.paths.roster_file("重复"))) == 1
        dialog.deleteLater()

    def test_the_plaintext_warning_is_on_the_dialog(self, panel):
        dialog = RosterDialog(panel.ctx)
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        assert any("明文" in text for text in texts)
        dialog.deleteLater()

    def test_fill_passcodes_never_overwrites(self, panel):
        dialog = RosterDialog(panel.ctx)
        dialog.add_row()
        dialog.table.setItem(0, 0, QTableWidgetItem("a1"))
        dialog.table.setItem(0, 2, QTableWidgetItem("999999"))
        dialog.add_row()
        dialog.table.setItem(1, 0, QTableWidgetItem("a2"))
        dialog.fill_passcodes()
        assert dialog.table.item(0, 2).text() == "999999"
        assert len(dialog.table.item(1, 2).text()) == 6
        dialog.deleteLater()


# ======================================================================
# 学生端：两种进场方式
# ======================================================================


class TestStudentJoinMode:
    def test_only_one_set_of_fields_is_visible_at_a_time(self, panel):
        assert panel.join_cred_stack.currentIndex() == 0
        panel.join_mode_combo.setCurrentIndex(1)
        assert panel.join_cred_stack.currentIndex() == 1
        panel.join_mode_combo.setCurrentIndex(0)
        assert panel.join_cred_stack.currentIndex() == 0

    def test_account_join_without_an_account_is_refused_locally(self, panel):
        panel.address_edit.setText("127.0.0.1")
        panel.join_mode_combo.setCurrentIndex(1)
        panel.student_account_edit.clear()
        panel.join_room()
        assert panel._worker is None
        assert any("账号" in text for _level, text in panel._notes)

    def test_room_join_still_asks_for_the_username(self, panel):
        panel.join_mode_combo.setCurrentIndex(0)
        panel.address_edit.setText("127.0.0.1")
        panel.student_code_edit.setText("135790")
        panel.student_name_edit.clear()
        panel.join_room()
        assert panel._worker is None
        assert any("用户名" in text for _level, text in panel._notes)


# ======================================================================
# 离场锁屏（防窥屏）
# ======================================================================


def stub_client(*, locked: bool = False, reason: str = "离开中",
                using_account: bool = True) -> SimpleNamespace:
    return SimpleNamespace(locked=locked, lock_reason=reason,
                           using_account=using_account, closed=False)


class TestLockCover:
    def test_locking_covers_the_whole_student_area(self, panel):
        panel._client = stub_client(locked=True)
        panel._apply_lock_state()
        assert panel.student_stack.currentIndex() == 1
        assert "离开" in panel.lock_reason_label.text()
        # 解开就回去
        panel._client.locked = False
        panel._apply_lock_state()
        assert panel.student_stack.currentIndex() == 0
        panel._client = None

    def test_the_cover_carries_the_host_reason(self, panel):
        panel._client = stub_client(locked=True, reason="老师让你先盖一下")
        panel._apply_lock_state()
        assert "老师" in panel.lock_reason_label.text()
        panel._client = None
        panel._apply_lock_state()

    def test_unlock_refusal_is_shown_on_the_cover(self, panel):
        panel._client = stub_client(locked=True)
        panel._apply_lock_state()
        panel._on_unlock_reply({"ok": False, "message": "口令不对，还可以试 4 次"})
        assert panel.student_stack.currentIndex() == 1        # 还盖着
        assert "4" in panel.lock_hint_label.text()
        assert panel.lock_submit_button.isEnabled()
        panel._client = None
        panel._apply_lock_state()

    def test_unlock_success_opens_it(self, panel):
        panel._client = stub_client(locked=True)
        panel._apply_lock_state()
        # 真链路里 ``ExamClient._handle`` 先把自身状态置为已解锁，回执才到
        # 界面 —— 存根照这个顺序来。
        panel._client.locked = False
        panel._on_unlock_reply({"ok": True, "message": ""})
        assert panel.student_stack.currentIndex() == 0
        panel._client = None
        panel._apply_lock_state()

    def test_the_away_button_refuses_to_cover_without_a_key(self, panel):
        """盖上一块解不开的屏幕不是保护，是事故。"""
        panel._client = stub_client(locked=False, using_account=True)
        panel.student_passcode_edit.clear()
        panel.away_button.setEnabled(True)
        panel.leave_seat()
        assert panel.student_stack.currentIndex() == 0
        assert any("口令" in text for _level, text in panel._notes)
        panel._client = None
        panel._apply_lock_state()

    def test_submitting_is_refused_while_covered(self, panel):
        """盖着屏幕还能用快捷键交代码，锁屏就成了摆设 —— 快捷键是全局的。"""
        panel._client = stub_client(locked=True)
        panel._apply_lock_state()
        panel.submit_code()
        assert any("锁定" in text for _level, text in panel._notes)
        panel._client = None
        panel._apply_lock_state()


# ======================================================================
# 主机端「名单」页的监考动作
# ======================================================================


class StubServer:
    """替掉真主机：只记调用。**必须有 ``stop``** —— 测试收尾会关房，
    而 ``close_room`` 对它调用的对象一视同仁。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_locked(self, device_id: str, locked: bool) -> bool:
        self.calls.append((device_id, locked))
        return True

    def lock_all(self, locked: bool) -> int:
        self.calls.append(("*", locked))
        return 2

    def stop(self, timeout: float = 0.0) -> None:
        pass


class TestHostRosterTab:
    def ready_session(self, panel):
        session = ExamSession("s-ui", entry_mode=EntryMode.ACCOUNT)
        session.set_contestants([
            {"account": "zhangsan", "name": "张三", "passcode": "135790"}])
        session.authenticate("zhangsan", "AAAA1111")
        panel._session = session
        return session

    def test_the_board_shows_the_account_and_the_lock(self, panel):
        session = self.ready_session(panel)
        panel._fill_roster()
        table = panel.host_roster_table
        assert table.rowCount() == 1
        assert table.item(0, 0).text() == "AAAA1111"
        assert table.item(0, 2).text() == "zhangsan"
        assert table.item(0, 4).text() == "—"
        assert table.item(0, 0).data(Qt.UserRole) == "AAAA1111"

    def test_locking_someone_shows_up_on_the_board(self, panel):
        session = self.ready_session(panel)
        session.set_locked("AAAA1111")
        panel._fill_roster()
        assert panel.host_roster_table.item(0, 4).text() == "离开中"

    def test_the_teacher_can_lock_the_selected_person(self, panel):
        session = self.ready_session(panel)
        server = StubServer()
        panel._server = server
        panel._fill_roster()
        panel.host_roster_table.selectRow(0)
        panel.set_selected_lock(True)
        assert server.calls == [("AAAA1111", True)]

    def test_the_teacher_can_lock_everyone(self, panel):
        self.ready_session(panel)
        server = StubServer()
        panel._server = server
        panel.set_all_lock(True)
        assert server.calls == [("*", True)]

    def test_buttons_need_a_running_room(self, panel):
        self.ready_session(panel)
        panel._server = None
        panel._fill_roster()
        assert not panel.lock_one_button.isEnabled()
        assert not panel.lock_all_button.isEnabled()
        assert panel.roster_hint.text() == ""
