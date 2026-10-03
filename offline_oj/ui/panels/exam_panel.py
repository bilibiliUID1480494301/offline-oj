"""局域网测验模块：主机端（老师开启房间）与学生端（学生进场）。

老师在教室里开一个房间、口头报一个六位房间号；学生输入房间号与自己的用户名
就能进来做题。**判题在主机的机器上做**，学生机不需要装任何编译器 ——
这一条同时决定了测试点永远不会离开主机，题目数据的安全性来自架构，
而不是来自加密。

为什么两端放在同一个面板里
--------------------------
教室里最常见的是"老师一台笔记本"，而老师在开考前往往要**以学生身份自己试一次**
（确认题目、确认网络通、确认样例给对了）。拆成两个选项卡，会让"我想自己试一下"
变成一个要重启程序的动作。所以这里是"一个面板 + 角色切换"：同一套房间概念，
切过去就能用，退出即可恢复。

线程模型（这一段是读懂本文件的关键）
------------------------------------
* **主机端** —— :class:`~offline_oj.net.server.ExamServer` 自己管线程
  （accept / 判题 / 定时广播），事件是从那些线程里回调出来的。回调里**绝不能碰控件**，
  那是 Qt 里最经典的一类未定义行为。这里统一用 :attr:`ExamPanel.host_event`
  这个 Qt 信号做桥：信号的发射本身是线程安全的，槽函数会被 Qt 排队投递回界面线程；
* **学生端** —— ``ExamClient.connect()`` 里有一次 scrypt（约 350 毫秒）和一次
  最长 15 秒的网络等待，放在界面线程会把窗口冻住，而"点了加入没反应"是学生最容易
  误判成"程序坏了"的现象。所以整个进场动作交给 :class:`ConnectWorker` 去做；
  连上之后读取线程归 ``ExamClient`` 自己管，界面只收信号。

到点收卷
--------
主机到点广播 ``COLLECT``。学生端收到之后：

* **开了强制收卷** —— 把编辑器里当前这道题的代码自动交一次（标 ``forced``，
  榜上一眼能看出哪几份是系统替学生交的），然后锁住编辑器。锁的是"还能不能继续写"，
  不是"能不能再交一次"——这两件事在需求里是分开的；
* **没开强制收卷** —— 只提醒，不替学生交。收不收是老师的决定，程序不越权。

倒计时用主机下发的剩余秒数 + 本机流逝时间推算，不拿本机时钟直接减 ——
学生机的时钟经常是错的，直接算会出现"主机已经收卷、这边还在倒数"的现场尴尬。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Callable

from PySide6.QtCore import QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import export as table_export
from ...core import records
from ...core import similarity as sim
from ...core.models import Language, Problem
from ...core.roster import Roster, RosterError
from ...win32 import process as win_process
from ...net.client import ConnectError, ExamClient, HandshakeRejected
from ...net.identity import get_identity, save_identity
from ...net.protocol import MessageKind
from ...net.server import ExamServer, ServerConfig, build_exam_problem
from ...net.session import (
    DEFAULT_TITLE,
    ROOM_CODE_LENGTH,
    EntryMode,
    ExamPolicy,
    ExamSession,
    ExamState,
    RoomMode,
    Submission,
    generate_room_code,
    normalize_account,
    normalize_room_code,
)
from ..codefile import CodeFileController
from ..export_dialog import ExportDialog
from ..similarity_dialog import SimilarityDialog
from ..roster_dialog import RosterDialog
from ..workers import TableExportWorker
from ..theme import (
    GROUP_MARGINS,
    PAGE_MARGINS,
    ROW_SPACING,
    SECTION_SPACING,
    TIGHT_SPACING,
    TOP_GAP,
)
from ..widgets import CodeEditor, MarkdownView, OutputView
from .base import Panel

log = logging.getLogger(__name__)

#: 主机默认端口。挑一个不常被占用的固定端口，学生就只需要知道"地址 + 房间号"
#: 两样东西；端口随机的话，老师每次都得额外报一个数字。
DEFAULT_PORT = 8899

#: 学生可提交的语言。判题在主机的工具链上跑，所以这里不做本机探测 ——
#: 学生机上没有编译器不代表不能用 C++。
SUBMIT_LANGUAGES = (Language.CPP, Language.C, Language.PYTHON, Language.JAVA)

OVERALL_COLUMNS = ("名次", "用户名", "设备 ID", "总分", "已解决",
                   "提交次数", "总耗时", "总内存", "最后提交")
PROBLEM_COLUMNS = ("名次", "用户名", "设备 ID", "得分", "通过", "结论",
                   "第几次", "耗时", "内存", "提交时间")
ROSTER_COLUMNS = ("设备 ID", "用户名", "账号", "状态", "锁", "地址",
                  "加入时间", "提交次数")
MINE_COLUMNS = ("编号", "题目", "第几次", "结论", "通过", "得分",
                "耗时", "内存", "提交时间")
#: 主机端「提交与代码」页的列表。这一页的目的是"找到一份提交去看它的源码"，
#: 所以列只保留"定位用"的信息（谁、哪题、第几次、判得怎么样）；耗时内存这些
#: 用来横向比较的数字留在单题榜里，不在这里重复一遍。
#:
#: 例外是「优化」：它不是用来横向比较的，而是"这份代码是怎么编出来的"——
#: 老师看到一份 TLE，第一个要排除的就是"当时是不是没开优化"。
SUBMISSION_COLUMNS = ("编号", "提交时间", "用户名", "设备 ID", "题目",
                      "第几次", "语言", "优化", "结论", "得分")

#: 「历史场次」列表。一场一行 —— 时间排在最前面，因为老师找档案的方式是
#: "上周三那场"。末列留给"读不出来"的原因（正常档案是空的）。
ARCHIVE_COLUMNS = ("场次时间", "测验名称", "题数", "人数", "提交", "源码", "状态")

#: 表格里"没有值"的统一写法。用连字符而不是 0，是为了让"没交这道题"
#: 和"交了得 0 分"在视觉上分得开。
BLANK = "—"


def _board_text(published: bool, show: bool) -> str:
    """状态栏里"榜单现在是什么状态"那一段。主机端与学生端共用同一份措辞。

    三种情况必须分开说 —— 说"稍后放榜"却根本不打算放，会让学生一直等一个
    永远不会来的榜单。``show`` 缺省当 True 处理，这样它也能兼容还没带这个
    字段的旧主机。
    """
    if not show:
        return "本场不公布榜单"
    return "榜单实时更新" if published else "封榜中 · 结束后统一放榜"


def _clock_text(*, timed: bool, remaining: int | None, state: str) -> str:
    """状态栏里倒计时那一段。主机端与学生端共用同一份措辞。

    **"距开考"与"剩余"必须分开**：备考阶段显示"剩余 3 分钟"，学生会以为考试
    已经开始、还有 3 分钟就收卷 —— 而实际上还没开考。
    """
    if state == ExamState.PENDING.value:
        return (f"距开考 {_fmt_countdown(remaining)}"
                if remaining is not None else "等待开考")
    if state == ExamState.ENDED.value:
        return "已结束"
    if not timed:
        return "不限时"
    return f"剩余 {_fmt_countdown(remaining)}"


def _fmt_optimize(item: Submission) -> str:
    """「优化」那一列：这份提交判的时候开没开编译优化。

    只有 C / C++ 有这个概念 —— Python / Java 是解释或 JIT 执行的，给它们标
    一个 ``-O2`` 会让人以为有个开关其实并不存在，所以直接留空。

    这里统一按 GCC 的写法（``-O2`` / ``-O0``）。MSVC 的对应写法是 ``/O2`` / ``/Od``，
    编译那一步只有主机知道用的是哪个编译器，所以那个精确写法出现在主机端
    「提交与代码」页的代码标题行上；这一列是给快速扫视用的。
    """
    if Language.from_value(item.language) not in (Language.C, Language.CPP):
        return BLANK
    return "-O2" if item.optimized else "-O0"

#: 主机端页签下标。**一律用名字，不要写裸数字** —— 往中间插一个页签，后面
#: 所有数字都会静默指到隔壁：不报错、不提示，只是跳到别的页上。截图脚本与
#: 测试都不会因此变红。顺序就是 ``_build_host_board`` 里 ``addTab`` 的顺序，
#: ``test_exam_code_view`` 有一条断言盯着这两处不许脱节。
(HOST_TAB_OVERALL, HOST_TAB_PROBLEM, HOST_TAB_CODE,
 HOST_TAB_ROSTER, HOST_TAB_LOG, HOST_TAB_ARCHIVE) = range(6)

#: 学生端页签下标，同理。
STUDENT_TAB_MINE, STUDENT_TAB_BOARD, STUDENT_TAB_LOG = range(3)

#: 主机端左列的**两副面孔**：房间没开时是"准备"，开了之后是"监考"。
#:
#: 这不是两个不同的功能，是同一件事的两个阶段 —— 开房前老师要配一堆东西
#: （叫什么、怎么进、考多久、考哪些题），开房后这些东西**一件都不该再动**
#: （动了会踢人、会让榜单不可比）。所以与其把十几项灰在屏幕上占着地方，
#: 不如整页换掉：准备阶段看设置，监考阶段看"谁在、考到哪、怎么收"。
#:
#: 和 ``HOST_TAB_*`` 一样，**一律用名字，不要写裸数字**。
HOST_SIDE_PREPARE, HOST_SIDE_PROCTOR = range(2)


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def _fmt_score(score: object, possible: object = 0) -> str:
    """得分。有满分就写成 ``37/50`` —— NOI 的成绩单就是这个写法。

    只写一个光秃秃的分数会读不出含义：满分现在由测试点分值决定，不再是恒定
    的 100，"37" 到底是 50 分里的还是 100 分里的，只有配上分母才知道。
    旧数据没有满分（``possible`` 为 0）时只写分数，绝不写成 ``37/0``。
    """
    try:
        value = int(score or 0)
    except (TypeError, ValueError):
        value = 0
    try:
        full = int(possible or 0)
    except (TypeError, ValueError):
        full = 0
    return f"{value}/{full}" if full > 0 else str(value)


def _fmt_ms(value: object) -> str:
    """耗时。测试点没跑到时不显示 0 秒，那是"没数据"而不是"极快"。"""
    try:
        ms = float(value or 0.0)
    except (TypeError, ValueError):
        return BLANK
    if ms <= 0:
        return BLANK
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _fmt_mb(value: object) -> str:
    """内存占用。"""
    try:
        mb = float(value or 0.0)
    except (TypeError, ValueError):
        return BLANK
    return f"{mb:.1f} MB" if mb > 0 else BLANK


def _fmt_clock(stamp: object) -> str:
    """ISO 时间戳 → ``时:分:秒``。榜单一行一个时刻，日期是噪音。"""
    text = str(stamp or "")
    if not text:
        return BLANK
    try:
        return datetime.fromisoformat(text).strftime("%H:%M:%S")
    except ValueError:
        return text[:19]


def _fmt_stamp(stamp: object) -> str:
    """ISO 时间戳 → ``2026-09-19 17:30``。历史场次要带日期（见 ``_fmt_clock``）。"""
    text = str(stamp or "")
    if not text:
        return BLANK
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:19].replace("T", " ")


def _fmt_countdown(seconds: int | None) -> str:
    """剩余时间。``None`` 表示本场不限时。"""
    if seconds is None:
        return "不限时"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _fmt_verdict(verdict: object) -> str:
    text = str(verdict or "").strip()
    return text or "…"


# ---------------------------------------------------------------------------
# 工人线程
# ---------------------------------------------------------------------------


class ConnectWorker(QThread):
    """把"加入房间"整个搬到后台线程。

    ``ExamClient.connect()`` 里有 scrypt 与网络往返，之后还要把读取线程拉起来；
    这三步都在这里做完，界面只等一个 ``connected`` 或者 ``failed``。

    信号由 ``ExamClient`` 的读取线程发出，但因为发射方不是本对象所属线程，
    Qt 会把它们排队投递到界面线程 —— 这正是我们要的：界面线程里不出现套接字。
    """

    #: 连接建立成功，携带 :class:`ExamClient` 本体
    connected = Signal(object)
    #: 连接失败，携带直接可展示的原因
    failed = Signal(str)
    #: 收到一条推送（游戏进行中）
    message = Signal(str, object)
    #: 连接断开，携带原因
    disconnected = Signal(str)

    def __init__(self, *, host: str, port: int, room_code: str, device_id: str,
                 username: str, password: str = "", account: str = "",
                 passcode: str = "", parent=None) -> None:
        super().__init__(parent)
        self._host = host
        self._port = int(port)
        self._room_code = room_code
        self._device_id = device_id
        self._username = username
        self._password = password
        self._account = account
        self._passcode = passcode
        self.client: ExamClient | None = None

    def run(self) -> None:  # noqa: D102 - QThread 约定
        client = ExamClient(
            self._host, self._port, self._room_code, self._device_id,
            self._username,
            password=self._password,
            account=self._account,
            passcode=self._passcode,
            on_message=lambda kind, payload: self.message.emit(kind, payload),
            on_disconnect=lambda reason: self.disconnected.emit(reason),
        )
        try:
            client.connect()
        except (ConnectError, HandshakeRejected) as exc:
            # 这两类是"给用户看的话"，原样上抛；其余异常在下一条分支里兜住
            self.failed.emit(str(exc))
            return
        except Exception as exc:                        # noqa: BLE001
            log.exception("加入房间时出现未预期异常")
            self.failed.emit(f"加入房间失败：{exc}")
            return

        try:
            client.start_listener()
        except Exception as exc:                        # noqa: BLE001
            client.close()
            self.failed.emit(f"连接已建立，但启动接收失败：{exc}")
            return

        self.client = client
        self.connected.emit(client)


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------


class ExamPanel(Panel):
    """局域网测验：一份代码，两个角色。"""

    #: 主机事件（由 ExamServer 的线程发出，Qt 排队投递回界面线程）
    host_event = Signal(str, object)

    def _build(self) -> None:
        # ---- 运行期状态 ----
        self._session: ExamSession | None = None
        self._server: ExamServer | None = None
        self._worker: ConnectWorker | None = None
        self._client: ExamClient | None = None
        #: 本机设备标识（构建时读一次，加入房间后再写回）
        self._identity = None
        #: 判题入口。正常为 ``None`` —— 用本机工具链真编译真跑；测试时注入一个
        #: 假判题器，就能在没有编译器的机器上把「面板 → 服务端 → 客户端」整条
        #: 链路跑通。:class:`ExamServer` 本来就有这个参数，这里只是把它透出去。
        self._judge: Callable | None = None
        #: 学生端当前选中的题目 ID
        self._current_problem_id: str | None = None
        #: 收到过收卷指令（用于按钮文案与状态提示）
        self._collect_seen = False
        #: 已锁编辑器（强制收卷之后）
        self._editor_locked = False
        #: 提交编号 → 「我的提交」表里的行号
        self._mine_rows: dict[int, int] = {}
        #: 提交编号 → 那次提交本身。主机端「提交与代码」页要连着源码一起看，
        #: 而源码永远不会上线（见 ``_build_code_tab``），所以只能在主机内存里
        #: 按编号取回来。
        self._submission_by_serial: dict[int, Submission] = {}
        #: 提交列表的内容签名，用来判断"要不要重填"（见 ``_fill_submissions``）
        self._submission_signature: list | None = None
        #: 正在查看的那一份源码的提交编号
        self._code_serial: int | None = None
        #: 老师是否亲手在提交列表里点过某一行。点过就再也不自动改选，
        #: 没点过则每次切到这一页都停在最新的一份上（见 ``_on_host_tab_changed``）。
        self._code_pinned = False
        #: 正在回看的测验档案。非 None 时「提交与代码」页的数据源换成它 ——
        #: 两者共用一个页面是刻意的：读档要的正是那套"选中跟编号走、内容没变
        #: 不重灌"的刷新纪律，重写一份必然会在某个角落丢掉其中一条。
        self._archive: records.ExamArchive | None = None
        #: 档案里的提交（``Submission`` 对象，从档案的 dict 读回来）。
        #: 转成真对象而不是自己再写一套只读视图，是为了让上面那些刷新逻辑
        #: 一行都不用改。
        self._archive_submissions: list[Submission] = []
        #: 档案列表（``core/records`` 的摘要），列表页按它填表
        self._archives: list[records.ArchiveSummary] = []
        #: 正在跑的导出线程。**必须留引用** —— QThread 被 GC 掉而线程还在跑，
        #: 进程会直接崩，而现象是"点导出之后程序没了"。
        self._export_worker: TableExportWorker | None = None
        #: 导出目标。PDF 走"工作线程出 HTML、界面线程渲染"两步，渲染那一步
        #: 需要知道往哪写。
        self._export_target: Path | None = None
        #: 倒计时基准：同步时刻的剩余秒数 + 本机单调时刻
        self._remaining_base: int | None = None
        self._remaining_at: float = 0.0
        #: 开房要用的选手名单（账号进场）。名单文件由 :class:`RosterDialog`
        #: 落盘，这里只留一份内存副本；随它一起记住"上次用的是哪份"，
        #: 下次开面板自动带回来。
        self._roster: Roster | None = None
        self._roster_path: Path | None = None
        #: 学生端「离开中」盖板。盖住的是题面与代码 —— 防的是隔壁同学的眼睛，
        #: 与防拷贝（``allow_copy_out``）、防截屏是三件独立的事。
        self._lock_visible = False

        self.host_event.connect(self._on_host_event)

        role_row = QWidget()
        role_layout = QHBoxLayout(role_row)
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.addWidget(QLabel("本机角色"))
        self.role_combo = QComboBox()
        self.role_combo.addItem("主机端 · 开启房间", "host")
        self.role_combo.addItem("学生端 · 加入房间", "student")
        self.role_combo.setToolTip(
            "同一台机器可以随时切换角色：老师在开考前用学生端自己试一次，"
            "不占第二台机器。")
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        role_layout.addWidget(self.role_combo)
        role_layout.addStretch(1)
        self.role_hint = QLabel()
        self.role_hint.setProperty("muted", True)
        role_layout.addWidget(self.role_hint)

        self.stack = QStackedWidget()
        self.host_page = self._build_host_page()
        self.student_page = self._build_student_page()
        self.stack.addWidget(self.host_page)
        self.stack.addWidget(self.student_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(SECTION_SPACING)
        layout.addWidget(role_row)
        layout.addWidget(self.stack, 1)

        # 一秒一次的心跳：只负责倒计时与"在线/提交"这类随时间变化的字。
        # 榜单不在这里刷 —— 那是事件驱动的，靠轮询榜会让人以为数据在跳。
        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(1000)
        self.tick_timer.timeout.connect(self._on_tick)
        self.tick_timer.start()

        self._on_role_changed()
        self._load_identity()
        self._load_last_roster()

    # ==================================================================
    # 主机端：搭界面
    # ==================================================================

    def _build_host_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SECTION_SPACING)

        layout.addWidget(self._build_host_settings(), 0)
        layout.addWidget(self._build_host_board(), 1)
        return page

    def _build_host_settings(self) -> QWidget:
        """主机端左列：**同一块地方的两副面孔**（准备 ⇄ 监考）。

        构造顺序有个讲究：所有控件都在这里一次性建好、**挂在准备视图上**，
        再由 :meth:`_build_host_proctor_view` 从准备视图里"接管"其中几个
        （大字房间号、操作按钮）搬去监考视图。控件的**父子关系允许搬家**
        （``layout.addWidget`` 会自动把它从旧父布局摘下来），但**必须在
        同一个方法里连续做完** —— 跨方法分两次建会让"哪个控件现在属于哪一页"
        变成需要追代码才知道的事，而这种不确定性正是下面那些守卫测试在防的。

        为什么不留"灰着的设置"：开房后房间设置/本场限制/本场题目**一件都不该再动**
        （动了会踢人、会让榜单不可比），把十几项灰在屏幕上只是占地方。
        """
        column = QWidget()
        column.setFixedWidth(400)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)

        # ---- 房间设置 ----
        room_group = QGroupBox("房间设置")
        form = QFormLayout(room_group)
        form.setContentsMargins(*GROUP_MARGINS)
        form.setSpacing(ROW_SPACING)

        self.title_edit = QLineEdit(DEFAULT_TITLE)
        self.title_edit.setPlaceholderText("给学生看的名字，例如「第三次模拟赛」")
        #: 名称是**唯一允许开房期间改**的设置：它只是个显示标签，改它不踢人。
        #: 开房前直接敲这个输入框；开房后准备视图收起，改名走监考视图那一组
        #: （``side_title_edit`` + ``rename_button``）—— 两条路都落到
        #: :meth:`rename_room`，一个写入口。
        #:
        #: 「改名」按钮平时灰着，开房后才亮 —— 这样"改一下名字"是一个明确动作，
        #: 不会因为顺手敲了几个字就把全班的标题改掉。
        self.rename_button = QPushButton("改名")
        self.rename_button.setProperty("flat", True)
        self.rename_button.setEnabled(False)
        self.rename_button.setToolTip(
            "把监考视图里的名称改成新名字并通知所有在线学生。\n"
            "房间号与连接都不受影响，不会把学生踢下线。")
        self.rename_button.clicked.connect(
            lambda: self.rename_room(self.side_title_edit.text()))

        # 开房前改名字不需要按钮 —— 输入框就在手边，随便敲。
        # 这里只放输入框，按钮归监考视图（它的启用条件本来就是"开了房"）。
        form.addRow("测验名称", self.title_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("练习模式 · 榜单实时更新", RoomMode.PRACTICE)
        self.mode_combo.addItem("考试模式 · 结束后统一放榜", RoomMode.EXAM)
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.setToolTip(
            "两种模式只有一处不同：榜单什么时候公开。\n"
            "练习模式全程实时；考试模式一律等测验结束（含收卷宽限）后统一放榜。")
        form.addRow("模式", self.mode_combo)

        code_row = QWidget()
        code_layout = QHBoxLayout(code_row)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_layout.setSpacing(TIGHT_SPACING)
        self.room_code_edit = QLineEdit(generate_room_code())
        self.room_code_edit.setMaxLength(ROOM_CODE_LENGTH)
        self.room_code_edit.setPlaceholderText("6 位数字")
        self.room_code_edit.setToolTip(
            "老师口头报给学生的号码。房间号本身就是凭据，\n"
            "请只报给本场的学生。")
        reroll = QPushButton("换一个")
        reroll.setProperty("flat", True)
        reroll.clicked.connect(self._reroll_room_code)
        code_layout.addWidget(self.room_code_edit, 1)
        code_layout.addWidget(reroll)
        form.addRow("房间号", code_row)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("留空表示只认房间号")
        self.password_edit.setToolTip(
            "可选。房间号只有 6 位数字，抗不住同网段的人抓包后离线爆破；\n"
            "设一个口令就是加在它上面的真实加固。口令区分大小写。\n"
            "账号进场时它是第二道闸（可选），也能用来解离场锁屏。")
        form.addRow("考场口令", self.password_edit)

        # ---- 进场方式 ----
        # 二选一，开房时定死：主机必须在明文握手第一步就知道"这个索引该去
        # 哪一侧查"，那一刻它还没读到学生自报的任何东西。
        self.entry_mode_combo = QComboBox()
        self.entry_mode_combo.addItem("房间号进场 · 学生报房间号", EntryMode.ROOM_CODE)
        self.entry_mode_combo.addItem("账号进场 · 学生报名单上的账号密码",
                                      EntryMode.ACCOUNT)
        self.entry_mode_combo.setToolTip(
            "房间号进场：报一个号全班进来，名字由学生自己填（旧行为）。\n"
            "账号进场：学生报名单上的账号与个人口令，姓名、座位都按名单显示，\n"
            "一个账号同时只能有一台机器 —— 谁是谁由你说了算。")
        form.addRow("进场方式", self.entry_mode_combo)

        roster_row = QWidget()
        roster_layout = QHBoxLayout(roster_row)
        roster_layout.setContentsMargins(0, 0, 0, 0)
        roster_layout.setSpacing(TIGHT_SPACING)
        self.roster_label = QLabel("未选择名单")
        self.roster_label.setProperty("muted", True)
        self.roster_button = QPushButton("名单…")
        self.roster_button.setProperty("flat", True)
        self.roster_button.clicked.connect(self.edit_roster)
        roster_layout.addWidget(self.roster_label, 1)
        roster_layout.addWidget(self.roster_button)
        form.addRow("选手名单", roster_row)
        self.entry_mode_combo.currentIndexChanged.connect(
            self._on_entry_mode_changed)
        # 构造时先走一遍：进场方式、名单两行的初始状态（含"房间号进场时
        # 名单行置灰"）都由这一个入口写，避免构造态与切换态两套文案。
        self._on_entry_mode_changed()

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        self.port_spin.setToolTip("学生端要填同一个端口。开启房间失败时可以换一个。")
        form.addRow("监听端口", self.port_spin)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 600)
        self.duration_spin.setValue(60)
        self.duration_spin.setSuffix(" 分钟")
        self.duration_spin.setSpecialValueText("不限时")
        self.duration_spin.setToolTip("设为 0 表示不限时（练习模式常用）")
        form.addRow("测验时长", self.duration_spin)

        # 备考：提前把题发下去，让学生先看题、先写代码，但到点之前交不了。
        # 用"N 分钟后"而不是一个具体时刻，是为了避开"这个时间今天还是明天""已经
        # 过了怎么办"这类需要猜的歧义 —— 老师要说的是一个间隔。
        self.start_delay_spin = QSpinBox()
        self.start_delay_spin.setRange(0, 240)
        self.start_delay_spin.setValue(0)
        self.start_delay_spin.setSuffix(" 分钟后")
        self.start_delay_spin.setSpecialValueText("立即开考")
        self.start_delay_spin.setToolTip(
            "定为 N 分钟后开考：学生可以先进场看题、写代码，但到点之前交不了。\n"
            "开考那一刻由主机统一通知 —— 不靠学生机的时钟判断。\n"
            "开房后老师也可以点「开始考试」提前开考。")
        form.addRow("开考方式", self.start_delay_spin)

        self.force_collect_check = QCheckBox("到点强制收卷（自动提交学生编辑器里的代码）")
        self.force_collect_check.setToolTip(
            "开启后：到点主机广播收卷，学生端把编辑器里当前的代码自动交一次并锁定编辑器。\n"
            "关闭时只提醒，交不交由学生自己决定。")
        form.addRow("", self.force_collect_check)

        self.resubmit_check = QCheckBox("允许对同一题重复提交")
        self.resubmit_check.setChecked(True)
        self.resubmit_check.setToolTip(
            "允许时同一题取最高分的一次计入总分，榜上同时标出这是第几次提交。")
        form.addRow("", self.resubmit_check)

        # 与「模式」正交：模式管"什么时候放榜"，这个开关管"放不放"。
        # 关掉之后连练习模式的实时榜也不出现 —— 就是"只考试不打榜"那个场景。
        self.leaderboard_check = QCheckBox("公开榜单（关闭则本场全程不显示）")
        self.leaderboard_check.setChecked(True)
        self.leaderboard_check.setToolTip(
            "关闭后无论练习还是考试模式，全程都不向学生显示任何排名。\n"
            "学生仍能在自己那一栏看到自己的成绩，只是看不到别人、也看不到名次。")
        form.addRow("", self.leaderboard_check)

        # 判题在主机上做，所以编译优化也是主机端的决定 —— 学生端没有这个开关，
        # 也不需要知道。默认跟随「高级设置」，开启房间时就固定下来：同一场测验里
        # 所有人的编译参数必须一致，否则榜单上的耗时不具可比性。
        self.o2_check = QCheckBox("判题时开启 O2 优化")
        self.o2_check.setChecked(self.ctx.settings.bool("o2_optimization"))
        self.o2_check.setToolTip(
            "C / C++ 提交编译时加 -O2（MSVC 为 /O2）。\n"
            "关闭后按 -O0 编译 —— 同一份代码的耗时会有数倍差距，\n"
            "要用运行时间卡人的题目请保持开启。开启房间后不可更改。")
        form.addRow("", self.o2_check)

        # ---- 本场限制 ----
        # 语言与功能开关。**这些只是"别让人白点一次"**：真正的边界在服务端
        # （学生端可以被改、可以直接发包），所以每一条在 server.py 里都有对应的
        # 拒绝逻辑，见 `_on_submit` 的语言校验。
        limit_group = QGroupBox("本场限制")
        limit_layout = QVBoxLayout(limit_group)
        limit_layout.setContentsMargins(*GROUP_MARGINS)
        limit_layout.setSpacing(ROW_SPACING)

        language_row = QWidget()
        language_layout = QHBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(TIGHT_SPACING)
        language_layout.addWidget(QLabel("允许的语言"))
        self.language_checks: dict[str, QCheckBox] = {}
        for language in SUBMIT_LANGUAGES:
            box = QCheckBox(language.short)
            box.setChecked(True)
            box.setToolTip(f"取消勾选后本场不接受 {language.display} 的提交")
            self.language_checks[language.value] = box
            language_layout.addWidget(box)
        language_layout.addStretch(1)
        limit_layout.addWidget(language_row)

        self.copy_out_check = QCheckBox("允许学生把代码存成文件")
        self.copy_out_check.setChecked(True)
        self.copy_out_check.setToolTip(
            "关掉后学生端的「另存为…」会被禁用 —— 用于不希望代码被带出考场的场合。")
        limit_layout.addWidget(self.copy_out_check)

        self.lock_after_submit_check = QCheckBox("提交判定后锁定编辑器")
        self.lock_after_submit_check.setToolTip(
            "开启后每份代码判定回来即锁定编辑器（本场一题只交一次时特别有用）。")
        limit_layout.addWidget(self.lock_after_submit_check)

        # ---- 选题 ----
        pick_group = QGroupBox("本场题目")
        pick_layout = QVBoxLayout(pick_group)
        pick_layout.setContentsMargins(*GROUP_MARGINS)
        pick_layout.setSpacing(ROW_SPACING)

        self.problem_list = QListWidget()
        self.problem_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.problem_list.setMinimumHeight(150)
        pick_layout.addWidget(self.problem_list, 1)

        pick_buttons = QHBoxLayout()
        pick_buttons.setSpacing(TIGHT_SPACING)
        select_all = QPushButton("全选")
        select_all.setProperty("flat", True)
        select_all.clicked.connect(lambda: self._set_all_checked(True))
        select_none = QPushButton("全不选")
        select_none.setProperty("flat", True)
        select_none.clicked.connect(lambda: self._set_all_checked(False))
        pick_buttons.addWidget(select_all)
        pick_buttons.addWidget(select_none)
        pick_buttons.addStretch(1)
        self.pick_hint = QLabel()
        self.pick_hint.setProperty("muted", True)
        pick_buttons.addWidget(self.pick_hint)
        pick_layout.addLayout(pick_buttons)

        # ---- 操作：按钮本体在这里建，但**分属两页**（见下）----
        # 「开启房间」常驻准备视图；「关闭房间」常驻监考视图。两个按钮、一个
        # 动作（toggle_room），因为一页上永远只会出现其中一个 —— 让同一个按钮
        # 在两页之间搬家，会让"这个按钮现在属于哪一页"变成需要追代码才知道的事。
        self.open_button = QPushButton("开启房间")
        self.open_button.setProperty("variant", "primary")
        self.open_button.setMinimumHeight(36)
        self.open_button.clicked.connect(self.toggle_room)
        self.close_button = QPushButton("关闭房间")
        self.close_button.setMinimumHeight(36)
        self.close_button.clicked.connect(self.toggle_room)

        self.start_button = QPushButton("开始考试")
        self.start_button.setEnabled(False)
        self.start_button.setToolTip(
            "把开考时刻提前到现在。\n"
            "只在「定时开考」且还没到点时可用 —— 已经开考了就点不动。")
        self.start_button.clicked.connect(self.start_exam_now)

        self.collect_button = QPushButton("提前收卷（让全体立刻交卷）")
        self.collect_button.setEnabled(False)
        self.collect_button.setToolTip(
            "不等时间到就要求学生把当前代码交上来。\n"
            "用于「老师临时有事」这类现场情况。")
        self.collect_button.clicked.connect(self.collect_now)

        self.end_button = QPushButton("提前结束测验")
        self.end_button.setEnabled(False)
        self.end_button.setToolTip(
            "把截止时刻改到现在：收卷、停止接受新提交，\n"
            "并在收卷宽限（30 秒）之后统一放榜。")
        self.end_button.clicked.connect(self.end_quiz_now)

        self.copy_button = QPushButton("复制房间信息")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_room_info)

        # ---- 两副面孔：准备视图 ⇄ 监考视图 ----
        # 准备视图 = 上面三组设置（开房前的一切）。
        prepare = QWidget()
        prepare_layout = QVBoxLayout(prepare)
        prepare_layout.setContentsMargins(0, 0, 0, 0)
        prepare_layout.setSpacing(ROW_SPACING)
        prepare_layout.addWidget(room_group)
        prepare_layout.addWidget(limit_group)
        prepare_layout.addWidget(pick_group, 1)
        prepare_layout.addWidget(self.open_button)

        self.host_side = QStackedWidget()
        self.host_side.addWidget(prepare)                       # HOST_SIDE_PREPARE
        self.host_side.addWidget(self._build_host_proctor_view())  # HOST_SIDE_PROCTOR
        layout.addWidget(self.host_side, 1)
        return column

    def _build_host_proctor_view(self) -> QWidget:
        """监考视图：房间开着的这段时间，左列该看的东西。

        四件事，从"最该被一眼看到"到"偶尔才用"：

        1. **大字房间号与状态** —— 老师要向全班念它，学生连不上时第一句问的
           也是它。右列顶上虽然也有，但左列是老师的"操作台"，念的时候眼睛
           不用跨到屏幕另一边；
        2. **只读摘要** —— 本场是练习还是考试、考多久、几道题、怎么进场。
           这些开房后改不了，但老师会被问到（"老师考多久来着？"），所以
           以**事实**的形式留一份，而不是留一堆灰着的输入框；
        3. **操作** —— 开始考试 / 提前收卷 / 提前结束 / 复制信息 / 关闭房间；
        4. **改名** —— 全程唯一允许改的东西。它跟着名称输入框一起进来，
           因为"改一下名字"和"改其他设置"在语义上就是两回事。

        摘要里的每个字段用 ``key: value`` 的两段式文本，靠 theme 的 muted
        与 role 令牌区分主次；**不新增 QLabel 文案**（见 test_ui_dedup：
        同一句话出现在两个父类下就判违规），所以这里全部复用已有的
        ``host_room_label`` / ``host_status_label``。
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)

        # 大字房间号与状态：**从右列借过来用**？不行 —— 一个控件只能有一个
        # 父对象。右列顶上那份继续留给"扫一眼就知道房间开了没"，这里另建
        # 一份放在操作台上，文案由 _refresh_host_status 统一写两处。
        self.side_room_label = QLabel("房间未开启")
        self.side_room_label.setProperty("role", "display")
        self.side_room_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.side_room_label.setWordWrap(True)
        layout.addWidget(self.side_room_label)

        # 只读摘要：开房后改不了的设置，以事实形式留一份备查。
        #
        # 排版上踩过两个坑（真机截图走查抓到的），记在这里免得改回去：
        # 1. 用 ``QFormLayout`` 时字段列按单行 sizeHint 定行高，``setWordWrap``
        #    后的多行高度不被采纳 —— 稍长的值会被横向撑开、底部被切掉；
        # 2. 改成 HBox 后仍会被切：值那一列拿到的宽度足够摆下**前半段**文字，
        #    ``wordWrap`` 于是不折行、后半段直接越过组框边缘被裁掉。
        #
        # 所以这里用**竖排**：字段名一行、值一行占满整宽。值那一行的可用宽度
        # 就等于组框内部宽度，折行判定与裁剪边界重合，不会再裁到字。
        summary = QGroupBox("本场设置（开房后不可更改）")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(*GROUP_MARGINS)
        summary_layout.setSpacing(ROW_SPACING)
        self.side_summary_labels: dict[str, QLabel] = {}
        for key, caption in (("mode", "模式"), ("entry", "进场方式"),
                             ("duration", "测验时长"), ("problems", "题目"),
                             ("limits", "本场限制")):
            item = QWidget()
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(0, 0, 0, 0)
            name = QLabel(caption)
            name.setProperty("muted", True)
            value = QLabel(BLANK)
            value.setWordWrap(True)
            item_layout.addWidget(name)
            item_layout.addWidget(value)
            self.side_summary_labels[key] = value
            summary_layout.addWidget(item)
        layout.addWidget(summary)

        # 改名：全程唯一允许改的设置。监考视图里有自己的输入框 —— 开房后
        # 准备视图整页收起了，如果只搬按钮过来，老师会点一个读不到东西的
        # 按钮。两个输入框指向同一件事（server.rename），落点只有一个。
        rename_group = QGroupBox("更改名称")
        rename_layout = QHBoxLayout(rename_group)
        rename_layout.setContentsMargins(*GROUP_MARGINS)
        rename_layout.setSpacing(TIGHT_SPACING)
        self.side_title_edit = QLineEdit()
        self.side_title_edit.setPlaceholderText("改成新名字")
        rename_layout.addWidget(self.side_title_edit, 1)
        rename_layout.addWidget(self.rename_button)
        layout.addWidget(rename_group)

        for button in (self.start_button, self.collect_button,
                       self.end_button, self.copy_button, self.close_button):
            layout.addWidget(button)
        layout.addStretch(1)
        return page

    def _build_host_board(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)

        self.host_room_label = QLabel("房间未开启")
        # 需要被老师念给全班的字，所以在整个界面里刻意最大。
        self.host_room_label.setProperty("role", "display")
        self.host_room_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.host_status_label = QLabel("设置好之后点「开启房间」，把房间号报给学生。")
        self.host_status_label.setProperty("muted", True)

        layout.addWidget(self.host_room_label)
        layout.addWidget(self.host_status_label)

        # 依次是 总分榜 / 单题榜 / 提交与代码 / 名单 / 现场记录，
        # 与模块顶部的 HOST_TAB_* 常量一一对应。插新页之前先改那边。
        self.host_tabs = QTabWidget()
        self.host_tabs.currentChanged.connect(self._on_host_tab_changed)

        self.host_overall_table = self._make_table(OVERALL_COLUMNS)
        self.host_tabs.addTab(self.host_overall_table, "总分榜")

        problem_tab = QWidget()
        problem_layout = QVBoxLayout(problem_tab)
        problem_layout.setContentsMargins(*TOP_GAP)
        problem_layout.setSpacing(TIGHT_SPACING)
        self.host_problem_combo = QComboBox()
        self.host_problem_combo.currentIndexChanged.connect(
            lambda _index: self._refresh_host_boards())
        problem_layout.addWidget(self.host_problem_combo)
        self.host_problem_table = self._make_table(PROBLEM_COLUMNS)
        problem_layout.addWidget(self.host_problem_table, 1)
        self.host_tabs.addTab(problem_tab, "单题榜")

        # 「提交与代码」紧跟两个榜：先看谁分高，再看他是怎么写出来的。
        # 名单是"谁在场"，属于另一类信息，排在它后面。
        self.host_tabs.addTab(self._build_code_tab(), "提交与代码")

        self.host_roster_table = self._make_table(ROSTER_COLUMNS)
        self.host_roster_table.itemSelectionChanged.connect(
            self._on_roster_selection_changed)
        # 「名单」页的监考动作：离场锁屏（防窥屏）。老师对某个人（或全体）
        # 下锁屏不需要口令 —— 他手上就有名单，没有什么需要向他证明的。
        roster_tab = QWidget()
        roster_layout = QVBoxLayout(roster_tab)
        roster_layout.setContentsMargins(*TOP_GAP)
        roster_layout.setSpacing(TIGHT_SPACING)
        roster_actions = QHBoxLayout()
        roster_actions.setSpacing(TIGHT_SPACING)
        self.lock_one_button = QPushButton("让 TA 离开一下")
        self.lock_one_button.setProperty("flat", True)
        self.lock_one_button.setEnabled(False)
        self.lock_one_button.setToolTip(
            "把选中这位的屏幕盖上（防窥屏）：他去上厕所时，别人看不到他的题面与代码。\n"
            "他自己回来输口令就能继续；你也可以随时替他解开。")
        self.lock_one_button.clicked.connect(lambda: self.set_selected_lock(True))
        self.unlock_one_button = QPushButton("让 TA 继续")
        self.unlock_one_button.setProperty("flat", True)
        self.unlock_one_button.setEnabled(False)
        self.unlock_one_button.setToolTip("替选中的这位解开屏幕 —— 老师解锁不需要口令。")
        self.unlock_one_button.clicked.connect(lambda: self.set_selected_lock(False))
        self.lock_all_button = QPushButton("全体盖上")
        self.lock_all_button.setProperty("flat", True)
        self.lock_all_button.setEnabled(False)
        self.lock_all_button.clicked.connect(lambda: self.set_all_lock(True))
        self.unlock_all_button = QPushButton("全体继续")
        self.unlock_all_button.setProperty("flat", True)
        self.unlock_all_button.setEnabled(False)
        self.unlock_all_button.clicked.connect(lambda: self.set_all_lock(False))
        for button in (self.lock_one_button, self.unlock_one_button,
                       self.lock_all_button, self.unlock_all_button):
            roster_actions.addWidget(button)
        roster_actions.addStretch(1)
        self.roster_hint = QLabel()
        self.roster_hint.setProperty("muted", True)
        roster_actions.addWidget(self.roster_hint)
        roster_layout.addWidget(self.host_roster_table, 1)
        roster_layout.addLayout(roster_actions)
        self.host_tabs.addTab(roster_tab, "名单")

        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(*TOP_GAP)
        self.host_log = OutputView(self.palette)
        self.host_log.setPlaceholderText("进出场、提交、判定、放榜都会记在这里")
        log_layout.addWidget(self.host_log)
        self.host_tabs.addTab(log_tab, "现场记录")

        self.host_tabs.addTab(self._build_archive_tab(), "历史场次")

        layout.addWidget(self.host_tabs, 1)
        # 构造时先扫一遍：老师切到这一页时列表里就该有东西，而不是一片空白
        # 等他点"刷新"。目录不存在时 list_archives 回空表，不报错。
        self.refresh_archives()
        return panel

    def _build_archive_tab(self) -> QWidget:
        """「历史场次」：历次测验的档案，可回看、删除（导出见 ``core/export.py``）。

        档案在关房时自动写到 ``%LOCALAPPDATA%\\OfflineOJ\\exams\\``（见
        ``core/records.py``），这一页只是把它列出来。

        **回看刻意复用「提交与代码」页**，这里不再做第二份只读列表：那一页的
        刷新纪律（选中跟提交编号走、内容没变不重灌、重填期间屏蔽信号）是拿真实
        考场里的问题换来的，抄一遍必然在某个角落丢掉其中一条。
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(*TOP_GAP)
        layout.setSpacing(TIGHT_SPACING)

        self.archive_hint = QLabel()
        self.archive_hint.setProperty("muted", True)
        self.archive_hint.setWordWrap(True)
        layout.addWidget(self.archive_hint)

        self.archive_table = self._make_table(ARCHIVE_COLUMNS)
        self.archive_table.itemDoubleClicked.connect(
            lambda _item: self.open_selected_archive())
        self.archive_table.itemSelectionChanged.connect(self._on_archive_selected)
        layout.addWidget(self.archive_table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(ROW_SPACING)
        self.archive_refresh_button = QPushButton("刷新")
        self.archive_refresh_button.setProperty("flat", True)
        self.archive_refresh_button.clicked.connect(self.refresh_archives)
        buttons.addWidget(self.archive_refresh_button)

        self.archive_open_button = QPushButton("打开查看")
        self.archive_open_button.setToolTip(
            "在「提交与代码」里回看这一场（含判定说明与源码）")
        self.archive_open_button.clicked.connect(self.open_selected_archive)
        buttons.addWidget(self.archive_open_button)

        self.archive_export_button = QPushButton("导出…")
        self.archive_export_button.setToolTip(
            "把选中的这一场导成成绩单 / 提交明细（txt / csv / Excel / Word / PDF），"
            "或者一个含源码的完整档案包")
        self.archive_export_button.clicked.connect(self.export_selected_archive)
        buttons.addWidget(self.archive_export_button)

        self.archive_reveal_button = QPushButton("打开目录")
        self.archive_reveal_button.setProperty("flat", True)
        self.archive_reveal_button.clicked.connect(self.reveal_archives_dir)
        buttons.addWidget(self.archive_reveal_button)

        buttons.addStretch(1)
        self.archive_delete_button = QPushButton("删除")
        self.archive_delete_button.setProperty("variant", "danger")
        self.archive_delete_button.clicked.connect(self.delete_selected_archive)
        buttons.addWidget(self.archive_delete_button)
        layout.addLayout(buttons)
        return tab

    def _build_code_tab(self) -> QWidget:
        """主机端看学生交上来的源码。

        **为什么主机端看得到、学生端看不到别人的。** 源码从来就没有上线：
        :meth:`Submission.to_dict` 刻意不带 ``code`` 字段，排行榜载荷里只有
        排名行，学生端收到的每一个字节里都没有别人的代码。所以"老师看得到
        代码、学生只看得到榜单"不是靠界面藏起来的，是那份数据压根没发出去 ——
        学生的机器上没有源码可解，也没有密文可破。主机这边能看，是因为
        :class:`ExamServer` 在自己内存里持有完整的 :class:`Submission`。

        也正因如此，学生即使把角色切到"主机端"也看不到什么：那时他开的是
        自己的房间，而那个房间里没有别人提交过。
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(*TOP_GAP)
        layout.setSpacing(TIGHT_SPACING)

        splitter = QSplitter(Qt.Vertical)

        # 这一行只在"回看档案"时出现，说明现在看的**不是**进行中的这一场。
        # 少了它，一页两年前的成绩和刚交上来的卷子长得一模一样。
        self.code_scope_label = QLabel()
        self.code_scope_label.setProperty("role", "strong")
        self.code_scope_label.setWordWrap(True)
        self.code_scope_label.setVisible(False)
        layout.addWidget(self.code_scope_label)

        self.host_code_table = self._make_table(SUBMISSION_COLUMNS)
        # 「优化」这一列写的是 GCC 家族的参数名，列头把 MSVC 的对应写法说清楚
        header_item = self.host_code_table.horizontalHeaderItem(7)
        if header_item is not None:
            header_item.setToolTip(
                "判这份提交时的编译优化开关。\n"
                "GCC 系：-O2 / -O0　　MSVC：/O2 / /Od\n"
                "Python 与 Java 没有这个概念，显示为空。")
        self.host_code_table.itemSelectionChanged.connect(self._on_submission_selected)
        splitter.addWidget(self.host_code_table)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(TIGHT_SPACING)

        head = QHBoxLayout()
        head.setSpacing(ROW_SPACING)
        self.host_code_caption = QLabel()
        self.host_code_caption.setProperty("role", "strong")
        head.addWidget(self.host_code_caption, 1)
        self.copy_code_button = QPushButton("复制代码")
        self.copy_code_button.setProperty("flat", True)
        self.copy_code_button.setEnabled(False)
        self.copy_code_button.clicked.connect(self.copy_selected_code)
        head.addWidget(self.copy_code_button)
        # 雷同检测放在这一页：它分析的就是"这一页看到的这批提交"，而且
        # 档案回看时同一页会切到档案的数据源 —— 一个按钮覆盖两种数据源，
        # 不需要为"考完了想查一下"再开一个新入口。
        self.similarity_button = QPushButton("雷同检测…")
        self.similarity_button.setProperty("flat", True)
        # 初始禁用：这里还没有任何提交可比。之后跟着表格内容走（见
        # _fill_submissions 里那一行），所以不用在开房/关房/开档案各处再同步。
        self.similarity_button.setEnabled(False)
        self.similarity_button.setToolTip(
            "把这一页的提交两两比一遍：\n"
            "① 完全重复 —— 去掉注释与空行后一字不差（确定性的）；\n"
            "② 高度相似 —— 改过名字也能看出来（启发式，**必须人工复核**）。\n"
            "只在同一道题内比较，并会自动扣除本题的公共模板。")
        self.similarity_button.clicked.connect(self.run_similarity_check)
        head.addWidget(self.similarity_button)
        detail_layout.addLayout(head)

        # 源码和判定说明各占一页签，而不是上下叠着。判定说明平时就一行，
        # 编译错误却能到几十行 —— 让它常驻会把源码挤成一条缝，而它的打开
        # 频率远低于源码。分成两页，两边都能占满。
        self.host_code_tabs = QTabWidget()
        self.host_code_view = CodeEditor(self.palette, completion=False)
        self.host_code_view.setReadOnly(True)
        self.host_code_view.setPlaceholderText("在上面选一次提交，这里显示他交上来的源码")
        self.host_code_tabs.addTab(self.host_code_view, "源码")

        self.host_verdict_view = OutputView(self.palette)
        self.host_verdict_view.setPlaceholderText("判定说明（编译错误、失败原因）显示在这里")
        self.host_code_tabs.addTab(self.host_verdict_view, "判定说明")
        detail_layout.addWidget(self.host_code_tabs, 1)

        splitter.addWidget(detail)
        splitter.setSizes([220, 420])
        layout.addWidget(splitter, 1)
        return tab

    # ==================================================================
    # 学生端：搭界面
    # ==================================================================

    def _build_student_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._build_join_group())
        # 学生界面与「离开中」盖板做成一个堆叠：盖住 = 切到另一页。
        # 不用遮罩式弹窗，是因为盖板必须连"关掉这个窗口"之外的一切都挡住 ——
        # 题面、代码、榜单，一样都不该在学生离座时留在屏幕上。
        self.student_stack = QStackedWidget()
        self.student_stack.addWidget(self._build_student_body())
        self.student_stack.addWidget(self._build_lock_cover())
        layout.addWidget(self.student_stack, 1)
        return page

    def _build_lock_cover(self) -> QWidget:
        """「离开中」盖板。

        覆盖整个学生区，只留一个口令框。口令**发到主机校验**（学生机可被改，
        本地比对等于没比对），所以这里显示的一切都只是"等主机回话"。
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(*PAGE_MARGINS)
        outer.addStretch(1)

        box = QGroupBox("屏幕已锁定 · 离开中")
        box.setMaximumWidth(460)
        form = QVBoxLayout(box)
        form.setContentsMargins(*GROUP_MARGINS)
        form.setSpacing(ROW_SPACING)

        self.lock_reason_label = QLabel("你离开了座位。回来后输入口令继续答题。")
        self.lock_reason_label.setWordWrap(True)
        form.addWidget(self.lock_reason_label)

        self.lock_passcode_edit = QLineEdit()
        self.lock_passcode_edit.setEchoMode(QLineEdit.Password)
        self.lock_passcode_edit.setPlaceholderText("输入口令解开屏幕")
        self.lock_passcode_edit.returnPressed.connect(self.unlock_screen)
        form.addWidget(self.lock_passcode_edit)

        self.lock_submit_button = QPushButton("解开，继续答题")
        self.lock_submit_button.setProperty("variant", "primary")
        self.lock_submit_button.clicked.connect(self.unlock_screen)
        form.addWidget(self.lock_submit_button)

        self.lock_hint_label = QLabel()
        self.lock_hint_label.setProperty("muted", True)
        self.lock_hint_label.setWordWrap(True)
        self.lock_hint_label.setText(
            "口令是你自己的个人口令（账号进场），或本场口令。\n"
            "输错次数有限，用完只能请老师放行。")
        form.addWidget(self.lock_hint_label)

        outer.addWidget(box, 0, Qt.AlignHCenter)
        outer.addStretch(1)
        return page

    def _apply_lock_state(self) -> None:
        """按客户端当前状态决定"屏幕盖没盖"。"""
        client = self._client
        locked = bool(client is not None and client.locked)
        if locked == self._lock_visible:
            return
        self._lock_visible = locked
        self.student_stack.setCurrentIndex(1 if locked else 0)
        if locked:
            self.lock_passcode_edit.clear()
            reason = client.lock_reason or "你离开了座位。"
            self.lock_reason_label.setText(
                f"{reason}\n输入口令后按回车（或点按钮）继续答题。")
            self.lock_hint_label.setText(
                "口令是你自己的个人口令（账号进场），或本场口令。\n"
                "输错次数有限，用完只能请老师放行。")
            self.lock_submit_button.setEnabled(True)
            self.lock_passcode_edit.setFocus()

    def leave_seat(self) -> None:
        """按「离开一下」：请主机把自己盖住。

        本机连一个可能的口令都没有时**拒绝盖屏**：盖上解不开的屏幕，
        学生只能干坐着等老师发现 —— 这不是保护，是事故。
        """
        client = self._client
        if client is None or self._lock_visible:
            return
        if not self._unlock_possible():
            self.notify.emit(
                "warning",
                "本场没有可用的解锁口令（你没输过个人口令，老师也没设考场口令），"
                "盖上之后解不开。请在进场时填好口令，或让老师设一个考场口令。")
            return
        client.request_lock()
        self._apply_lock_state()

    def _unlock_possible(self) -> bool:
        """本机知不知道一个可能解得开的口令。不知道就不该让人把屏幕盖上。"""
        client = self._client
        if client is None:
            return False
        if client.using_account:
            return bool(self.student_passcode_edit.text().strip())
        return bool(self.student_password_edit.text().strip())

    def unlock_screen(self) -> None:
        """请求解锁。口令发给主机核对，**本机不比对**。"""
        client = self._client
        if client is None or not client.locked:
            self._apply_lock_state()
            return
        code = self.lock_passcode_edit.text()
        if not code.strip():
            self.lock_hint_label.setText("先输入口令。")
            return
        self.lock_submit_button.setEnabled(False)
        self.lock_hint_label.setText("正在请老师那台机器核对口令…")
        try:
            client.request_unlock(code)
        except Exception as exc:                        # noqa: BLE001
            log.exception("请求解锁失败")
            self.lock_submit_button.setEnabled(True)
            self.lock_hint_label.setText(f"发不出去：{exc}")

    def _build_join_group(self) -> QWidget:
        """进场栏。

        排成两行而不是挤成一行：一行塞七八个控件时，窗口一窄，"房间号"标签
        和它右边的输入框就会被压得对不上号 —— 而进场这一步是不能猜的。
        两种进场方式用一个小堆叠切换：学生**看得到、也只看得到**自己要填的那
        两三个格子，另一套完全不占地方。
        """
        group = QGroupBox("加入房间")
        outer = QVBoxLayout(group)
        outer.setContentsMargins(*GROUP_MARGINS)
        outer.setSpacing(ROW_SPACING)

        grid = QGridLayout()
        grid.setHorizontalSpacing(ROW_SPACING)
        grid.setVerticalSpacing(ROW_SPACING)

        self.join_mode_combo = QComboBox()
        self.join_mode_combo.addItem("房间号进场", EntryMode.ROOM_CODE)
        self.join_mode_combo.addItem("账号进场", EntryMode.ACCOUNT)
        self.join_mode_combo.setToolTip(
            "老师宣布用哪种方式进场，就选哪一种。\n"
            "账号进场时不需要房间号，名字也由老师那份名单决定 —— 你报什么都不算数。")
        self.join_mode_combo.currentIndexChanged.connect(
            self._on_join_mode_changed)

        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("主机地址，如 192.168.1.5")
        self.address_edit.setMinimumWidth(160)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(1024, 65535)
        self.port_edit.setValue(DEFAULT_PORT)
        self.port_edit.setFixedWidth(96)

        grid.addWidget(QLabel("主机地址"), 0, 0)
        grid.addWidget(self.address_edit, 0, 1)
        grid.addWidget(QLabel("端口"), 0, 2)
        grid.addWidget(self.port_edit, 0, 3)
        grid.addWidget(QLabel("进场方式"), 0, 4)
        grid.addWidget(self.join_mode_combo, 0, 5)

        # ---- 凭据区：随进场方式切换 ----
        room_page = QWidget()
        room_layout = QHBoxLayout(room_page)
        room_layout.setContentsMargins(0, 0, 0, 0)
        room_layout.setSpacing(TIGHT_SPACING)
        self.student_code_edit = QLineEdit()
        self.student_code_edit.setMaxLength(ROOM_CODE_LENGTH)
        self.student_code_edit.setPlaceholderText("6 位数字")
        self.student_code_edit.setFixedWidth(120)
        self.student_code_edit.setToolTip("老师报的那 6 位数字")
        self.student_password_edit = QLineEdit()
        self.student_password_edit.setEchoMode(QLineEdit.Password)
        self.student_password_edit.setPlaceholderText("老师没设就留空")
        self.student_password_edit.setMaximumWidth(180)
        self.student_name_edit = QLineEdit()
        self.student_name_edit.setPlaceholderText("你的名字（可以和别人重名）")
        self.student_name_edit.setMinimumWidth(140)
        room_layout.addWidget(QLabel("房间号"))
        room_layout.addWidget(self.student_code_edit)
        room_layout.addWidget(QLabel("房间口令"))
        room_layout.addWidget(self.student_password_edit)
        room_layout.addWidget(QLabel("用户名"))
        room_layout.addWidget(self.student_name_edit, 1)

        account_page = QWidget()
        account_layout = QHBoxLayout(account_page)
        account_layout.setContentsMargins(0, 0, 0, 0)
        account_layout.setSpacing(TIGHT_SPACING)
        self.student_account_edit = QLineEdit()
        self.student_account_edit.setPlaceholderText("老师名单上的账号")
        self.student_account_edit.setMinimumWidth(140)
        self.student_passcode_edit = QLineEdit()
        self.student_passcode_edit.setEchoMode(QLineEdit.Password)
        self.student_passcode_edit.setPlaceholderText("口令条上的个人口令")
        self.student_passcode_edit.setMaximumWidth(180)
        self.student_passcode_edit.setToolTip(
            "你自己的口令。它同时也是「离开一下」之后解开屏幕的钥匙。")
        account_layout.addWidget(QLabel("账号"))
        account_layout.addWidget(self.student_account_edit)
        account_layout.addWidget(QLabel("个人口令"))
        account_layout.addWidget(self.student_passcode_edit)
        account_layout.addStretch(1)

        self.join_cred_stack = QStackedWidget()
        self.join_cred_stack.addWidget(room_page)
        self.join_cred_stack.addWidget(account_page)
        grid.addWidget(self.join_cred_stack, 1, 0, 1, 6)

        self.device_edit = QLineEdit()
        self.device_edit.setReadOnly(True)
        self.device_edit.setFixedWidth(120)
        self.device_edit.setToolTip(
            "本机设备 ID：首次运行生成后固定不变，跨场次都是同一个。\n"
            "排行榜靠它区分同名的同学，所以它不出现在任何可以手改的地方。")
        copy_device = QPushButton("复制")
        copy_device.setProperty("flat", True)
        copy_device.setFixedWidth(56)
        copy_device.clicked.connect(
            lambda _checked=False: self._copy_text(self.device_edit.text(), "设备 ID"))
        grid.addWidget(QLabel("设备 ID"), 2, 0)
        grid.addWidget(self.device_edit, 2, 1)
        grid.addWidget(copy_device, 2, 2)

        self.join_button = QPushButton("加入房间")
        self.join_button.setProperty("variant", "primary")
        self.join_button.setFixedWidth(120)
        self.join_button.clicked.connect(self.toggle_join)
        grid.addWidget(self.join_button, 0, 6, 3, 1, Qt.AlignVCenter)
        grid.setColumnStretch(5, 1)
        outer.addLayout(grid)

        self.join_status = QLabel("未加入。请填入老师给的地址、端口与房间号。")
        self.join_status.setProperty("muted", True)
        outer.addWidget(self.join_status)
        return group

    def _on_join_mode_changed(self, *_args) -> None:
        """两种进场方式只露出一套输入框。

        账号进场时**没有用户名可填**：名字由老师那份名单决定 —— 这一条就是
        "绑定选手信息"，把它留在界面上只会让学生以为自己填了就算数。
        """
        account_mode = (EntryMode.from_value(self.join_mode_combo.currentData())
                        is EntryMode.ACCOUNT)
        self.join_cred_stack.setCurrentIndex(1 if account_mode else 0)

    def _build_student_body(self) -> QWidget:
        body = QSplitter(Qt.Horizontal)

        # ---- 左：题目、题面与开考倒计时 ----
        left_box = QWidget()
        left_layout = QVBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(ROW_SPACING)

        # 备考阶段的倒计时挂在这里（只在未开考时出现）。**同一屏上同一个数字
        # 只出现一次** —— 这一栏既然写了"距开考 M:SS"，右边状态行就不再重复，
        # 见 ``_apply_student_state`` 里把 clock 置空那一段。
        self.student_clock_label = QLabel()
        self.student_clock_label.setProperty("role", "strong")
        self.student_clock_label.setVisible(False)
        left_layout.addWidget(self.student_clock_label)

        left = QSplitter(Qt.Vertical)
        self.student_problem_list = QListWidget()
        self.student_problem_list.setToolTip("加入房间后，老师选定的题目会出现在这里")
        self.student_problem_list.currentItemChanged.connect(
            self._on_student_problem_changed)
        left.addWidget(self.student_problem_list)

        self.statement_view = MarkdownView(self.palette)
        self.statement_view.setPlaceholderText("选中左边的题目看题面")
        left.addWidget(self.statement_view)
        left.setSizes([200, 460])
        left_layout.addWidget(left, 1)
        body.addWidget(left_box)

        # ---- 右：编辑器与结果 ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(ROW_SPACING)

        self.student_status_label = QLabel("未加入房间")
        self.student_status_label.setProperty("role", "strong")
        right_layout.addWidget(self.student_status_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(ROW_SPACING)
        action_row.addWidget(QLabel("语言"))
        self.language_combo = QComboBox()
        for language in SUBMIT_LANGUAGES:
            self.language_combo.addItem(language.display, language.value)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        action_row.addWidget(self.language_combo)

        self.submit_button = QPushButton("提交代码")
        self.submit_button.setProperty("variant", "primary")
        self.submit_button.setEnabled(False)
        self.submit_button.clicked.connect(self.submit_code)
        action_row.addWidget(self.submit_button)

        # 代码文件也放在这一行：考试时学生要先能把自己的代码存下来。
        # 两者都在"改这份代码"这一类动作里，和「提交代码」并排最顺手。
        self.open_code_button = QPushButton("打开…")
        self.open_code_button.setProperty("flat", True)
        self.open_code_button.setToolTip(
            "从文件读一份代码到编辑器 (Ctrl+O)\n"
            "记事本存的 UTF-8 带 BOM 也能正确读入。")
        self.open_code_button.clicked.connect(self.open_code_file)

        self.save_code_button = QPushButton("另存为…")
        self.save_code_button.setProperty("flat", True)
        self.save_code_button.setToolTip("把编辑器里的代码存成文件 (Ctrl+Shift+S)")
        self.save_code_button.clicked.connect(self.save_code_as)
        action_row.addWidget(self.open_code_button)
        action_row.addWidget(self.save_code_button)

        self.attempt_label = QLabel()
        self.attempt_label.setProperty("muted", True)
        action_row.addWidget(self.attempt_label, 1)

        # 离场锁屏（防窥屏）：去厕所、去讲台交卷这类离开座位的时候，
        # 把题面和代码整个盖上，回来输口令解开。
        self.away_button = QPushButton("离开一下")
        self.away_button.setProperty("flat", True)
        self.away_button.setEnabled(False)
        self.away_button.setToolTip(
            "暂时离开座位时盖上屏幕，别人看不到题面和代码。\n"
            "回来要输你自己的口令（账号进场）或本场口令才能继续。")
        self.away_button.clicked.connect(self.leave_seat)
        action_row.addWidget(self.away_button)
        right_layout.addLayout(action_row)

        self.code_editor = CodeEditor(self.palette)
        right_layout.addWidget(self.code_editor, 1)

        self.code_files = CodeFileController(
            widget=self,
            editor=self.code_editor,
            settings=self.ctx.settings,
            notify=self.notify,
            current_language=self.current_language,
            select_language=self._select_language,
        )
        # 学生端不放"当前文件・未保存"那行小字：这一栏上面已经排了七件东西，
        # 再挤一个小字标签只会更乱。文件关联仍在，标题栏状态由状态栏那条
        # "已打开 xxx" 交代。
        self.code_editor.connect_content_changed(self.code_files.refresh_label)

        # 依次是 我的提交 / 排行榜 / 连接记录，与 STUDENT_TAB_* 常量对应。
        self.student_tabs = QTabWidget()
        self.mine_table = self._make_table(MINE_COLUMNS)
        self.student_tabs.addTab(self.mine_table, "我的提交")

        self.student_board_table = self._make_table(OVERALL_COLUMNS)
        self.student_tabs.addTab(self.student_board_table, "排行榜")

        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(*TOP_GAP)
        self.student_log = OutputView(self.palette)
        self.student_log.setPlaceholderText("连接状态、收卷提醒与提交回执会记在这里")
        log_layout.addWidget(self.student_log)
        self.student_tabs.addTab(log_tab, "连接记录")
        right_layout.addWidget(self.student_tabs, 1)

        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([460, 840])
        return body

    # ==================================================================
    # 通用小工具
    # ==================================================================

    @staticmethod
    def _make_table(columns: tuple[str, ...]) -> QTableWidget:
        """榜单/名单用同一套表格：只读、整行选中、不要行号。

        列宽一律**贴合内容**，多出来的宽度留在右边。榜单的每一列宽度都是
        有含义的（名次、设备 ID、耗时、得分），把某几列拉宽只会让读者
        在中间隔着一大片空白去对行；OJ 界的榜单惯例也是左侧紧凑排布。
        """
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        return table

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list[tuple[str, ...]],
                    *, right_aligned: tuple[int, ...] = (),
                    row_keys: list | None = None) -> None:
        """整体重填一张表。

        重填而不是"按差异更新"：榜单的每一行都可能因为别人提交而整体平移，
        做增量更新要维护的映射比重新填一遍更容易出错，而一场测验也就几十行。

        :param row_keys: 给了就在第 0 列挂一个"这一行指的是谁"的键
            （``Qt.UserRole``）。表格里只放给人看的文本，要按选中行取回对应的
            对象时（例如从提交列表取出那份源码）就靠这个键 —— **不能用行号**，
            新提交插到最前面会把所有行整体往下推一格。
        """
        table.clearSpans()
        table.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column in right_aligned:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if row_keys is not None and column == 0:
                    item.setData(Qt.UserRole, row_keys[index])
                table.setItem(index, column, item)

    def _copy_text(self, text: str, what: str) -> None:
        """复制到剪贴板并回报。空内容时不写剪贴板，免得把别人的内容洗掉。"""
        if not text:
            self.notify.emit("warning", f"还没有{what}可以复制。")
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self.status(f"{what}已复制到剪贴板")

    def _log(self, view: OutputView, text: str, color: str = "") -> None:
        view.append_line(f"[{datetime.now():%H:%M:%S}] {text}", color)

    # ==================================================================
    # 角色切换与身份
    # ==================================================================

    def set_role(self, role: str) -> None:
        """以代码方式切换角色（菜单/工具栏入口用）。"""
        index = self.role_combo.findData(role)
        if index >= 0:
            self.role_combo.setCurrentIndex(index)

    def _on_role_changed(self, *_args) -> None:
        role = self.role_combo.currentData()
        self.stack.setCurrentWidget(
            self.host_page if role == "host" else self.student_page)
        if role == "host":
            self.role_hint.setText("学生端只需要「地址 + 端口 + 房间号」")
        else:
            self.role_hint.setText("判题在老师那台机器上做，本机不需要装编译器")
        if role == "host":
            self.refresh_problem_choices()

    def _load_identity(self) -> None:
        """把本机设备 ID 与上次用过的用户名填进学生端。

        **在面板构建时就落盘一次**：这样"第一次运行"和"第二次运行"在代码上是
        同一条路径，不会出现"只有真正加入过房间才会有 ID"这种隐藏状态。
        """
        identity = get_identity(self.ctx.paths.device_file)
        self._identity = identity
        self.device_edit.setText(identity.device_id)
        if identity.username:
            self.student_name_edit.setText(identity.username)
        if identity.host:
            self.device_edit.setToolTip(
                f"本机设备 ID（{identity.host}）。首次运行生成后固定不变，"
                "排行榜靠它区分同名的同学。")

    def _persist_identity(self) -> None:
        identity = self._identity or get_identity(self.ctx.paths.device_file)
        identity.username = self.student_name_edit.text().strip()
        if self._client is not None:
            identity.device_id = self._client.device_id
        if not save_identity(self.ctx.paths.device_file, identity):
            # 写不了只是"下次进场换个新 ID"这种程度的问题，不打断学生
            self.status("设备标识写入失败，本次仍可正常答题")

    # ==================================================================
    # 主机端：开启房间与运行
    # ==================================================================

    def refresh_problem_choices(self) -> None:
        """按题库重填选题列表。房间开着的时候不重填 —— 那会改掉本场题目。"""
        if self._server is not None:
            return
        checked = self._checked_problem_ids()
        self.problem_list.clear()
        for problem in self.ctx.repository.all():
            item = QListWidgetItem(f"{problem.id}  {problem.title}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, problem.id)
            item.setCheckState(
                Qt.Checked if not checked or problem.id in checked
                else Qt.Unchecked)
            item.setToolTip(
                f"英文名 {problem.english_name} · "
                f"{problem.time_limit} ms / {problem.memory_limit} MB · "
                f"{len(problem.testcases)} 个测试点 · "
                f"满分 {problem.total_points}")
            self.problem_list.addItem(item)
        self._refresh_pick_hint()

    def _checked_problem_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.problem_list.count()):
            item = self.problem_list.item(row)
            if item.checkState() == Qt.Checked:
                ids.append(str(item.data(Qt.UserRole)))
        return ids

    def _set_all_checked(self, value: bool) -> None:
        state = Qt.Checked if value else Qt.Unchecked
        for row in range(self.problem_list.count()):
            self.problem_list.item(row).setCheckState(state)
        self._refresh_pick_hint()

    def _refresh_pick_hint(self) -> None:
        self.pick_hint.setText(f"已选 {len(self._checked_problem_ids())} 题")

    def _reroll_room_code(self) -> None:
        self.room_code_edit.setText(generate_room_code())

    def toggle_room(self) -> None:
        """开启 / 关闭房间。"""
        if self._server is not None:
            self.close_room()
            return
        self.open_room()

    # ---- 进场方式与名单 ----------------------------------------------------

    def _on_entry_mode_changed(self, *_args) -> None:
        """换进场方式时把名单行点亮 / 置灰。

        **标签只有一个写入口**（本方法）：开房状态、进场方式、名单内容三件事
        都会影响它该怎么显示，散在几处写迟早会互相覆盖。
        """
        account_mode = self._entry_mode is EntryMode.ACCOUNT
        self.roster_button.setEnabled(
            account_mode and self._server is None)
        self.roster_label.setEnabled(account_mode)
        if not account_mode:
            self.roster_label.setText("不需要名单")
        elif self._roster is None:
            self.roster_label.setText("未选择名单")
        else:
            self.roster_label.setText(
                f"{self._roster.title} · {len(self._roster)} 人")

    @property
    def _entry_mode(self) -> EntryMode:
        return EntryMode.from_value(self.entry_mode_combo.currentData())

    def _load_last_roster(self) -> None:
        """把上一次用过的名单带回来。

        老师的名单是按班存的，一学期就那几份 —— 每次开面板都要重新点一遍
        「名单…」是把同一个动作做成日常负担。
        """
        name = self.ctx.settings.get("last_roster", "")
        if not name:
            return
        path = self.ctx.paths.roster_file(str(name))
        if not path.exists():
            return
        try:
            self._roster = Roster.load(path)
        except RosterError as exc:
            log.warning("上次用过的名单读不出来: %s", exc)
            return
        self._roster_path = path
        self._refresh_roster_label()

    def edit_roster(self) -> None:
        """打开名单编辑器（建名单 / 改名单都在这里，不必先去画 Excel 表）。"""
        dialog = RosterDialog(self.ctx, roster=self._roster, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        roster = dialog.roster()
        if roster is None:
            return
        self._roster = roster
        self._roster_path = self.ctx.paths.roster_file(roster.title)
        self.ctx.settings.set("last_roster", roster.title)
        self._refresh_roster_label()
        self._log(self.host_log,
                  f"名单已更新：{roster.title} · {len(roster)} 人 "
                  f"（{self._roster_path.name}）", self.palette.info)

    def _refresh_roster_label(self) -> None:
        self._on_entry_mode_changed()

    def open_room(self) -> None:
        problems = self._selected_problems()
        if not problems:
            self.notify.emit("warning", "请先勾选至少一道题目。")
            return

        code = normalize_room_code(self.room_code_edit.text())
        if len(code) != ROOM_CODE_LENGTH or not code.isdigit():
            self.notify.emit("warning",
                             f"房间号必须是 {ROOM_CODE_LENGTH} 位数字。")
            return

        policy = self._collect_policy()
        if policy is None:
            return

        entry_mode = self._entry_mode
        if entry_mode is EntryMode.ACCOUNT:
            if self._roster is None or not len(self._roster):
                self.notify.emit(
                    "warning",
                    "账号进场需要一份选手名单。点「名单…」建一份，"
                    "或者把进场方式换回「房间号进场」。")
                return
            # 名单上有没口令的人 = 只要账号就能进场。开房前替老师补上并提示，
            # 比让他事后发现"有人没密码也进来了"强。
            filled = self._roster.fill_missing_passcodes()
            if filled:
                self.notify.emit(
                    "warning",
                    f"名单里有 {filled} 位选手还没有口令，已经补上了。"
                    "开考前请把口令发下去（「名单…」里可以看到）。")

        # 备考：把开考时刻放到 N 分钟之后。0 就是"立即开考"，与旧行为一致
        # （starts_at 传 None 时 ExamSession 自己取 now）。
        delay = self.start_delay_spin.value()
        starts_at = (datetime.now() + timedelta(minutes=delay)) if delay else None

        session = ExamSession(
            session_id=f"{datetime.now():%Y%m%d%H%M%S}",
            title=self.title_edit.text().strip() or DEFAULT_TITLE,
            mode=self.mode_combo.currentData(),
            room_code=code,
            password=self.password_edit.text(),
            duration_minutes=self.duration_spin.value(),
            force_collect=self.force_collect_check.isChecked(),
            allow_resubmit=self.resubmit_check.isChecked(),
            show_leaderboard=self.leaderboard_check.isChecked(),
            policy=policy,
            starts_at=starts_at,
            entry_mode=entry_mode,
        )
        if entry_mode is EntryMode.ACCOUNT and self._roster is not None:
            session.set_contestants(item.to_dict() for item in self._roster)
        session.set_problems([build_exam_problem(item) for item in problems])

        server = ExamServer(
            session, problems,
            compiler_paths=self.ctx.compiler_paths(),
            work_root=str(self.ctx.paths.work_dir),
            config=ServerConfig(port=self.port_spin.value(),
                                optimize=self.o2_check.isChecked()),
            judge=self._judge,
            on_event=self.host_event.emit,
        )
        try:
            server.start()
        except OSError as exc:
            # 端口被占用是最常见的失败。说清楚怎么办，而不是丢一个 WinError。
            self.notify.emit("error",
                             f"开启房间失败：{exc}\n"
                             f"端口 {self.port_spin.value()} 可能已被占用，"
                             "换一个端口再试。")
            return

        self._session = session
        self._server = server
        # 新的一间房不该接着显示上一份档案的提交（那一页长得一模一样）
        self.close_archive()
        self._apply_host_state(running=True)
        self._refresh_host_views()
        self._log(self.host_log,
                  f"房间已开启 · 房间号 {session.room_code} · "
                  f"{session.mode.label} · {session.entry_mode.label} · "
                  f"{len(problems)} 题 · 端口 {server.port}", self.palette.success)
        if session.entry_mode is EntryMode.ACCOUNT:
            self._log(self.host_log,
                      f"学生凭名单上的账号与个人口令进场"
                      f"（{len(session.contestants)} 人）", self.palette.info)
        if session.password:
            self._log(self.host_log, "已启用考场口令", self.palette.info)
        self.status(f"房间 {session.room_code} 已开启，端口 {server.port}")

    def _collect_policy(self) -> ExamPolicy | None:
        """把「本场限制」那几个勾选读成一份策略；配置不合法时返回 ``None``。

        "一种语言都不允许"是没意义的配置（学生交不了任何代码）。与其让它变成一间
        谁也用不了的房间，不如在开房这一步挡住 —— 那时老师还看得见提示。
        """
        allowed = tuple(value for value, box in self.language_checks.items()
                        if box.isChecked())
        everything = {language.value for language in SUBMIT_LANGUAGES}
        if not allowed:
            self.notify.emit("warning",
                             "至少要允许一种语言，否则学生交不了任何代码。")
            return None
        return ExamPolicy(
            # 全选 == 不限。写空元组的意义在于：没设限制时载荷与旧版本逐字节一致。
            allowed_languages=() if set(allowed) == everything else allowed,
            allow_copy_out=self.copy_out_check.isChecked(),
            lock_after_submit=self.lock_after_submit_check.isChecked(),
        )

    def _selected_problems(self) -> list[Problem]:
        wanted = self._checked_problem_ids()
        return [problem for problem in self.ctx.repository.all()
                if problem.id in wanted]

    def close_room(self, *, quiet: bool = False) -> None:
        server, self._server = self._server, None
        session, self._session = self._session, None
        # 存档必须在丢掉 session 之前做 —— 下面 _clear_host_views() 会把
        # _submission_by_serial 连同这一场的全部提交一起丢掉。
        if session is not None:
            self._archive_session(session, quiet=quiet)
        if server is not None:
            # 超时给短一点：这是退出路径，卡住界面比少等一会儿更糟。
            # 判题线程是 daemon，正在跑的测试点会随进程一起走。
            server.stop(timeout=1.5)
        self._apply_host_state(running=False)
        self._clear_host_views()
        if not quiet:
            self._log(self.host_log, "房间已关闭", self.palette.text_muted)
            self.status("房间已关闭")

    def _archive_session(self, session, *, quiet: bool) -> None:
        """关房时把整场写进档案（见 ``core/records.py``）。

        **这一条绝不能把关房搞失败。** 存档是"顺手多做一件事"，任何异常都只
        记一条日志 —— 老师下课关房这个动作不能因为磁盘满了/文件被占用而弹框
        或卡住。所以整段包在 try 里。
        """
        if not self.ctx.settings.bool("archive_on_close"):
            return
        if not session.submissions and not session.participants:
            # 开了又关、一个人都没进来：不留空档案，否则档案列表会被试错塞满。
            return
        keep_code = self.ctx.settings.bool("archive_keep_code")
        started = session.starts_at or datetime.now()
        try:
            path = records.write_archive(
                self.ctx.paths.exams_dir,
                stamp=started.strftime("%Y%m%d-%H%M%S"),
                record=session.archive_record(),
                submissions=session.archive_submissions(keep_code=keep_code),
                leaderboard=session.archive_leaderboard(),
                keep_code=keep_code,
            )
        except Exception as exc:                      # noqa: BLE001 - 见上文
            log.exception("写测验档案失败")
            self._log(self.host_log, f"本场存档失败：{exc}", self.palette.danger)
            return
        tail = "" if keep_code else "（不含源码）"
        self._log(self.host_log, f"本场已存档：{path.name}{tail}",
                  self.palette.success)
        if not quiet:
            self.status(f"本场已存档：{path.name}{tail}")
        self.refresh_archives()

    def _apply_host_state(self, *, running: bool) -> None:
        # ---- 先换屏：房间开没开决定左列是"准备"还是"监考" ----
        # 这是本方法的第一件事：下面那一长串逐控件 setEnabled 有一半只在准备
        # 视图里看得见，先把正确的页翻出来，老师看到的才是"该看到的那一页"。
        # 注意**不能**改成给整页 setEnabled —— 有六个测试直接断言单个控件的
        # isEnabled()（见 test_exam_settings / test_o2_option），逐控件冻结
        # 的写法是它们的前提。
        self.host_side.setCurrentIndex(
            HOST_SIDE_PROCTOR if running else HOST_SIDE_PREPARE)
        if running:
            # 进监考视图时焦点给"改名"那个框：老师最可能在开房后立刻想改的
            # 就是它（比如刚才顺手写的名字要改成"第三次模拟赛"）。
            self.side_title_edit.setText(self.title_edit.text())
        for widget in (self.mode_combo, self.room_code_edit,
                       self.password_edit, self.port_spin, self.duration_spin,
                       self.force_collect_check, self.resubmit_check,
                       self.leaderboard_check, self.o2_check, self.problem_list,
                       self.copy_out_check, self.lock_after_submit_check,
                       self.start_delay_spin, self.entry_mode_combo,
                       *self.language_checks.values()):
            widget.setEnabled(not running)
        # 名单按钮跟着"房间开没开"走，但**不**跟着进场方式的置灰走 ——
        # 开房之后仍允许改名单是错的（主机已经按这份名单在认人），所以
        # 这里一并冻结；改名单请关房之后、下一场开房之前。
        self.roster_button.setEnabled(False)
        self._on_entry_mode_changed()
        if self._server is None:
            self.roster_button.setEnabled(self._entry_mode is EntryMode.ACCOUNT)
        # 名称是**唯一开房期间还能改**的设置：它只是显示标签，改它不踢人
        # （房间号与密钥都与 title 无关）。所以上面那份"开房即冻结"的名单里
        # 刻意不含 self.title_edit；改名按钮反过来——只在开房后才亮。
        self.title_edit.setEnabled(True)
        self.rename_button.setEnabled(running)
        # 两个房间开关各管自己那一页的文案与主次（一页上只会出现一个）。
        self.open_button.setText("开启房间")
        self.open_button.setProperty("variant", "primary")
        self.close_button.setProperty("variant", "danger" if running else "")
        # 「开始考试」还要再细一层：只有"备考中"才点得动，那由
        # _refresh_host_status 依据会话状态决定，这里先跟随房间开没开。
        self.start_button.setEnabled(running)
        self.collect_button.setEnabled(running)
        self.end_button.setEnabled(running)
        self.copy_button.setEnabled(running)
        # 名单页的锁屏按钮跟着房间开关走（明细的"选中且未锁"再由
        # _on_roster_selection_changed 细化）。
        self._on_roster_selection_changed()
        if running:
            self.role_hint.setText(
                "房间已开启 · 把大字的房间号报给学生"
                if self._entry_mode is EntryMode.ROOM_CODE else
                "房间已开启 · 把「地址、端口与每人一条的账号口令」发给学生")
        else:
            self.role_hint.setText("学生端只需要「地址 + 端口 + 房间号」")

    def _clear_host_views(self) -> None:
        self.host_room_label.setText("房间未开启")
        self.host_status_label.setText("设置好之后点「开启房间」，把房间号报给学生。")
        self.host_problem_combo.clear()
        for table in (self.host_overall_table, self.host_problem_table,
                      self.host_roster_table, self.host_code_table):
            table.setRowCount(0)
        # 关房间之后那些代码就没有归属了（下一场是另一批学生），所以连缓存
        # 一起丢掉 —— 留着只会让"上一场的某次提交"还能被翻出来。
        self._submission_by_serial.clear()
        self._submission_signature = None
        self._code_serial = None
        self._code_pinned = False
        # 回看档案的状态一起丢掉：下一场是另一批学生，留着只会让上一份档案
        # 还能从「提交与代码」里被翻出来当成刚交的卷子。
        self._archive = None
        self._archive_submissions = []
        self._sync_code_scope()
        self.host_code_caption.setText("")
        self.copy_code_button.setEnabled(False)
        # 关房之后这一页就没有可比的东西了（下一场是另一批学生）。
        self.similarity_button.setEnabled(False)
        # 回到「源码」那一页签：停在「判定说明」上，下一场切进来会先看到
        # 一块"还没有判定结果"的空白。
        self.host_code_tabs.setCurrentWidget(self.host_code_view)
        self.host_code_view.clear()
        self.host_verdict_view.clear_output()

    # ---- 主机动作 ---------------------------------------------------------

    def start_exam_now(self) -> None:
        """提前开考：把开考时刻挪到现在，并通知全体学生。

        刻意不加确认框：这是现场的高频动作（学生都到齐了就提前开），而且
        **误触也没有损失** —— 按钮只在"还没开考"时可点，开考本身又是可预期的。
        """
        server = self._server
        if server is None:
            return
        if not server.start_exam():
            self.status("本场已经开考了。")
            return
        self._log(self.host_log, "已提前开考，学生现在可以提交了",
                  self.palette.success)
        self.status("已开考，学生现在可以提交了")
        self._refresh_host_views()

    def collect_now(self) -> None:
        """提前收卷：不等时间到就让全体交卷。"""
        if self._server is None or self._session is None:
            return
        answer = QMessageBox.question(
            self, "提前收卷",
            "现在要求所有在线学生立刻提交当前代码？\n"
            "开了强制收卷的话，学生端会自动交卷并锁定编辑器。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._server.collect_from_all("host")
        self._log(self.host_log, "已发出提前收卷指令", self.palette.warning)

    def end_quiz_now(self) -> None:
        """提前结束整场测验：把截止时刻改到现在。"""
        session = self._session
        if self._server is None or session is None:
            return
        answer = QMessageBox.question(
            self, "提前结束测验",
            "把本场测验的截止时刻改到现在？\n\n"
            "之后：收取全部提交 → 停止接受新提交 → "
            "收卷宽限（30 秒）走完后统一放榜。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        session.ends_at = datetime.now()
        self._server.collect_from_all("host")
        self._server.broadcast_status()
        if session.leaderboard_visible():
            self._server.broadcast_leaderboard()
        self._log(self.host_log, "测验已提前结束", self.palette.warning)
        self._refresh_host_views()

    def rename_room(self, wanted: str | None = None) -> None:
        """开房期间改名字：只换显示标签，房间号与连接都不动。

        刻意不加"改名前确认"框：这个动作可逆（再改一次就回去了），
        而且点「改名」本身就是明确意图，再拦一道只是拖慢现场操作。

        ``wanted`` 为空时读准备视图里的名称输入框 —— 那是开房前改名的路径。
        监考视图里没有输入框（开房后整页换成了监考视图），所以那边的按钮
        传自己的文本进来：同一件事在两张脸上入口不同，落点只有一个。
        """
        session, server = self._session, self._server
        if session is None or server is None:
            return
        wanted = (self.title_edit.text() if wanted is None else wanted).strip()
        if not wanted:
            self.notify.emit("warning", "测验名称不能为空。")
            return
        if wanted == session.title:
            self.status("名称没有变化。")
            return
        applied = server.rename(wanted)
        self._log(self.host_log, f"测验已改名为「{applied}」", self.palette.info)
        self.status(f"测验已改名为「{applied}」")

    def copy_room_info(self) -> None:
        """把"进房间需要知道的一切"拼成一段可以粘到班群里的文字。"""
        session, server = self._session, self._server
        if session is None or server is None:
            return
        addresses = server.lan_addresses()
        lines = [
            f"{session.title}（{session.mode.label}）",
            f"房间号：{session.room_code}",
            f"端口：{server.port}",
            f"时长：{'不限时' if not session.timed else f'{session.duration_minutes} 分钟'}",
        ]
        if session.password:
            lines.append(f"房间口令：{session.password}")
        if addresses:
            lines.append("主机地址（任选其一）：" + "、".join(addresses))
        else:
            lines.append("主机地址：请在本机执行 ipconfig 查看局域网 IPv4 地址")
        lines.append("")
        lines.append("在学生端点「学生端 · 加入房间」，填入上面的地址、端口、"
                     "房间号，写上自己的名字即可。")
        self._copy_text("\n".join(lines), "房间信息")

    # ---- 主机视图刷新 -----------------------------------------------------

    def _refresh_host_views(self) -> None:
        session, server = self._session, self._server
        if session is None or server is None:
            return
        addresses = server.lan_addresses()
        where = addresses[0] if addresses else "127.0.0.1"
        room_text = f"房间号 {session.room_code}    {where}:{server.port}"
        self.host_room_label.setText(room_text)
        # 监考视图的大字与右列顶上那份是同一句话的两个落点：一个在老师的
        # "操作台"上、一个在屏幕另一头。两边都从这里写，就不会出现一处刷新
        # 另一处还停在上一个房间的情况。
        self.side_room_label.setText(room_text)
        self._refresh_host_summary()
        # 字号粗细由 role="display" 决定，这里不再重复描一遍内联样式
        self._refresh_host_status()
        self._refresh_host_boards()
        self._sync_problem_combo()

    def _refresh_host_summary(self) -> None:
        """把"开房后改不了的设置"以**事实**形式写进监考视图的摘要。

        开房之后那张准备视图整页收起了，老师想问的"考多久""考哪几题"
        "限了什么语言"就只剩这里能看。所以写的是当前生效值（含服务端
        实际定下来的那份），不是输入框里可能被改过的草稿。
        """
        session = self._session
        if session is None:
            return
        labels = self.side_summary_labels
        labels["mode"].setText(f"{session.mode.label}"
                               + ("（不公开榜单）"
                                  if not session.show_leaderboard else ""))
        labels["entry"].setText(
            "房间号（学生自填姓名）"
            if session.entry_mode is EntryMode.ROOM_CODE
            else f"账号进场 · 名单 {len(session.contestants)} 人")
        labels["duration"].setText(
            "不限时" if not session.timed
            else f"{session.duration_minutes} 分钟"
                 + ("（到点强制收卷）" if session.force_collect else ""))
        count = len(session.problems)
        labels["problems"].setText(f"{count} 题" if count else "未选题")
        labels["limits"].setText(self._policy_text())

    def _policy_text(self) -> str:
        """本场限制的一句话描述。由 ``_collect_policy`` 的实际取值反推，
        不另存一份 —— 免得摘要和真正生效的策略不一致。"""
        policy = self._collect_policy()
        if policy is None:
            return BLANK
        parts = [language.display for language in SUBMIT_LANGUAGES
                 if policy.allows_language(language)]
        text = "语言 " + ("、".join(parts) if parts else "不限")
        if not policy.allow_copy_out:
            text += " · 禁止另存为"
        if policy.lock_after_submit:
            text += " · 判定后锁编辑器"
        return text

    def _refresh_host_status(self) -> None:
        session, server = self._session, self._server
        if session is None or server is None:
            return
        online = len(server.connected_devices())
        total = len(session.participants)
        published = session.leaderboard_visible()
        moment_state = session.state()
        clock = _clock_text(timed=session.timed,
                            remaining=session.remaining_seconds(),
                            state=moment_state.value)
        board = _board_text(published, session.show_leaderboard)
        # 「开始考试」只在备考中有意义：已经开考就点不动
        self.start_button.setEnabled(moment_state is ExamState.PENDING)
        self.host_status_label.setText(
            f"{session.mode.label} · {moment_state.label} · {clock} · "
            f"在线 {online}/{total} 人 · "
            f"提交 {len(session.submissions)} 份 · 已判 {server.judged_count} 份 · "
            f"{board}"
            + ("  ·  到点强制收卷" if session.force_collect else "")
            + ("" if session.allow_resubmit else "  ·  不允许重复提交"))

    def _sync_problem_combo(self) -> None:
        """单题榜的题目下拉框跟着本场题目走，尽量保住当前选择。"""
        session = self._session
        if session is None:
            return
        wanted = [view.id for view in session.problems]
        current = [self.host_problem_combo.itemData(index)
                   for index in range(self.host_problem_combo.count())]
        if current == wanted:
            return
        keep = self.host_problem_combo.currentData()
        self.host_problem_combo.blockSignals(True)
        self.host_problem_combo.clear()
        for view in session.problems:
            self.host_problem_combo.addItem(
                f"{view.id}  {view.title or view.slug}", view.id)
        index = self.host_problem_combo.findData(keep)
        self.host_problem_combo.setCurrentIndex(max(0, index))
        self.host_problem_combo.blockSignals(False)

    def _refresh_host_boards(self) -> None:
        session = self._session
        if session is None:
            return
        self._fill_table(
            self.host_overall_table,
            [self._overall_row(row.to_dict(), session.max_total_score)
             for row in session.overall_ranking()],
            right_aligned=(0, 3, 4, 5, 6, 7))
        problem_id = str(self.host_problem_combo.currentData() or "")
        self._fill_table(
            self.host_problem_table,
            [self._problem_row(row.to_dict())
             for row in (session.problem_ranking(problem_id) if problem_id else [])],
            right_aligned=(0, 3, 4, 6, 7))
        self._fill_roster()
        self._fill_submissions()

    def _fill_roster(self) -> None:
        session = self._session
        if session is None:
            return
        rows = []
        keys = []
        for participant in session.participants.values():
            account = session.account_of(participant.device_id)
            rows.append((
                participant.device_id,
                participant.username,
                account or BLANK,
                "在线" if participant.connected else "离线",
                "离开中" if participant.locked else BLANK,
                participant.address or BLANK,
                _fmt_clock(participant.joined_at.isoformat(timespec="seconds")),
                str(len(session.submissions_of(participant.device_id))),
            ))
            keys.append(participant.device_id)
        self._fill_table(self.host_roster_table, rows, right_aligned=(6, 7),
                         row_keys=keys)
        self._on_roster_selection_changed()

    def _selected_roster_device(self) -> str | None:
        """「名单」页当前选中行对应的那台机器。**跟键走，不跟行号。**"""
        item = self.host_roster_table.item(self.host_roster_table.currentRow(), 0)
        if item is None:
            return None
        key = item.data(Qt.UserRole)
        return str(key) if key else None

    def _on_roster_selection_changed(self) -> None:
        device = self._selected_roster_device()
        has_row = device is not None
        locked = False
        if has_row and self._session is not None:
            participant = self._session.participant(device)
            locked = bool(participant is not None and participant.locked)
        running = self._server is not None
        # 两个互斥按钮**都**亮着，只是当前状态对应的那一个置灰 ——
        # 按状态切换会让老师连点两下之间找不到按钮在哪。
        self.lock_one_button.setEnabled(running and has_row and not locked)
        self.unlock_one_button.setEnabled(running and has_row and locked)
        self.lock_all_button.setEnabled(running)
        self.unlock_all_button.setEnabled(running)
        count = len(self._session.locked_devices()) if self._session else 0
        self.roster_hint.setText(
            f"{count} 人离开中" if count else "")

    def set_selected_lock(self, locked: bool) -> None:
        server = self._server
        if server is None:
            return
        device = self._selected_roster_device()
        if not device:
            return
        server.set_locked(device, locked)

    def set_all_lock(self, locked: bool) -> None:
        if self._server is not None:
            self._server.lock_all(locked)

    def _fill_submissions(self, *, force: bool = False) -> None:
        """刷新「提交与代码」页的提交列表（最新的在最上面）。

        这个列表每几秒就会因为别人的提交或判定结果而变，而重填表格
        （``setRowCount`` + 逐格 ``setItem``）会把选中清掉 —— 老师正盯着一份
        代码，页面自己跳回空白，那这一页在活跃的考场里就没法用。所以填之前
        先记住正在看的是**哪一份提交**（编号，不是行号：新提交插到最前面会把
        行号整体推下去），填完再选回来。

        内容没变时连重填都不做：省的是小头，主要是别让列表的滚动位置跟着
        别人的提交抖。

        :param force: 内容没变也重填一遍。切进这一页时用得着 —— 那时要重新
            挑一份来看，而"挑哪一份"是填表的一部分，跳过填表就不会发生。
        """
        if self._session is None and self._archive is None:
            return
        items = sorted(self._code_items(), key=lambda one: one.serial, reverse=True)
        # 有没有可比的提交，只有这一个真源 —— 按钮的可用性跟着它走，
        # 免得在"开房 / 关房 / 打开档案 / 关闭档案"四处各写一遍同步。
        self.similarity_button.setEnabled(bool(items))
        # 判定结果落下来时 verdict 和得分会变，所以它们要进签名 ——
        # 只看提交条数的话，"这人过了"这件事永远不会反映到列表上。
        signature = [(item.serial, item.verdict, item.score) for item in items]
        if signature == self._submission_signature and not force:
            return
        self._submission_signature = signature
        self._submission_by_serial = {item.serial: item for item in items}

        # 还没有要看的对象时，停在最新的一份上：点进来就有东西看，
        # 而不是一块"请先选择"的空白。
        keep = self._code_serial
        if keep is None and items:
            keep = items[0].serial

        table = self.host_code_table
        # 逐格 setItem 的过程中 Qt 会发好几次 itemSelectionChanged，
        # 那时候行还是半填的。填完再一次性响应。
        table.blockSignals(True)
        try:
            self._fill_table(
                table,
                [self._submission_row(item) for item in items],
                right_aligned=(0, 5, 7, 9),
                row_keys=[item.serial for item in items])
            self._select_serial(keep)
        finally:
            table.blockSignals(False)
        self._show_selected_code()

    def _code_items(self) -> list[Submission]:
        """「提交与代码」页当前的数据源。

        **回看档案时是档案里那一份**，否则是进行中的这一场。两条路喂的是同一种
        对象，所以下面找编号、摆源码、写判定说明那一整套逻辑一行都不用分叉。
        """
        if self._archive is not None:
            return self._archive_submissions
        session = self._session
        return list(session.submissions) if session is not None else []

    # ---- 雷同检测 ---------------------------------------------------------

    def run_similarity_check(self) -> None:
        """对「提交与代码」页当前的数据源跑一次雷同检测。

        数据源由 :meth:`_code_items` 决定：进行中的这一场，或者打开的档案 ——
        所以"刚考完想马上查一下"和"翻出去年那场看看"走的是同一条路。
        """
        rows = self._similarity_rows()
        if not rows:
            self.notify.emit("warning", "还没有可比的提交。")
            return
        report = sim.analyse(rows, problem_titles=self._problem_titles())
        if report.is_empty and not report.analysed:
            self.notify.emit("warning", "这些提交里没有源码，无法比对。")
            return
        self.status(f"雷同检测完成：比对 {len(report.analysed)} 份提交")
        dialog = self._similarity_dialog(report)
        dialog.exec()

    def _similarity_rows(self) -> list[dict]:
        """当前数据源 → :func:`offline_oj.core.similarity.analyse` 认识的 dict。

        转换放在界面这一侧做，因为 ``core/`` 不 import ``net/``（分层约定）——
        与导出走 ``ExportData`` 是同一个套路。
        """
        return [
            {
                "serial": item.serial,
                "device_id": item.device_id,
                "username": item.username,
                "problem_id": item.problem_id,
                "language": item.language,
                "code": item.code,
                "score": item.score,
                "verdict": item.verdict,
                "attempt": item.attempt,
            }
            for item in self._code_items()
        ]

    def _problem_titles(self) -> dict[str, str]:
        """题目编号 → 标题。两条数据源各取各的，取不到就退回编号本身
        （报告里显示 ``P0001`` 也比显示空白强）。"""
        titles: dict[str, str] = {}
        archive = self._archive
        if archive is not None:
            for problem in getattr(archive.record, "problems", ()) or ():
                if isinstance(problem, dict) and problem.get("id"):
                    titles[str(problem["id"])] = str(problem.get("title")
                                                        or problem["id"])
            return titles
        session = self._session
        if session is not None:
            for view in session.problems:
                titles[view.id] = view.title or view.id
        return titles

    def _similarity_dialog(self, report) -> SimilarityDialog:
        """单独一个方法是为了让测试能替掉它 —— 离屏环境里 ``QDialog.exec()``
        会永久阻塞（跟 :meth:`_export_dialog` / :meth:`_confirm_delete` 同一个理由）。
        """
        return SimilarityDialog(report, palette=self.palette, parent=self)

    def _select_serial(self, serial: int | None) -> None:
        """把选中行指回某个提交编号；找不到（或给的就是 ``None``）就清空选中。"""
        table = self.host_code_table
        if serial is not None:
            for row in range(table.rowCount()):
                cell = table.item(row, 0)
                if cell is not None and cell.data(Qt.UserRole) == serial:
                    table.setCurrentCell(row, 0)
                    return
        table.setCurrentItem(None)

    def _selected_serial(self) -> int | None:
        """当前选中的是哪一份提交。

        读的是挂在第 0 列上的键，不是 ``currentRow()`` —— 行号会随着新提交
        插到最前面而整体平移，键不会。
        """
        table = self.host_code_table
        model = table.selectionModel()
        if model is None:
            return None
        rows = model.selectedRows()
        if not rows:
            return None
        cell = table.item(rows[0].row(), 0)
        if cell is None:
            return None
        value = cell.data(Qt.UserRole)
        return int(value) if value is not None else None

    def _on_host_tab_changed(self, index: int) -> None:
        """切到「提交与代码」页时，替老师挑一份来看。

        列表是按到达顺序攒起来的，而"第一条提交"在它到达的那一刻当然就是
        最新的 —— 于是 ``_code_serial`` 会在测验刚开始就被钉在第 1 号上。
        老师十分钟后才切进这一页，看到的却会是**最早**那份。所以"先看哪一份"
        要推迟到老师真的看向这一页的那一刻再决定。

        老师自己点过某一行之后就不再自动改选（``_code_pinned``）：
        "我要看这一份"已经表达过了，再改就是抢。
        """
        if index == HOST_TAB_ARCHIVE:
            # 每次切进来都重扫：档案目录是可以从资源管理器里被改动的
            # （拷进来一场别人的、手工删一份），只在构造时扫一次就会过期。
            self.refresh_archives()
            return
        if index != HOST_TAB_CODE or self._code_pinned:
            return
        self._code_serial = None
        self._fill_submissions(force=True)

    def _on_submission_selected(self) -> None:
        """提交列表的选中变了。

        能走到这里就说明是**人**在选：程序重填表格时是 ``blockSignals`` 的，
        填完那一次由 ``_fill_submissions`` 自己调 ``_show_selected_code``。
        """
        self._code_pinned = True
        self._show_selected_code()

    def _show_selected_code(self) -> None:
        """把选中的那一份的源码和判定说明摆出来。

        同一份不重复灌：列表每次刷新都重灌文本的话，``setPlainText`` 会把
        滚动位置和光标一起打回开头 —— 老师正看到第 80 行，视图自己跳回第 1 行。
        提交之后的源码不会再变，所以"编号没变"就等于"该显示的内容没变"。
        """
        serial = self._selected_serial()
        if serial == self._code_serial:
            return
        self._code_serial = serial

        item = self._submission_by_serial.get(serial) if serial is not None else None
        if item is None:
            self.host_code_caption.setText("")
            self.copy_code_button.setEnabled(False)
            self.host_code_view.clear()
            self.host_verdict_view.clear_output()
            return

        language = Language.from_value(item.language)
        caption = (f"{item.username or item.device_id} · {item.problem_id} · "
                   f"第 {item.attempt} 次 · {language.short if language else item.language}")
        # 编译参数直接写在标题行上：老师看一份 TLE 的代码时，"当时开没开优化"
        # 决定了这段代码值不值得继续读下去。
        optimize = _fmt_optimize(item)
        if optimize != BLANK:
            caption += f" · {optimize}"
        self.host_code_caption.setText(caption)

        if language is not None:
            # 学生提交的语言就是这一份的语言：换了语言但没清掉上一份的高亮，
            # 关键字会按错误的语言着色。
            self.host_code_view.set_language(language)
        self.host_code_view.setPlainText(item.code)

        color = self.palette.verdict_color(str(item.verdict or ""))
        self.host_verdict_view.clear_output()
        self.host_verdict_view.append_block(
            item.message or "（还没有判定结果）", color)
        if not item.code:
            # 空编辑器看起来像"还没加载完"。说清楚是这份档案本来就没存源码，
            # 否则老师会一直等它出来。
            self.host_verdict_view.append_block(
                "这份档案没有保存源码（存档时选了「只存成绩」）。",
                self.palette.text_muted)
        self.copy_code_button.setEnabled(bool(item.code))

    def copy_selected_code(self) -> None:
        """复制当前查看的这份源码。老师讲评时要把它粘到自己的稿子里。"""
        item = self._submission_by_serial.get(self._code_serial)
        if item is None:
            self.notify.emit("warning", "上面还没有选中的提交。")
            return
        who = item.username or item.device_id
        self._copy_text(item.code, f"{who} 的代码")

    # ==================================================================
    # 历史场次（测验档案）
    # ==================================================================

    def refresh_archives(self) -> None:
        """重扫档案目录并填表。关房存档后、切进这一页时都会调。"""
        self._archives = records.list_archives(self.ctx.paths.exams_dir)
        rows: list[tuple[str, ...]] = []
        for item in self._archives:
            record = item.record
            rows.append((
                _fmt_stamp(record.started_at),
                item.label,
                str(len(record.problems)),
                str(len(record.participants)),
                str(record.submission_count),
                "含源码" if item.has_code else "仅成绩",
                item.broken or "",
            ))
        self._fill_table(self.archive_table, rows, right_aligned=(2, 3, 4),
                         row_keys=[str(item.directory) for item in self._archives])
        self._sync_archive_hint()
        self._on_archive_selected()

    def _sync_archive_hint(self) -> None:
        if not self._archives:
            self.archive_hint.setText(
                "还没有档案。开启房间后正常关房，这一场就会自动存到这里。")
            return
        broken = sum(1 for item in self._archives if item.broken)
        text = f"{len(self._archives)} 份档案"
        if broken:
            text += f"（其中 {broken} 份读不出来）"
        self.archive_hint.setText(f"{text} · {records.SOURCE_NOTICE}")

    def _selected_archive(self) -> records.ArchiveSummary | None:
        """选中行对应的那份档案摘要；没选中就返回 ``None``。

        认的是挂在第 0 列上的**目录路径**，不是行号 —— 刷新会整体重填，
        行号随时可能变。
        """
        table = self.archive_table
        model = table.selectionModel()
        if model is None:
            return None
        rows = model.selectedRows()
        if not rows:
            return None
        cell = table.item(rows[0].row(), 0)
        if cell is None:
            return None
        key = cell.data(Qt.UserRole)
        return next((item for item in self._archives
                     if str(item.directory) == str(key)), None)

    def _on_archive_selected(self) -> None:
        has_one = self._selected_archive() is not None
        self.archive_open_button.setEnabled(has_one)
        self.archive_delete_button.setEnabled(has_one)
        self.archive_export_button.setEnabled(has_one)

    def open_selected_archive(self) -> None:
        """把选中的档案读进内存，并切到「提交与代码」页回看。"""
        summary = self._selected_archive()
        if summary is None:
            self.notify.emit("warning", "请先在列表里选一份档案。")
            return
        try:
            archive = records.read_archive(summary.directory)
        except records.ArchiveError as exc:
            self.notify.emit("error", str(exc))
            return
        self._archive = archive
        self._archive_submissions = [
            Submission.from_dict(item) for item in archive.submissions]
        self._code_serial = None
        self._code_pinned = False
        self._submission_signature = None
        self._submission_by_serial.clear()
        self.host_tabs.setCurrentIndex(HOST_TAB_CODE)
        self._fill_submissions(force=True)
        self._sync_code_scope()
        note = "" if archive.integrity_ok else "（校验和对不上，内容可能不完整）"
        self.status(f"正在回看 {archive.record.title or '未命名场次'}{note}")

    def close_archive(self) -> None:
        """退出回看模式，数据源换回"进行中的这一场"。"""
        if self._archive is None:
            return
        self._archive = None
        self._archive_submissions = []
        self._submission_signature = None
        self._code_serial = None
        self._code_pinned = False
        self._submission_by_serial.clear()
        self._fill_submissions(force=True)
        self._sync_code_scope()

    def reveal_archives_dir(self) -> None:
        directory = self.ctx.paths.exams_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.notify.emit("error", f"打不开档案目录：{exc}")
            return
        # 打开资源管理器失败不算错误（可能被组策略挡了）：把路径说出来，
        # 老师自己粘到地址栏也一样。所以这里不检查返回值。
        win_process.reveal_in_explorer(directory)
        self.status(f"档案目录：{directory}")

    def delete_selected_archive(self) -> None:
        """删掉选中的档案。删之前必须问一次，且要说清删的是什么。"""
        summary = self._selected_archive()
        if summary is None:
            self.notify.emit("warning", "请先在列表里选一份档案。")
            return
        what = summary.record.title or "未命名场次"
        when = _fmt_stamp(summary.record.started_at)
        if not self._confirm_delete(f"{when} · {what}",
                                   summary.record.submission_count):
            return
        if self._archive is not None \
                and self._archive.directory == summary.directory:
            # 正在回看的正是它：先退出回看再删，否则页面上留着一份已消失的数据
            self.close_archive()
        if records.delete_archive(summary.directory):
            self.status(f"档案已删除：{what}")
            self.refresh_archives()
        else:
            self.notify.emit("error", "删除失败 —— 这个目录不像是一份档案，已跳过。")

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export_selected_archive(self) -> None:
        """把选中的档案导成表格 / 文档 / 完整档案包。

        导出的是**档案**而不是"进行中的这一场"：关房会自动留档（见
        ``_archive_session``），所以"刚考完想马上要成绩单"这条路是通的，
        而按档案导可以让"导出的东西 = 存档的东西"，不会出现两份对不上的数据。
        """
        summary = self._selected_archive()
        if summary is None:
            self.notify.emit("warning", "请先在列表里选一份档案。")
            return
        try:
            archive = records.read_archive(summary.directory)
        except records.ArchiveError as exc:
            self.notify.emit("error", str(exc))
            return

        data = table_export.ExportData.from_archive(archive)
        mode = RoomMode.from_value(data.mode)
        if mode is not None:
            data.mode_label = mode.label

        dialog = self._export_dialog(data)
        if dialog.exec() != QDialog.Accepted:
            return
        fmt = dialog.selected_format()
        bundle = fmt == "bundle"
        sections = (list(table_export.ALL_SECTIONS) if bundle
                    else dialog.selected_sections())
        self._start_export(data, fmt, sections, dialog.target(), bundle=bundle)

    def _export_dialog(self, data) -> ExportDialog:
        """单独一个方法是为了让测试能替掉它 —— 离屏环境里没人点得到模态框，
        而 ``QDialog.exec()`` 会永久阻塞。跟 :meth:`_confirm_delete` 同一个理由。
        """
        return ExportDialog(data, default_directory=self._export_directory(),
                            parent=self)

    @staticmethod
    def _export_directory() -> str:
        """默认导出到"文档"。取不到就退回用户主目录，再不行就当前目录。"""
        for kind in (QStandardPaths.DocumentsLocation, QStandardPaths.HomeLocation):
            found = QStandardPaths.writableLocation(kind)
            if found:
                return found
        return str(Path.cwd())

    def _start_export(self, data, fmt: str, sections, target, *,
                      bundle: bool) -> None:
        worker = TableExportWorker(data, target, fmt, sections, bundle=bundle,
                                   parent=self)
        worker.done.connect(self._on_export_done)
        worker.needs_pdf.connect(self._render_export_pdf)
        worker.crashed.connect(self._on_export_failed)
        worker.finished.connect(worker.deleteLater)
        # 必须自己拿着引用：QThread 被 GC 掉而线程还在跑，进程会直接崩
        self._export_worker = worker
        self._export_target = Path(target)
        self.status("正在导出…")
        worker.start()

    def _render_export_pdf(self, html: str) -> None:
        """PDF 在界面线程里渲染（理由见 ``TableExportWorker`` 的说明）。"""
        target = self._export_target
        if target is None:
            return
        try:
            from ..export_pdf import render_pdf

            render_pdf(html, target, title=self._export_title())
        except Exception as exc:                      # noqa: BLE001 - 见下
            # 渲染失败要报出来，不能静默 —— 老师会以为文件已经导好了
            log.exception("渲染 PDF 失败")
            self._on_export_failed(str(exc))
            return
        self._on_export_done([str(target)])

    def _export_title(self) -> str:
        summary = self._selected_archive()
        return summary.record.title if summary is not None else ""

    def _on_export_done(self, files) -> None:
        self._export_target = None
        names = "、".join(Path(item).name for item in files)
        self._log(self.host_log, f"已导出：{names}", self.palette.success)
        self.status(f"已导出 {len(files)} 个文件：{names}")

    def _on_export_failed(self, message: str) -> None:
        self._export_target = None
        self._log(self.host_log, f"导出失败：{message}", self.palette.danger)
        self.notify.emit("error", f"导出失败：{message}")

    def _confirm_delete(self, label: str, submissions: int) -> bool:
        """删除前确认。

        单独一个方法是为了让测试能替掉它 —— 离屏环境里没人点得到模态框，
        而 ``QMessageBox.exec()`` 会永久阻塞（这个项目的沙箱里挂死过好几个小时）。
        """
        answer = QMessageBox.question(
            self, "删除档案",
            f"确定要删除这份档案吗？\n\n{label}\n共 {submissions} 份提交。\n\n"
            "档案连同其中保存的源码会被永久删除，不能撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _sync_code_scope(self) -> None:
        """「提交与代码」页顶上的那一行：现在看的是哪一场。

        回看档案时不说清楚，老师会把两年前的提交当成刚交上来的 —— 那一页
        长得一模一样。
        """
        archive = self._archive
        if archive is None:
            self.code_scope_label.setText("")
            self.code_scope_label.setVisible(False)
            return
        record = archive.record
        mode = RoomMode.from_value(record.mode)
        parts = [f"档案 · {_fmt_stamp(record.started_at)}",
                 record.title or "未命名场次",
                 mode.label if mode else record.mode,
                 f"{len(record.participants)} 人 / {record.submission_count} 份提交"]
        if not archive.has_code:
            parts.append("未保存源码")
        if not archive.integrity_ok:
            parts.append("校验和不一致")
        self.code_scope_label.setText(" · ".join(parts))
        self.code_scope_label.setVisible(True)

    @staticmethod
    def _overall_row(row: dict, max_total: int = 0) -> tuple[str, ...]:
        """总分榜的一行。

        ``max_total`` 是本场满分（各行相同），写成 ``240/300`` 才读得出离满分
        还有多远 —— 满分现在由每题测试点分值决定，不再恒定 100。
        """
        return (
            str(row.get("rank", "")),
            str(row.get("username") or BLANK),
            str(row.get("device_id") or BLANK),
            _fmt_score(row.get("score"), max_total),
            str(row.get("solved", 0)),
            str(row.get("submit_count", 0)),
            _fmt_ms(row.get("total_time_ms")),
            _fmt_mb(row.get("total_memory_mb")),
            _fmt_clock(row.get("last_submit_at")),
        )

    @staticmethod
    def _problem_row(row: dict) -> tuple[str, ...]:
        return (
            str(row.get("rank", "")),
            str(row.get("username") or BLANK),
            str(row.get("device_id") or BLANK),
            _fmt_score(row.get("score"), row.get("possible")),
            f"{row.get('passed', 0)}/{row.get('total', 0)}",
            _fmt_verdict(row.get("verdict")),
            f"第 {row.get('attempt', 1)} 次",
            _fmt_ms(row.get("time_ms")),
            _fmt_mb(row.get("memory_mb")),
            _fmt_clock(row.get("submitted_at")),
        )

    @staticmethod
    def _submission_row(item: Submission) -> tuple[str, ...]:
        language = Language.from_value(item.language)
        return (
            str(item.serial),
            _fmt_clock(item.submitted_at.isoformat(timespec="seconds")),
            item.username or BLANK,
            item.device_id or BLANK,
            item.problem_id,
            f"第 {item.attempt} 次",
            language.short if language else item.language,
            _fmt_optimize(item),
            _fmt_verdict(item.verdict),
            _fmt_score(item.score, item.possible),
        )

    # ---- 主机事件 ---------------------------------------------------------

    def _on_host_event(self, event: str, payload: object) -> None:
        """主机事件（已在界面线程执行）。"""
        data = payload if isinstance(payload, dict) else {}
        if event == "joined":
            self._log(self.host_log,
                      f"{data.get('username')} 进场 · {data.get('device_id')} "
                      f"· {data.get('address')}", self.palette.success)
        elif event == "left":
            self._log(self.host_log, f"{data.get('device_id')} 离开",
                      self.palette.text_muted)
        elif event == "replaced":
            # 同一设备 ID 又连上来了。多半是"网卡松动后重连"，但也可能是有人
            # 拿别人的设备 ID 在冒名 —— 值得让老师看见。
            self._log(self.host_log,
                      f"{data.get('username')}（{data.get('device_id')}）"
                      f"换了一条新连接，旧连接已断开",
                      self.palette.warning)
        elif event == "submitted":
            tag = "（自动收卷）" if data.get("forced") else ""
            self._log(self.host_log,
                      f"{data.get('username')} 提交 {data.get('problem_id')} "
                      f"第 {data.get('attempt')} 次{tag}",
                      self.palette.info)
        elif event == "judged":
            self._log(self.host_log,
                      f"{data.get('username')} {data.get('problem_id')} "
                      f"第 {data.get('attempt')} 次 → {data.get('verdict')} "
                      f"{_fmt_score(data.get('score'), data.get('possible'))} 分 "
                      f"{_fmt_ms(data.get('time_ms'))} "
                      f"{_fmt_mb(data.get('memory_mb'))}",
                      self.palette.verdict_color(str(data.get("verdict", ""))))
        elif event == "collect":
            reason = "到点收卷" if data.get("reason") == "deadline" else "老师提前收卷"
            self._log(self.host_log, f"已发出{reason}指令", self.palette.warning)
        elif event == "published":
            self._log(self.host_log, "测验结束，已统一放榜", self.palette.success)
        elif event == "closed":
            self._log(self.host_log, "已到截止时刻", self.palette.warning)
        elif event == "started":
            self._log(self.host_log, f"服务已监听端口 {data.get('port')}",
                      self.palette.success)
        elif event == "stopped":
            return
        elif event == "locked":
            if data.get("count") is not None:
                verb = "盖上" if data.get("locked") else "解开"
                self._log(self.host_log, f"已对全体{verb}屏幕（{data['count']} 人）",
                          self.palette.info)
            else:
                who = data.get("username") or data.get("device_id")
                verb = "盖上" if data.get("locked") else "解开"
                self._log(self.host_log, f"{who} 的屏幕已{verb}",
                          self.palette.info)
            self._on_roster_selection_changed()
            self._refresh_host_views()
        elif event == "unlock":
            if data.get("ok"):
                self._log(self.host_log,
                          f"{data.get('username')} 已用口令解开屏幕",
                          self.palette.info)
            else:
                self._log(self.host_log,
                          f"{data.get('username')} 尝试解锁未通过："
                          f"{data.get('message', '')}", self.palette.warning)
            self._on_roster_selection_changed()
        elif event == "rejected":
            # 握手被拒（账号口令不对、账号已绑在别的机器……）。有人连不上
            # 这件事老师必须看得见，否则现场会是"他举手说我进不去"。
            self._log(self.host_log,
                      f"有人进场被拒（{data.get('address', '')}）："
                      f"{data.get('reason', '')}", self.palette.warning)

        self._refresh_host_views()

    # ==================================================================
    # 学生端：进场
    # ==================================================================

    def toggle_join(self) -> None:
        if self._client is not None or self._worker is not None:
            self.leave_room()
            return
        self.join_room()

    def join_room(self) -> None:
        address = self.address_edit.text().strip()
        device_id = self.device_edit.text().strip()
        using_account = (EntryMode.from_value(self.join_mode_combo.currentData())
                         is EntryMode.ACCOUNT)

        if not address:
            self.notify.emit("warning", "请填写老师那台机器的地址。")
            return
        if using_account:
            code = ""
            username = ""
            account = normalize_account(self.student_account_edit.text())
            passcode = self.student_passcode_edit.text().strip()
            if not account:
                self.notify.emit(
                    "warning", "请填写老师名单上的账号。")
                return
        else:
            code = normalize_room_code(self.student_code_edit.text())
            account = ""
            passcode = ""
            username = self.student_name_edit.text().strip()
            if len(code) != ROOM_CODE_LENGTH or not code.isdigit():
                self.notify.emit(
                    "warning",
                    f"房间号是 {ROOM_CODE_LENGTH} 位数字，请向老师确认。")
                return
            if not username:
                self.notify.emit("warning", "请填写用户名，老师要靠它认人。")
                return
        if not device_id:
            self._load_identity()
            device_id = self.device_edit.text().strip()
        if not device_id:
            self.notify.emit("error", "本机设备 ID 生成失败，请检查数据目录是否可写。")
            return

        self.join_button.setEnabled(False)
        self.join_button.setText("连接中…")
        self.join_status.setText(f"正在连接 {address}:{self.port_edit.value()} …")

        worker = ConnectWorker(
            host=address,
            port=self.port_edit.value(),
            room_code=code,
            device_id=device_id,
            username=username,
            password=self.student_password_edit.text(),
            account=account,
            passcode=passcode,
            parent=self,
        )
        worker.message.connect(self._on_client_event)
        worker.connected.connect(self._on_client_connected)
        worker.failed.connect(self._on_client_failed)
        worker.disconnected.connect(self._on_client_disconnected)
        self._worker = worker
        worker.start()

    def _on_client_connected(self, client: object) -> None:
        self._worker = None
        self._client = client if isinstance(client, ExamClient) else None
        if self._client is None:
            return
        self._collect_seen = False
        self._editor_locked = False
        self.code_editor.setReadOnly(False)
        self._mine_rows.clear()
        self.mine_table.setRowCount(0)
        self._persist_identity()

        self.join_button.setEnabled(True)
        self.join_button.setText("离开房间")
        for widget in (self.address_edit, self.port_edit, self.student_code_edit,
                       self.student_password_edit, self.student_name_edit,
                       self.student_account_edit, self.student_passcode_edit,
                       self.join_mode_combo):
            widget.setEnabled(False)

        exam = self._client.exam
        who = (f"账号 {self._client.account} · 姓名 {self._client.username}"
               if self._client.using_account
               else f"用户名 {self._client.username}")
        self.join_status.setText(
            f"已加入「{self._client.welcome.get('title', '')}」 · "
            f"{exam.get('mode_label', '')} · {who} · "
            f"设备 ID {self._client.device_id}")
        self._log(self.student_log,
                  f"已加入房间，{who}，设备 ID {self._client.device_id}",
                  self.palette.success)
        self._sync_remaining(exam)
        self._refresh_student_problems()
        self._apply_student_state()
        self._apply_lock_state()

    def _on_client_failed(self, reason: str) -> None:
        self._worker = None
        self.join_button.setEnabled(True)
        self.join_button.setText("加入房间")
        self.join_status.setText("未加入。")
        self._log(self.student_log, f"加入失败：{reason}", self.palette.danger)
        self.notify.emit("error", reason)

    def _on_client_disconnected(self, reason: str) -> None:
        if self._client is None:
            return
        self._log(self.student_log, f"连接已断开：{reason}", self.palette.danger)
        self._teardown_client(keep_log=True)
        self.join_status.setText(f"连接已断开：{reason}")

    def leave_room(self) -> None:
        self._teardown_client()
        self.status("已离开房间")

    def _teardown_client(self, *, keep_log: bool = False) -> None:
        client, self._client = self._client, None
        worker, self._worker = self._worker, None
        if client is not None:
            client.close()
        if worker is not None:
            worker.wait(1000)
        self.join_button.setEnabled(True)
        self.join_button.setText("加入房间")
        for widget in (self.address_edit, self.port_edit, self.student_code_edit,
                       self.student_password_edit, self.student_name_edit,
                       self.student_account_edit, self.student_passcode_edit,
                       self.join_mode_combo):
            widget.setEnabled(True)
        self.student_problem_list.clear()
        self._apply_lock_state()
        self.statement_view.clear()
        self.student_board_table.setRowCount(0)
        self.submit_button.setEnabled(False)
        self.submit_button.setText("提交代码")
        self.submit_button.setToolTip("")
        self.student_clock_label.setVisible(False)
        self.code_editor.setReadOnly(False)
        # 策略与"打开/另存为"两个按钮都回到默认：上一场限制过的语言、锁过的
        # 编辑器，不该跟着学生进下一场房间。
        self._apply_policy(ExamPolicy())
        self.open_code_button.setEnabled(True)
        self._editor_locked = False
        self.student_status_label.setText("未加入房间")
        self.attempt_label.clear()
        if not keep_log:
            self.join_status.setText("未加入。请填入老师给的地址、端口与房间号。")

    # ---- 学生端事件 -------------------------------------------------------

    def _on_client_event(self, kind: str, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        if kind == MessageKind.PROBLEMS:
            self._refresh_student_problems()
        elif kind == MessageKind.EXAM:
            # client.exam 已经由 ExamClient._handle 更新过了，这里只负责刷界面。
            # **锁屏状态也跟着刷一次**：学生自己按「离开一下」时主机不下发
            # LOCK 帧，只广播状态快照（locked 就在 participants 里自己那一行），
            # ExamClient._handle 已经把它同步进 client.locked —— 界面要是不在
            # 这里跟着切，学生按下按钮自己的屏幕根本不会盖上（老师远程下发
            # 走 LOCK 帧，所以单元测试里发现不了这条缝）。
            self._sync_remaining(self._client.exam if self._client else {})
            self._apply_lock_state()
            self._apply_student_state()
        elif kind == MessageKind.START:
            # 开考帧与 EXAM 同构（不引入新载荷），所以这一支和上一支几乎一样，
            # 只多一句日志：到点开考是**主机告诉你的**，不用学生自己盯表。
            self._sync_remaining(self._client.exam if self._client else {})
            self._apply_lock_state()
            self._apply_student_state()
            self._log(self.student_log, "已开考，现在可以提交了",
                      self.palette.success)
        elif kind == MessageKind.LEADERBOARD:
            self._render_student_board(data)
        elif kind == MessageKind.VERDICT:
            self._on_verdict(data)
        elif kind == MessageKind.COLLECT:
            self._on_collect(data)
        elif kind == MessageKind.LOCK:
            # 主机下发的锁屏/解锁（也可能是老师对全班按的）。状态由
            # ExamClient._handle 维护好，这里只负责把界面跟着切过去。
            self._apply_lock_state()
            if self._client is not None:
                if self._client.locked:
                    self._log(self.student_log,
                              "屏幕已锁定（离开中）", self.palette.warning)
                else:
                    self._log(self.student_log,
                              "屏幕已解开", self.palette.success)
        elif kind == MessageKind.UNLOCKED:
            self._on_unlock_reply(data)
        elif kind == MessageKind.ERROR:
            self._on_server_error(data)
        elif kind == MessageKind.PONG:
            pass

    def _on_unlock_reply(self, data: dict) -> None:
        """解锁请求的回执。口令对不对**只有主机知道**。"""
        ok = bool(data.get("ok"))
        reason = str(data.get("message", "") or "")
        self.lock_submit_button.setEnabled(True)
        if ok:
            self._apply_lock_state()
            self._log(self.student_log, "已解开屏幕，继续答题",
                      self.palette.success)
            return
        self.lock_passcode_edit.clear()
        self.lock_passcode_edit.setFocus()
        self.lock_hint_label.setText(reason or "口令不对。")
        self._log(self.student_log,
                  f"解锁被拒绝：{reason or '口令不对'}", self.palette.danger)

    def _sync_remaining(self, exam: dict) -> None:
        """记下"同步那一刻主机说还剩多少秒"，倒计时由本机单调时钟往前推。"""
        value = exam.get("remaining_seconds")
        try:
            self._remaining_base = None if value is None else max(0, int(value))
        except (TypeError, ValueError):
            self._remaining_base = None
        self._remaining_at = monotonic()

    def _student_remaining(self) -> int | None:
        if self._remaining_base is None:
            return None
        return max(0, self._remaining_base - int(monotonic() - self._remaining_at))

    def _apply_student_state(self) -> None:
        """按主机下发的状态决定"还能不能交"。"""
        exam = self._client.exam if self._client else {}
        state = str(exam.get("state", "pending"))
        labels = {"pending": "未开考", "running": "进行中", "ended": "已结束"}
        # 备考阶段倒计时挪去了左边题目栏（那里也挂着"什么时候能动"），
        # 状态行里就不再写第二遍同一个数字。
        clock = ("" if state == ExamState.PENDING.value
                 else _clock_text(timed=bool(exam.get("timed", True)),
                                  remaining=self._student_remaining(),
                                  state=state))
        board = _board_text(bool(exam.get("leaderboard_published")),
                            bool(exam.get("show_leaderboard", True)))
        self.student_status_label.setText(
            " · ".join(part for part in (
                str(exam.get("title", "")), str(exam.get("mode_label", "")),
                labels.get(state, state), clock, board) if part))
        self._show_student_clock(state)

        self._apply_policy(self._current_policy())

        # 未开考时按钮直接说"等待开考"，比一个灰着的"提交代码"清楚：
        # 后者看着像坏了，前者说明是"还没到时候"。
        self.submit_button.setText(
            {ExamState.PENDING.value: "等待开考",
             ExamState.ENDED.value: "已结束"}.get(state, "提交代码"))

        # 只有"进行中"才允许提交。未开始就交、结束后还交，服务端都会拒，
        # 但在本地先拦住能少一次白跑的往返与一句莫名其妙的报错。
        allowed = (state == ExamState.RUNNING.value and not self._editor_locked
                   and not self._client.locked)
        self.submit_button.setEnabled(allowed and self._client is not None)
        # 锁定状态的按钮由 ``_lock_editor_after_submit`` / ``_lock_editor`` 写过
        # 更准确的提示语（"你已经交了"与"收卷了"不是一回事），别在这里覆盖掉。
        if not self._editor_locked:
            self.submit_button.setToolTip(
                "" if allowed else "当前不接受提交（未开始 / 已结束 / 已收卷）")
        # 「离开一下」只有在"有口令可解"时才该亮：盖上一块解不开的屏幕，
        # 学生只能干坐着等老师发现 —— 那不是保护，是事故。
        self.away_button.setEnabled(bool(self._client) and not self._lock_visible
                                    and self._unlock_possible())
        if self._client is not None:
            self._refresh_attempt_hint()

    def _show_student_clock(self, state: str) -> None:
        """备考阶段在题目栏上方挂开考倒计时，其余时候收起来。"""
        if state != ExamState.PENDING.value:
            self.student_clock_label.setVisible(False)
            return
        remaining = self._student_remaining()
        when = (f"{_fmt_countdown(remaining)} 后开考" if remaining is not None
                else "等待老师开考")
        self.student_clock_label.setText(
            f"{when} · 可以先读题、写代码，开考后才能提交")
        self.student_clock_label.setVisible(True)

    def _current_policy(self) -> ExamPolicy:
        """本场策略。没连主机、或主机不带这个字段时给默认（全开）。"""
        exam = self._client.exam if self._client else {}
        return ExamPolicy.from_dict(exam.get("policy"))

    def _apply_policy(self, policy: ExamPolicy) -> None:
        """把本场策略落到控件上。

        这些只是"别让人白点一次"：真正的边界在服务端（学生端可以被改、可以直接
        发包），所以语言限制在 ``server.py`` 的 ``_on_submit`` 里还有一次校验。
        """
        self._sync_language_choices(policy)
        self.save_code_button.setEnabled(policy.allow_copy_out)
        self.save_code_button.setToolTip(
            "把编辑器里的代码存成文件 (Ctrl+Shift+S)" if policy.allow_copy_out
            else "本场不允许把代码存成文件")

    def _sync_language_choices(self, policy: ExamPolicy) -> None:
        """语言下拉框只列本场允许的语言。

        **集合没变就不重建** —— 这个方法每收到一帧状态就会被调一次，无脑重建会把
        学生刚选的语言打回第一项。
        """
        allowed = policy.allowed_languages
        wanted = [language for language in SUBMIT_LANGUAGES
                  if not allowed or language.value in allowed]
        current = [self.language_combo.itemData(index)
                   for index in range(self.language_combo.count())]
        if current == [language.value for language in wanted]:
            return
        keep = self.language_combo.currentData()
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for language in wanted:
            self.language_combo.addItem(language.display, language.value)
        row = self.language_combo.findData(keep)
        self.language_combo.setCurrentIndex(row if row >= 0 else 0)
        self.language_combo.blockSignals(False)
        # 语言换了，编辑器的语法配色与补全词表要跟着换
        self._on_language_changed()

    def _refresh_student_problems(self) -> None:
        client = self._client
        if client is None:
            return
        views = client.problems
        wanted = [view.id for view in views]
        current = [self.student_problem_list.item(row).data(Qt.UserRole)
                   for row in range(self.student_problem_list.count())]
        if current == wanted:
            return
        keep = self._current_problem_id
        self.student_problem_list.blockSignals(True)
        self.student_problem_list.clear()
        for view in views:
            item = QListWidgetItem(f"{view.id}  {view.title or view.slug}")
            item.setData(Qt.UserRole, view.id)
            item.setToolTip(f"{view.time_limit} ms / {view.memory_limit} MB")
            self.student_problem_list.addItem(item)
        index = next((row for row in range(self.student_problem_list.count())
                      if self.student_problem_list.item(row).data(Qt.UserRole) == keep),
                     0)
        if self.student_problem_list.count():
            self.student_problem_list.setCurrentRow(index)
        self.student_problem_list.blockSignals(False)
        if self.student_problem_list.count():
            self._on_student_problem_changed(
                self.student_problem_list.currentItem(), None)
        self._log(self.student_log, f"收到 {len(views)} 道题的题面",
                  self.palette.info)

    def _on_student_problem_changed(self, current, _previous) -> None:
        if current is None:
            return
        problem_id = str(current.data(Qt.UserRole))
        self._current_problem_id = problem_id
        client = self._client
        if client is None:
            return
        view = client.problem(problem_id)
        if view is None:
            return
        header = (f"# {view.id} {view.title}\n\n"
                  f"时间限制 {view.time_limit} ms · 内存限制 {view.memory_limit} MB")
        if view.io_mode == "file" and view.input_file:
            # CCF 规约：程序读写当前目录下的数据文件，不是标准输入输出。
            # 不把这一条摆在题面最上面，学生交上去必然 RE 而不知道为什么。
            header += (f"\n\n**本题使用文件输入输出**：读 `{view.input_file}`，"
                       f"写 `{view.output_file}`（程序运行在题目英文名 "
                       f"`{view.slug}` 的目录下）。")
        elif view.slug:
            header += f"\n\n英文名 `{view.slug}` · 标准输入输出"
        body = view.description.strip()
        text = header + ("\n\n---\n\n" + body if body else "")
        for index, sample in enumerate(view.samples, start=1):
            text += (f"\n\n---\n\n**样例 {index}**\n\n输入\n\n```\n"
                     f"{sample.get('input', '')}\n```\n\n输出\n\n```\n"
                     f"{sample.get('output', '')}\n```")
        self.statement_view.render_markdown(text, images_enabled=False)
        self._refresh_attempt_hint()

    def _refresh_attempt_hint(self) -> None:
        client = self._client
        if client is None or not self._current_problem_id:
            self.attempt_label.clear()
            return
        session_attempts = sum(
            1 for item in client.verdicts
            if item.get("problem_id") == self._current_problem_id)
        text = f"本题已提交 {session_attempts} 次"
        if self._remaining_base is not None and self._student_remaining() == 0:
            text += " · 已到收卷时间"
        self.attempt_label.setText(text)

    def submit_code(self, *, forced: bool = False) -> None:
        client = self._client
        if client is None:
            self.notify.emit("warning", "还没有加入房间。")
            return
        if client.locked and not forced:
            # 盖着屏幕还能用快捷键交代码，锁屏就成了摆设 —— 快捷键是全局的，
            # 不会因为界面被盖住而失效，所以这里必须再拦一道。
            self.notify.emit("warning", "屏幕已锁定，请先输入口令解开。")
            return
        problem_id = self._current_problem_id
        if not problem_id:
            self.notify.emit("warning", "请先在左边选择一道题目。")
            return
        code = self.code_editor.toPlainText()
        if not code.strip():
            if not forced:
                self.notify.emit("warning", "代码是空的。")
            return
        language = str(self.language_combo.currentData())
        try:
            client.submit(problem_id, language, code, forced=forced)
        except Exception as exc:                            # noqa: BLE001
            log.exception("提交失败")
            self.notify.emit("error", f"提交失败：{exc}")
            return
        if forced:
            self._log(self.student_log, "到点收卷：已自动提交当前代码",
                      self.palette.warning)
        else:
            self._log(self.student_log, f"已提交 {problem_id}（{language}）",
                      self.palette.info)
        self.status(f"已提交 {problem_id}")

    def _on_verdict(self, data: dict) -> None:
        """评测回执。先来一条"已收到、等待评测"，判完再来一条最终结果。"""
        serial = int(data.get("serial", 0) or 0)
        problem_id = str(data.get("problem_id", ""))
        attempt = int(data.get("attempt", 1) or 1)
        pending = bool(data.get("pending"))
        verdict = "…" if pending else _fmt_verdict(data.get("verdict"))
        row = (
            str(serial),
            problem_id,
            f"第 {attempt} 次" + ("（自动）" if data.get("forced") else ""),
            "等待评测" if pending else verdict,
            BLANK if pending else f"{data.get('passed', 0)}/{data.get('total', 0)}",
            BLANK if pending else _fmt_score(data.get("score"),
                                             data.get("possible")),
            BLANK if pending else _fmt_ms(data.get("time_ms")),
            BLANK if pending else _fmt_mb(data.get("memory_mb")),
            datetime.now().strftime("%H:%M:%S"),
        )
        index = self._mine_rows.get(serial)
        if index is None or index >= self.mine_table.rowCount():
            index = self.mine_table.rowCount()
            self.mine_table.insertRow(index)
            self._mine_rows[serial] = index
        for column, text in enumerate(row):
            item = QTableWidgetItem(text)
            if column >= 3:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if not pending:
                item.setForeground(QColor(self.palette.verdict_color(verdict)))
            self.mine_table.setItem(index, column, item)
        self.mine_table.scrollToBottom()

        if pending:
            self._log(self.student_log,
                      f"{problem_id} 第 {attempt} 次提交已收到，等待评测",
                      self.palette.text_muted)
        else:
            self._log(self.student_log,
                      f"{problem_id} 第 {attempt} 次 → {verdict} "
                      f"{data.get('passed', 0)}/{data.get('total', 0)} "
                      f"{_fmt_score(data.get('score'), data.get('possible'))} 分 "
                      f"{_fmt_ms(data.get('time_ms'))} "
                      f"{_fmt_mb(data.get('memory_mb'))}",
                      self.palette.verdict_color(verdict))
            if data.get("message"):
                self._log(self.student_log, f"  {data['message']}")
            self.status(f"{problem_id} 第 {attempt} 次：{verdict} "
                        f"{_fmt_score(data.get('score'), data.get('possible'))} 分")
            if self._current_policy().lock_after_submit:
                self._lock_editor_after_submit()
        self._refresh_attempt_hint()

    def _on_collect(self, data: dict) -> None:
        """收卷指令。这是"到点"这件事唯一可信的信号。"""
        reason = "到点收卷" if data.get("reason") == "deadline" else "老师提前收卷"
        force = bool(data.get("force_collect"))
        self._collect_seen = True
        self._remaining_base = 0
        self._remaining_at = monotonic()
        if force:
            self._log(self.student_log, f"{reason}：正在自动提交当前代码",
                      self.palette.warning)
            if not self._editor_locked:
                self.submit_code(forced=True)
            self._lock_editor(f"{reason}，编辑器已锁定")
        else:
            self._log(self.student_log,
                      f"{reason}：请自行提交，老师没有开启强制收卷",
                      self.palette.warning)
        self._apply_student_state()

    def _lock_editor_after_submit(self) -> None:
        """本场要求"交完就锁"：判定一回来就冻结编辑器。

        与 :meth:`_lock_editor`（收卷）分开，是因为两者的**提示语**必须不同：
        收卷说的是"考试结束了"，这条说的是"你已经交了"。混用会让学生以为
        整场测验已经结束，不再去看后面的题。
        """
        if self._editor_locked:
            return
        self._editor_locked = True
        self.code_editor.setReadOnly(True)
        self.submit_button.setEnabled(False)
        self.submit_button.setToolTip("本场提交后即锁定，不能再改")
        self.open_code_button.setEnabled(False)
        self.join_status.setText("已提交 · 本场提交后锁定编辑器")

    def _lock_editor(self, message: str) -> None:
        self._editor_locked = True
        self.code_editor.setReadOnly(True)
        self.submit_button.setEnabled(False)
        self.submit_button.setToolTip("已收卷，不能再提交")
        # 收卷之后连代码文件也不该再换：编辑器已经锁定，"打开另一份"只会
        # 让人以为还能改。保存仍然放开 —— 交完卷把代码存一份带走是正当需求。
        self.open_code_button.setEnabled(False)
        self.join_status.setText(f"已收卷 · {message}")

    def _on_server_error(self, data: dict) -> None:
        message = str(data.get("message", "主机拒绝了这次操作"))
        self._log(self.student_log, f"主机提示：{message}", self.palette.warning)
        self.notify.emit("warning", message)

    def _render_student_board(self, payload: dict) -> None:
        published = bool(payload.get("published"))
        myself = payload.get("myself")
        try:
            max_total = int(payload.get("max_total_score") or 0)
        except (TypeError, ValueError):
            max_total = 0
        if not published:
            # 封榜期间榜单是空的，把"为什么空"写在表里 —— 空表会被当成故障。
            # 同时把"你自己那一行"带上：考试模式不公开别人的成绩，但不该连
            # 自己的排名都看不到。
            self._fill_table(self.student_board_table, [])
            self.student_board_table.setRowCount(1)
            reason = str(payload.get("withheld_reason", "榜单暂不可见"))
            if isinstance(myself, dict):
                reason += (f"　·　你自己：第 {myself.get('rank', BLANK)} 名，"
                           f"{_fmt_score(myself.get('score'), max_total)} 分")
            item = QTableWidgetItem(reason)
            item.setTextAlignment(Qt.AlignCenter)
            self.student_board_table.setItem(0, 0, item)
            self.student_board_table.setSpan(
                0, 0, 1, max(1, self.student_board_table.columnCount()))
            return
        self._fill_table(self.student_board_table,
                         [self._overall_row(row, max_total)
                          for row in (payload.get("overall") or [])],
                         right_aligned=(0, 3, 4, 5, 6, 7))

    # ==================================================================
    # 计时与生命周期
    # ==================================================================

    def _on_tick(self) -> None:
        if self._server is not None:
            self._refresh_host_status()
        if self._client is not None:
            self._apply_student_state()

    def on_activated(self) -> None:
        """切到本面板时刷新题目清单（房间开着就不动）。"""
        if self.role_combo.currentData() == "host":
            self.refresh_problem_choices()
        elif self._client is None:
            self._load_identity()

    def on_settings_changed(self) -> None:
        self.refresh_problem_choices()

    def on_closing(self) -> bool:
        """关窗口前把房间收干净。

        房间不关就退出，学生端会一直停在"连不上"直到超时；而主机这边
        因为进程结束，榜单也一起没了。所以这里要么关干净，要么明确问一句。
        """
        if self._server is not None:
            answer = QMessageBox.question(
                self, "房间还在开着",
                "本机还在开着房间。退出会断开所有学生并结束本场测验。\n\n确定退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return False
            self.close_room(quiet=True)
        if self._client is not None or self._worker is not None:
            self._teardown_client()
        self.tick_timer.stop()
        return True

    # ---- 主题 -------------------------------------------------------------

    def apply_palette(self, palette) -> None:
        self.palette = palette
        for widget in (self.code_editor, self.statement_view, self.host_log,
                       self.student_log, self.host_code_view,
                       self.host_verdict_view):
            if widget is not None and hasattr(widget, "set_palette_theme"):
                widget.set_palette_theme(palette)
        self.code_editor.set_language(
            Language.from_value(str(self.language_combo.currentData())) or Language.CPP)
        if self._server is not None:
            self._refresh_host_boards()

    def _on_language_changed(self) -> None:
        language = Language.from_value(str(self.language_combo.currentData()))
        if language is not None:
            self.code_editor.set_language(language)

    def current_language(self) -> Language:
        """当前选中的提交语言。"""
        value = self.language_combo.currentData()
        return Language.from_value(str(value)) or Language.CPP

    def _select_language(self, language: Language) -> None:
        """切换语言下拉框（打开 ``.py`` 时用得上）。"""
        if self.current_language() is language:
            return
        index = self.language_combo.findData(language.value)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)

    # ---- 代码文件（打开 / 另存为） ----------------------------------------

    def open_code_file(self) -> None:
        """从文件读一份代码进来。

        考试中学生手边可能有自己课前写好的框架，读进来是合理的。
        收卷之后按钮会被禁用（见 :meth:`_lock_editor`）—— 那时候编辑器已经锁定，
        再让他换一份代码没有意义。
        """
        if self.code_files.open_file():
            self._log(self.student_log,
                      f"已从文件读入代码（{os.path.basename(self.code_files.path)}）",
                      self.palette.text_muted)

    def save_code_as(self) -> None:
        """把编辑器里的代码存成文件。"""
        if self.code_files.save_as():
            self._log(self.student_log,
                      f"代码已保存（{os.path.basename(self.code_files.path)}）",
                      self.palette.text_muted)


__all__ = ["ExamPanel", "ConnectWorker"]
