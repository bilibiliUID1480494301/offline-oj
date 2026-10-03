"""房间测验会话、参与者、提交记录与排名。

这一层是纯数据与规则，不碰网络也不碰界面 —— 排名这种"最容易算错又最难发现
算错"的东西，必须能在没有套接字、没有桌面的环境里单独测。

身份模型（本模块最要紧的一条设计决定）
--------------------------------------
**身份是设备八位 ID，用户名只是标签。** 展开说：

* 设备 ID 由客户端首次运行时生成、存在本机，之后每一场都是同一个。学生一眼认得出
  自己那一行，老师念 ID 点名也稳定；
* 用户名**允许重复**。强行要求唯一，会在"班里两个张三"时把第二个人挡在门外，
  而这时改名的成本落在学生身上；靠设备 ID 区分同名的人更省事，也更诚实；
* 同一设备 ID 同时只允许一条连接，新连接顶掉旧的。因为"换台机器继续做"不是这个
  场景的需求，而"断线重连"是 —— 网卡松动、笔记本合盖、切个 Wi-Fi 都得能回来。

设备 ID 的字符集复用 Crockford Base32 的思路：排除 I、L、O、U，只留 32 个字符。
8 位就是整数 40 bit，同一场测验里撞号的概率可以忽略。

房间号与凭据
------------
**房间号（6 位数字）就是唯一凭据**：老师报一个号，学生输入房间号加自定义用户名就能进，
不需要逐人发牌。这是刻意的现场取舍。

但它只有 6 位数字，强度有限：它的作用是"分房间 + 挡住隔壁教室的人"，**不是**强凭据。
要防同网段的人抓包后离线爆破，请由老师另设**房间口令**（:attr:`ExamSession.password`），
口令默认留空。这条边界在 README 里也写了，不要夸大它。

两种进场方式（:class:`EntryMode`）
----------------------------------
* ``ROOM_CODE``（默认，与旧版本逐字节一致）：学生输**主机地址 + 房间号**（+ 可选的全场口令），
  用户名自己起。老师报一个号就能让全教室进来，代价是"谁是谁"只能靠学生自己填的名字；
* ``ACCOUNT``：学生输**主机地址 + 账号 + 个人口令**，账号与姓名来自老师事先导入的名单
  （:mod:`offline_oj.core.roster`）。这时**房间号不再是凭据**，学生根本不需要知道它。

两种方式走的是同一段握手，区别只在"这一步拿什么去查"：

====  ===============================================  ==============================
步骤  ROOM_CODE                                        ACCOUNT
====  ===============================================  ==============================
HELLO ``room_id(房间号[, 口令])``                        ``credential_id(账号, 口令[, 全场口令])``
查表  与主机自己那一个索引比                              在名单里反查这个索引属于谁
密钥  ``room_secret(房间号[, 口令])``                    ``credential_secret(账号, 口令[, 全场口令])``
名字  由学生自报（AUTH 帧里）                             由名单决定，**学生报什么都不算数**
====  ===============================================  ==============================

于是"账号模式"顺带把"绑定选手信息"做掉了：主机认的是名单上那一行，
学生改客户端、改内存、直接发包都改不了自己叫什么、是哪一号。

两条路径的索引都由 :mod:`offline_oj.credentials` 定义，两端共用同一份规则 ——
一旦不一致，现象是"名单上明明有你、密码也对，就是进不去"，极难查。

积分规则
--------
每题满分 100，按通过的测试点比例给分（``100 * 通过数 / 总数``，四舍五入）。
同一人同一题取**最高分**的一次提交计入总分 —— 这是 OJ 界通行做法，
也符合"想拿更高分就重交"的直觉。

排名打破平局的三级顺序（先比什么，后比什么，都能从 :meth:`ProblemRanking.sort_key`
一处看出，不做隐式约定）：

1. 分数高者在前；
2. 同分比**耗时**，短者在前；
3. 再同比**内存**，小者在前；
4. 再同比**提交时间**，早者在前；
5. 全同则按设备 ID，保证结果稳定可复现。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Sequence

from ..credentials import (ACCOUNT_MAX_LENGTH, PASSCODE_MAX_LENGTH,
                           credential_id, credential_secret,
                           normalize_account, normalize_passcode)

log = logging.getLogger(__name__)

__all__ = [
    "ExamState", "RoomMode", "ExamPolicy", "Participant", "Submission",
    "ExamProblemView",
    "RankingRow", "ProblemRanking", "ExamSession", "JoinError",
    "EntryMode",
    "DEVICE_ID_ALPHABET", "DEVICE_ID_LENGTH", "ROOM_CODE_LENGTH",
    "MAX_USERNAME_LENGTH", "MAX_SCORE_PER_PROBLEM", "DEFAULT_TITLE",
    "ACCOUNT_MAX_LENGTH", "PASSCODE_MAX_LENGTH", "MAX_UNLOCK_ATTEMPTS",
    "competition_ranks",
    "generate_device_id", "normalize_device_id",
    "generate_room_code", "normalize_room_code",
    "room_secret", "room_id",
    "normalize_account", "normalize_passcode",
    "credential_secret", "credential_id",
]

#: 设备 ID 的字符集，沿用 Crockford Base32 的思路：**保留 0 和 1**，排除容易被
#: 误读成它们的 I、L、O，再排除 U（避免与 V 混淆、也顺带避开不雅词）。
#: 正好 32 个字符 = 5 bit/字符，8 位就是整数 40 bit。
#:
#: 为什么要保留 0 和 1：只有 0/1 在字符集里，"用户把 O 打成 0" 才能被
#: **无歧义地**纠正回来（见 :func:`normalize_device_id`）。如果字符集里既没有 0
#: 也没有 O，那么用户打出哪个都是错，谁也没法猜他想要什么。
DEVICE_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEVICE_ID_LENGTH = 8

#: 房间号长度。6 位数字 ≈ 10^6 个房间，课堂场景足够，且口头报号不会有歧义。
ROOM_CODE_LENGTH = 6

#: 用户名长度上限。排行榜是一行一个名字，太长会把表撑变形。
MAX_USERNAME_LENGTH = 24

#: 每题**默认**满分，沿用 CCF 每题 100 分的约定。
#:
#: 它不再参与算分：一次提交的实际满分是判题器按测试点分值累加出来的
#: （``Submission.possible``），本题 5 个点就是 50 分。这个常量剩下两处用途：
#:
#: * 外部接入的判题器只填通过数、不给分值时，用该题分值之和当分母折算
#:   （:meth:`Submission.ratio_score`）；连分值都拿不到时才退回 100 分制；
#: * ``points`` 字段缺失的旧载荷据此理解为满分 100。
MAX_SCORE_PER_PROBLEM = 100

#: 默认测验名称。开门与改名两处都用它，避免"名字留空该回落到什么"
#: 出现第二种答案 —— 两处各写一份字面量，迟早有一处改了另一处没改。
DEFAULT_TITLE = "局域网测验"

#: 离场锁屏时最多允许输错几次口令。错满就锁死，只能等老师在主机端放行。
#:
#: 不是为了防"学生猜自己的口令"，而是因为解锁是**在主机校验的**（见
#: :meth:`ExamSession.verify_unlock`）—— 没有上限的话，一台学生机可以
#: 拿它当在线爆破接口，把 4 位数字口令在几秒内试穿。给个 5 次，
#: 现场补救成本（举手找老师）远低于被穷举的风险。
MAX_UNLOCK_ATTEMPTS = 5


class JoinError(Exception):
    """进不了房间：房间号不对、设备 ID 不合法、用户名不合法。文案直接给用户看。"""


class RoomMode(str, Enum):
    """房间模式。两种模式的差别只有一处：**榜单什么时候公开**。"""

    PRACTICE = "practice"     # 练习：榜单实时更新
    EXAM = "exam"             # 考试：结束后统一放榜

    @property
    def label(self) -> str:
        return {RoomMode.PRACTICE: "练习模式", RoomMode.EXAM: "考试模式"}[self]

    @classmethod
    def from_value(cls, value: Any) -> "RoomMode":
        # 先认枚举实例本身：``(str, Enum)`` 下 ``str(RoomMode.PRACTICE)``
        # 得到的是 ``"RoomMode.PRACTICE"`` 而不是 ``"practice"``，
        # 直接 ``cls(str(value))`` 会把一个合法的模式悄悄判成"考试模式"。
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.EXAM


class ExamState(str, Enum):
    """测验状态。状态只由时间推导，不单独存字段 —— 少一个能不一致的东西。"""

    PENDING = "pending"     # 还没开始
    RUNNING = "running"     # 进行中
    ENDED = "ended"         # 已结束

    @property
    def label(self) -> str:
        return {
            ExamState.PENDING: "未开始",
            ExamState.RUNNING: "进行中",
            ExamState.ENDED: "已结束",
        }[self]


class EntryMode(str, Enum):
    """学生**怎么进来**。建场时二选一，开房后不再改。

    它与 :class:`RoomMode` 正交，别把两者混起来：RoomMode 管"榜单什么时候放"，
    EntryMode 管"进门报什么"。四种组合都成立（练习+账号、考试+房间号……）。

    为什么不给"两种都开"：两条路径派生出的会话密钥来自不同的秘密，
    主机必须**在明文握手第一步就知道该查哪一侧** —— 那一刻它还没读到任何
    学生自报的东西。要是让 HELLO 里带一个"我走哪种方式"的标记，
    那就等于让客户端决定主机用哪套校验，等于没有校验。
    """

    ROOM_CODE = "room_code"     # 报房间号（+可选全场口令），用户名自起
    ACCOUNT = "account"          # 报名单上的账号 + 个人口令，姓名由名单决定

    @property
    def label(self) -> str:
        return {EntryMode.ROOM_CODE: "房间号进场",
                EntryMode.ACCOUNT: "账号进场"}[self]

    @classmethod
    def from_value(cls, value: Any) -> "EntryMode":
        # 与 RoomMode.from_value 同样的坑：``(str, Enum)`` 下 str(枚举) 不是值
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.ROOM_CODE


def _now() -> datetime:
    return datetime.now()


def _iso(moment: datetime | None) -> str:
    return moment.isoformat(timespec="seconds") if moment else ""


def _moment(value: object) -> datetime | None:
    """把 :func:`_iso` 写出去的字符串读回来（读档用）。

    认不出来的时间一律当 ``None``（"不知道"），不要退化成"现在" ——
    把一份两年前的档案显示成今天考的，比时间空着更糟。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 房间号
