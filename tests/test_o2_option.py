"""O2 优化开关：从勾选框一路到编译命令。

这个开关的价值全在"一致"两个字上，所以测试也盯着这几个一致性：

* **提交**与**测试运行**必须用同一套编译参数 —— 否则"自测挺稳"没有参考价值，
  自测按 -O0、判题按 -O2，耗时能差好几倍；
* **考试主机**端要把勾选值真正传进 ``ServerConfig`` —— 它原先被写死成默认的
  真值，界面上根本无从更改，等于这个开关在局域网模式下不存在；
* 编译命令本身：``optimize=True`` 要有 ``-O2`` / ``/O2``，``False`` 要没有。

写法上刻意**不去真跑判题**：把 worker / server 换成只记录参数的替身，
这样断言的是"参数有没有传对"，而不是"编译器能不能跑"（后者取决于本机工具链，
是另一件事，已有 e2e 工具覆盖）。
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
from offline_oj.core.models import Language  # noqa: E402
from offline_oj.core.runners import GccProfile  # noqa: E402
from offline_oj.paths import build_paths  # noqa: E402
from offline_oj.ui.main_window import (  # noqa: E402
    TAB_EXAM,
    TAB_SOLVE,
    MainWindow,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path_factory.mktemp("o2")))
    ctx = AppContext.create(build_paths().ensure_layout())
    win = MainWindow(ctx)
    yield win
    # 关窗口前把两处"会弹模态框"的状态清掉，否则离屏环境没人点得到那个框，
    # 整轮测试就挂死了 —— 存题面板的"未保存"和考试面板的"房间还开着"是一类坑。
    if win.exam_panel._server is not None:
        win.exam_panel.close_room(quiet=True)
    win.problems_panel._set_dirty(False)
    win.close()


@pytest.fixture
def seeded(window):
    """放一道题进题库。

    数据目录是新建的空目录，不预置题目的话 ``load_problem`` 会静默失败，
    随后 ``submit_code`` 走到"请先选择一道题目"那条分支 ——
    而 ``MainWindow.notify`` 对 info 级也是**模态对话框**，
    离屏环境下直接挂死。
    """
    from offline_oj.core.models import Problem, TestCase

    problem = Problem(
        id="P0001",
        title="两数求和",
        description="读入两个整数，输出它们的和。",
        time_limit=1000,
        memory_limit=128,
        testcases=[TestCase(input="1 2\n", output="3\n")],
    )
    window.ctx.repository.put(problem)
    return problem


# ======================================================================
# 编译命令
# ======================================================================


class TestCompileFlags:
    """勾选值最终要变成编译参数。"""

    def test_gcc_adds_o2_when_enabled(self):
        command = GccProfile().command(
            compiler="g++.exe", work_dir="C:/ws", source_name="main.cpp",
            target_name="main.exe", flag="-std=c++17", optimize=True,
            language=Language.CPP)
        assert "-O2" in command

    def test_gcc_omits_o2_when_disabled(self):
        command = GccProfile().command(
            compiler="g++.exe", work_dir="C:/ws", source_name="main.cpp",
            target_name="main.exe", flag="-std=c++17", optimize=False,
            language=Language.CPP)
        assert "-O2" not in command, "关掉优化就不该出现 -O2"

    def test_o2_is_appended_before_the_source_file(self):
        """顺序本身不重要，但别掉到源文件后面去 —— 那会被当成输入文件。"""
        command = GccProfile().command(
            compiler="g++.exe", work_dir="C:/ws", source_name="main.cpp",
            target_name="main.exe", flag="-std=c++17", optimize=True,
            language=Language.CPP)
        source = next(item for item in command if item.endswith("main.cpp"))
        assert command.index("-O2") < command.index(source)


# ======================================================================
# 测试运行的 worker
# ======================================================================


class TestWorkerCarriesTheFlag:
    def test_test_run_worker_defaults_to_no_optimization(self):
        from offline_oj.ui.workers import TestRunWorker

        worker = TestRunWorker("int main(){}", Language.CPP, {}, "C:/ws", "")
        assert worker._optimize is False

    def test_test_run_worker_takes_the_caller_value(self):
        from offline_oj.ui.workers import TestRunWorker

        worker = TestRunWorker("int main(){}", Language.CPP, {}, "C:/ws", "",
                               optimize=True)
        assert worker._optimize is True


# ======================================================================
# 写题面板
# ======================================================================


class _FakeSignal:
    def connect(self, *_args) -> None:
        pass


class _FakeJudgeWorker:
    """只记录参数，不真的起线程判题。"""

    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        type(self).last_kwargs = kwargs
        self.event = _FakeSignal()
        self.crashed = _FakeSignal()
        self.finished = _FakeSignal()

    def start(self) -> None:
        pass

    def isRunning(self) -> bool:        # noqa: N802 - Qt 命名
        """第一次提交后面板会留着这个 worker，靠它判断"正在评测"。

        返回 False 表示"已经跑完了"，这样两次提交都能走到构造 worker 那一步。
        """
        return False


class TestSolvePanelCheckbox:
    def test_follows_the_global_setting(self, window):
        """面板上的勾选是"提交级覆盖"，默认值永远跟着全局设置走。"""
        window.switch_tab(TAB_SOLVE)
        assert window.solve_panel.o2_check.isChecked(), \
            "全局默认是开的，面板初次打开也该是开的"

        window.ctx.settings.set("o2_optimization", False)
        window.solve_panel.on_settings_changed()
        assert not window.solve_panel.o2_check.isChecked(), "全局关掉后应同步关掉"

        window.ctx.settings.set("o2_optimization", True)
        window.solve_panel.on_settings_changed()
        assert window.solve_panel.o2_check.isChecked(), "全局打开后应同步打开"

    def test_submit_passes_the_checkbox_value(self, window, seeded, monkeypatch):
        from offline_oj.ui.panels import solve_panel as module

        monkeypatch.setattr(module, "JudgeWorker", _FakeJudgeWorker)
        window.switch_tab(TAB_SOLVE)
        panel = window.solve_panel
        panel.load_problem(seeded.id)
        assert panel.current_problem_id == seeded.id, "题目没载进来，后面全是假象"
        panel.code_editor.setPlainText("int main(){return 0;}")
        monkeypatch.setattr(panel, "_check_environment", lambda language: (True, ""))
        window.ctx.settings.set("security_mode", False)

        panel.o2_check.setChecked(True)
        panel.submit_code()
        assert _FakeJudgeWorker.last_kwargs["optimize"] is True

        panel.o2_check.setChecked(False)
        panel.submit_code()
        assert _FakeJudgeWorker.last_kwargs["optimize"] is False, \
            "取消勾选后提交必须按 -O0 编译"


# ======================================================================
# 考试主机
# ======================================================================


class _FakeServer:
    """只记录配置，不真的开端口 —— 这里测的是"参数有没有传对"。

    方法清单是照着 ``exam_panel`` 里 ``server.xxx()`` 的调用点抄下来的：
    ``lan_addresses`` 拼房间地址、``connected_devices`` 数在线人数、
    ``broadcast_*`` / ``collect_from_all`` 是榜单与收卷按钮触发的，
    开启房间那一步会用前两个，后面几个留着给"点按钮"类用例用。
    """

    last_config = None

    def __init__(self, *args, **kwargs) -> None:
        type(self).last_config = kwargs.get("config")
        self.started = False
        self.stopped = False
        config = kwargs.get("config")
        # 界面会把 port 拼进"房间号 xxx  192.168.x.x:port"，所以得给一个
        self.port = getattr(config, "port", 0) or 50800
        # 状态行里的"已判 N 份"读的是属性不是方法，别漏
        self.judged_count = 0

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float = 1.0) -> None:
        self.stopped = True

    def lan_addresses(self) -> list[str]:
        return ["127.0.0.1:50800"]

    def connected_devices(self) -> list[str]:
        return []

    def broadcast_leaderboard(self) -> None:
        pass

    def broadcast_status(self) -> None:
        pass

    def collect_from_all(self) -> None:
        pass


class TestExamHostCheckbox:
    def test_open_room_passes_the_checkbox_to_server_config(self, window, seeded,
                                                            monkeypatch):
        from offline_oj.ui.panels import exam_panel as module

        monkeypatch.setattr(module, "ExamServer", _FakeServer)
        window.switch_tab(TAB_EXAM)
        panel = window.exam_panel
        panel.refresh_problem_choices()
        panel._set_all_checked(True)
        assert panel.problem_list.count() > 0, "题库是空的，开启房间会被前置校验挡下"

        panel.o2_check.setChecked(False)
        panel.open_room()
        assert _FakeServer.last_config is not None, "开启房间没有构造 ServerConfig"
        assert _FakeServer.last_config.optimize is False, \
            "关掉勾选后主机端仍按 -O2 判题 —— 开关没接上"

        panel.o2_check.setChecked(True)
        panel.open_room()
        assert _FakeServer.last_config.optimize is True

    def test_the_checkbox_is_locked_while_the_room_is_open(self, window):
        window.switch_tab(TAB_EXAM)
        panel = window.exam_panel
        panel._apply_host_state(running=True)
        assert not panel.o2_check.isEnabled(), "开启房间后编译参数不该还能改"
        panel._apply_host_state(running=False)
        assert panel.o2_check.isEnabled()


# ======================================================================
# 标注：判完之后看得出这份提交是怎么编出来的
# ======================================================================
#
# 这一组盯的是"事后可回看"。同一个开关，勾着的时候是个便利功能，
# 判完之后不留下痕迹就变成了陷阱 —— 一份 TLE 摆在面前，"当时忘了开优化"
# 和"算法确实慢"是两件完全不同的事，光看代码分不出来。


class TestOptimizationLabel:
    """运行器要知道自己这门语言的优化开关在界面上怎么写。"""

    @staticmethod
    def _judge(language: Language, optimize: bool, profile=None):
        from offline_oj.core.judge import Judge
        from offline_oj.core.runners import make_runner

        runner = make_runner(language, {}, "C:/ws", optimize=optimize)
        if profile is not None:
            # 正常流程里 profile 是 compile() 认出来的；这里直接摆上，
            # 免得为了验两个字符串去起一次真编译
            runner._profile = profile
        return Judge(runner), runner

    def test_gcc_reports_minus_o2(self):
        judge, _ = self._judge(Language.CPP, True, GccProfile())
        assert judge._optimize_label() == "-O2"

    def test_gcc_off_flag_is_named_too(self):
        """关优化也要有名字（``-O0``），不能只是"没有参数"。"""
        judge, _ = self._judge(Language.CPP, False, GccProfile())
        assert judge._optimize_label() == "-O0"

    def test_msvc_uses_slash_flags(self):
        from offline_oj.core.runners import MsvcProfile

        judge, _ = self._judge(Language.CPP, False, MsvcProfile())
        assert judge._optimize_label() == "/Od"
        judge, _ = self._judge(Language.CPP, True, MsvcProfile())
        assert judge._optimize_label() == "/O2"

    def test_python_and_java_have_nothing_to_report(self):
        """解释执行的语言没有"编译优化"这回事。

        给它们编一个开关出来，等于让老师以为"把 O2 打开 Python 就会快"。
        """
        for language in (Language.PYTHON, Language.JAVA):
            judge, runner = self._judge(language, True)
            assert runner.optimize_labels is None
            assert judge._optimize_label() == ""

    def test_the_profile_decides_not_the_language(self):
        """同样是 C++，GCC 和 MSVC 的写法必须分开 —— 标错了比不标还糟。"""
        from offline_oj.core.runners import MsvcProfile

        gcc, _ = self._judge(Language.CPP, True, GccProfile())
        msvc, _ = self._judge(Language.CPP, True, MsvcProfile())
        assert gcc._optimize_label() != msvc._optimize_label()


class TestOptimizationText:
    """结果显示的那一行。"""

    @staticmethod
    def _report(**kwargs):
        from offline_oj.core.judge import JudgeReport
        from offline_oj.core.models import Verdict

        return JudgeReport(problem_id="P0001", verdict=Verdict.TLE, **kwargs)

    def test_enabled(self):
        assert self._report(optimized=True, optimize_label="-O2").optimization_text \
            == "编译优化：-O2"

    def test_disabled_is_stated_explicitly(self):
        """**未开启**也要写出来。

        只在开启时才标的话，"没有这一行"到底是"没开优化"还是"这个版本的软件
        还没有这一行"，看的人无从判断。
        """
        assert self._report(optimized=False, optimize_label="-O0").optimization_text \
            == "编译优化：-O0（未开启优化）"

    def test_not_applicable_means_no_line(self):
        assert self._report().optimization_text == ""


class TestSubmissionCarriesTheFlag:
    """主机端的提交记录里要留下这个字段（向后兼容：默认值不落盘）。"""

    @staticmethod
    def _submission(**kwargs):
        from offline_oj.net.session import Submission

        return Submission(serial=1, device_id="AAAA1111", problem_id="P0001",
                          language="cpp", code="int main(){}", **kwargs)

    def test_default_is_not_written(self):
        """没开优化的老提交，落盘结果要和以前逐字节一致。"""
        assert "optimized" not in self._submission().to_dict()

    def test_enabled_is_written(self):
        assert self._submission(optimized=True).to_dict()["optimized"] is True


class TestExamSubmissionColumn:
    """主机端「提交与代码」页的那一列。"""

    @staticmethod
    def _item(language: str, optimized: bool):
        from offline_oj.net.session import Submission

        return Submission(serial=1, device_id="AAAA1111", problem_id="P0001",
                          language=language, code="", optimized=optimized)

    def test_cpp_shows_the_flag(self):
        from offline_oj.ui.panels.exam_panel import _fmt_optimize

        assert _fmt_optimize(self._item("cpp", True)) == "-O2"
        assert _fmt_optimize(self._item("c", False)) == "-O0"

    def test_other_languages_show_blank(self):
        from offline_oj.ui.panels.exam_panel import BLANK, _fmt_optimize

        for language in ("python", "java"):
            assert _fmt_optimize(self._item(language, True)) == BLANK

    def test_the_label_reflects_the_stored_flag(self):
        """这一列读的是提交上落的那个值，不是界面上当前的勾选状态。

        老师判完之后可能把勾选框改回去；那一列要是跟着改，历史记录就成了假的。
        """
        from offline_oj.ui.panels.exam_panel import _fmt_optimize

        assert _fmt_optimize(self._item("cpp", True)) == "-O2"
        assert _fmt_optimize(self._item("cpp", False)) == "-O0"
