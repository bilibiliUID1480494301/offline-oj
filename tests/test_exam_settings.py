"""主机端房间设置：改名 / 不放榜 / 本场限制（语言与功能开关）。

这三条都是"开房周边设置"，价值全在**接线上**：老师敲了名字、点了按钮、勾了限制，
``ExamSession`` 上到底有没有变。所以这里用替身 Server 记录调用，不去真开端口 ——
真的端到端链路（EXAM 帧广播、不踢人、**服务端拒绝不被允许的语言**）在
``test_net_lan.py`` 里另有覆盖。

放在同一个文件里是因为它们共用一份"装一个 MainWindow + 替身 Server"的脚手架：
装配一个窗口要好几秒，按特性拆成三个文件就要装三次。

页签下标一律走具名常量（``TAB_EXAM`` / ``HOST_TAB_*``）：往中间插一页，
裸数字会静默指到隔壁，脚本不报错、图看着也正常。
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
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import TAB_EXAM, MainWindow  # noqa: E402
from offline_oj.ui.panels.exam_panel import _board_text  # noqa: E402

# 注意：``offline_oj.core.models.TestCase`` 是题目的数据类，**不要**在模块顶层
# 导入它 —— pytest 看到名字以 Test 开头、又带 __init__ 的类会试着收集它，
# 报一条 PytestCollectionWarning。同理 ``Problem`` 放在下面夹具里就地导入。


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def window(qapp, tmp_path_factory):
    """整轮共用一个窗口 —— 装配一次好几秒，而这里量的是"接线对不对"。

    ``pytest.MonkeyPatch()`` 直接实例化（模块级夹具配不了函数级的
    ``monkeypatch``）；收尾必须 ``undo()``，``OFFLINE_OJ_HOME`` 是进程级
    环境变量，留着会把同轮其他测试模块指到这个临时目录上。
    """
    from offline_oj.core.models import Problem, TestCase

    patch = pytest.MonkeyPatch()
    patch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("rename")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    # 题库空了 open_room 会被前置校验挡下（"请先勾选至少一道题目"），
    # 那道分支会弹模态框，离屏环境没人点得到 → 挂死。所以必须预置题目。
    win.ctx.repository.put(Problem(
        id="P0001", title="两数求和", description="读入两个整数，输出它们的和。",
        time_limit=1000, memory_limit=128,
        testcases=[TestCase(input="1 2\n", output="3\n")]))
    win.problems_panel.problems_changed.emit()
    try:
        yield win
    finally:
        if win.exam_panel._server is not None:
            win.exam_panel.close_room(quiet=True)
        win.problems_panel._set_dirty(False)
        win.close()
        patch.undo()


@pytest.fixture
def panel(window):
    """切到考试页，跑完把房间关掉、控件状态复位 —— 窗口是整轮共用的。"""
    exam = window.exam_panel
    window.switch_tab(TAB_EXAM)
    yield exam
    if exam._server is not None:
        exam.close_room(quiet=True)
    # 学生端替身客户端也要摘掉：留着它，下一轮 `_apply_student_state` 会照着
    # 上一轮那份策略去改控件。
    exam._client = None
    exam._apply_host_state(running=False)
    # 备考状态那几条用例会把按钮改成「等待开考」、把倒计时挂出来，
    # 不复位就会渗进下一个用例。
    exam.submit_button.setText("提交代码")
    exam.submit_button.setToolTip("")
    exam.student_clock_label.setVisible(False)
    exam.student_status_label.setText("未加入房间")
    # "交完就锁"那条用例会把编辑器锁上，不还原就会渗进下一个用例。
    exam._editor_locked = False
    exam.code_editor.setReadOnly(False)
    # 勾选状态也复位：设置区是整轮共用的，一个用例取消掉的勾会渗进下一个用例。
    for box in exam.language_checks.values():
        box.setChecked(True)
    exam.copy_out_check.setChecked(True)
    exam.lock_after_submit_check.setChecked(False)


class _FakeServer:
    """只记录参数，不真的开端口。方法清单照着 exam_panel 的调用点抄。

    ``rename`` 是这次新增的：主机端界面点「改名」会走到它，替身把
    "改成了什么"记下来，同时转调真实 session 的 ``rename``（这样界面读到的
    ``session.title`` 与真链路一致）。
    """

    last_session = None
    last_config = None
    renamed_to: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        session = args[0] if args else kwargs.get("session")
        type(self).last_session = session
        type(self).last_config = kwargs.get("config")
        type(self).renamed_to = []
        config = kwargs.get("config")
        self.port = getattr(config, "port", 0) or 50800
        self.judged_count = 0

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 1.0) -> None:
        pass

    def lan_addresses(self) -> list[str]:
        return ["127.0.0.1:50800"]

    def connected_devices(self) -> list[str]:
        return []

    def broadcast_status(self) -> None:
        pass

    def broadcast_leaderboard(self) -> None:
        pass

    def collect_from_all(self, *args: object, **kwargs: object) -> None:
        pass

    def rename(self, title: str) -> str:
        applied = self.last_session.rename(title)
        type(self).renamed_to.append(applied)
        return applied


def _open_room(panel, monkeypatch) -> None:
    """用替身 Server 开房。"""
    from offline_oj.ui.panels import exam_panel as module

    monkeypatch.setattr(module, "ExamServer", _FakeServer)
    panel.refresh_problem_choices()
    panel._set_all_checked(True)
    panel.open_room()


# ======================================================================
# 不放榜开关
# ======================================================================


class TestLeaderboardSwitchUI:
    def test_open_room_passes_the_switch_to_the_session(self, panel, monkeypatch):
        _open_room(panel, monkeypatch)
        assert _FakeServer.last_session.show_leaderboard is True, "默认应当公开榜单"
        panel.close_room(quiet=True)

        panel.leaderboard_check.setChecked(False)
        panel.open_room()
        assert _FakeServer.last_session.show_leaderboard is False, \
            "取消勾选后会话仍公开榜单 —— 开关没接上"

    def test_the_switch_is_frozen_while_the_room_is_open(self, panel):
        panel._apply_host_state(running=True)
        assert not panel.leaderboard_check.isEnabled(), "开房后不该还能改放榜策略"
        panel._apply_host_state(running=False)
        assert panel.leaderboard_check.isEnabled()


# ======================================================================
# 改名
# ======================================================================


class TestRenameUI:
    def test_rename_button_only_lights_up_while_running(self, panel):
        panel._apply_host_state(running=False)
        assert not panel.rename_button.isEnabled()
        panel._apply_host_state(running=True)
        assert panel.rename_button.isEnabled(), "开房后「改名」应当可用"
        panel._apply_host_state(running=False)

    def test_only_the_name_stays_editable_while_running(self, panel):
        """名字是唯一允许开房期间改的设置：它只是显示标签，改它不踢人。"""
        panel._apply_host_state(running=True)
        assert panel.title_edit.isEnabled(), \
            "名字输入框被冻住了 —— 那「改名」就没有可改的来源"
        assert not panel.mode_combo.isEnabled()
        assert not panel.resubmit_check.isEnabled()
        assert not panel.leaderboard_check.isEnabled()

    def test_rename_applies_and_broadcasts(self, panel, monkeypatch):
        _open_room(panel, monkeypatch)
        panel.title_edit.setText("第三次模拟赛")
        panel.rename_room()
        assert panel._session.title == "第三次模拟赛"
        assert _FakeServer.renamed_to[-1] == "第三次模拟赛"
        # 房间号不受影响 —— 改名不该把学生踢下线
        assert panel._session.room_code

    def test_rename_ignores_the_same_name(self, panel, monkeypatch):
        _open_room(panel, monkeypatch)
        current = panel._session.title
        panel.title_edit.setText(current)
        panel.rename_room()
        assert _FakeServer.renamed_to == [], "名字没变不该发起一次广播"


# ======================================================================
# 文案：三种"看不到榜"必须分得开
# ======================================================================


class TestBoardText:
    def test_three_cases_read_differently(self):
        assert _board_text(True, True) == "榜单实时更新"
        assert _board_text(False, True) == "封榜中 · 结束后统一放榜"
        assert _board_text(False, False) == "本场不公布榜单"

    def test_switched_off_wins_over_everything(self):
        """关掉放榜时，就算 ``published`` 传了真值也只说"不公布"。"""
        assert _board_text(True, False) == "本场不公布榜单"


# ======================================================================
# 本场限制：语言与功能开关
# ======================================================================


class _Notifier:
    """接住 ``notify`` 信号。

    离屏环境下 ``MainWindow.notify`` 会弹**模态框**、没人点得到，
    测试直接挂死。所以凡是要走"配置不合法 → 警告"这条路的用例，
    先把 ``panel.notify`` 换成这个替身。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def emit(self, level: str, message: str) -> None:
        self.messages.append((level, message))