# ---------------------------------------------------------------------------


def generate_room_code() -> str:
    """生成一个 6 位数字房间号，首位不为 0（口头念出来更清楚）。

    用 :mod:`secrets` 而不是 :mod:`random` —— 房间号是凭据，不能可预测。
    """
    head = secrets.choice("123456789")
    tail = "".join(secrets.choice("0123456789") for _ in range(ROOM_CODE_LENGTH - 1))
    return head + tail


def normalize_room_code(text: str) -> str:
    """把用户手打的房间号归一化。

    房间号是**纯数字**，所以这里只做两件无歧义的事：去掉所有空白、
    把全角数字转成半角（中文输入法下极易打出全角）。不做补零也不做截断 ——
    位数不对就该报"房间号是 6 位"，而不是悄悄猜一个。
    """
    out: list[str] = []
    for char in text or "":
        if char.isspace():
            continue
        if "\uff10" <= char <= "\uff19":        # 全角 ０-９
            char = chr(ord(char) - 0xFEE0)
        out.append(char)
    return "".join(out)


def room_secret(room_code: str, password: str = "") -> str:
    """房间号（+ 可选口令）构成的共享秘密，两端据此派生会话密钥。

    口令**区分大小写**，不做任何折叠 —— 折叠会白白削掉一大截熵。
    分隔符用冒号，避免"房间号 123456 + 口令 78"和"1234567 + 口令 8"撞到一起。
    """
    code = normalize_room_code(room_code)
    extra = (password or "").strip()
    return f"{code}:{extra}" if extra else code


def room_id(room_code: str, password: str = "") -> str:
    """房间秘密的单向索引，握手时明文上线的是它，而不是房间号本身。

    单向：拿到索引推不回房间号，因此**抓包拿不到钥匙**。
    但要说清楚它没解决什么 —— 房间号的搜索空间只有 10^6，拿着这个索引
    离线穷举房间号来比对是可行的（scrypt 让每次比对约 350ms，多核并行下
    是"小时"量级）。它的定位是"不主动泄密"，真正的加固手段是设房间口令。
    """
    return hashlib.sha256(room_secret(room_code, password).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 设备八位 ID
# ---------------------------------------------------------------------------


def generate_device_id() -> str:
    """生成一个设备八位 ID。"""
    return "".join(secrets.choice(DEVICE_ID_ALPHABET)
                   for _ in range(DEVICE_ID_LENGTH))


def normalize_device_id(text: str) -> str:
    """把用户手打的设备 ID 归一化到字符集内。

    只做三件事，都是**无歧义**的：

    1. 去掉所有空白（学生常把 ``ABCD 2345`` 连写或带空格抄）；
    2. 统一大写；
    3. 把易混字符折叠回来：``O``→``0``、``I``/``L``→``1``、``U``→``V``。

    折叠之所以成立，是因为 ``I``/``L``/``O``/``U`` **不在字符集里**，
    所以用户打出它们一定是想打 ``1``/``0``/``V``。反过来做（把 ``0`` 映射成 ``O``）
    就不成立了 —— 那会让一个合法 ID 被改坏。
    """
    folded = (text or "").upper().translate(
        str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"}))
    return "".join(char for char in folded if not char.isspace())


# ---------------------------------------------------------------------------
# 账号与个人口令
# ---------------------------------------------------------------------------
#
# 规则**不在这里**：``normalize_account`` / ``normalize_passcode`` /
# ``credential_secret`` / ``credential_id`` 全部住在 :mod:`offline_oj.credentials`，
# 上面已经 import 进来并重新导出。原因写在那个模块的开头 —— 一句话：
# ``core/roster.py`` 也要用同一份规则，而它不许 import ``net/``。
#
# 本模块只负责"把它接到房间上"：见 :meth:`ExamSession.credential_of`。


# ---------------------------------------------------------------------------
# 考场策略
# ---------------------------------------------------------------------------


@dataclass
class ExamPolicy:
    """本场测验"哪些能做"的限制。**默认值就是全开**，与既有行为逐字节一致。

    这里只装**新增**的可限制项。``allow_resubmit`` 与 ``show_leaderboard``
    刻意不搬进来：它们早就长在 :class:`ExamSession` 上，判定逻辑
    （:meth:`ExamSession.can_submit` / :meth:`ExamSession.leaderboard_visible`）
    也已经是实例方法并随 ``summary()`` 广播出去。再抄一份进这里就有了
    **两个真相源** —— 那正是本项目最忌的一类结构问题。

    这里**没有** ``allow_local_run``（自测开关）：学生端面板本来就没有「测试运行」
    入口 —— 判题一律在主机做、学生机不装编译器（这是整个架构的前提）。给一个
    没有可关之物的开关，老师关了却发现什么都没变，比不给更糟。哪天真要在学生端
    加自测入口，再把字段补上。

    :param allowed_languages: 允许的语言值（``Language.value``，如 ``"cpp"``）。
        空元组 = 不限。**服务端必须自己校验**：学生端可以被改（改 exe、改内存、
        直接发包都行），界面置灰只是"别让人白点一次"，不是安全边界。
    :param allow_copy_out: 关掉「把代码带出考场」的入口（学生端的另存为…）。
    :param lock_after_submit: 提交判定成功后锁住编辑器，防止交完继续改。
    """

    allowed_languages: tuple[str, ...] = ()
    allow_copy_out: bool = True
    lock_after_submit: bool = False

    def allows_language(self, language: Any) -> bool:
        """这种语言本场收不收。空元组 = 不限。

        同时接受 ``Language`` 枚举与裸字符串 —— 调用方一个是已解析的枚举
        （服务端），一个是原始载荷（客户端），两边都不该被迫先做转换。
        """
        if not self.allowed_languages:
            return True
        value = getattr(language, "value", language)
        return str(value) in self.allowed_languages

    def is_default(self) -> bool:
        return (not self.allowed_languages and self.allow_copy_out
                and not self.lock_after_submit)

    def to_dict(self) -> dict[str, Any]:
        """**只写非默认项**：默认策略序列化成 ``{}``，与既有线上载荷逐字节兼容。"""
        data: dict[str, Any] = {}
        if self.allowed_languages:
            data["allowed_languages"] = list(self.allowed_languages)
        if not self.allow_copy_out:
            data["allow_copy_out"] = False
        if self.lock_after_submit:
            data["lock_after_submit"] = True
        return data

    @classmethod
    def from_dict(cls, data: Any = None) -> "ExamPolicy":
        """从载荷还原。**未知字段忽略、缺字段取默认** —— 读旧档 / 旧主机不发策略时都不该炸。"""
        payload = data if isinstance(data, dict) else {}
        raw = payload.get("allowed_languages") or []
        allowed = tuple(str(item) for item in raw if str(item))
        return cls(
            allowed_languages=allowed,
            allow_copy_out=bool(payload.get("allow_copy_out", True)),
            lock_after_submit=bool(payload.get("lock_after_submit", False)),
        )


# ---------------------------------------------------------------------------
# 题目视图
# ---------------------------------------------------------------------------


@dataclass
class ExamProblemView:
    """下发给客户端的题目视图。

    **只包含样例测试点。** 正式测试点的期望输出是整场测验里最有价值的东西，
    它永远不离开主机 —— 这是"主机统一判题"这个架构选择最大的安全红利，
    比任何传输加密都更根本。
    """

    id: str
    title: str = ""
    description: str = ""
    time_limit: int = 1000
    memory_limit: int = 256
    #: CCF 规约下的题目英文名 —— 客户端据此知道数据文件叫什么
    slug: str = ""
    #: 文件模式的数据文件名（题目英文名已展开），标准输入输出时为空串
    input_file: str = ""
    output_file: str = ""
    io_mode: str = "stdio"
    samples: list[dict[str, str]] = field(default_factory=list)
    #: 本题满分（各测试点分值之和）。学生要靠它才读得懂自己那个"37 分"是
    #: 满分 50 里的 37 还是满分 100 里的 37。
    points: int = MAX_SCORE_PER_PROBLEM

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "time_limit": self.time_limit,
            "memory_limit": self.memory_limit,
            "slug": self.slug,
            "input_file": self.input_file,
            "output_file": self.output_file,
            "io_mode": self.io_mode,
            "samples": self.samples,
        }
        # 满分只在不是 100 时才写：旧主机的载荷里没有这个键，客户端一律按
        # 100 理解，于是"每题 100 分"的常见情形下新旧逐字节一致。
        if self.points != MAX_SCORE_PER_PROBLEM:
            data["points"] = self.points
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExamProblemView":
        samples = data.get("samples") or []
        try:
            points = int(data.get("points", MAX_SCORE_PER_PROBLEM))
        except (TypeError, ValueError):
            points = MAX_SCORE_PER_PROBLEM
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            time_limit=int(data.get("time_limit", 1000) or 1000),
            memory_limit=int(data.get("memory_limit", 256) or 256),
            slug=str(data.get("slug", "") or ""),
            input_file=str(data.get("input_file", "") or ""),
            output_file=str(data.get("output_file", "") or ""),
            io_mode=str(data.get("io_mode", "stdio") or "stdio"),
            samples=[{"input": str(item.get("input", "")),
                      "output": str(item.get("output", ""))}
                     for item in samples if isinstance(item, dict)],
            points=points if points > 0 else MAX_SCORE_PER_PROBLEM,
        )


