"""主机端：按房间号握手、下发题面、判题队列、放榜策略。

架构上最重要的一条
------------------
**判题在主机上做，测试数据不出主机。** 客户端拿到的只有题面和样例，
源码加密回传，回传的只有判定、耗时、内存。

这条不只是安全考虑，它同时解决了三个问题：

1. 不同机器的 CPU / 磁盘 / 内存带宽不同，各自本机判出来的 300ms 和 800ms
   没法比 —— 而需求要的正是"时间空间排名"，必须同机判才可比；
2. 沙箱只需要在主机一侧做扎实，不用指望 60 台学生机都配好；
3. 最有价值的东西（正式测试点的期望输出）根本不落网。

握手为什么不需要把房间号发上网
------------------------------
房间号（+ 可选口令）是**共享秘密**，两端各自用它 + 本次连接的随机 salt 派生出
同一个会话密钥。所以主机既不需要客户端"上报"什么索引去查表，也不需要把房间号
放进任何一方发出去的帧里：

* 客户端在明文 HELLO 里发 ``room_id``（``sha256(房间号+口令)`` 的前 16 位，单向，
  抓包拿不到钥匙）、``supported_suites`` 与 X25519 临时公钥 ``client_pub``；
* 主机拿自己的房间秘密算一遍索引，对不上就直接拒；再按"客户端声明 ∩ 主机偏好序"
  选定 ``chosen_suite`` 并回 ``server_pub``；
* 密钥由双方各自派生（X25519 把房间口令绑进 HKDF 的 ``info``，scrypt 走 salt），
  客户端的 AUTH 帧能不能解开，就是"它到底知不知道房间号"的证明。

这条设计让房间号从头到尾没有出现在线路上，比上一版的"令牌索引 + 查表"更简单，
也少一张需要在内存里维护的索引表。

握手速率限制
------------
每次握手要跑一次 scrypt（350ms），这本身就是天然的限速器；再叠一层按 IP
的尝试次数限制，防止有人拿它做在线爆破或单纯的资源耗尽。

进场方式与离场锁屏
------------------
**进场二选一**（:class:`offline_oj.net.session.EntryMode`）：房间号（默认）或
名单上的账号+个人口令。两条路径在 :meth:`ExamServer._handshake` 里只有一处分叉
—— 主机用 :meth:`ExamSession.resolve` 把 HELLO 里的索引换成一个共享秘密，
剩下的派生、AUTH、WELCOME 完全共用。账号进场的**名字由名单决定**，
AUTH 帧里学生自报的用户名不采信。

**离场锁屏**（防窥屏）是三个帧：学生按「离开一下」发 ``LOCK``（只许锁自己）、
回来输口令发 ``UNLOCK``、主机校验后回 ``UNLOCKED``；老师也能远程对某人/全体
下发 ``LOCK``。解锁的**判定只在主机**（``ExamSession.verify_unlock``）——
学生机是可以被改的，本地比对等于没比对；错满 5 次就得等老师放行。

放榜策略
--------
**练习模式**：每次判完就广播一次榜单，实时更新。
**考试模式**：全程封榜，直到测验结束（含收卷宽限期）才统一放榜；
期间只把"你自己那一行"私下发给本人 —— 自己的成绩不该也看不到。
两种模式共用同一套判题与排名代码，差别只在 :meth:`ExamSession.leaderboard_visible`。

再往上一层还有一个 ``show_leaderboard`` 开关：关掉之后**任何模式下都不放榜**。
它与模式正交，所以"考试 + 全程不放榜"是合法的第四种组合，不必为它另立一种模式。
关掉放榜时那个"结束统一放榜"的 ``published`` 事件也不会发 —— 没有榜可放时
通知界面"已放榜"只会让老师以为学生看到了一份并不存在的成绩单。
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from ..core.judge import Judge, JudgeReport
from ..core.models import Language, Problem, Verdict
from ..core.runners import make_runner
from . import crypto, protocol
from .session import (MAX_SCORE_PER_PROBLEM, EntryMode, ExamProblemView,
                      ExamSession, ExamState, JoinError, RoomMode, Submission)

log = logging.getLogger(__name__)

__all__ = ["ExamServer", "ServerConfig", "build_exam_problem",
           "PROTOCOL_VERSION"]

#: 协议版本。两端不一致时直接拒绝，比"连上了但行为诡异"好得多。
#: v2：握手改用房间号索引，身份改用设备八位 ID。
#:
#: **账号进场与离场锁屏（v2 之后新增的两组帧）刻意没有升版本号。**
#: 这两组都是纯增量：老客户端在房间号进场的房间里照常工作，只是它不知道
#: LOCK / UNLOCK 这两组帧（收到不认识的帧会当业务错误忽略）。升版本号会让
#: "只更新了老师那台机器"的教室**全班都进不来**，而他们本来什么都不缺。
#: 拿不准时选"老客户端还能用"，不要把升级变成一次断网。
PROTOCOL_VERSION = 3

#: 每个 IP 每分钟允许的握手次数
HANDSHAKE_ATTEMPTS_PER_IP = 12
HANDSHAKE_WINDOW_SECONDS = 60.0

#: 状态与排名广播间隔（秒）
STATUS_INTERVAL = 5.0
LEADERBOARD_INTERVAL = 10.0


def _language_labels(values: Iterable[str]) -> str:
    """把允许的语言值翻成显示名，用于拒绝提示（``"本场只收 C++ / Python"``）。"""
    labels: list[str] = []
    for value in values:
        item = Language.from_value(value)
        labels.append(item.short if item is not None else str(value))
    return " / ".join(labels)


def build_exam_problem(problem: Problem) -> ExamProblemView:
    """把题库里的题转成可以下发给客户端的视图 —— **只带样例测试点**。

    没有一道题被标记为样例时，样例列表就是空的。这是刻意的：宁可学生看不到
    样例，也不能因为"找不到样例就退而求其次发全部"而把正式测试点泄漏出去。

    题目英文名与数据文件名要一起下发：学生端得知道这道题是标准输入输出还是
    读写 ``poker.in`` / ``poker.out``（CCF 规约），否则写出来的代码在本机自测能过、
    交上去必然 RE。

    ``points``（本题满分）也要下发 —— 学生看到"37 分"得知道那是满分 50 里的
    还是满分 100 里的。没有测试点这种配置错误退回默认满分，别把一个坏题目
    变成"满分 0 分"。
    """
    input_file, output_file = problem.io_files()
    return ExamProblemView(
        id=problem.id,
        title=problem.title,
        description=problem.description,
        time_limit=problem.time_limit,
        memory_limit=problem.memory_limit,
        slug=problem.english_name,
        input_file=input_file if problem.judge.uses_files else "",
        output_file=output_file if problem.judge.uses_files else "",
        io_mode=problem.judge.io_mode.value,
        samples=[{"input": case.input, "output": case.output}
                 for case in problem.testcases if case.sample],
        points=problem.total_points or MAX_SCORE_PER_PROBLEM,
    )


@dataclass
class ServerConfig:
    """主机端可调项。"""

    bind_host: str = "0.0.0.0"
    port: int = 0                    # 0 = 让系统挑一个空闲端口
    max_workers: int = 2             # 并发的判题线程数
    optimize: bool = True            # C/C++ 编译开 -O2
    handshake_attempts: int = HANDSHAKE_ATTEMPTS_PER_IP
    status_interval: float = STATUS_INTERVAL
    leaderboard_interval: float = LEADERBOARD_INTERVAL


class _ClientSession:
    """一条已鉴权的客户端连接。"""

    def __init__(self, channel: protocol.SecureChannel, device_id: str,
                 username: str, address: str) -> None:
        self.channel = channel
        self.device_id = device_id
        self.username = username
        self.address = address
        self.send_lock = threading.Lock()
        self.alive = True

    def send(self, message: dict[str, Any], blob: bytes = b"") -> bool:
        """线程安全地推一条消息。失败说明连接已经断了，返回 ``False``。"""
        if not self.alive:
            return False
        with self.send_lock:
            try:
                self.channel.send(message, blob)
                return True
            except (OSError, protocol.ProtocolError) as exc:
                log.debug("向 %s 推送失败: %s", self.device_id, exc)
                self.alive = False
                return False

    def close(self) -> None:
        self.alive = False
        try:
            self.channel.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.channel.sock.close()
        except OSError:
            pass


class ExamServer:
    """一个局域网房间测验的主机。"""

    def __init__(
        self,
        session: ExamSession,
        problems: Iterable[Problem],
        *,
        compiler_paths: dict[str, str] | None = None,
        work_root: str | None = None,
        config: ServerConfig | None = None,
        judge: Callable[[Problem, str, Language], JudgeReport] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.session = session
        self.config = config or ServerConfig()
        self._problems: dict[str, Problem] = {p.id: p for p in problems}
        self._compiler_paths = dict(compiler_paths or {})
        self._work_root = work_root or "."
        #: 判题入口。测试时注入一个假判题器，就不需要真编译器也能跑通全链路。
        self._judge = judge
        self._on_event = on_event

        self._server: socket.socket | None = None
        self._port = 0
        self._accept_thread: threading.Thread | None = None
        self._ticker_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._judge_queue: queue.Queue[tuple[Submission, Problem] | None] = queue.Queue()
        self._stop = threading.Event()

        #: 设备 ID → 连接
        self._clients: dict[str, _ClientSession] = {}
        self._clients_lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

        self._started_at: float | None = None
        self._judged = 0
        #: "开考"这件事有没有广播过。开房时若开考时刻已经过去（starts_at 在过去），
        #: 就不存在"开考"这个瞬间可播 —— 直接算已播报，免得每个客户端一连上
        #: 就收到一条莫名其妙的"开考了"。
        self._started_announced = True

    # ------------------------------------------------------------------
    # 房间信息
    # ------------------------------------------------------------------

    @property
    def room_code(self) -> str:
        return self.session.room_code

    @property
    def room_id(self) -> str:
        """本房间秘密的单向索引，用于比对客户端上报的那一个。"""
        return self.session.fingerprint

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """绑定端口并开始接受连接。"""
        if self._server is not None:
            raise RuntimeError("服务已经在运行")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.config.bind_host, self.config.port))
        self._port = server.getsockname()[1]
        server.listen(64)
        server.settimeout(0.5)          # 让 accept 能定期回来看 stop 标志
        self._server = server
        self._stop.clear()
        self._started_at = time.time()
        # 已经开考的（含"开房时就设成过去"的）没有开考瞬间可播
        self._started_announced = self.session.state() is not ExamState.PENDING

        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="oj-accept", daemon=True)
        self._accept_thread.start()

        for index in range(max(1, self.config.max_workers)):
            worker = threading.Thread(target=self._judge_loop,
                                      name=f"oj-judge-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)

        self._ticker_thread = threading.Thread(target=self._ticker_loop,
                                               name="oj-ticker", daemon=True)
        self._ticker_thread.start()
        log.info("测验主机已启动，端口 %d，房间 %s，%s",
                 self._port, self.room_code, self.session.mode.value)
        # 事件里**不带房间号**：这个回调是给本机界面用的，界面本来就能从
        # session 上读到它，没有理由让它顺流进日志或问题反馈里 ——
        # 房间号是凭据，凭据少出现一次就少一条外泄路径。
        self._emit("started", {"port": self._port,
                               "mode": self.session.mode.value,
                               "addresses": self.lan_addresses()})

    def stop(self, timeout: float = 3.0) -> None:
        """停止服务并断开所有客户端。"""
        self._stop.set()
        for client in self._snapshot_clients():
            client.close()
        with self._clients_lock:
            self._clients.clear()

        # 叫醒所有判题线程
        for _ in self._workers:
            self._judge_queue.put(None)

        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

        for thread in (self._accept_thread, self._ticker_thread, *self._workers):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
        self._accept_thread = self._ticker_thread = None
        self._workers.clear()
        log.info("测验主机已停止")
        self._emit("stopped", {})

    @property
    def port(self) -> int:
        return self._port

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def judged_count(self) -> int:
        return self._judged

    @property
    def pending_count(self) -> int:
        return self._judge_queue.qsize()

    def lan_addresses(self) -> list[str]:
        """本机能被局域网访问到的 IPv4 地址，供主机界面上大字显示。"""
        addresses: list[str] = []
        # 先问操作系统"到公网走哪张网卡"，这个地址通常是教室里的主网卡
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("223.5.5.5", 80))
            addresses.append(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None,
                                           socket.AF_INET):
                candidate = info[4][0]
                if candidate not in addresses and not candidate.startswith("127."):
                    addresses.append(candidate)
        except OSError:
            pass
        return addresses

    def connected_devices(self) -> list[str]:
        with self._clients_lock:
            return sorted(self._clients)

    # ------------------------------------------------------------------
    # 接受连接
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client,
                             args=(connection, address),
                             name="oj-client", daemon=True).start()

    def _handle_client(self, connection: socket.socket,
                       address: tuple[str, str]) -> None:
        peer = f"{address[0]}:{address[1]}"
        device_id = ""
        try:
            connection.settimeout(30.0)
            channel, device_id, username = self._handshake(connection, address)
            if channel is None:
                return
            connection.settimeout(None)
            client = _ClientSession(channel, device_id, username, peer)
            with self._clients_lock:
                previous = self._clients.get(device_id)
                self._clients[device_id] = client
            if previous is not None:
                # 同一设备重连：把旧连接关掉，避免两个连接同时代表一台机器
                previous.close()
                self.session.note_replacement(device_id, peer)
                self._emit("replaced", {"device_id": device_id,
                                        "username": username, "address": peer})
            self.session.mark_connected(device_id, peer)
            self._emit("joined", {"device_id": device_id,
                                  "username": username, "address": peer})
            # WELCOME 已经在握手最后一步发过了，这里只补题面与快照
            self._push_problems(client)
            self._push_status(client)
            self._push_leaderboard(client)
            self._maybe_collect_late_joiner(client)
            self._serve(client)
        except protocol.ProtocolError as exc:
            log.info("客户端 %s 断开（协议层）: %s", peer, exc)
        except (OSError, crypto.AuthenticationError) as exc:
            log.info("客户端 %s 断开: %s", peer, exc)
        except Exception:
            log.exception("处理客户端 %s 时出现未预期异常", peer)
        finally:
            if device_id:
                with self._clients_lock:
                    current = self._clients.get(device_id)
                    if current is not None and current.address == peer:
                        self._clients.pop(device_id, None)
                self.session.mark_disconnected(device_id)
                self._emit("left", {"device_id": device_id, "address": peer})
            try:
                connection.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 握手
    # ------------------------------------------------------------------

    def _handshake(self, connection: socket.socket,
                   address: tuple[str, str]):
        """明文两帧 + 加密一帧完成鉴权，返回 ``(通道, 设备 ID, 用户名)``。

        注意明文段里**没有任何秘密**：只有协议版本和房间秘密的单向索引。
        """
        if not self._allow_handshake(address[0]):
            plain = protocol.PlainChannel(connection)
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": "握手太频繁，请稍后再试"})
            return None, "", ""

        plain = protocol.PlainChannel(connection)
        hello, _ = plain.recv()
        if hello.get("kind") != protocol.MessageKind.HELLO:
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": "没有先打招呼"})
            return None, "", ""

        version = hello.get("protocol_version")
        if version != PROTOCOL_VERSION:
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": f"版本不匹配：主机是 {PROTOCOL_VERSION}，"
                                  f"你是 {version}。请让两台机器用同一个版本。"})
            return None, "", ""

        # 两种进场方式在这一步合流。HELLO 里那串索引要么是"房间号（+全场口令）"
        # 的索引，要么是"账号（+个人口令+全场口令）"的索引 —— 查哪一侧由
        # **本场设定**决定，不由客户端自报。让客户端挑校验方式等于没有校验。
        entry = self.session.resolve(str(hello.get("room_id", "")))
        if entry is None:
            reason = self._credential_reason()
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": reason,
                        # 顺带告诉对端"这个房间认什么"，它好把提示语换成
                        # "账号或密码不对"而不是让人对着房间号输入框发呆
                        "entry_mode": self.session.entry_mode.value,
                        "entry_mode_label": self.session.entry_mode.label})
            self._emit("rejected", {"address": f"{address[0]}:{address[1]}",
                                    "reason": reason})
            return None, "", ""

        # --- 套件协商（#55：v3 起强制）---
        # 客户端必须在 HELLO 带 supported_suites + client_pub；没有就是老客户端，
        # 直接拒（强升 v3，不再兼容 v2 的 scrypt-only 握手）。
        client_suites = hello.get("supported_suites") or []
        client_pub_hex = hello.get("client_pub")
        if not client_suites or not client_pub_hex:
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": f"协议版本过低（需要 v{PROTOCOL_VERSION}），"
                                  f"请升级学生端。"})
            return None, "", ""
        chosen_suite = crypto.negotiate_suite(client_suites)
        if chosen_suite is None:
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": "双方没有都支持的加密套件"})
            return None, "", ""
        server_priv, server_pub = crypto.x25519_generate_keypair()
        try:
            peer_public = bytes.fromhex(str(client_pub_hex))
        except ValueError:
            plain.send({"kind": protocol.MessageKind.REJECTED,
                        "reason": "client_pub 格式不对"})
            return None, "", ""

        state = self.session.state()

        salt = crypto.random_salt()
        plain.send({
            "kind": protocol.MessageKind.CHALLENGE,
            "salt": salt.hex(),
            "protocol_version": PROTOCOL_VERSION,
            "chosen_suite": chosen_suite,
            "server_pub": server_pub.hex(),
            "title": self.session.title,
            "session_id": self.session.session_id,
            "mode": self.session.mode.value,
            "entry_mode": self.session.entry_mode.value,
            "entry_mode_label": self.session.entry_mode.label,
            "state": state.value,
            "password_required": bool(self.session.password),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        })

        # 派生会话密钥：X25519 套件走临时密钥对 + HKDF，并把房间口令绑进
        # HKDF 的 info（不知口令 → 密钥错 → AUTH 解密失败 → 恢复进场鉴权）；
        # scrypt 套件仍走旧的 derive_secret_key(secret, salt)。
        if crypto.SUITES[chosen_suite].kex == "x25519":
            key = crypto.kex_session_key(
                chosen_suite, private_key=server_priv, peer_public=peer_public,
                info=entry["secret"].encode("utf-8"))
        else:
            key = crypto.kex_session_key(chosen_suite, secret=entry["secret"],
                                         salt=salt)
        channel = protocol.SecureChannel(connection, key,
                                         protocol.DIRECTION_SERVER, chosen_suite)
        message, _ = channel.recv()
        if message.get("kind") != protocol.MessageKind.AUTH:
            channel.send({"kind": protocol.MessageKind.REJECTED,
                          "reason": "鉴权流程不对"})
            return None, "", ""

        device_id = str(message.get("device_id", ""))
        try:
            if self.session.entry_mode is EntryMode.ACCOUNT:
                # 名字**不看** AUTH 帧里那个：它由名单决定。学生报什么都
                # 改不了自己是谁 —— 这条就是"绑定选手信息"。
                target = self.session.authenticate(
                    str(entry.get("account", "")), device_id)
            else:
                target = self.session.join(device_id,
                                           str(message.get("username", "")))
        except JoinError as exc:
            # 这一条是业务上的正常拒绝（名字太长、设备 ID 位数不对、
            # 账号已绑在别的机器上），要原样转达给用户，不能记成"协议异常"
            channel.send({"kind": protocol.MessageKind.REJECTED,
                          "reason": str(exc)})
            self._emit("rejected", {"address": f"{address[0]}:{address[1]}",
                                    "reason": str(exc)})
            return None, "", ""

        # join 之后以会话里存下来的名字为准：重连时名字以第一次为准
        device_id, username = target.device_id, target.username

        channel.send({
            "kind": protocol.MessageKind.WELCOME,
            "device_id": device_id,
            "username": username,
            "title": self.session.title,
            "session_id": self.session.session_id,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "exam": self.session.summary(),
        })
        return channel, device_id, username

    def _credential_reason(self) -> str:
        """索引对不上时给用户看的话。

        两种进场方式下"对不上"的原因完全不同，用同一句话会把学生指到错误的方向：
        账号进场的房间里，学生根本没有房间号可输，让他"确认房间号"等于让他乱试。
        """
        if self.session.entry_mode is EntryMode.ACCOUNT:
            if self.session.password:
                return "账号、个人密码或考场口令不对，请向老师确认"
            return "账号或个人密码不对，请向老师确认"
        return "房间号或房间口令不对，请向老师确认"

    def _allow_handshake(self, ip: str) -> bool:
        """按 IP 限制握手频率。每次握手要跑一次 scrypt，不能任人刷。"""
        now = time.time()
        window = self._attempts[ip]
        while window and now - window[0] > HANDSHAKE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= self.config.handshake_attempts:
            return False
        window.append(now)
        return True

    # ------------------------------------------------------------------
    # 消息循环
    # ------------------------------------------------------------------

    def _serve(self, client: _ClientSession) -> None:
        while not self._stop.is_set() and client.alive:
            try:
                kind, message, _ = client.channel.recv_kind()
            except protocol.ProtocolError as exc:
                log.info("%s 的连接结束: %s", client.device_id, exc)
                return
            except OSError as exc:
                log.info("%s 的连接中断: %s", client.device_id, exc)
                return

            if kind == protocol.MessageKind.PING:
                client.send({"kind": protocol.MessageKind.PONG,
                             "server_time": datetime.now().isoformat(
                                 timespec="seconds")})
            elif kind == protocol.MessageKind.FETCH:
                self._push_problems(client)
            elif kind == protocol.MessageKind.LEADERBOARD:
                self._push_leaderboard(client)
            elif kind == protocol.MessageKind.SUBMIT:
                self._on_submit(client, message)
            elif kind == protocol.MessageKind.UNLOCK:
                self._on_unlock(client, message)
            elif kind == protocol.MessageKind.LOCK:
                self._on_lock(client, message)
            else:
                client.send({"kind": protocol.MessageKind.ERROR,
                             "message": f"不认识的消息类型 {kind}"})

    def _on_lock(self, client: _ClientSession,
                 message: dict[str, Any]) -> None:
        """学生自己按了「离开一下」。

        **只受理"锁"，不受理"开"。** 否则"把 locked 改成 false 再发一次"
        就是万能钥匙 —— 锁屏挡的是隔壁同学的眼睛，可它一样得建立在
        "解锁要口令"这个前提上，不然按一下就没意义了。解锁走 :meth:`_on_unlock`。
        """
        if not bool(message.get("locked", True)):
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": "解锁请用「解开」按钮并输入口令"})
            return
        self.session.set_locked(client.device_id, True)
        self._emit("locked", {"device_id": client.device_id,
                              "username": client.username, "locked": True,
                              "reason": str(message.get("reason", "") or "")})
        self.broadcast_status()

    def _on_unlock(self, client: _ClientSession,
                   message: dict[str, Any]) -> None:
        """学生请求解锁。

        **校验在主机。** 学生机是可以被改的（改 exe、改内存、直接发包），
        本地比对等于没比对 —— 与"提交策略的真正边界在服务端"同一条原则。
        所以这里连"你锁着没有"都由会话说了算，而不是采信客户端报的状态。
        """
        ok, reason = self.session.verify_unlock(
            client.device_id, str(message.get("passcode", "")))
        client.send({"kind": protocol.MessageKind.UNLOCKED,
                     "ok": ok, "message": reason})
        self._emit("unlock", {"device_id": client.device_id,
                              "username": client.username,
                              "ok": ok, "message": reason})
        if ok:
            # 解锁成功要顺带刷一次状态：主机界面上"离开中"那一条得跟着消失。
            self.broadcast_status()

    # ------------------------------------------------------------------
    # 离场锁屏（主机侧）
    # ------------------------------------------------------------------

    def set_locked(self, device_id: str, locked: bool = True,
                   *, reason: str = "") -> bool:
        """对某个人下锁屏 / 解除。返回是否找到了这个人。

        解除时**不需要口令**：老师是监考者，他手上就有名单。
        """
        participant = self.session.set_locked(device_id, locked)
        if participant is None:
            return False
        self._send_to(participant.device_id,
                      {"kind": protocol.MessageKind.LOCK,
                       "locked": bool(locked), "reason": reason})
        self._emit("locked", {"device_id": participant.device_id,
                              "username": participant.username,
                              "locked": bool(locked), "reason": reason})
        self.broadcast_status()
        return True

    def lock_all(self, locked: bool = True, *, reason: str = "") -> int:
        """对全体下锁屏（老师站在门口喊一声"都盖一下"时用）。返回人数。"""
        touched = self.session.set_locked_all(locked)
        for device in touched:
            self._send_to(device, {"kind": protocol.MessageKind.LOCK,
                                   "locked": bool(locked), "reason": reason})
        self._emit("locked", {"device_id": "", "username": "", "count": len(touched),
                              "locked": bool(locked), "reason": reason})
        self.broadcast_status()
        return len(touched)

    def _send_to(self, device_id: str, message: dict[str, Any]) -> bool:
        client = self._client_for(device_id)
        if client is None:
            return False
        return client.send(message)

    def _on_submit(self, client: _ClientSession, message: dict[str, Any]) -> None:
        problem_id = str(message.get("problem_id", ""))
        language_value = str(message.get("language", ""))
        code = str(message.get("code", ""))
        # 到点强制收卷时客户端会带上这个标记，主机据此在榜上区分"自动交的"
        forced = bool(message.get("forced"))

        if not self.session.accepts_submissions():
            # 未开始与已结束是两种完全不同的现场情况，提示语不能混用：
            # 对着还没开考的学生说"已经结束"会让人以为错过了整场测验。
            reason = ("本场测验尚未开始，请等待老师宣布开始"
                      if self.session.state() is ExamState.PENDING
                      else "本场测验已经结束，不再接受提交")
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": reason,
                         "problem_id": problem_id,
                         "forced": forced})
            return

        problem = self._problems.get(problem_id)
        if problem is None:
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": f"题目 {problem_id} 不存在",
                         "problem_id": problem_id})
            return

        language = Language.from_value(language_value)
        if language is None:
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": f"不支持的语言 {language_value}",
                         "problem_id": problem_id})
            return

        # 本场限定的语言，服务端**再校验一次**：学生端是可以被改的（改 exe、
        # 改内存、直接发包都行），界面上的置灰只是"别让人白点一次"，
        # 不是安全边界。真正的边界在这一行。
        if not self.session.policy.allows_language(language):
            allowed = _language_labels(self.session.policy.allowed_languages)
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": f"本场只收 {allowed}，不接受 {language.short}",
                         "problem_id": problem_id})
            return

        if not code.strip():
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": "代码是空的",
                         "problem_id": problem_id})
            return

        allowed, why = self.session.can_submit(client.device_id, problem_id)
        if not allowed:
            client.send({"kind": protocol.MessageKind.ERROR,
                         "message": why,
                         "problem_id": problem_id})
            return

        submission = self.session.new_submission(
            client.device_id, client.username, problem_id,
            language.value, code, forced=forced)
        self.session.record_submission(submission)
        client.send({"kind": protocol.MessageKind.VERDICT,
                     "pending": True,
                     "serial": submission.serial,
                     "problem_id": problem_id,
                     "attempt": submission.attempt,
                     "forced": forced,
                     "position": self.pending_count + 1,
                     "message": f"第 {submission.attempt} 次提交已收到，等待评测"})
        self._emit("submitted", {"device_id": client.device_id,
                                 "username": client.username,
                                 "problem_id": problem_id,
                                 "serial": submission.serial,
                                 "attempt": submission.attempt,
                                 "forced": forced,
                                 "language": language.value})
        self._judge_queue.put((submission, problem))

    # ------------------------------------------------------------------
    # 判题
    # ------------------------------------------------------------------

    def _judge_loop(self) -> None:
        while not self._stop.is_set():
            item = self._judge_queue.get()
            if item is None:
                break
            submission, problem = item
            try:
                self._judge_one(submission, problem)
            except Exception:
                log.exception("评测 %s 时出现未预期异常", submission.brief())
                self._finish(submission, JudgeReport(
                    problem_id=problem.id, problem_title=problem.title,
                    verdict=Verdict.IE))
                continue
            self._judged += 1

    def _judge_one(self, submission: Submission, problem: Problem) -> None:
        if self._judge is not None:
            report = self._judge(problem, submission.code,
                                 Language.from_value(submission.language))
        else:
            runner = make_runner(Language.from_value(submission.language),
                                 self._compiler_paths, self._work_root,
                                 optimize=self.config.optimize)
            report = Judge(runner).judge(problem, submission.code)
        self._finish(submission, report)

    def _finish(self, submission: Submission, report: JudgeReport) -> None:
        submission.verdict = report.verdict.value
        submission.passed = report.passed
        submission.total = report.total
        submission.time_ms = report.max_time_ms
        submission.memory_mb = report.max_memory_mb
        # 给分走 NOI 那条路：**通过的测试点的分值之和**，满分是各点分值之和。
        # 判题器没给分值信息时（外部接入的判题器只填通过数）退回"按通过比例
        # 折算"——但折算的**分母仍取该题的分值之和**，不是老的恒定 100。
        # 两条路共用一把尺子，榜上"得分/满分"才读得通：全对 = 该题满分。
        if report.points_total:
            submission.possible = report.points_total
            submission.score = report.points_earned
        else:
            view = self.session.problem(submission.problem_id)
            full = view.points if view is not None else MAX_SCORE_PER_PROBLEM
            submission.possible = full
            submission.score = submission.ratio_score(full)
        submission.judged_at = datetime.now()
        submission.optimized = report.optimized
        if not report.compile_ok:
            submission.message = report.compile_message
        elif not report.checker_ok:
            submission.message = report.checker_message
        elif report.aborted:
            submission.message = "评测已中止"
        else:
            submission.message = report.summary()

        client = self._client_for(submission.device_id)
        if client is not None:
            client.send({"kind": protocol.MessageKind.VERDICT,
                         "pending": False,
                         "serial": submission.serial,
                         "problem_id": submission.problem_id,
                         "attempt": submission.attempt,
                         "forced": submission.forced,
                         "verdict": submission.verdict,
                         "passed": submission.passed,
                         "total": submission.total,
                         "score": submission.score,
                         # 满分也发给学生："37 分"脱离满分读不出含义
                         "possible": submission.possible,
                         "time_ms": round(submission.time_ms, 2),
                         "memory_mb": round(submission.memory_mb, 2),
                         "message": submission.message})
        self._emit("judged", {"device_id": submission.device_id,
                              "username": submission.username,
                              "problem_id": submission.problem_id,
                              "serial": submission.serial,
                              "attempt": submission.attempt,
                              "verdict": submission.verdict,
                              "score": submission.score,
                              "possible": submission.possible,
                              "optimized": submission.optimized,
                              "time_ms": submission.time_ms,
                              "memory_mb": submission.memory_mb})

        # 练习模式实时放榜；考试模式一律等到结束后统一放榜，
        # 这里只把"你自己那一行"私下推给本人。判完就广播全榜，
        # 会让"考试模式"名存实亡。
        if self.session.leaderboard_visible():
            self.broadcast_leaderboard()
        elif client is not None:
            self._push_leaderboard(client)

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def _push_problems(self, client: _ClientSession) -> None:
        client.send({"kind": protocol.MessageKind.PROBLEMS,
                     "problems": [view.to_dict() for view in self.session.problems],
                     "server_time": datetime.now().isoformat(timespec="seconds")})

    def _push_status(self, client: _ClientSession) -> None:
        client.send({"kind": protocol.MessageKind.EXAM,
                     **self.session.summary()})

    def _push_leaderboard(self, client: _ClientSession) -> None:
        """榜单是**按人**推的：封榜期间只有他自己那一行会跟着过去。"""
        client.send(self.session.leaderboard_payload(viewer=client.device_id))

    def broadcast_status(self) -> None:
        payload = {"kind": protocol.MessageKind.EXAM, **self.session.summary()}
        self._broadcast(payload)

    def rename(self, title: str) -> str:
        """改房间名，并把新名字随一帧 EXAM 广播出去，返回实际生效的名字。

        学生端的状态栏本来就从 EXAM 载荷里读 ``title``，所以广播这一帧就够了 ——
        题面、名单、榜单都与标题无关，不必重发。**不会踢人**：房间号与派生密钥
        都跟 title 无关（见 :meth:`ExamSession.rename`），改名只是换个显示标签。
        """
        applied = self.session.rename(title)
        self.broadcast_status()
        self._emit("renamed", {"title": applied})
        return applied

    def broadcast_leaderboard(self) -> None:
        self._broadcast(self.session.leaderboard_payload())

    def start_exam(self) -> bool:
        """提前开考。返回是否真的开了（已开考 / 已结束时为 ``False``）。

        开考这件事**必须由主机说出来**，不能让各机自己看表：学生机的系统时钟
        不可信（时区没设对、对时失败、故意改表都会中招），一旦按本地时间判定，
        同一间教室里会同时出现"有人提前 5 分钟进场"和"有人永远进不去"。
        """
        if not self.session.start_now():
            return False
        self._announce_start("host")
        return True

    def _announce_start(self, reason: str) -> None:
        """广播开考。

        载荷与 ``EXAM`` **同构**（还多一个 ``reason``），这样客户端拿它当一次
        状态跳变处理即可 —— 不必再等下一帧周期状态才解锁提交按钮，
        到点那一刻是立刻开口的。
        """
        self._started_announced = True
        self._broadcast({"kind": protocol.MessageKind.START,
                         "reason": reason,
                         **self.session.summary()})
        self._emit("exam_started", {"reason": reason})

    def collect_from_all(self, reason: str = "host") -> None:
        """收卷：要求所有客户端立刻把当前代码交上来。

        到点自动触发（``reason="deadline"``）或老师手动提前收卷
        （``reason="host"``）走的是同一条路径 —— 两条实现迟早会不一致。
        """
        self._broadcast({
            "kind": protocol.MessageKind.COLLECT,
            "reason": reason,
            "force_collect": self.session.force_collect,
            "server_time": datetime.now().isoformat(timespec="seconds"),
        })
        self.broadcast_status()
        self._emit("collect", {"reason": reason,
                               "force_collect": self.session.force_collect})

    def _maybe_collect_late_joiner(self, client: _ClientSession) -> None:
        """刚连进来但已经到点了：补发一条收卷指令。

        不补的话，一个在截止后才连上的客户端会一直停在"考试进行中"，
        而主机早就不再收新提交了。
        """
        if not self.session.force_collect or not self.session.timed:
            return
        if self.session.remaining_seconds() == 0 and self.session.accepts_submissions():
            client.send({"kind": protocol.MessageKind.COLLECT,
                         "reason": "deadline",
                         "force_collect": True,
                         "server_time": datetime.now().isoformat(
                             timespec="seconds")})

    def _broadcast(self, payload: dict[str, Any]) -> None:
        """给所有在线客户端推同一条消息。

        推送失败的连接就地摘掉：广播是每几秒一次的常规动作，正好兼做
        "清理已经掉线但还没被读线程发现"的连接。
        """
        for client in self._snapshot_clients():
            if not client.send(payload):
                with self._clients_lock:
                    if self._clients.get(client.device_id) is client:
                        self._clients.pop(client.device_id, None)
                self.session.mark_disconnected(client.device_id)

    def _ticker_loop(self) -> None:
        """定期广播状态与排名，处理到点收卷，并在结束后统一放榜。"""
        last_status = last_board = time.time()
        collected = False
        published = False
        while not self._stop.is_set():
            time.sleep(0.25)
            now = time.time()

            if now - last_status >= self.config.status_interval:
                self.broadcast_status()
                last_status = now

            # 备考 → 开考这一下由主机播报，不是让学生机自己看表到点
            if (not self._started_announced
                    and self.session.state() is ExamState.RUNNING):
                self._announce_start("deadline")

            if self.session.leaderboard_visible():
                # 练习模式：全程实时；考试模式：走到这里说明已经过了收卷宽限
                if now - last_board >= self.config.leaderboard_interval:
                    self.broadcast_leaderboard()
                    last_board = now
                if not published:
                    published = True
                    # 练习模式的榜从一开始就是公开的，不是"放榜"这个时刻；
                    # 只有考试模式才有"结束统一放榜"这件事值得通知界面
                    if self.session.mode is RoomMode.EXAM:
                        self.broadcast_leaderboard()
                        self._emit("published", {
                            "leaderboard": self.session.leaderboard_payload()})

            if (not collected and self.session.timed
                    and self.session.remaining_seconds() == 0):
                collected = True
                if self.session.force_collect:
                    # 只有到点后仍在宽限期内才发：服务器若是在截止很久之后
                    # 才启动的，补发一条"立刻交卷"只会让学生莫名其妙
                    if self.session.accepts_submissions():
                        self.collect_from_all("deadline")
                self._emit("closed", {
                    "remaining": self.session.remaining_seconds()})

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _client_for(self, device_id: str) -> _ClientSession | None:
        with self._clients_lock:
            return self._clients.get(device_id)

    def _snapshot_clients(self) -> list[_ClientSession]:
        with self._clients_lock:
            return list(self._clients.values())

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, payload)
        except Exception:
            log.debug("事件回调异常", exc_info=True)