class TestPolicyUI:
    """主机端勾选 → ExamPolicy。"""

    def test_everything_checked_means_unrestricted(self, panel, monkeypatch):
        """全选 == 不限：策略序列化成 {}，线上载荷与旧版本逐字节一致。"""
        _open_room(panel, monkeypatch)
        policy = _FakeServer.last_session.policy
        assert policy.is_default(), "全选时不该留下任何限制"
        assert policy.to_dict() == {}

    def test_unchecking_a_language_restricts_it(self, panel, monkeypatch):
        _open_room(panel, monkeypatch)
        panel.close_room(quiet=True)

        panel.language_checks["java"].setChecked(False)
        panel.language_checks["c"].setChecked(False)
        panel.open_room()
        policy = _FakeServer.last_session.policy
        assert set(policy.allowed_languages) == {"cpp", "python"}

    def test_the_feature_switches_become_a_policy(self, panel, monkeypatch):
        _open_room(panel, monkeypatch)
        panel.close_room(quiet=True)

        panel.copy_out_check.setChecked(False)
        panel.lock_after_submit_check.setChecked(True)
        panel.open_room()
        policy = _FakeServer.last_session.policy
        assert policy.allow_copy_out is False
        assert policy.lock_after_submit is True

    def test_refuses_to_open_with_no_language_at_all(self, panel, monkeypatch):
        """一种语言都不允许是没意义的配置，必须在开房那一步挡住。"""
        from offline_oj.ui.panels import exam_panel as module

        monkeypatch.setattr(module, "ExamServer", _FakeServer)
        monkeypatch.setattr(panel, "notify", _Notifier())
        for box in panel.language_checks.values():
            box.setChecked(False)
        panel.refresh_problem_choices()
        panel._set_all_checked(True)
        _FakeServer.last_session = None

        panel.open_room()

        assert _FakeServer.last_session is None, "一种语言都不允许时不该把房间开起来"
        assert panel._server is None

    def test_the_restrictions_freeze_when_the_room_opens(self, panel):
        panel._apply_host_state(running=True)
        assert not panel.copy_out_check.isEnabled()
        assert not panel.lock_after_submit_check.isEnabled()
        assert not panel.language_checks["cpp"].isEnabled()
        panel._apply_host_state(running=False)


