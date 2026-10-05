"""全局测试夹具。

目前有两件事：**把 GUI 用例产生的垃圾在 GUI 线程上清干净**，以及
**让临时目录的名字永不重复**（见文件末尾，那是拿整轮全量测试换来的）。

背景（真机上抓到的崩溃，跨模块、跨用例，极难定位）：

判题用例会起一条 ``oj-monitor`` 监控线程去采样子进程内存。那条线程第一次
碰到惰性导入时，导入过程会分配内存并触发一轮分代回收 —— 于是**回收发生在
监控线程上**。而 CPython 的 GC 会顺手回收进程中任何已经不可达的对象，
其中就包括前面补全用例留下的、已经没有引用的 PySide 控件。PySide 的
``tp_dealloc`` 会在**当前线程**上析构 C++ 对象，Qt 却要求 ``QWidget`` 只能在
GUI 线程析构，结果是访问违例（或者状态被打坏之后直接挂死）。

faulthandler 抓到的最内层帧就一句 ``Garbage-collecting``，往上一路是
``importlib`` 的 ``_compile_bytecode`` → ``sandbox.psutil_module`` →
``sandbox._monitor_loop`` —— 崩溃点与"代码补全弹窗"没有任何逻辑关联，
所以现场看起来像玄学：单独跑用例全过，凑到一起必炸。

两道防线各管一头，缺一不可：

* 产品侧（``sandbox.ProcessMonitor``）不再让监控线程承担首次导入；
* 测试侧（本文件）在每个用例结束后、仍然在 GUI 线程上就把垃圾收掉，
  这样后面任何线程触发的 GC 都没有控件可回收。

这里刻意**不**去手动析构控件：所有用例共用同一个 ``QApplication``，
强行拆控件会连累后续用例（实测整个测试进程直接挂死）。
用 GC 把"已经没人引用"的对象收掉则是安全的。
"""

from __future__ import annotations

import faulthandler
import gc
import itertools
import os

import pytest

_mktemp_counter = itertools.count()

# 挂死诊断探针（默认关）。全量跑里出现过"不崩、而是满核空转"的形态：
# 没有栈可看时，连卡在哪个用例、哪条线程都无从知晓。设 ``OJ_DUMP_STACKS=1``
# 后每 10 分钟把全部线程的调用栈转储到 stderr —— 正常跑完一轮只会多出
# 一两段无害的转储块；挂死时它就是现场照片。
if os.environ.get("OJ_DUMP_STACKS"):
    faulthandler.dump_traceback_later(600, repeat=True)


def _drain() -> None:
    # 没有 QApplication 说明还没跑过 GUI 用例，也就不会攒下 GUI 垃圾；
    # 这时跳过，免得给纯逻辑用例白加一次全量回收的开销。
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return
    gc.collect()
    # collect 之后把**活着的**对象全部搬进永久代：之后任何线程触发的 GC
    # 都扫不到它们，自然也就不会在错误的线程上析构 Qt 控件。
    # 导出/判题这类后台线程是收不住的 —— 它们第一次惰性导入时的分配随时
    # 可能触发一轮分代回收，光 collect 挡不住那一轮（全量跑里抓到过一次
    # access violation，事发点在导出工作线程的首次导入上）。
    gc.freeze()


@pytest.fixture(autouse=True)
def drain_gui_garbage():
    """用例结束后，在 GUI 线程上回收本轮留下的对象。"""
    yield
    _drain()


# ---------------------------------------------------------------------------
# 临时目录的名字永不重复
# ---------------------------------------------------------------------------
#
# pytest 每次 ``mktemp(同名)`` 都会去**重写**根目录下那个
# ``<名字>current`` 链接：先 ``unlink()`` 旧的、再建新的。本机环境对 Python
# 进程里的删除有一道按轮次计数的守卫（一次超过 50 个文件就要逐个确认），
# 而它把"删这个链接"按**链接所指目录的全部内容**计数 —— 一个装了 93 份
# 档案的目录就是 93 个文件，立刻触顶，然后 ``SystemExit(1)``。
#
# 表现极其迷惑：全量跑挂出 100+ 条 ``ERROR at setup``，回溯最深一行是
# ``SystemExit: 1``，与测试本身毫无关系；单独重跑这几个文件又全绿
# （第一条 mktemp 没有旧链接可删）。三条用例分别验证过。
#
# 修法很直接：**让每一次 mktemp 的名字都不一样**（追加一个单调计数）。
# 名字唯一 → "current" 链接永远是新建的，不存在"先删旧的"这一步 →
# 守卫永远不会被触碰。没有人依赖那个链接（它只是 pytest 留给人看的
# "最新一份在哪"），把它废掉没有任何代价。


def _install_unique_mktemp() -> None:
    try:
        from _pytest.tmpdir import TempPathFactory
    except Exception:                                  # pragma: no cover
        return

    original = TempPathFactory.mktemp

    def mktemp(self, basename: str, numbered: bool = True):
        # 全进程一个单调计数就够：名字唯一是唯一目标，序号是否从某个根目录
        # 重新数起没有任何用例关心。
        suffix = next(_mktemp_counter)
        return original(self, f"{basename}-{suffix:05d}", numbered)

    TempPathFactory.mktemp = mktemp                    # type: ignore[method-assign]


_install_unique_mktemp()


# ---------------------------------------------------------------------------
# 数据根隔离：每个用例的 %LOCALAPPDATA%\OfflineOJ 都指向本次用例的临时目录。
# 内核套件（records/export/roster）原来不设 OFFLINE_OJ_HOME，与其它套件
# 凑到一起时会撞上真实数据根里的意外文件而随机挂。
# paths.local_app_data() 每次调用都读环境变量，进程内 setenv 即全局生效。
# ---------------------------------------------------------------------------

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path / "ojdata"))
    yield
