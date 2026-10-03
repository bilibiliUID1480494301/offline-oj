"""客户端：用房间号加入、取题面、提交源码、接收判定与排名。

两种用法
--------
1. **同步**（命令行工具、测试）：不启动后台线程，自己调 :meth:`recv` /
   :meth:`wait_for` 按需取消息；
2. **异步**（界面）：调 :meth:`start_listener` 起一条读取线程，所有推送
   通过 ``on_message`` 回调转交 —— 界面线程只负责更新控件，不碰套接字。

两种方式**不能同时用**：读取线程一旦起来，套接字就归它独占，
调用方再自己去 ``recv`` 只会和它抢数据。所以 :meth:`start_listener` 里
做了显式拦截，而不是让使用者自己去记住这条规则。

同步等待**一律有时间上限**（:data:`DEFAULT_READ_TIMEOUT` /
:data:`DEFAULT_VERDICT_TIMEOUT`）。握手之后套接字是阻塞无超时的 —— 长连接
本该如此 —— 但对"一问一答"的同步用法来说，无上限就意味着对端一不说话就
永久卡死。卡死是最难查的一类故障：在现象上它和"跑得慢"没有区别。

进场的三样东西
--------------
* **房间号**：老师报的那 6 位数字，就是唯一凭据。它本身不发上网，
  线上只走它的单向索引；
* **设备八位 ID**：本机身份。由 :mod:`offline_oj.net.identity` 生成并持久化，
  跨场次不变 —— 学生认得出自己那一行，老师念 ID 点名也稳定；
* **用户名**：显示用的标签，允许与别人重名，靠设备 ID 区分。

账号进场时上面三样里只留**设备 ID**：学生报**账号 + 个人口令**，
线上走的是它们的单向索引（:func:`offline_oj.credentials.credential_id`），
**用户名由主机按名单回填** —— 学生报什么都不算数。房间号此时不必知道，
``username`` 也可以留空。两种方式在 :meth:`ExamClient.connect` 里只有一处分叉，
其余（CHALLENGE、AUTH、WELCOME、断线重连）完全共用。
"""

from __future__ import annotations

import logging
import socket
import threading
from datetime import datetime
from time import monotonic
from typing import Any, Callable

from ..credentials import credential_id, credential_secret, normalize_account
from . import crypto, protocol
from .session import (DEVICE_ID_LENGTH, EntryMode, ExamProblemView,
                      normalize_device_id, normalize_room_code, room_id,
                      room_secret)

log = logging.getLogger(__name__)

__all__ = ["ExamClient", "ConnectError", "HandshakeRejected", "PROTOCOL_VERSION"]

#: 协议版本，与主机端一致
PROTOCOL_VERSION = 3

#: 握手阶段的超时（秒）。scrypt 要跑 350ms，加上网络，给足 15 秒。
CONNECT_TIMEOUT = 15.0

#: 同步等待的默认上限（秒）。**必须有个上限**：握手之后套接字是阻塞无超时的，
#: 对端要是一直不回，表现出来就是"程序卡死"。卡死是最难查的一类故障 ——
#: 测试跑一整晚不结束、命令行工具挂在那里没有任何输出，都比直接报错难定位得多。
#: 主机每 5 秒会推一次状态、每 10 秒推一次榜单，所以健康连接远不会碰到这个上限。
DEFAULT_READ_TIMEOUT = 60.0

#: 等一次评测结果的上限。判题要编译要跑测试点，给得比普通等待宽。
DEFAULT_VERDICT_TIMEOUT = 180.0


class ConnectError(Exception):
    """连不上主机：地址不对、端口不通、被防火墙挡了。"""


class HandshakeRejected(protocol.HandshakeRejected):
    """主机明确拒绝（房间号错、用户名不合法、版本不匹配）。文案直接给用户看。"""