# ---------------------------------------------------------------------------
# 参与者与提交
# ---------------------------------------------------------------------------


@dataclass
class Participant:
    """一位参与者。身份就是 :attr:`device_id`，用户名只是显示标签。"""

    device_id: str
    username: str = ""
    joined_at: datetime = field(default_factory=_now)
    last_seen: datetime | None = None
    connected: bool = False
    address: str = ""
    #: 是否处在**离场锁屏**状态（学生去上厕所时自己按的，或老师远程按的）。
    #:
    #: 这一位只用来给主机和本人显示"你现在是盖着的"，**不是安全边界** ——
    #: 真正的判定在 :meth:`ExamSession.verify_unlock`（主机侧），
    #: 因为学生机是可以被改的。锁屏盖住的是"隔壁同学的眼睛"，不是这台机器。
    locked: bool = False
    locked_at: datetime | None = None
    #: 解锁口令连续输错的次数。达到 :data:`MAX_UNLOCK_ATTEMPTS` 就不再受理，
    #: 只能由老师在主机端放行（见 :meth:`ExamSession.verify_unlock`）。
    unlock_failures: int = 0

    def touch(self, address: str = "") -> None:
        self.last_seen = _now()
        if address:
            self.address = address

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "username": self.username,
            "joined_at": _iso(self.joined_at),
            "last_seen": _iso(self.last_seen),
            "connected": self.connected,
            "address": self.address,
            "locked": self.locked,
            "locked_at": _iso(self.locked_at),
        }