class _FakeClient:
    """只带 ``_apply_student_state`` 用到的那几样东西。"""

    def __init__(self, exam: dict) -> None:
        self.exam = exam
        self.verdicts: list[dict] = []
        # 离场锁屏（2026-09-19 新增）也挂在状态刷新这条路径上：
        # 「离开一下」按钮与提交按钮都要看这两位。
        self.locked = False
        self.lock_reason = ""
        self.using_account = False

    def close(self) -> None:
        """``_teardown_client`` 会来关它，替身得接得住。"""


class TestPolicyOnTheStudentSide:
    """主机下发的策略 → 学生端控件。"""

    def _attach(self, panel, **exam: object) -> None:
        payload: dict = {"state": "running", "title": "测验", "mode_label": "练习模式",
                         "timed": False, "leaderboard_published": True}
        payload.update(exam)
        panel._client = _FakeClient(payload)
        panel._apply_student_state()

    @staticmethod
    def _combo_values(panel) -> list[str]:
        return [str(panel.language_combo.itemData(index))
                for index in range(panel.language_combo.count())]

    def test_combo_only_lists_allowed_languages(self, panel):
        self._attach(panel, policy={"allowed_languages": ["cpp"]})
        assert self._combo_values(panel) == ["cpp"]
        assert panel.language_combo.currentData() == "cpp"

    def test_no_restriction_lists_every_language(self, panel):
        self._attach(panel, policy={})
        assert self._combo_values(panel) == ["cpp", "c", "python", "java"]

    def test_junk_policy_falls_back_to_open(self, panel):
        """旧主机不发 policy、或载荷被写坏时，都不该把学生的入口收掉。"""
        self._attach(panel, policy="nonsense")
        assert self._combo_values(panel) == ["cpp", "c", "python", "java"]
        assert panel.save_code_button.isEnabled()

    def test_copy_out_off_disables_saving(self, panel):
        self._attach(panel, policy={"allow_copy_out": False})
        assert not panel.save_code_button.isEnabled(), \
            "关掉「把代码带出考场」之后，另存为仍亮着"
        self._attach(panel, policy={})
        assert panel.save_code_button.isEnabled()

    def test_lock_after_submit_locks_the_editor_on_verdict(self, panel):
        self._attach(panel, policy={"lock_after_submit": True})
        assert not panel._editor_locked
        panel._on_verdict({"serial": 1, "problem_id": "P0001", "attempt": 1,
                           "pending": False, "verdict": "AC", "passed": 2,
                           "total": 2, "score": 100})
        assert panel._editor_locked, "本场要求交完就锁，判定回来却没锁"
        assert not panel.submit_button.isEnabled()
        assert not panel.open_code_button.isEnabled()
        assert "已提交" in panel.join_status.text()

    def test_no_lock_when_the_policy_is_default(self, panel):
        self._attach(panel, policy={})
        panel._on_verdict({"serial": 2, "problem_id": "P0001", "attempt": 1,
                           "pending": False, "verdict": "AC", "passed": 2,
                           "total": 2, "score": 100})
        assert not panel._editor_locked, "默认策略下不该锁编辑器"