class ExamClient:
    """一个连接到测验房间的客户端。"""

    def __init__(
        self,
        host: str,
        port: int,
        room_code: str = "",
        device_id: str = "",
        username: str = "",
        *,
        password: str = "",
        account: str = "",
        passcode: str = "",
        on_message: Callable[[str, dict[str, Any]], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.room_code = normalize_room_code(room_code)
        #: **全场口令**（房间号进场时它就是"房间口令"）。默认空。
        #: **不参与任何日志输出**
        self.password = password or ""
        #: 账号进场的账号；留空即为房间号进场
        self.account = normalize_account(account)
        #: 账号进场的**个人口令**。房间号进场时它没有意义，保持空
        self.passcode = passcode or ""
        self.device_id = normalize_device_id(device_id)
        self.username = username.strip()
        self.timeout = timeout
        self._on_message = on_message
        self._on_disconnect = on_disconnect

        self.channel: protocol.SecureChannel | None = None
        self.welcome: dict[str, Any] = {}
        self.problems: list[ExamProblemView] = []
        self.exam: dict[str, Any] = {}
        self.leaderboard: dict[str, Any] = {}
        self.verdicts: list[dict[str, Any]] = []
        #: 最近一次收卷指令（``None`` 表示还没收到）
        self.collect_request: dict[str, Any] | None = None
        #: 主机说的进场方式（CHALLENGE 帧里带回来的）。学生选错了页签时，
        #: 界面据此纠正提示语，而不是让他对着错误的输入框一直试。
        self.entry_mode: str = (EntryMode.ACCOUNT.value if self.account
                                else EntryMode.ROOM_CODE.value)
        #: 主机下发的离场锁屏状态。**只是显示状态** —— 能不能解开由主机说了算。
        self.locked = False
        self.lock_reason = ""
        #: 最近一次解锁请求的结果（``None`` 表示还没请求过）
        self.unlock_reply: dict[str, Any] | None = None

        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._listener: threading.Thread | None = None
        self._closed = threading.Event()

    # ------------------------------------------------------------------
    # 连接与握手
    # ------------------------------------------------------------------

    @property
    def using_account(self) -> bool:
        """这次是账号进场还是房间号进场。有账号就是账号进场。"""
        return bool(self.account)

    def handshake_index(self) -> str:
        """明文 HELLO 里上线的那一串索引。**两种方式在这里分叉，只此一处。**

        不管哪条路径，上去的都是单向索引 —— 房间号与账号都不明文上线，
        同网段抓一次包拿不到任何能直接用的凭据。
        """
        if self.using_account:
            return credential_id(self.account, self.passcode, self.password)
        return room_id(self.room_code, self.password)

    def handshake_secret(self) -> str:
        """派生会话密钥用的共享秘密。与 :meth:`handshake_index` 一一对应。"""
        if self.using_account:
            return credential_secret(self.account, self.passcode, self.password)
        return room_secret(self.room_code, self.password)

    def connect(self) -> dict[str, Any]:
        """完成握手，返回 WELCOME 里的内容。失败抛异常。"""
        if self.using_account:
            # 账号进场：用户名不必填（主机按名单回填），房间号也不必知道
            if len(self.device_id) != DEVICE_ID_LENGTH:
                raise HandshakeRejected(f"设备 ID 必须是 {DEVICE_ID_LENGTH} 位")
        else:
            if not self.room_code:
                raise HandshakeRejected("请先填写房间号")
            if len(self.device_id) != DEVICE_ID_LENGTH:
                raise HandshakeRejected(f"设备 ID 必须是 {DEVICE_ID_LENGTH} 位")
            if not self.username:
                raise HandshakeRejected("请先填写用户名")

        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.timeout)
        except OSError as exc:
            raise ConnectError(
                f"连不上 {self.host}:{self.port} —— {exc}。"
                "请确认老师已经开始测验、地址与端口正确、"
                "且两台机器在同一局域网内。"
            ) from exc
        self._socket = sock
        sock.settimeout(self.timeout)

        try:
            # 临时密钥对：X25519 握手用，握完即弃（前向保密）。即使最终协商到
            # scrypt 套件，这只公钥也只是多带一个无害字段，密钥派生仍走 salt。
            client_priv, client_pub = crypto.x25519_generate_keypair()
            plain = protocol.PlainChannel(sock)
            # 明文 HELLO 只有版本、凭据的单向索引与协商能力 —— 房间号/账号本身不上线
            plain.send({
                "kind": protocol.MessageKind.HELLO,
                "protocol_version": PROTOCOL_VERSION,
                "room_id": self.handshake_index(),
                "supported_suites": list(crypto.SUPPORTED_SUITES),
                "client_pub": client_pub.hex(),
            })
            challenge, _ = plain.recv()
            if challenge.get("kind") == protocol.MessageKind.REJECTED:
                # 主机顺带告诉我们"这个房间认什么"。学生选错了页签时，
                # 界面据此换提示语，而不是让他对着错误的输入框一直试。
                hint = str(challenge.get("entry_mode", "") or "")
                if hint:
                    self.entry_mode = hint
                raise HandshakeRejected(str(challenge.get("reason", "主机拒绝了连接")))
            if challenge.get("kind") != protocol.MessageKind.CHALLENGE:
                raise protocol.ProtocolError(
                    f"握手第二步期望 challenge，收到 {challenge.get('kind')}")

            if str(challenge.get("entry_mode", "") or ""):
                self.entry_mode = str(challenge["entry_mode"])

            # 服务器权威选定套件；客户端必须确认它在自己声明的能力内。
            chosen_suite = challenge.get("chosen_suite")
            server_pub_hex = challenge.get("server_pub")
            if (not chosen_suite or chosen_suite not in crypto.SUITES
                    or chosen_suite not in crypto.SUPPORTED_SUITES):
                raise protocol.ProtocolError(
                    f"服务端返回了无效或未协商的套件: {chosen_suite}")

            salt = bytes.fromhex(str(challenge.get("salt", "")))
            # 派生会话密钥：X25519 套件把房间口令绑进 HKDF 的 info，
            # 不知口令 → 密钥错 → AUTH 解密失败 → 恢复进场鉴权；
            # scrypt 套件仍走旧的 derive_secret_key(secret, salt)。
            if crypto.SUITES[chosen_suite].kex == "x25519":
                try:
                    server_pub = bytes.fromhex(str(server_pub_hex))
                except (ValueError, TypeError):
                    raise protocol.ProtocolError("server_pub 格式不对")
                key = crypto.kex_session_key(
                    chosen_suite, private_key=client_priv, peer_public=server_pub,
                    info=self.handshake_secret().encode("utf-8"))
            else:
                key = crypto.kex_session_key(chosen_suite,
                                             secret=self.handshake_secret(),
                                             salt=salt)
            channel = protocol.SecureChannel(sock, key,
                                             protocol.DIRECTION_CLIENT, chosen_suite)
            # 设备 ID 与用户名都放在加密帧里：主机不需要提前知道它们
            # 才能派生密钥，所以没理由让它们裸奔。
            # 账号进场时这个 username 是**空的**，主机不看它 —— 名字由名单定。
            channel.send({"kind": protocol.MessageKind.AUTH,
                          "device_id": self.device_id,
                          "username": self.username})
            reply, _ = channel.recv()
            if reply.get("kind") == protocol.MessageKind.REJECTED:
                raise HandshakeRejected(str(reply.get("reason", "主机拒绝了连接")))
            if reply.get("kind") != protocol.MessageKind.WELCOME:
                raise protocol.ProtocolError(
                    f"握手第三步期望 welcome，收到 {reply.get('kind')}")

            self.channel = channel
            self.welcome = reply
            self.exam = dict(reply.get("exam") or {})
            self.entry_mode = str(self.exam.get("entry_mode", self.entry_mode))
            # 名字以主机为准（重连时主机保留的是第一次的名字；账号进场时
            # 它是名单上那个 —— 学生报什么都没用）
            self.device_id = normalize_device_id(
                str(reply.get("device_id", self.device_id)))
            self.username = str(reply.get("username", self.username))
            # 重连时可能本来就被锁着（老师刚按过）—— 从快照里把它捡回来，
            # 免得学生看到一块没盖住的屏幕、以为锁没了
            self._sync_lock_from_exam()
            sock.settimeout(None)
            return reply
        except Exception:
            self.close()
            raise

    # ------------------------------------------------------------------
    # 读取线程
    # ------------------------------------------------------------------

    def start_listener(self) -> None:
        """起后台读取线程，把所有推送交给 ``on_message``。

        调用之后**不能再自己调 recv / wait_for** —— 数据归读取线程独占。
        """
        if self.channel is None:
            raise RuntimeError("还没有连接，先调 connect()")
        if self._listener is not None:
            raise RuntimeError("读取线程已经在跑了")
        self._listener = threading.Thread(target=self._listen_loop,
                                          name="oj-client-recv", daemon=True)
        self._listener.start()

    def _listen_loop(self) -> None:
        reason = "连接已关闭"
        try:
            while not self._closed.is_set() and self.channel is not None:
                kind, message, _ = self.channel.recv_kind()
                self._handle(kind, message)
        except protocol.ProtocolError as exc:
            reason = str(exc)
        except OSError as exc:
            reason = f"网络中断：{exc}"
        finally:
            self._closed.set()
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect(reason)
                except Exception:
                    log.debug("断线回调异常", exc_info=True)

    def _handle(self, kind: str, message: dict[str, Any]) -> None:
        """把内部状态更新与外部回调分开：回调抛异常不该影响状态维护。"""
        if kind == protocol.MessageKind.PROBLEMS:
            raw = message.get("problems") or []
            self.problems = [ExamProblemView.from_dict(item) for item in raw
                             if isinstance(item, dict)]
        elif kind == protocol.MessageKind.EXAM:
            self.exam = dict(message)
        elif kind == protocol.MessageKind.START:
            # 载荷与 EXAM 同构，就是状态的一次跳变（备考 → 进行中）。照收即可 ——
            # 界面从来只从 self.exam 读"能不能交"，多一个来源就会有两个真相。
            self.exam = dict(message)
        elif kind == protocol.MessageKind.LEADERBOARD:
            self.leaderboard = dict(message)
        elif kind == protocol.MessageKind.VERDICT:
            self.verdicts.append(dict(message))
        elif kind == protocol.MessageKind.COLLECT:
            self.collect_request = dict(message)
        elif kind == protocol.MessageKind.LOCK:
            # 主机远程下发的锁屏/解锁（也可能是老师对全班按的）
            self.locked = bool(message.get("locked", True))
            self.lock_reason = str(message.get("reason", "") or "")
        elif kind == protocol.MessageKind.UNLOCKED:
            self.unlock_reply = dict(message)
            if message.get("ok"):
                self.locked = False
                self.lock_reason = ""

        # 状态快照里也带着"我锁着没有"（``participants`` 里我自己那一行）。
        # 两条来源并存不是冗余：断线重连之后不会有 LOCK 帧补发，而快照每几秒
        # 就来一次 —— 少了它，重连的学生会看到一块没盖住的屏幕。
        #
        # **偏偏不含 LOCK 那一帧。** LOCK 是显式指令，它说什么就是什么；
        # 再拿手边的旧快照去"补正"，会把老师刚解除的锁又盖回去。
        if kind in (protocol.MessageKind.EXAM, protocol.MessageKind.START):
            self._sync_lock_from_exam()

        if self._on_message is not None:
            try:
                self._on_message(kind, message)
            except Exception:
                log.exception("消息回调异常，已忽略（不影响连接）")

    def _sync_lock_from_exam(self) -> None:
        """从状态快照里取"我自己是不是被盖住了"。

        **只在快照说"锁着"时才跟着锁。** 反过来（快照没提这事就当解锁）会让
        一个稍旧的快照替学生把屏幕揭开 —— 而 LOCK 帧本身不带序号，
        无从判断谁更新。让"锁"这件事只能由显式的解除来打断，是唯一安全的做法。
        """
        for row in self.exam.get("participants") or ():
            if not isinstance(row, dict):
                continue
            if normalize_device_id(str(row.get("device_id", ""))) != self.device_id:
                continue
            if row.get("locked"):
                self.locked = True
                if not self.lock_reason:
                    self.lock_reason = "离开中"
            return

    # ------------------------------------------------------------------
    # 同步收发
    # ------------------------------------------------------------------

    def recv(self, timeout: float | None = None) -> tuple[str, dict[str, Any]]:
        """收一条消息（同步用法）。内部状态会一并更新。

        :param timeout: 单次等待的上限（秒）。``None`` 表示一直等下去 ——
            只有"我知道对端马上会回"的场合才该这么用。
        """
        if self.channel is None:
            raise RuntimeError("还没有连接")
        if self._listener is not None:
            raise RuntimeError("读取线程已经在跑，不能再自己收消息")
        kind, message, _ = self._read_frame(timeout)
        self._handle(kind, message)
        return kind, message

    def _read_frame(self, timeout: float | None):
        """带上限地读一帧。

        超时**会把这条连接作废**，这是刻意的：TCP 是字节流，超时可能停在
        一帧的中间，剩下的半帧还在内核缓冲区里 —— 带着这种错位的流继续读，
        只会读出一堆莫名其妙的解析错误，比断线更难查。既然同步用法本来就是
        "一问一答"，让它就地作废、由调用方重新连，是唯一诚实的选择。
        """
        sock = self._socket
        if sock is None or self.channel is None:
            raise RuntimeError("还没有连接")
        if timeout is None:
            return self.channel.recv_kind()

        previous = sock.gettimeout()
        sock.settimeout(timeout)
        try:
            return self.channel.recv_kind()
        except TimeoutError as exc:
            self._closed.set()
            raise protocol.ProtocolError(
                f"等待主机响应超过 {timeout:.0f} 秒，连接已作废") from exc
        finally:
            sock.settimeout(previous)

    def wait_for(self, kind: str, *, limit: int = 200,
                 timeout: float | None = DEFAULT_READ_TIMEOUT) -> dict[str, Any]:
        """一直收到指定类型的消息为止，返回它。

        :param limit: 最多跳过多条无关消息，防止在协议出问题时无限等下去。
        :param timeout: 整体等待上限（秒）。
        """
        deadline = monotonic() + timeout if timeout else None
        for _ in range(limit):
            budget = None if deadline is None else max(0.0, deadline - monotonic())
            if budget is not None and budget <= 0:
                break
            got, message = self.recv(timeout=budget)
            if got == kind:
                return message
        raise protocol.ProtocolError(f"等了 {limit} 条消息也没等到 {kind}")

    def drain_snapshot(self, *, limit: int = 12,
                       timeout: float | None = DEFAULT_READ_TIMEOUT) -> int:
        """把主机在握手后**主动补推**的快照读干净（题面 + 状态 + 排名）。

        主机在 WELCOME 之后会立刻补推三条快照。同步用法下调用方是
        "发一条、收一条"的节奏，不清掉它们，下一次 :meth:`recv` 拿到的就是
        这些快照而不是自己那条请求的回执 —— 表现出来是"提交了却收到题面"
        这种莫名其妙的错误。异步用法（:meth:`start_listener`）不需要它：
        快照由回调自然消化掉。

        三条**都要**读到才收手。只等两条的话，一条恰好排在前面的收卷指令
        会被漏掉，而"漏掉收卷"意味着学生的最后一次提交没了。

        :return: 读掉的消息条数。
        """
        if self._listener is not None:
            raise RuntimeError("读取线程已经在跑，快照由它处理")
        required = {protocol.MessageKind.PROBLEMS, protocol.MessageKind.EXAM,
                    protocol.MessageKind.LEADERBOARD}
        deadline = monotonic() + timeout if timeout else None
        seen: set[str] = set()
        count = 0
        for _ in range(limit):
            budget = None if deadline is None else max(0.0, deadline - monotonic())
            if budget is not None and budget <= 0:
                break
            kind, _message = self.recv(timeout=budget)
            seen.add(kind)
            count += 1
            if required <= seen:
                break
        return count

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            if self.channel is None:
                raise RuntimeError("还没有连接")
            self.channel.send(message)

    def fetch_problems(self) -> list[ExamProblemView]:
        self.send({"kind": protocol.MessageKind.FETCH})
        if self._listener is not None:
            return self.problems          # 异步用法：由回调更新
        self.wait_for(protocol.MessageKind.PROBLEMS)
        return self.problems

    def submit(self, problem_id: str, language: str, code: str,
               *, forced: bool = False) -> None:
        """提交一份代码。判定结果会作为 verdict 消息异步回来。

        :param forced: 到点强制收卷触发的自动提交。主机据此在榜上标注，
            也便于老师事后区分"自己交的"和"系统替他交的"。
        """
        self.send({"kind": protocol.MessageKind.SUBMIT,
                   "problem_id": problem_id,
                   "language": language,
                   "code": code,
                   "forced": forced})

    def request_leaderboard(self) -> None:
        self.send({"kind": protocol.MessageKind.LEADERBOARD})

    def request_lock(self) -> None:
        """学生按「离开一下」：请主机把自己置为"离开中"，界面随即盖住。

        **只能请求"锁"，不能请求"开"。** 主机那一侧只受理 ``locked=True``，
        解锁必须走 :meth:`request_unlock` 并带上口令 —— 否则改一下客户端
        就能把锁摘掉，等于没锁。
        """
        self.send({"kind": protocol.MessageKind.LOCK, "locked": True})

    def request_unlock(self, passcode: str) -> None:
        """请求解锁。口令**发给主机校验**，本机不比对。

        回执是一条 ``unlocked`` 消息（见 :attr:`unlock_reply`），
        界面据此决定是揭开屏幕还是显示"还可以试几次"。
        """
        self.send({"kind": protocol.MessageKind.UNLOCK,
                   "passcode": passcode or ""})

    def ping(self) -> None:
        self.send({"kind": protocol.MessageKind.PING})

    def wait_for_verdict(self, serial: int | None = None, *,
                         timeout: float | None = DEFAULT_VERDICT_TIMEOUT
                         ) -> dict[str, Any]:
        """等到一条**已判定**的结果（跳过"已收到，等待评测"那种占位）。

        :param timeout: 秒。**用时间兜底，不用"最多跳过多条消息"。** 主机会
            周期性推状态与榜单（练习模式每 10 秒一次全榜），消息条数与
            "等了多久"根本不是一回事 —— 拿条数当上限，会让一台忙碌的主机
            看起来像"永远等不到结果"。
        """
        deadline = monotonic() + timeout if timeout else None
        while True:
            budget = None if deadline is None else deadline - monotonic()
            if budget is not None and budget <= 0:
                break
            kind, message = self.recv(timeout=budget)
            if kind != protocol.MessageKind.VERDICT:
                continue
            if message.get("pending"):
                continue
            if serial is not None and message.get("serial") != serial:
                continue
            return message
        raise protocol.ProtocolError("迟迟没有等到评测结果")

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def timed(self) -> bool:
        """本场是否限时。不限时机 `remaining_seconds` 返回 ``None``。"""
        return bool(self.exam.get("timed", True))

    @property
    def leaderboard_published(self) -> bool:
        """榜单是否已公开。考试模式封榜期间为 ``False``。"""
        return bool(self.exam.get("leaderboard_published",
                                  self.leaderboard.get("published", True)))

    def remaining_seconds(self) -> int | None:
        """按服务器时间换算剩余秒数；**不限时返回 ``None``**。

        用主机下发的 ``remaining_seconds`` 加上"本机与主机的时间差"来推算，
        而不是拿本机时钟直接算 —— 学生机的时钟经常是错的，直接算会出现
        "主机已经收卷、客户端还在倒计时"这种现场尴尬。
        """
        value = self.exam.get("remaining_seconds")
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    def server_clock_offset(self) -> float:
        """本机与主机的时钟偏差（秒）。为正值表示本机快。"""
        stamp = self.exam.get("server_time") or self.welcome.get("server_time")
        if not stamp:
            return 0.0
        try:
            host_time = datetime.fromisoformat(str(stamp))
        except ValueError:
            return 0.0
        return (datetime.now() - host_time).total_seconds()

    def problem(self, problem_id: str) -> ExamProblemView | None:
        return next((p for p in self.problems if p.id == problem_id), None)

    def close(self) -> None:
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self.channel = None