@dataclass
class Submission:
    """一次提交及其判定结果。"""

    serial: int
    device_id: str
    problem_id: str
    language: str
    code: str
    username: str = ""
    #: 同一人同一题的第几次提交，从 1 开始
    attempt: int = 1
    submitted_at: datetime = field(default_factory=_now)
    judged_at: datetime | None = None
    verdict: str = ""
    passed: int = 0
    total: int = 0
    time_ms: float = 0.0
    memory_mb: float = 0.0
    message: str = ""
    score: int = 0
    #: 本题满分 = 各测试点分值之和（判题器给的 ``JudgeReport.points_total``）。
    #: ``0`` 表示这次判题**连题目分值都没拿到** —— 只有外部接入的判题器会这样，
    #: 那时的 ``score`` 走 :meth:`ratio_score` 折算，分母仍是该题分值之和。
    possible: int = 0
    #: 这次提交是不是"到点强制收卷"自动交上来的
    forced: bool = False
    #: 判这次提交时主机开的编译优化（C/C++ 的 ``-O2`` / ``/O2``）。
    #:
    #: 记下来的理由：同一份代码开不开优化能差好几倍耗时，事后再看"他为什么 TLE"
    #: 必须先知道当时是怎么编的。不适用（Python / Java）或旧数据一律 False。
    optimized: bool = False

    @property
    def accepted(self) -> bool:
        return self.verdict == "AC"

    @property
    def perfect(self) -> bool:
        """这道题拿满了没有。

        ``possible`` 为 0 时按 :data:`MAX_SCORE_PER_PROBLEM` 这个老刻度判断 ——
        那意味着连题目分值都没拿到，只能按约定值理解。
        """
        return self.score >= (self.possible or MAX_SCORE_PER_PROBLEM)

    def ratio_score(self, full: int = MAX_SCORE_PER_PROBLEM) -> int:
        """按通过测试点的**个数**比例折算到 ``full`` 分制。

        这条只在判题器没给测试点分值时才走（外部接入的判题器）。本项目自己的
        :class:`~offline_oj.core.judge.Judge` 一律带分值，走的是
        ``JudgeReport.points_earned`` 那条路 —— 也就是 NOI 的给分方式。

        ``full`` 默认是老的 100 分刻度；服务端给的是**该题测试点分值之和**，
        这样兜底路径和 NOI 路径共用一把尺子（全对就是该题的满分），否则榜上
        会出现"得分 100 / 满分 60"这种读不通的行。

        测试点数为 0 时给 0 分，不给满分："没有测试点"是出题人的配置问题，
        不该变成送分。
        """
        if self.total <= 0 or full <= 0:
            return 0
        return int(round(full * self.passed / self.total))

    def to_dict(self) -> dict[str, Any]:
        data = {
            "serial": self.serial,
            "device_id": self.device_id,
            "username": self.username,
            "problem_id": self.problem_id,
            "language": self.language,
            "attempt": self.attempt,
            "verdict": self.verdict,
            "passed": self.passed,
            "total": self.total,
            "score": self.score,
            "time_ms": round(self.time_ms, 2),
            "memory_mb": round(self.memory_mb, 2),
            "message": self.message,
            "forced": self.forced,
            "submitted_at": _iso(self.submitted_at),
            "judged_at": _iso(self.judged_at),
        }
        # 约定：只写非默认值。这样旧读档代码看到的老数据逐字节不变，
        # 也不会因为多一个恒为 False 的键而误判成"新版本数据"。
        if self.optimized:
            data["optimized"] = True
        if self.possible:
            data["possible"] = self.possible
        return data

    def to_archive_dict(self) -> dict[str, Any]:
        """写进测验档案的那一份 —— :meth:`to_dict` 加上**源码**。

        与 :meth:`to_dict` 刻意分开，因为那个方法的契约是"**绝不含 code**"：
        它在出站载荷上跑（``server.py`` 给客户端发判定、发榜单），
        ``tests/test_net_lan.py`` 会埋标记扫全部出站帧来保证这一点。
        把源码塞进 ``to_dict`` 一次，学生机上就有别人的代码可解了。

        这里没有那道约束（档案只落在主机自己的磁盘上），但它必须是**另一个
        方法名** —— 让人一眼看出"这两条路不一样"，而不是靠读注释才发现。
        """
        data = self.to_dict()
        data["code"] = self.code
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Submission":
        """读档用。**未知字段忽略、缺字段取默认**（档案格式的兼容契约）。

        ``code`` 缺失是正常的：``keep_code=False`` 的档案本来就不存源码。
        """
        def _int(key: str, fallback: int = 0) -> int:
            try:
                return int(data.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def _float(key: str) -> float:
            try:
                return float(data.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0

        return cls(
            serial=_int("serial"),
            device_id=str(data.get("device_id", "") or ""),
            problem_id=str(data.get("problem_id", "") or ""),
            language=str(data.get("language", "") or ""),
            code=str(data.get("code", "") or ""),
            username=str(data.get("username", "") or ""),
            attempt=max(1, _int("attempt", 1)),
            submitted_at=_moment(data.get("submitted_at")) or _now(),
            judged_at=_moment(data.get("judged_at")),
            verdict=str(data.get("verdict", "") or ""),
            passed=_int("passed"),
            total=_int("total"),
            time_ms=_float("time_ms"),
            memory_mb=_float("memory_mb"),
            message=str(data.get("message", "") or ""),
            score=_int("score"),
            possible=_int("possible"),
            forced=bool(data.get("forced")),
            optimized=bool(data.get("optimized")),
        )

    def brief(self) -> str:
        tag = "（自动收卷）" if self.forced else ""
        return (f"#{self.serial} {self.problem_id} 第{self.attempt}次{tag} "
                f"{self.verdict} {self.passed}/{self.total} {self.score}分 "
                f"{self.time_ms:.0f}ms/{self.memory_mb:.0f}MB")


# ---------------------------------------------------------------------------
# 排名
# ---------------------------------------------------------------------------


def competition_ranks(scores: Sequence[int]) -> list[int]:
    """名次序列：**同分并列，下一名跳号**（NOI 式）。

    ``[300, 300, 200, 100, 100]`` → ``[1, 1, 3, 4, 4]``。

    这正是 NOI / NOIP 官方成绩单的排法。它和"总分 → 耗时 → 内存"一路破平
    的区别不是风格问题：后者会给出"同为 300 分，你第 3 我第 4"这种**没有
    依据**的名次 —— 耗时之差常常只来自评测机的抖动，而选手会把它当成真实的
    差距。所以耗时与内存继续在榜上显示，只是不再参与名次。

    :param scores: 已按名次先后排好序的分数序列（降序）
    """
    ranks: list[int] = []
    for index, score in enumerate(scores):
        if index and score == scores[index - 1]:
            ranks.append(ranks[-1])
        else:
            ranks.append(index + 1)
    return ranks


@dataclass
class RankingRow:
    """排名表的一行。"""

    rank: int = 0
    device_id: str = ""
    username: str = ""
    score: int = 0
    #: 该人每题的最高分，键是题目 ID
    per_problem: dict[str, int] = field(default_factory=dict)
    #: 该人每题计入总分的是第几次提交
    per_problem_attempts: dict[str, int] = field(default_factory=dict)
    #: 用于打破平局的总耗时（只累计计入总分的那次提交）
    total_time_ms: float = 0.0
    total_memory_mb: float = 0.0
    last_submit_at: datetime | None = None
    solved: int = 0
    #: 这个人的提交总次数（含没计入总分的那些）
    submit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "device_id": self.device_id,
            "username": self.username,
            "score": self.score,
            "solved": self.solved,
            "per_problem": dict(self.per_problem),
            "per_problem_attempts": dict(self.per_problem_attempts),
            "total_time_ms": round(self.total_time_ms, 2),
            "total_memory_mb": round(self.total_memory_mb, 2),
            "last_submit_at": _iso(self.last_submit_at),
            "submit_count": self.submit_count,
        }


@dataclass
class ProblemRanking:
    """单题排名的一行 —— 这就是需求里的"同题提交的分以及时间空间排名"。"""

    rank: int
    submission: Submission

    @property
    def device_id(self) -> str:
        return self.submission.device_id

    @property
    def username(self) -> str:
        return self.submission.username

    @property
    def sort_key(self) -> tuple:
        """单题排名的完整排序键，按重要性从高到低。

        分数高 → 耗时短 → 内存小 → 提交早 → 设备 ID。
        把"提交早"排在设备 ID 前面，是为了让同分同资源的两人按先来后到排，
        符合直觉且不依赖字典序这种偶然属性；最后一级用设备 ID 而不是用户名，
        是因为用户名允许重复，拿它收尾会让排序结果不稳定。
        """
        return (-self.submission.score,
                self.submission.time_ms,
                self.submission.memory_mb,
                self.submission.submitted_at,
                self.submission.device_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "device_id": self.submission.device_id,
            "username": self.submission.username,
            "score": self.submission.score,
            # 单题榜必须带满分：脱离满分看"37 分"，读不出是 50 分里的 37
            # 还是 100 分里的 37。旧数据（possible 为 0）由界面自己退让成只写分数。
            "possible": self.submission.possible,
            "verdict": self.submission.verdict,
            "passed": self.submission.passed,
            "total": self.submission.total,
            "attempt": self.submission.attempt,
            "time_ms": round(self.submission.time_ms, 2),
            "memory_mb": round(self.submission.memory_mb, 2),
            "language": self.submission.language,
            "submitted_at": _iso(self.submission.submitted_at),
        }


class ExamSession:
    """一场房间测验。

    时间只由 ``starts_at`` / ``ends_at`` 两个时刻决定，状态是推导出来的。
    刻意不存"当前状态"字段：存了就有两处真相，一旦有个路径忘了更新，
    就会出现"界面显示进行中、服务端已经拒绝提交"这种现场事故。

    ``duration_minutes`` 传 0 表示**不限时**（``ends_at`` 为 ``None``），
    练习模式的常用形态。
    """

    def __init__(
        self,
        session_id: str,
        *,
        title: str = DEFAULT_TITLE,
        mode: RoomMode = RoomMode.EXAM,
        room_code: str = "",
        password: str = "",
        duration_minutes: int = 60,
        starts_at: datetime | None = None,
        grace_seconds: int = 30,
        force_collect: bool = False,
        allow_resubmit: bool = True,
        show_leaderboard: bool = True,
        policy: ExamPolicy | None = None,
        entry_mode: EntryMode = EntryMode.ROOM_CODE,
    ) -> None:
        self.session_id = session_id
        self.title = title
        self.mode = RoomMode.from_value(mode)
        #: 学生怎么进来。默认与旧版本一致（报房间号）。
        self.entry_mode = EntryMode.from_value(entry_mode)
        #: 房间号即凭据。留空自动随机生成一个。
        #:
        #: **账号进场时它照样生成**，只是不再当凭据用：老师在主机端仍然需要
        #: 一个稳定的短标识来口头称呼这一场（以及写进档案的 ``room_id`` 索引）。
        #: 学生看不到、也不需要它。
        self.room_code = normalize_room_code(room_code) or generate_room_code()
        #: 可选的**全场口令**。留空表示"只认房间号"（房间号进场）或
        #: "只认个人口令"（账号进场）。**切不要放进 summary()**
        self.password = (password or "").strip()
        self.starts_at = starts_at or _now()
        self.duration_minutes = max(0, int(duration_minutes))
        self.ends_at: datetime | None = (
            self.starts_at + timedelta(minutes=self.duration_minutes)
            if self.duration_minutes else None
        )
        #: 收卷宽限：截止后仍接受已经发出、只是还没到的帧。
        #: 没有它，网络晚 0.2 秒就能把一份按时交的卷子判成迟到。
        self.grace_seconds = max(0, int(grace_seconds))
        #: 到点是否强制收卷（客户端自动提交并锁编辑器）
        self.force_collect = bool(force_collect)
        #: 是否允许对同一题重复提交
        self.allow_resubmit = bool(allow_resubmit)
        #: 是否公开榜单。关掉之后**全程不放榜**（练习模式也不例外）。
        #:
        #: 它与 :class:`RoomMode` 正交：RoomMode 管"什么时候放"（练习=实时、
        #: 考试=结束后），它管"放不放"。于是"考试 + 全程不放榜"只是把它置 False，
        #: 不必为此再造第三种 RoomMode。默认 True，保持既有行为逐字节不变。
        self.show_leaderboard = bool(show_leaderboard)
        #: 语言与功能的限制。默认即全开，所以旧代码（不传这个参数）行为不变。
        self.policy = policy if isinstance(policy, ExamPolicy) else ExamPolicy()

        self.problems: list[ExamProblemView] = []
        #: 设备 ID → 参与者
        self.participants: dict[str, Participant] = {}
        self.submissions: list[Submission] = []
        self._serial = 0
        #: 被新连接顶替过的设备 ID（主机界面据此提示"某某换了台机器 / 有人冒名"）
        self.replacements: list[dict[str, Any]] = []
        #: 归一化账号 → 名单条目 dict（``core/roster.py::Contestant.to_dict()``）。
        #:
        #: 存 dict 而不是 import ``core.roster.Contestant``：本模块要保住
        #: "零第三方依赖、不 import ``core/``"这个属性，两边靠 dict 对接 ——
        #: 与 ``archive_record()`` 那三个方法同一套做法。
        self.contestants: dict[str, dict[str, Any]] = {}
        #: 设备 ID → 账号。**刻意不放进** :meth:`Participant.to_dict`：
        #: 那份 dict 会被广播给全教室，而名单是全班的学号，没必要人手一份。
        #: 主机界面与档案走 :meth:`account_of` / :meth:`archive_record` 拿。
        self.bindings: dict[str, str] = {}

    # ---- 房间信息 -----------------------------------------------------------

    def rename(self, title: str) -> str:
        """改房间名，返回实际生效的名字。留空则回落到 :data:`DEFAULT_TITLE`。

        **绝对不要动 :attr:`room_id`。** 它是 ``sha256(房间号[:口令])[:16]``，
        与 title 无关 —— 正因为无关，改名只是换一个显示标签，已经连上的学生
        不受任何影响。哪一天有人"顺手"把 title 拼进 :func:`room_secret`，
        改一次名字就会让全教室同时掉线，而且现象是"改名之后就连不上了"，
        跟标题看起来毫不相干，极难查。
        """
        cleaned = (title or "").strip()
        self.title = cleaned or DEFAULT_TITLE
        return self.title

    # ---- 时间与状态 ---------------------------------------------------------

    @property
    def secret(self) -> str:
        """房间号与口令拼成的共享秘密，两端据此派生会话密钥。

        **它绝不上线。** 线上只走 :attr:`fingerprint`。放在这里而不是各自拼，
        是因为两端一旦拼法不一致，现象是"密码明明对却连不上"，极难查。
        """
        return room_secret(self.room_code, self.password)

    @property
    def fingerprint(self) -> str:
        """房间秘密的单向索引，握手时明文上线的是它。"""
        return room_id(self.room_code, self.password)

    # ---- 握手凭据 -----------------------------------------------------------
    #
    # 两种进场方式在这里合流。``server.py`` 的握手只调这两个方法，
    # 它自己不需要知道当前是哪一种 —— 想知道什么就问会话。
    #
    # 为什么不给"客户端在 HELLO 里报告自己走哪一种"留位置：那一刻主机还没读到
    # 学生自报的任何东西，如果由客户端决定主机用哪套校验，就等于没有校验。

    def credential_index(self) -> dict[str, str]:
        """索引 → 账号。**只在账号进场时非空。**

        每次现算而不是缓存：一份 200 人的名单也就 200 次 sha256（微秒级），
        而缓存一旦和 ``password`` / 名单的改动脱节，现象就是"改了全场口令之后
        一部分人进不来了"，还是那个最难查的类。现算不可能过期。
        """
        if self.entry_mode is not EntryMode.ACCOUNT:
            return {}
        return {
            credential_id(entry.get("account", ""),
                          entry.get("passcode", ""), self.password): key
            for key, entry in self.contestants.items()
        }

    def resolve(self, index: str) -> dict[str, Any] | None:
        """握手拿到的那串索引 → ``{"account": ..., "secret": ...}``；查不到给 ``None``。

        返回的是**共享秘密**（而不是账号）是因为调用方紧接着就要拿它派生会话密钥 ——
        少一次转换，也就少一处"拿错了字段"的机会。

        房间号进场时它退化成 :attr:`secret` 本身：索引对不上就给 ``None``，
        对得上就把房间秘密交出去。于是"报房间号"和"报账号"在握手代码里
        长得一模一样，只有这里一处分叉。
        """
        wanted = str(index or "")
        if self.entry_mode is EntryMode.ACCOUNT:
            key = self.credential_index().get(wanted)
            if key is None:
                return None
            entry = self.contestants.get(key) or {}
            return {
                "account": key,
                "secret": credential_secret(entry.get("account", key),
                                            entry.get("passcode", ""),
                                            self.password),
            }
        if wanted == self.fingerprint:
            return {"account": "", "secret": self.secret}
        return None

    def credential_of(self, index: str) -> str | None:
        """只要秘密时的便利入口（等价于 ``resolve(index)["secret"]``）。"""
        found = self.resolve(index)
        return found["secret"] if found else None

    @property
    def timed(self) -> bool:
        return self.ends_at is not None

    def state(self, now: datetime | None = None) -> ExamState:
        moment = now or _now()
        if moment < self.starts_at:
            return ExamState.PENDING
        if self.ends_at is None or moment <= self.ends_at:
            return ExamState.RUNNING
        return ExamState.ENDED

    def remaining_seconds(self, now: datetime | None = None) -> int | None:
        """剩余秒数；**不限时返回 ``None``**（调用方据此显示"不限时"）。"""
        moment = now or _now()
        if moment < self.starts_at:
            return int((self.starts_at - moment).total_seconds())
        if self.ends_at is None:
            return None
        return max(0, int((self.ends_at - moment).total_seconds()))

    def start_now(self, now: datetime | None = None) -> bool:
        """提前开考：把开考时刻挪到现在。返回是否真的改动过。

        **时长跟着一起挪。** 一场 60 分钟的测验提前 10 分钟开考，结束时刻也提前
        10 分钟 —— 老师想的是"早开始早结束"，不是"早开始、结束时刻不动"。
        已经开考或已结束的房间不动，返回 ``False``（调用方据此决定要不要广播）。
        """
        moment = now or _now()
        if self.state(moment) is not ExamState.PENDING:
            return False
        self.starts_at = moment
        if self.duration_minutes:
            self.ends_at = moment + timedelta(minutes=self.duration_minutes)
        return True

    def accepts_submissions(self, now: datetime | None = None) -> bool:
        """是否还接受提交。宽限期内也算接受。"""
        moment = now or _now()
        if moment < self.starts_at:
            return False
        if self.ends_at is None:
            return True
        return moment <= self.ends_at + timedelta(seconds=self.grace_seconds)

    def closing_soon(self, seconds: int = 30, now: datetime | None = None) -> bool:
        """是否已进入"即将收卷"的窗口（客户端据此提醒学生）。

        **备考阶段一律为 ``False``。** 那时 :meth:`remaining_seconds` 给的是
        "距开考还有多久"，拿它去比"快收卷了"的 30 秒阈值，会在开考前 30 秒
        报一次根本不存在的"即将收卷"。
        """
        moment = now or _now()
        if self.state(moment) is not ExamState.RUNNING:
            return False
        remaining = self.remaining_seconds(moment)
        return remaining is not None and remaining <= seconds

    # ---- 题目 ---------------------------------------------------------------

    def set_problems(self, problems: Iterable[ExamProblemView]) -> None:
        self.problems = list(problems)

    def problem(self, problem_id: str) -> ExamProblemView | None:
        return next((p for p in self.problems if p.id == problem_id), None)

    @property
    def max_total_score(self) -> int:
        """本场满分 = 各题满分之和。

        榜上写"总分 240"而不给分母，读不出离满分还有多远；而每题满分现在
        由测试点分值决定（不再是恒定的 100），所以必须算出来而不是乘一下。
        """
        return sum(item.points for item in self.problems)

    # ---- 选手名单与绑定 -----------------------------------------------------
    #
    # 名单的**文件**形态在 ``core/roster.py``（导入 CSV、落盘 JSON）。
    # 这里只拿它的一份内存副本，用 dict 对接，于是本模块不必 import ``core/``。

    def set_contestants(self, rows: Iterable[dict[str, Any]]) -> int:
        """装入本场名单，返回实际收下的条数。

        收下的规矩（都是"宁可少收一条，也不要在场上出现两个看起来一样的人"）：

        * 账号先归一化再当键 —— ``ZhangSan`` 与 ``zhangsan`` 是同一个人；
        * 空账号丢掉（CSV 里拖出来的一行空行）；
        * 归一化之后撞号的，**第一条留下**，后面的丢掉并记日志。
          当场抛异常会让老师连房间都开不起来，而他此刻正站在讲台上。
        """
        cleaned: dict[str, dict[str, Any]] = {}
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            account = normalize_account(row.get("account", ""))
            if not account:
                continue
            if account in cleaned:
                log.warning("名单里有归一化之后重复的账号，已丢掉后一条: %s", account)
                continue
            cleaned[account] = {
                "account": account,
                "name": " ".join(str(row.get("name", "") or "").split()),
                "passcode": normalize_passcode(row.get("passcode", "")),
                "seat": str(row.get("seat", "") or "").strip(),
            }
        self.contestants = cleaned
        return len(cleaned)

    def contestant(self, account: str) -> dict[str, Any] | None:
        return self.contestants.get(normalize_account(account))

    def account_of(self, device_id: str) -> str:
        """这台机器这一场是谁在用。没绑定过就返回空串。

        **不放进** ``Participant.to_dict()``：那份 dict 会随 ``summary()``
        广播给全教室，而名单是全班的学号。要用它的只有主机界面和档案。
        """
        return self.bindings.get(normalize_device_id(device_id), "")

    def device_of(self, account: str) -> str:
        """反过来：这个账号这一场绑在哪台机器上。"""
        key = normalize_account(account)
        for device, bound in self.bindings.items():
            if bound == key:
                return device
        return ""

    def authenticate(self, account: str, device_id: str) -> Participant:
        """账号进场：把人**认成名单上的那一行**，而不是他自己报的名字。

        口令不在这里比 —— 它在握手第一步就用掉了：主机是按"账号+口令"算出的
        索引反查到这一行的，算得出这个索引的人必然知道口令。再比一次
        只是把同一件事做第二遍，还会多出一处"两套判定"的不一致源。

        这里管的是两件握手管不了的事：

        * 这个账号本场是不是**已经绑在另一台机器**上（两个人用同一个账号
          同时做题，榜上会出现两行一模一样的名字，而老师根本分不出谁是谁）；
        * 这台机器本场是不是**已经用别的账号**进过场（换个人接着做）。

        同一个设备用同一个账号重连（网卡松动、合盖、切 Wi-Fi）永远放行 ——
        那是最常见的正常路径，绝不能误伤。
        """
        key = normalize_account(account)
        entry = self.contestants.get(key)
        if entry is None:
            raise JoinError("账号不在本场名单里，请向老师确认账号与密码")

        device = normalize_device_id(device_id)
        bound_account = self.bindings.get(device, "")
        if bound_account and bound_account != key:
            other = self.contestants.get(bound_account, {})
            raise JoinError(
                f"这台机器已用「{other.get('name') or bound_account}」的账号进过场，"
                "换人请先让老师解绑")

        owner = self.device_of(key)
        if owner and owner != device:
            raise JoinError("这个账号已经在另一台机器上登录，请让老师解绑")

        participant = self.join(device, entry.get("name") or key)
        self.bindings[device] = key
        return participant

    def unbind_account(self, account: str) -> bool:
        """解绑一个账号（老师用）。返回是否真的解掉了一条。

        同时也把人从参与者里**摘掉**：绑定和参与者是同一件事的两面，
        只解一半会留下"榜上还挂着他、但他再也进不来"的状态。
        已经交过的卷子不动 —— 那是既成事实。
        """
        device = self.device_of(account)
        if not device:
            return False
        self.bindings.pop(device, None)
        self.participants.pop(device, None)
        return True

    def unbind_device(self, device_id: str) -> bool:
        return bool(self.bindings.pop(normalize_device_id(device_id), None))

    # ---- 参与者 -------------------------------------------------------------

    def join(self, device_id: str, username: str) -> Participant:
        """登记一位参与者；同一设备 ID 再次进来就是断线重连。

        重连时**名字以第一次为准**（返回已存的参与者，忽略这次传上来的名字）。
        理由：老师是照名字点名的，中途悄悄改名比一个错别字更麻烦。
        """
        cleaned = normalize_device_id(device_id)
        name = " ".join((username or "").split())
        if not cleaned:
            raise JoinError("设备 ID 不能为空")
        if any(char not in DEVICE_ID_ALPHABET for char in cleaned):
            raise JoinError("设备 ID 含有非法字符，请让程序自动生成")
        if len(cleaned) != DEVICE_ID_LENGTH:
            raise JoinError(f"设备 ID 必须是 {DEVICE_ID_LENGTH} 位，"
                            f"收到 {len(cleaned)} 位")
        if not name:
            raise JoinError("用户名不能为空")
        if len(name) > MAX_USERNAME_LENGTH:
            raise JoinError(f"用户名最长 {MAX_USERNAME_LENGTH} 个字符")

        existing = self.participants.get(cleaned)
        if existing is not None:
            existing.touch()
            return existing

        participant = Participant(device_id=cleaned, username=name)
        self.participants[cleaned] = participant
        return participant

    def participant(self, device_id: str) -> Participant | None:
        return self.participants.get(normalize_device_id(device_id))

    def note_replacement(self, device_id: str, address: str = "") -> None:
        """记一笔"同一设备 ID 又连上来了"（旧连接已被顶掉）。"""
        self.replacements.append({
            "device_id": normalize_device_id(device_id), "address": address,
            "at": _iso(_now()),
        })

    def mark_connected(self, device_id: str, address: str = "") -> None:
        participant = self.participant(device_id)
        if participant is not None:
            participant.connected = True
            participant.touch(address)

    def mark_disconnected(self, device_id: str) -> None:
        participant = self.participant(device_id)
        if participant is not None:
            participant.connected = False

    def has_device(self, device_id: str) -> bool:
        return normalize_device_id(device_id) in self.participants

    # ---- 离场锁屏（防窥屏） -------------------------------------------------
    #
    # 场景：学生举手去上厕所，屏幕上还摊着题面和代码。走过路过的、隔壁座位
    # 的都能顺手看两眼。于是学生按一下「离开一下」，整块界面被盖住，
    # 回来要输口令才继续。老师也能远程对某个人（或全体）按下去。
    #
    # 它防的是**人的眼睛**，不是截屏、不是拷贝 —— 那两件事各有各的开关
    # （``ExamPolicy.allow_copy_out`` 与 Windows 的窗口显示亲和性），
    # 混在一起谈只会让人以为关了这个就等于关了那个。

    def set_locked(self, device_id: str, locked: bool = True) -> Participant | None:
        """把某人置为"离开中" / 解除。**主机调用时不需要口令** —— 老师是监考者，
        他手上就有名单，没有什么需要向他证明的。

        解除时顺手把输错计数清零：老师放行之后，学生重新获得完整的
        :data:`MAX_UNLOCK_ATTEMPTS` 次机会。
        """
        participant = self.participant(device_id)
        if participant is None:
            return None
        participant.locked = bool(locked)
        participant.locked_at = _now() if locked else None
        if not locked:
            participant.unlock_failures = 0
        return participant

    def set_locked_all(self, locked: bool = True) -> list[str]:
        """对全体下发（老师站在门口喊一声"都盖一下"时用）。返回受影响的设备 ID。"""
        touched: list[str] = []
        for device, participant in self.participants.items():
            participant.locked = bool(locked)
            participant.locked_at = _now() if locked else None
            if not locked:
                participant.unlock_failures = 0
            touched.append(device)
        return touched

    def locked_devices(self) -> list[str]:
        return [device for device, item in self.participants.items() if item.locked]

    def passcodes_for(self, device_id: str) -> list[str]:
        """某人可以用哪些口令解锁。**空列表 = 本场没有可用的解锁口令。**

        两种都收：**个人口令**（账号进场时名单上那个）与**全场口令**
        （``self.password``）。双因子那档本该两个都对，但解锁这件事挡的是
        隔壁同学的眼睛、不是本人，为了少一次"我两个都打了还是解不开"的
        现场耽误，认任一即可。
        """
        codes: list[str] = []
        account = self.account_of(device_id)
        if account:
            entry = self.contestants.get(account) or {}
            personal = normalize_passcode(entry.get("passcode", ""))
            if personal:
                codes.append(personal)
        if self.password:
            codes.append(self.password)
        return codes

    def verify_unlock(self, device_id: str, passcode: str) -> tuple[bool, str]:
        """校验一次解锁口令，返回 ``(是否放行, 给用户看的话)``。

        **必须在主机校验。** 学生机是可以被改的（改 exe、改内存、直接发包），
        本地比对等于没比对 —— 这与"提交策略的真正边界在服务端"是同一条原则。

        比对口令用 :func:`hmac.compare_digest`：逐字符短路比较会让"前几位对"
        的猜测快一点点，日积月累就是一个可用的侧信道。这里几乎不花什么代价。

        错够 :data:`MAX_UNLOCK_ATTEMPTS` 次就不再受理，只能等老师在主机端
        点解绑/放行 —— 否则一台学生机可以把这里当成在线爆破接口。
        """
        participant = self.participant(device_id)
        if participant is None:
            return False, "主机不认识这台机器，请重新进场"
        if not participant.locked:
            # 没锁就等于已经开着。重复解锁不是错误，别让人以为被拒绝了。
            return True, ""
        if participant.unlock_failures >= MAX_UNLOCK_ATTEMPTS:
            return False, "解锁次数用完了，请举手让老师放行"

        allowed = self.passcodes_for(device_id)
        if not allowed:
            return False, "本场没有设置解锁口令，请举手让老师放行"

        typed = normalize_passcode(passcode)
        if typed and any(hmac.compare_digest(typed, code) for code in allowed):
            participant.locked = False
            participant.locked_at = None
            participant.unlock_failures = 0
            return True, ""

        participant.unlock_failures += 1
        left = MAX_UNLOCK_ATTEMPTS - participant.unlock_failures
        if left <= 0:
            return False, "口令不对，解锁次数已用完，请举手让老师放行"
        return False, f"口令不对，还可以试 {left} 次"

    # ---- 提交 ---------------------------------------------------------------

    def next_serial(self) -> int:
        self._serial += 1
        return self._serial

    def attempts_for(self, device_id: str, problem_id: str) -> int:
        """某人在某题上已经提交过的次数。"""
        cleaned = normalize_device_id(device_id)
        return sum(1 for item in self.submissions
                   if item.device_id == cleaned and item.problem_id == problem_id)

    def can_submit(self, device_id: str, problem_id: str) -> tuple[bool, str]:
        """这次提交收不收。返回 ``(可否, 拒绝原因)``。"""
        if not self.allow_resubmit and self.attempts_for(device_id, problem_id):
            return False, "本题已设置为不允许重复提交，你已经提交过了"
        return True, ""

    def new_submission(self, device_id: str, username: str, problem_id: str,
                       language: str, code: str, *, forced: bool = False) -> Submission:
        """构造一条提交记录。

        次数在这里算，而不是让调用方自己数 —— 少一处能与真相脱节的地方。
        """
        cleaned = normalize_device_id(device_id)
        return Submission(
            serial=self.next_serial(),
            device_id=cleaned,
            username=username,
            problem_id=problem_id,
            language=language,
            code=code,
            attempt=self.attempts_for(cleaned, problem_id) + 1,
            forced=forced,
        )

    def record_submission(self, submission: Submission) -> None:
        self.submissions.append(submission)

    def submissions_of(self, device_id: str,
                       problem_id: str | None = None) -> list[Submission]:
        cleaned = normalize_device_id(device_id)
        return [item for item in self.submissions
                if item.device_id == cleaned
                and (problem_id is None or item.problem_id == problem_id)]

    # ---- 排名 ---------------------------------------------------------------

    def best_per_problem(self) -> dict[str, dict[str, Submission]]:
        """每人每题的最高分提交，键是设备 ID。

        NOI 也是这个规矩：同一题可以反复交，**取最高分**，交几次都不罚。

        同分时取**最快**的那次。这条只决定榜上那一行显示的是哪一次的耗时与
        内存，**不影响名次** —— 名次只按分数并列（见 :func:`competition_ranks`）。
        """
        best: dict[str, dict[str, Submission]] = {}
        for submission in self.submissions:
            if submission.total <= 0:
                continue      # 还没判完（或没有测试点）的不参与排名
            slot = best.setdefault(submission.device_id, {})
            current = slot.get(submission.problem_id)
            if current is None or self._better(submission, current):
                slot[submission.problem_id] = submission
        return best

    @staticmethod
    def _better(candidate: Submission, current: Submission) -> bool:
        if candidate.score != current.score:
            return candidate.score > current.score
        if candidate.time_ms != current.time_ms:
            return candidate.time_ms < current.time_ms
        if candidate.memory_mb != current.memory_mb:
            return candidate.memory_mb < current.memory_mb
        return candidate.submitted_at < current.submitted_at

    def problem_ranking(self, problem_id: str,
                        limit: int | None = None) -> list[ProblemRanking]:
        """单题排名：**同分并列**（见 :func:`competition_ranks`）。

        同分者之间仍按耗时 → 内存 → 提交时间排，但那只决定**谁显示在前面**，
        名次是同一个 —— 原需求要的"分以及时间空间排名"看的就是这几列。
        """
        best = self.best_per_problem()
        rows: list[ProblemRanking] = []
        for per_problem in best.values():
            submission = per_problem.get(problem_id)
            if submission is not None:
                rows.append(ProblemRanking(rank=0, submission=submission))
        rows.sort(key=lambda row: row.sort_key)
        for row, rank in zip(rows, competition_ranks(
                [row.submission.score for row in rows])):
            row.rank = rank
        return rows[:limit] if limit else rows

    def overall_ranking(self, limit: int | None = None) -> list[RankingRow]:
        """总分排名：**只按总分，同分并列**（NOI 式）。

        每个参与者都会出现在表里，哪怕一分未得 —— 交了白卷也是信息，
        "榜上无名"和"考了 0 分"在现场是完全不同的两件事。

        耗时 / 内存 / 最后提交只用来决定**同分者的显示先后**（让每次刷新的
        顺序稳定），它们不参与名次，理由见 :func:`competition_ranks`。
        """
        best = self.best_per_problem()
        rows: list[RankingRow] = []
        for device_id, participant in self.participants.items():
            per_problem = best.get(device_id, {})
            row = RankingRow(device_id=device_id, username=participant.username)
            row.submit_count = len(self.submissions_of(device_id))
            for problem_id, submission in per_problem.items():
                row.per_problem[problem_id] = submission.score
                row.per_problem_attempts[problem_id] = submission.attempt
                row.score += submission.score
                row.total_time_ms += submission.time_ms
                row.total_memory_mb += submission.memory_mb
                if submission.perfect:
                    row.solved += 1
                if (row.last_submit_at is None
                        or submission.submitted_at > row.last_submit_at):
                    row.last_submit_at = submission.submitted_at
            rows.append(row)

        rows.sort(key=lambda row: (-row.score, row.total_time_ms,
                                   row.total_memory_mb,
                                   row.last_submit_at or datetime.max,
                                   row.device_id))
        for row, rank in zip(rows, competition_ranks([row.score for row in rows])):
            row.rank = rank
        return rows[:limit] if limit else rows

    # ---- 榜单可见性 ---------------------------------------------------------

    def leaderboard_visible(self, now: datetime | None = None) -> bool:
        """榜单现在能不能公开。

        头一条最要紧：**本场压根不放榜时，任何时刻都不公开** ——
        ``show_leaderboard`` 与 :class:`RoomMode` 正交，关掉它压过下面所有规则
        （连练习模式的"实时"也不例外），所以"考试 + 全程不放榜"才表达得出。

        放榜时再看模式：

        * **练习模式**全程实时 —— 这是"练习模式实时更新"这条需求的落点；
        * **考试模式**一律等到测验结束（含收卷宽限期）之后统一放榜。
          宽限期没走完就放榜，会让刚过截止还在补交的人看到自己没被算进去。
        """
        if not self.show_leaderboard:
            return False
        moment = now or _now()
        if self.mode is RoomMode.PRACTICE:
            return True
        return (self.state(moment) is ExamState.ENDED
                and not self.accepts_submissions(moment))

    def my_row(self, device_id: str) -> dict[str, Any] | None:
        """某台设备自己的那一行。封榜期间也给他自己看 —— 那是他自己的数据。"""
        cleaned = normalize_device_id(device_id)
        for row in self.overall_ranking():
            if row.device_id == cleaned:
                return row.to_dict()
        return None

    # ---- 持久化 -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """广播给客户端的状态摘要。

        **房间号与口令绝不进这里。** 这个字典会原样发给每一个客户端；
        房间号是凭据，虽然学生本来就知道，但没有任何理由把它回述到线上。
        """
        now = _now()
        state = self.state(now)
        return {
            "session_id": self.session_id,
            "title": self.title,
            "mode": self.mode.value,
            "mode_label": self.mode.label,
            "state": state.value,
            "state_label": state.label,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "timed": self.timed,
            "remaining_seconds": self.remaining_seconds(now),
            "duration_minutes": self.duration_minutes,
            "force_collect": self.force_collect,
            "allow_resubmit": self.allow_resubmit,
            "show_leaderboard": self.show_leaderboard,
            # 默认策略序列化成 {} —— 不带限制时载荷与旧版本逐字节一致
            "policy": self.policy.to_dict(),
            # 进场方式。学生端据此知道"这个房间认账号还是认房间号"，
            # **与 password_required 是两件事**：后者说的是"要不要全场口令"。
            "entry_mode": self.entry_mode.value,
            "entry_mode_label": self.entry_mode.label,
            "password_required": bool(self.password),
            "leaderboard_published": self.leaderboard_visible(now),
            "server_time": _iso(now),
            "problems": [item.to_dict() for item in self.problems],
            "participants": [item.to_dict()
                             for item in self.participants.values()],
            "submission_count": len(self.submissions),
        }

    def leaderboard_payload(self, *, viewer: str = "",
                            per_problem_limit: int = 50) -> dict[str, Any]:
        """榜单载荷。

        :param viewer: 请求者的设备 ID。**封榜期间**用它把"你自己那一行"单独带上 ——
            考试模式不公开别人的成绩，但不该连自己的排名都看不到。

        封榜时 ``overall`` / ``per_problem`` 是空的，``published`` 为 ``False``，
        客户端据 ``withheld_reason`` 显示占位说明而不是空表。
        """
        published = self.leaderboard_visible()
        payload: dict[str, Any] = {
            "kind": "leaderboard",
            "published": published,
            "mode": self.mode.value,
            "mode_label": self.mode.label,
            # 本场满分（每题满分之和）。学生端要拿它写"总分 240/300"——
            # 光看"240 分"读不出离满分还有多远。
            "max_total_score": self.max_total_score,
            "overall": ([row.to_dict() for row in self.overall_ranking()]
                        if published else []),
            "per_problem": ({
                problem.id: [row.to_dict()
                             for row in self.problem_ranking(problem.id,
                                                             per_problem_limit)]
                for problem in self.problems
            } if published else {}),
            "server_time": _iso(_now()),
        }
        if not published:
            # 三种"看不到榜"是三种不同的现场情况，提示语不能混用：
            # 说"之后再放"却根本不打算放，会让学生一直等一个不会来的榜单。
            if not self.show_leaderboard:
                payload["withheld_reason"] = "本场不公布榜单"
            elif self.mode is RoomMode.EXAM:
                payload["withheld_reason"] = "考试模式下成绩将在测验结束后统一公布"
            else:
                payload["withheld_reason"] = "榜单暂不可见"
        if viewer:
            payload["myself"] = self.my_row(viewer)
        return payload

    # ---- 测验档案 -----------------------------------------------------------
    #
    # 这三个方法只吐 dict，不 import ``core.records`` —— 本模块到目前为止只依赖
    # 标准库，那是它作为"纯逻辑层"的分层属性（见 tests/test_net_*），值得保住。
    # 落盘由 ``core/records.py`` 负责，两边靠这些 dict 对接。

    def archive_record(self) -> dict[str, Any]:
        """这一场的元数据（**不含源码**，源码在各条提交里）。

        ``room_id`` 存的是 :attr:`fingerprint`（单向哈希）而不是房间号：
        老师认档案靠标题和时间，把凭据撒进文件名与档案里没有收益。

        **账号进档案，但不进 ``summary()``。** 档案是老师一个人的东西
        （写在 ``%LOCALAPPDATA%\\OfflineOJ\\exams\\`` 里），成绩单上要按学号列人；
        ``summary()`` 是广播给全教室的，名单散出去对谁都没好处。所以
        ``account`` 在这里**并进每条参与者**，而不是长在
        :meth:`Participant.to_dict` 上。
        """
        return {
            "session_id": self.session_id,
            "title": self.title,
            "mode": self.mode.value,
            "entry_mode": self.entry_mode.value,
            "room_id": self.fingerprint,
            "started_at": _iso(self.starts_at),
            "ended_at": _iso(self.ends_at),
            "duration_minutes": self.duration_minutes,
            "grace_seconds": self.grace_seconds,
            "force_collect": self.force_collect,
            "allow_resubmit": self.allow_resubmit,
            "show_leaderboard": self.show_leaderboard,
            "policy": self.policy.to_dict(),
            "problems": [item.to_dict() for item in self.problems],
            "participants": [self._archive_participant(item)
                             for item in self.participants.values()],
            "max_total_score": self.max_total_score,
            "submission_count": len(self.submissions),
        }

    def _archive_participant(self, participant: Participant) -> dict[str, Any]:
        """参与者 + 他这一场用的账号。没有账号（房间号进场）时字段就不出现。"""
        data = participant.to_dict()
        account = self.bindings.get(participant.device_id, "")
        if account:
            data["account"] = account
        return data

    def archive_leaderboard(self, per_problem_limit: int = 200) -> dict[str, Any]:
        """收卷时那份**完整**榜单，不受"放不放榜"影响。

        档案记的是事实。:meth:`leaderboard_payload` 会按 ``show_leaderboard``
        与 ``RoomMode`` 把 ``overall`` / ``per_problem`` 清空 —— 那是给客户端看的
        视图，不是给历史记录看的。拿它去存档，老师会得到一份
        "当年没放榜，所以档案里连成绩都没有"的空壳。
        """
        return {
            "max_total_score": self.max_total_score,
            "overall": [row.to_dict() for row in self.overall_ranking()],
            "per_problem": {
                problem.id: [row.to_dict()
                             for row in self.problem_ranking(problem.id,
                                                             per_problem_limit)]
                for problem in self.problems
            },
            "snapshot_at": _iso(_now()),
        }

    def archive_submissions(self, *, keep_code: bool = True) -> list[dict[str, Any]]:
        """档案里的提交列表。

        ``keep_code=False`` 时**逐条抠掉源码**而不是"不读它" —— 老师选了
        "只存成绩"，落盘的字节里就不该有学生代码，包括将来可能被加上的
        其它导出路径。抠在这里，导出只是照着已经抠干净的数据走。
        """
        rows: list[dict[str, Any]] = []
        for item in self.submissions:
            data = item.to_archive_dict()
            if not keep_code:
                data.pop("code", None)
            rows.append(data)
        return rows