# ======================================================================
# 备考状态：能读题、能写代码，不能提交
# ======================================================================


class TestPrepStateOnTheStudentSide:
    """开考前的学生端。

    这一组里最要紧的是**同一个数字只出现一次**：倒计时归左边题目栏，
    右边状态行就得把这一段让出来。两处都写，学生会以为看的是两件事。
    """

    def _attach(self, panel, **exam: object) -> dict:
        payload: dict = {"state": "pending", "title": "第三次模拟赛",
                         "mode_label": "考试模式", "timed": True,
                         "remaining_seconds": 180, "leaderboard_published": False,
                         "show_leaderboard": True}
        payload.update(exam)
        panel._client = _FakeClient(payload)
        # 真链路里 EXAM / START 都是"先同步剩余秒数，再刷界面"，这里照抄 ——
        # 少了第一步，倒计时永远算不出来（它会退化成"等待老师开考"）。
        panel._sync_remaining(payload)
        panel._apply_student_state()
        return payload

    @staticmethod
    def _clock_shown(panel) -> bool:
        # 离屏测试里窗口没 show 过，``isVisible()`` 恒为 False；只能看有没有
        # 被**显式藏起来**。
        return not panel.student_clock_label.isHidden()

    def test_pending_blocks_submitting_and_says_why(self, panel):
        self._attach(panel)
        assert not panel.submit_button.isEnabled()
        assert panel.submit_button.text() == "等待开考", \
            "备考期按钮还写着「提交代码」—— 看着像坏了，不像还没到时候"
        # 读题与写代码不受影响，只是交不了
        assert not panel.code_editor.isReadOnly()
        assert "未开考" in panel.student_status_label.text()

    def test_the_countdown_lives_in_the_problem_column_only(self, panel):
        self._attach(panel)
        assert self._clock_shown(panel), "备考期题目栏没挂倒计时"
        assert panel.student_clock_label.text().startswith("03:00 后开考")
        assert "距开考" not in panel.student_status_label.text(), \
            "同一个倒计时在状态行里又写了一遍"

    def test_an_unknown_start_time_waits_instead_of_counting_from_zero(self, panel):
        self._attach(panel, remaining_seconds=None)
        assert self._clock_shown(panel)
        assert "等待老师开考" in panel.student_clock_label.text()

    def test_the_start_frame_pulls_the_countdown_and_unlocks_submitting(self, panel):
        """开考帧与 EXAM 同构 —— 客户端只把它当一次状态跳变。"""
        self._attach(panel)
        panel._client.exam = {**panel._client.exam, "state": "running"}
        panel._on_client_event("start", panel._client.exam)
        assert not self._clock_shown(panel), "开考了倒计时还挂着"
        assert panel.submit_button.isEnabled()
        assert panel.submit_button.text() == "提交代码"
        assert "已开考" in panel.student_log.toPlainText()

    def test_ended_says_it_is_over(self, panel):
        self._attach(panel, state="ended", remaining_seconds=0)
        assert not panel.submit_button.isEnabled()
        assert panel.submit_button.text() == "已结束"
        assert "已结束" in panel.student_status_label.text()

    def test_leaving_the_room_puts_everything_back(self, panel):
        self._attach(panel)
        panel._teardown_client()
        assert panel.submit_button.text() == "提交代码"
        assert not self._clock_shown(panel)
        assert panel.student_status_label.text() == "未加入房间"
