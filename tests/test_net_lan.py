"""主机 + 客户端的端到端集成测试（本地回环，不需要编译器）。

用一个**假判题器**替换真实评测：这一层要验的是网络、鉴权、题面下发、
提交回传、排名广播这条链路，而不是编译器能不能用。
真实编译判题的端到端走 ``tools/lan_e2e.py``。

关于夹具为什么是"每条用例一台主机"
----------------------------------
主机是有状态的：参与者、提交记录、排名都挂在 ``ExamSession`` 上。
而 ``unittest`` 的收集器会按**方法名字母序**重排用例（不是书写顺序），
共享一台主机就会串味 —— 上一条用例里"张三"占掉的设备 ID，会让下一条用例里
同名的人进不来（名字可以重，但设备 ID 是身份），报错信息还指不到真正的原因。

所以这里刻意用 ``setUp`` 而不是 ``setUpClass`` 建主机，宁可多花几百毫秒
（每次握手一次 scrypt），换每条用例都从干净状态出发。

最关键的三条断言，都是需求原文：

* :meth:`TestProblemDelivery.test_only_samples_are_sent` —— 正式测试点的
  期望输出**不能出现在**任何一条下发给客户端的消息里；
* :meth:`TestRankingBroadcast.test_ranking_orders_by_score_then_time_but_ties_share_the_rank`
  —— 同题按分数排、同分者按耗时显示，但同分是**并列名次**（NOI 式）；
* :meth:`TestPointValuesOnTheWire` —— 按测试点分值给分，满分不再是恒定的 100；
* :meth:`TestExamMode.test_board_stays_empty_while_running` —— 考试模式
  全程封榜，不是"改个标题的练习模式"。
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from datetime import datetime, timedelta
from typing import Sequence

from offline_oj.core.judge import JudgeReport, TestOutcome as Outcome
from offline_oj.core.models import (DEFAULT_TESTCASE_POINTS, Language, Problem,
                                    TestCase as CaseModel, Verdict)
from offline_oj.net import crypto, protocol
from offline_oj.net.client import ConnectError, ExamClient, HandshakeRejected
from offline_oj.net.server import ExamServer, ServerConfig, build_exam_problem
from offline_oj.net.session import (EntryMode, ExamPolicy, ExamSession,
                                    ExamState, MAX_UNLOCK_ATTEMPTS, RoomMode,
                                    room_id)

#: 主机会**周期性主动推**的消息类型，它们不属于"对某条请求的回执"。
#: 生产环境是 5 / 10 秒一次，测试里压到 0.2 秒以缩短用例耗时。
UNSOLICITED_KINDS = frozenset({
    protocol.MessageKind.EXAM,
    protocol.MessageKind.START,
    protocol.MessageKind.LEADERBOARD,
    protocol.MessageKind.COLLECT,
})


def make_problem(problem_id: str = "P0001", *,
                 samples: int = 1, hidden: int = 2,
                 points: int | Sequence[int] = DEFAULT_TESTCASE_POINTS) -> Problem:
    """造一道题。``points`` 可以是一个整数（每个点同分）或逐个点的分值列表。

    **分值属于测试点，满分是它们的和** —— 默认 10 分，于是 3 个点的题满分是
    30 而不是 100。想造"满分 100"的题就传 10 分 × 10 个点，或者按 NOI 的
    每题 100 分约定给够总点数。
    """
    total = samples + hidden
    values = ([points] * total if isinstance(points, int) else list(points))
    if len(values) != total:
        raise ValueError(f"points 要给 {total} 个值，收到 {len(values)} 个")
    cases = [CaseModel(input=f"sample-in-{i}", output=f"sample-out-{i}",
                       sample=True, points=values[i]) for i in range(samples)]
    cases += [CaseModel(input=f"HIDDEN-IN-{i}", output=f"SECRET-ANSWER-{i}",
                        points=values[samples + i]) for i in range(hidden)]
    return Problem(id=problem_id, title=f"题目{problem_id}",
                   description="读入两个整数，输出它们的和。",
                   time_limit=1000, memory_limit=256, testcases=cases)


def fake_judge(passed: int, total: int, *, time_ms: float = 10.0,
               memory_mb: float = 5.0):
    """造一个**不带分值**的假判题器：按给定通过数返回报告。

    没有分值就走"按通过比例折算"那条兼容路径 —— 外部接入的判题器可以只填
    verdict 与通过数，所以这条路必须一直能用。折算的分母是**该题的分值之和**
    （服务端从 ``ExamProblemView.points`` 取），不是恒定 100，这样它和 NOI
    路径共用一把尺子。
    """

    def judge(problem: Problem, code: str, language: Language) -> JudgeReport:
        report = JudgeReport(problem_id=problem.id, problem_title=problem.title,
                             language=language,
                             verdict=Verdict.AC if passed == total else Verdict.WA)
        for index in range(1, total + 1):
            ok = index <= passed
            report.outcomes.append(Outcome(
                index=index, total=total,
                verdict=Verdict.AC if ok else Verdict.WA,
                time_ms=time_ms, memory_mb=memory_mb))
        return report

    return judge


def fake_scored_judge(points: int | Sequence[int], passed: int, total: int):
    """造一个**带测试点分值**的假判题器：走 NOI 那条给分路径。

    与 :func:`fake_judge` 的唯一差别就是 ``Outcome.points``。有了它，
    ``JudgeReport.points_earned`` / ``points_total`` 才有值，分数才是
    "通过的测试点的分值之和"，满分也才是"各点分值之和"。

    注意让 ``total`` 与题目的测试点数一致：满分的分母同时来自"判题器报告的
    各点分值之和"（这一次提交的 ``possible``）和"题目测试点分值之和"
    （本场榜上的 ``max_total_score``），真判题器两者必然相等，假判题器要
    自己对齐，否则验出来的数字会前后打架。
    """

    def judge(problem: Problem, code: str, language: Language) -> JudgeReport:
        values = ([points] * total if isinstance(points, int) else list(points))
        report = JudgeReport(problem_id=problem.id, problem_title=problem.title,
                             language=language,
                             verdict=Verdict.AC if passed == total else Verdict.WA)
        for index in range(1, total + 1):
            report.outcomes.append(Outcome(
                index=index, total=total,
                verdict=Verdict.AC if index <= passed else Verdict.WA,
                time_ms=10.0, memory_mb=5.0,
                points=values[index - 1]))
        return report

    return judge


class MessageLog:
    """带后台读取线程的消息收集器。

    涉及"到点自动发生"的用例（强制收卷、考试模式放榜）没法用"发一条收一条"
    的同步节奏去等，只能起读取线程把推送攒起来再查。这里刻意不碰
    :class:`ExamClient` 的私有字段，而是走它公开的 ``on_message`` 回调。
    """

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, kind: str, message: dict) -> None:
        with self._lock:
            self.messages.append((kind, dict(message)))

    def snapshot(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.messages)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.snapshot()]

    def of(self, kind: str) -> list[dict]:
        return [message for got, message in self.snapshot() if got == kind]

    def wait_until(self, predicate, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate(self.snapshot()):
                return True
            time.sleep(0.05)
        return predicate(self.snapshot())


def expect_kind(client: ExamClient, kind: str, *, limit: int = 40) -> dict:
    """读到 ``kind`` 为止，跳过主机周期性的状态/排名广播。

    回执没有请求 id，无法和广播严格配对，所以这里只做**有限度**的跳过：
    只忽略明确的周期性广播，遇到别的类型立刻失败。无限跳过会把
    "主机回了别的东西"这种真实故障掩盖成"等不到"。
    """
    for _ in range(limit):
        got, message = client.recv()
        if got == kind:
            return message
        if got not in UNSOLICITED_KINDS:
            raise AssertionError(f"期望 {kind}，实际收到 {got}：{message}")
    raise AssertionError(f"等了 {limit} 条消息也没等到 {kind}")


class ServerFixture:
    """起一台主机，测完负责收干净。"""

    def __init__(self, *, problems=None, judge=None,
                 mode: RoomMode = RoomMode.PRACTICE,
                 room_code: str = "135790", password: str = "",
                 force_collect: bool = False, allow_resubmit: bool = True,
                 show_leaderboard: bool = True, policy: ExamPolicy | None = None,
                 starts_in: int = -5, duration: int = 120,
                 entry_mode: EntryMode = EntryMode.ROOM_CODE,
                 roster: list[dict] | None = None,
                 status_interval: float = 0.2,
                 leaderboard_interval: float = 0.2):
        # 默认"5 秒前开始、120 分钟时长"：被测的是网络链路，
        # 不该让"测验在中途真实到期"这种时钟抖动混淆失败原因。
        # 默认练习模式：绝大多数用例关心的是"判完就广播"这条实时链路。
        # 广播间隔从生产环境的 5 / 10 秒压到 0.2 秒以缩短用例耗时；
        # 想验"对端一直不说话"的用例可以把它调大，让连接真的安静下来。
        self.session = ExamSession(
            "S-E2E", title="集成测试", mode=mode, room_code=room_code,
            password=password, force_collect=force_collect,
            allow_resubmit=allow_resubmit, show_leaderboard=show_leaderboard,
            policy=policy, duration_minutes=duration,
            entry_mode=entry_mode,
            starts_at=datetime.now() + timedelta(seconds=starts_in),
        )
        if roster is not None:
            self.session.set_contestants(roster)
        self.problem_models = list(problems or [make_problem()])
        self.session.set_problems(
            build_exam_problem(p) for p in self.problem_models)
        self.server = ExamServer(
            self.session, self.problem_models,
            config=ServerConfig(bind_host="127.0.0.1", port=0, max_workers=2,
                                status_interval=status_interval,
                                leaderboard_interval=leaderboard_interval),
            judge=judge,
        )
        self._device_seq = 0

    def start(self) -> "ServerFixture":
        self.server.start()
        return self

    def stop(self) -> None:
        self.server.stop()

    def new_device_id(self) -> str:
        """造一个合法的八位设备 ID。字符集只含 0-9 与 A-Z（去掉 I/L/O/U）。"""
        self._device_seq += 1
        return f"DEV{self._device_seq:05d}"

    def client(self, device_id: str, username: str, **kwargs) -> ExamClient:
        return ExamClient("127.0.0.1", self.server.port, self.session.room_code,
                          device_id, username,
                          password=self.session.password, **kwargs)

    def account_client(self, device_id: str, account: str, passcode: str,
                       **kwargs) -> ExamClient:
        """账号进场用的客户端：**不需要房间号，也不需要用户名** ——
        名字由主机按名单回填，这正是这条路径要验的事。"""
        kwargs.setdefault("password", self.session.password)
        return ExamClient("127.0.0.1", self.server.port, "", device_id, "",
                          account=account, passcode=passcode, **kwargs)


class BaseCase(unittest.TestCase):
    """每条用例一台**全新**主机 —— 理由见模块顶部说明。"""

    def setUp(self):
        self.fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        self.addCleanup(self.fixture.stop)
        self.session = self.fixture.session

    def connect(self, username: str, *, device_id: str | None = None,
                drain: bool = True, log: MessageLog | None = None,
                **kwargs) -> ExamClient:
        if log is not None:
            kwargs["on_message"] = log
        client = self.fixture.client(device_id or self.fixture.new_device_id(),
                                     username, **kwargs)
        self.addCleanup(client.close)
        client.connect()
        if drain and log is None:
            client.drain_snapshot()
        return client

    def connect_logged(self, username: str, **kwargs):
        """连上并起读取线程，返回 ``(客户端, 消息收集器)``。

        ``self.connect`` 已经把握手做完了，这里只补上读取线程 ——
        再调一次 ``connect()`` 会另开一个 socket 而把上一个直接丢掉
        （旧连接没人关，主机那边要等到心跳超时才收场）。
        """
        log = MessageLog()
        client = self.connect(username, drain=False, log=log, **kwargs)
        client.start_listener()
        return client, log


class TestHandshake(BaseCase):
    """鉴权链路：房间号即凭据。"""

    def test_valid_room_code_connects(self):
        client = self.connect("张三")
        self.assertEqual(client.welcome["username"], "张三")
        self.assertEqual(client.welcome["device_id"], client.device_id)
        self.assertEqual(client.welcome["session_id"], "S-E2E")
        self.assertIn("exam", client.welcome)

    def test_wrong_room_code_is_rejected(self):
        client = ExamClient("127.0.0.1", self.fixture.server.port, "999999",
                            self.fixture.new_device_id(), "王五")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        self.assertIn("房间号", str(caught.exception))

    def test_wrong_room_password_is_rejected(self):
        fixture = ServerFixture(password="s3cret").start()
        self.addCleanup(fixture.stop)
        client = ExamClient("127.0.0.1", fixture.server.port,
                            fixture.session.room_code, "DEV00001", "张三",
                            password="guess")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        self.assertIn("房间号或房间口令", str(caught.exception))

    def test_correct_room_password_works(self):
        fixture = ServerFixture(password="s3cret").start()
        self.addCleanup(fixture.stop)
        client = fixture.client("DEV00001", "张三")
        client.connect()
        self.assertEqual(client.welcome["username"], "张三")

    def test_reconnect_with_the_same_device_id_works(self):
        """断线重连必须能进来，否则现场没法补救。"""
        first = self.connect("张三", device_id="DEV00001")
        first.close()
        again = self.connect("张三", device_id="DEV00001")
        self.assertEqual(again.welcome["username"], "张三")

    def test_same_device_id_from_two_connections_replaces(self):
        """同一设备 ID 又连上来：旧连接被顶掉，新连接接管，名单里只有一行。"""
        self.connect("张三", device_id="DEV00001")      # 刻意不关，让它还是"在线"
        self.connect("张三", device_id="DEV00001")
        self.assertEqual(len(self.session.participants), 1)
        self.assertEqual(len(self.session.replacements), 1)
        self.assertEqual(self.session.replacements[0]["device_id"], "DEV00001")
        self.assertIn("DEV00001", self.fixture.server.connected_devices())

    def test_duplicate_usernames_are_allowed(self):
        """两个张三各占一行 —— 设备 ID 才是身份。"""
        self.connect("张三", device_id="DEV00001")
        self.connect("张三", device_id="DEV00002")
        self.assertEqual(len(self.session.participants), 2)

    def test_blank_username_is_rejected(self):
        client = self.fixture.client(self.fixture.new_device_id(), "   ")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        self.assertIn("用户名", str(caught.exception))

    def test_bad_device_id_is_rejected(self):
        client = ExamClient("127.0.0.1", self.fixture.server.port,
                            self.session.room_code, "ABC", "张三")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        self.assertIn("8 位", str(caught.exception))

    def test_unreachable_host_raises_connect_error(self):
        client = ExamClient("127.0.0.1", 1, "135790", "DEV00001", "张三",
                            timeout=2)
        with self.assertRaises(ConnectError):
            client.connect()

    def test_room_code_never_appears_on_the_wire(self):
        """房间号是凭据，握手线上跑的只能是它的单向索引。

        一旦房间号明文上线，同网段抓一次包就等于拿到了钥匙，
        后面所有的传输加密都成了摆设。
        """
        code, password = "246810", "s3cret"
        receiver, sender = socket.socketpair()
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)

        written: list[bytes] = []

        class Recorder:
            """只抄不发的套接字壳：记录 PlainChannel 究竟写了什么。"""

            def __init__(self, sock):
                self._sock = sock

            def sendall(self, data):
                written.append(bytes(data))

            def recv(self, size):
                return self._sock.recv(size)

        protocol.PlainChannel(Recorder(sender)).send({
            "kind": protocol.MessageKind.HELLO,
            "protocol_version": 3,
            "room_id": room_id(code, password),
            "supported_suites": list(crypto.SUPPORTED_SUITES),
            "client_pub": crypto.x25519_generate_keypair()[1].hex(),
        })

        wire = b"".join(written)
        # 明文帧 = [4 字节帧长][4 字节 JSON 长度][JSON][二进制体]，
        # 用生产代码的解码器拆，避免手算偏移写错（帧长头不含在 payload 里）
        message, blob = protocol.decode_body(wire[4:])
        self.assertEqual(blob, b"")
        self.assertEqual(message["room_id"], room_id(code, password))

        # 只扫 JSON 文本本身（跳过 4 字节帧长头 + 4 字节 JSON 长头，两者都是
        # 二进制整数，不含凭据）。用生产代码同款 _JSON_LEN 解析偏移，避免手算。
        (json_len,) = protocol._JSON_LEN.unpack_from(wire, 4)
        json_text = wire[8:8 + json_len].decode("utf-8")
        self.assertNotIn(code, json_text)
        self.assertNotIn(password, json_text)
        # 再按字段比一遍：字符集与十六进制有交集，按子串比理论上有假阳性
        self.assertNotIn(code, [str(value) for value in message.values()])

    def test_username_and_device_id_are_not_in_the_clear(self):
        """设备 ID 与用户名都收进加密的 AUTH 帧，明文段里不该有它们。"""
        plaintext = protocol.encode_body({
            "kind": protocol.MessageKind.HELLO, "protocol_version": 3,
            "room_id": room_id("135790"),
            "supported_suites": list(crypto.SUPPORTED_SUITES),
            "client_pub": crypto.x25519_generate_keypair()[1].hex(),
        })
        self.assertNotIn(b"username", plaintext)
        self.assertNotIn(b"device_id", plaintext)

    def test_version_mismatch_is_rejected(self):
        """两端版本不一致要当场挡住，不能"连上了但行为诡异"。"""
        reply = self.raw_hello(protocol_version=1)
        self.assertEqual(reply["kind"], protocol.MessageKind.REJECTED)
        self.assertIn("版本不匹配", reply["reason"])

    def test_talking_without_hello_is_rejected(self):
        reply = self.raw_hello(kind="how-are-you")
        self.assertEqual(reply["kind"], protocol.MessageKind.REJECTED)
        self.assertIn("没有先打招呼", reply["reason"])

    def test_unknown_room_fingerprint_is_rejected(self):
        reply = self.raw_hello(room_id="0" * 16)
        self.assertEqual(reply["kind"], protocol.MessageKind.REJECTED)
        self.assertIn("房间号", reply["reason"])

    def raw_hello(self, **overrides) -> dict:
        """手搓一次明文握手，返回主机的第一条回执。

        用来测那些"正常客户端根本发不出来"的帧（错版本、不打先招呼、
        伪造的房间号索引）—— 这些正是攻击面所在。
        """
        payload = {"kind": protocol.MessageKind.HELLO,
                   "protocol_version": 3,
                   "room_id": self.session.fingerprint,
                   "supported_suites": list(crypto.SUPPORTED_SUITES),
                   "client_pub": crypto.x25519_generate_keypair()[1].hex()}
        payload.update(overrides)
        sock = socket.create_connection(("127.0.0.1", self.fixture.server.port),
                                        timeout=5)
        try:
            channel = protocol.PlainChannel(sock)
            channel.send(payload)
            reply, _ = channel.recv()
            return reply
        finally:
            sock.close()


class TestProblemDelivery(BaseCase):
    """题面下发。"""

    def test_fetch_returns_problems(self):
        client = self.connect("张三")
        problems = client.fetch_problems()
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].id, "P0001")
        self.assertIn("读入两个整数", problems[0].description)

    def test_only_samples_are_sent(self):
        """正式测试点的期望输出绝不能下发 —— 这是整个架构的安全底线。"""
        client = self.connect("张三")
        client.fetch_problems()
        problem = client.problem("P0001")
        self.assertEqual(len(problem.samples), 1)
        self.assertEqual(problem.samples[0]["output"], "sample-out-0")
        blob = repr(problem.to_dict())
        self.assertNotIn("SECRET-ANSWER", blob)
        self.assertNotIn("HIDDEN-IN", blob)

    def test_problem_without_samples_sends_no_testcases(self):
        """一道题一个样例都没标时，宁可不给样例，也不能退而发送全部。"""
        model = make_problem("P2002", samples=0, hidden=3)
        view = build_exam_problem(model)
        self.assertEqual(view.samples, [])
        self.assertNotIn("SECRET-ANSWER", repr(view.to_dict()))

    def test_ccf_data_file_names_travel_with_the_problem(self):
        """文件模式的题目要把数据文件名一起下发。

        不下发的话，学生只能靠题面里的文字描述去猜，写出来的代码在本机自测
        能过、交上去必然 RE —— 而且失败原因（找不到 poker.in）在服务端看来
        只是"输出文件没生成"。
        """
        from offline_oj.core.models import IOMode, JudgeConfig

        model = Problem(id="P3003", title="文件题", slug="poker",
                        judge=JudgeConfig(io_mode=IOMode.FILE),
                        testcases=[CaseModel(input="1 2", output="3",
                                             sample=True)])
        view = build_exam_problem(model)
        self.assertEqual(view.io_mode, "file")
        self.assertEqual(view.input_file, "poker.in")
        self.assertEqual(view.output_file, "poker.out")
        self.assertEqual(view.slug, "poker")
        round_trip = type(view).from_dict(view.to_dict())
        self.assertEqual(round_trip.input_file, "poker.in")

    def test_no_secret_leaks_anywhere_in_the_stream(self):
        """把整条连接上**解出来的每一条消息**都翻一遍，找正式测试点的痕迹。

        上面那条只看题面对象；这条兜底：万一将来有人往 WELCOME、状态广播
        或排名里塞了题目详情，也会在这里被抓住。
        """
        received: list[str] = []
        client = self.connect(
            "张三",
            on_message=lambda _kind, message: received.append(repr(message)))
        client.fetch_problems()
        client.request_leaderboard()
        for _ in range(20):
            kind, _message = client.recv()
            if kind == protocol.MessageKind.LEADERBOARD:
                break

        self.assertGreater(len(received), 0, "没有捕获到任何消息")
        blob = "".join(received)
        self.assertIn("sample-out-0", blob)       # 样例确实下发过，排除空跑
        self.assertNotIn("SECRET-ANSWER", blob)
        self.assertNotIn("HIDDEN-IN", blob)

    def test_welcome_carries_exam_summary(self):
        client = self.connect("张三")
        self.assertEqual(client.exam["title"], "集成测试")
        self.assertEqual(client.exam["state"], ExamState.RUNNING.value)

    def test_remaining_seconds_is_positive_during_exam(self):
        client = self.connect("张三")
        self.assertGreater(client.remaining_seconds(), 0)

    def test_summary_never_carries_the_room_code(self):
        """状态摘要会广播给所有人，凭据不能进它。"""
        client = self.connect("张三")
        blob = repr(client.exam) + repr(client.welcome)
        self.assertNotIn(self.session.room_code, blob)


class TestSubmitAndVerdict(BaseCase):
    """提交与判定回传。"""

    def test_submit_returns_pending_then_verdict(self):
        client = self.connect("张三")
        client.submit("P0001", "cpp", "int main(){}")

        # 先收到"已收到，等待评测"的占位，再收到真正的判定
        placeholder = expect_kind(client, protocol.MessageKind.VERDICT)
        self.assertTrue(placeholder.get("pending"))
        self.assertEqual(placeholder["problem_id"], "P0001")
        self.assertEqual(placeholder["attempt"], 1)
        self.assertIn("第 1 次", placeholder["message"])

        verdict = client.wait_for_verdict()
        self.assertEqual(verdict["verdict"], "AC")
        # 满分跟着测试点分值走：P0001 有 3 个点、每点默认 10 分 → 满分 30
        self.assertEqual(verdict["score"], 30)
        self.assertEqual(verdict["possible"], 30)
        self.assertEqual(verdict["problem_id"], "P0001")
        self.assertFalse(verdict.get("pending"))

        self.assertEqual(len(self.session.submissions), 1)
        record = self.session.submissions[0]
        self.assertEqual(record.username, "张三")
        self.assertEqual(record.verdict, "AC")
        self.assertEqual(record.score, 30)
        self.assertIsNotNone(record.judged_at)

    def test_the_code_reaches_the_host_and_stops_there(self):
        """源码传得到主机，但主机不会把它转发给任何人。

        「老师看得到代码、学生只看得到榜单」不是界面藏起来的：源码在
        ``Submission.to_dict()`` 里就被排除了，进不了任何出站载荷。这条同时验
        两头 —— 主机侧收全了（「提交与代码」页才有得看），学生侧一个字节都没收到。
        """
        marker = "// SECRET-MARKER-9F3A"
        code = f"{marker}\nint main(){{ return 0; }}"
        client, log = self.connect_logged("张三")

        client.submit("P0001", "cpp", code)
        self.assertTrue(
            log.wait_until(lambda items: any(
                kind == protocol.MessageKind.VERDICT and not message.get("pending")
                for kind, message in items)),
            f"没等到判定结果，收到的是：{log.kinds()}")

        # 主机这边：完整源码留在内存里
        self.assertEqual(len(self.session.submissions), 1)
        self.assertIn(marker, self.session.submissions[0].code)

        # 线路那边：学生收到的每一个载荷里都没有它
        self.assertNotIn(marker, repr(log.snapshot()), "源码被回传给客户端了")
        verdict = log.of(protocol.MessageKind.VERDICT)[-1]
        self.assertEqual(verdict["verdict"], "AC")
        self.assertFalse(verdict.get("pending"))

    def test_unknown_problem_is_an_error(self):
        client = self.connect("张三")
        client.send({"kind": protocol.MessageKind.SUBMIT, "problem_id": "NOPE",
                     "language": "cpp", "code": "x"})
        message = expect_kind(client, protocol.MessageKind.ERROR)
        self.assertIn("不存在", message["message"])
        self.assertEqual(self.session.submissions, [])

    def test_unsupported_language_is_an_error(self):
        client = self.connect("张三")
        client.send({"kind": protocol.MessageKind.SUBMIT, "problem_id": "P0001",
                     "language": "brainfuck", "code": "x"})
        message = expect_kind(client, protocol.MessageKind.ERROR)
        self.assertIn("语言", message["message"])

    def test_empty_code_is_an_error(self):
        client = self.connect("张三")
        client.send({"kind": protocol.MessageKind.SUBMIT, "problem_id": "P0001",
                     "language": "cpp", "code": "   "})
        message = expect_kind(client, protocol.MessageKind.ERROR)
        self.assertIn("空", message["message"])

    def test_partial_score_from_fake_judge(self):
        """判题器只填通过数、不给分值时，按**该题分值之和**折算。

        折算的分母跟着题目走（P0001 = 3 个点 × 10 分 = 30），不是老的恒定
        100 —— 否则榜上会出现"得分 100 / 满分 30"这种读不通的行。
        """
        fixture = ServerFixture(judge=fake_judge(1, 3)).start()
        try:
            client = fixture.client("DEV00001", "半个")
            client.connect()
            client.drain_snapshot()
            client.submit("P0001", "python", "print(1)")
            verdict = client.wait_for_verdict()
            self.assertEqual(verdict["passed"], 1)
            self.assertEqual(verdict["total"], 3)
            self.assertEqual(verdict["score"], 10)      # 1/3 × 30
            self.assertEqual(verdict["possible"], 30)
            client.close()
        finally:
            fixture.stop()

    def test_unknown_message_kind_is_an_error(self):
        client = self.connect("张三")
        client.send({"kind": "teleport"})
        message = expect_kind(client, protocol.MessageKind.ERROR)
        self.assertIn("teleport", message["message"])

    def test_attempts_are_numbered_per_person_and_problem(self):
        """允许重复提交时，第几次要真的数出来。"""
        client = self.connect("张三")
        for expected in (1, 2, 3):
            client.submit("P0001", "cpp", f"// v{expected}")
            placeholder = expect_kind(client, protocol.MessageKind.VERDICT)
            self.assertEqual(placeholder["attempt"], expected)
            client.wait_for_verdict()
        self.assertEqual([s.attempt for s in self.session.submissions], [1, 2, 3])

    def test_resubmit_can_be_forbidden(self):
        """关掉重复提交后，第二次要被明确挡回来，且不留记录。"""
        fixture = ServerFixture(judge=fake_judge(2, 2),
                                allow_resubmit=False).start()
        try:
            client = fixture.client("DEV00001", "张三")
            client.connect()
            client.drain_snapshot()
            client.submit("P0001", "cpp", "int main(){}")
            client.wait_for_verdict()

            client.submit("P0001", "cpp", "// 又交一次")
            message = expect_kind(client, protocol.MessageKind.ERROR)
            self.assertIn("不允许重复提交", message["message"])
            self.assertEqual(len(fixture.session.submissions), 1)
            client.close()
        finally:
            fixture.stop()

    def test_forbidden_resubmit_still_allows_another_problem(self):
        fixture = ServerFixture(problems=[make_problem("P0001"),
                                          make_problem("P0002")],
                                judge=fake_judge(2, 2),
                                allow_resubmit=False).start()
        try:
            client = fixture.client("DEV00001", "张三")
            client.connect()
            client.drain_snapshot()
            client.submit("P0001", "cpp", "a")
            client.wait_for_verdict()
            client.submit("P0002", "cpp", "b")
            verdict = client.wait_for_verdict()
            self.assertEqual(verdict["problem_id"], "P0002")
            self.assertEqual(verdict["verdict"], "AC")
            client.close()
        finally:
            fixture.stop()


class TestRankingBroadcast(BaseCase):
    """练习模式的实时排名广播。"""

    def test_ranking_orders_by_score_then_time_but_ties_share_the_rank(self):
        """同题先按分数，同分者按耗时显示 —— 但**同分是同一个名次**。

        需求原文要的"时间空间排名"就是这条链：它决定谁显示在前面。
        名次本身按 NOI 的规矩并列，于是三个人全是满分时，榜上是"三个第 1"。
        """
        fixture = ServerFixture(judge=None).start()
        try:
            # 三个人、三种速度，全部满分 → 只能靠耗时决定先后
            plans = {"慢手": 900.0, "快手": 80.0, "中手": 400.0}
            for index, (name, elapsed) in enumerate(plans.items(), start=1):
                fixture.server._judge = fake_judge(2, 2, time_ms=elapsed)
                client = fixture.client(f"DEV{index:05d}", name)
                client.connect()
                client.drain_snapshot()
                client.submit("P0001", "cpp", "int main(){}")
                client.wait_for_verdict()
                client.close()

            rows = fixture.session.problem_ranking("P0001")
            self.assertEqual([r.username for r in rows], ["快手", "中手", "慢手"])
            self.assertEqual([r.rank for r in rows], [1, 1, 1])
            self.assertEqual([r.submission.time_ms for r in rows],
                             [80.0, 400.0, 900.0])
        finally:
            fixture.stop()

    def test_lower_score_ranks_below_even_if_faster(self):
        """分数优先于耗时：错得快的不能压过对的。"""
        fixture = ServerFixture(judge=None).start()
        try:
            for index, (name, passed, elapsed) in enumerate(
                    (("快但错", 1, 1.0), ("慢但对", 2, 5000.0)), start=1):
                fixture.server._judge = fake_judge(passed, 2, time_ms=elapsed)
                client = fixture.client(f"DEV{index:05d}", name)
                client.connect()
                client.drain_snapshot()
                client.submit("P0001", "cpp", name)
                client.wait_for_verdict()
                client.close()

            rows = fixture.session.problem_ranking("P0001")
            self.assertEqual([r.username for r in rows], ["慢但对", "快但错"])
            # 2/2 与 1/2 折到该题的 30 分上
            self.assertEqual([r.submission.score for r in rows], [30, 15])
            self.assertEqual([r.rank for r in rows], [1, 2])
        finally:
            fixture.stop()

    def test_leaderboard_is_pushed_to_other_clients(self):
        """判完一题要广播给所有人，不能只有提交者看得到。"""
        watcher = self.connect("旁观")
        author = self.connect("张三")

        author.submit("P0001", "cpp", "int main(){}")
        author.wait_for_verdict()

        seen = None
        for _ in range(80):
            kind, message = watcher.recv()
            if kind != protocol.MessageKind.LEADERBOARD:
                continue
            rows = message.get("overall") or []
            if any(row["username"] == "张三" and row["score"] > 0
                   for row in rows):
                seen = message
                break
        self.assertIsNotNone(seen, "旁观者没有收到含张三得分的排行榜")

        per_problem = seen.get("per_problem") or {}
        self.assertIn("P0001", per_problem)
        self.assertEqual(per_problem["P0001"][0]["username"], "张三")
        # 一条提交都没交的人也要留在榜上 —— "考了 0 分"和"榜上无名"不一样
        self.assertIn("旁观", [row["username"] for row in seen["overall"]])

    def test_board_shows_device_ids_and_attempts(self):
        """榜上要能看见设备八位 ID 与"计入总分的是第几次"。"""
        fixture = ServerFixture(judge=None).start()
        try:
            fixture.server._judge = fake_judge(1, 2)
            client = fixture.client("ABCD2345", "张三")
            client.connect()
            client.drain_snapshot()
            client.submit("P0001", "cpp", "first")
            client.wait_for_verdict()
            fixture.server._judge = fake_judge(2, 2)
            client.submit("P0001", "cpp", "second")
            client.wait_for_verdict()

            row = fixture.session.overall_ranking()[0].to_dict()
            self.assertEqual(row["device_id"], "ABCD2345")
            self.assertEqual(row["per_problem_attempts"]["P0001"], 2)
            self.assertEqual(row["submit_count"], 2)
            self.assertEqual(row["per_problem"]["P0001"], 30)
            client.close()
        finally:
            fixture.stop()

    def test_overall_ranking_reflects_scores(self):
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        try:
            for index, name in enumerate(("甲", "乙"), start=1):
                client = fixture.client(f"DEV{index:05d}", name)
                client.connect()
                client.drain_snapshot()
                client.submit("P0001", "cpp", name)
                client.wait_for_verdict()
                client.close()
            rows = fixture.session.overall_ranking()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.score for row in rows}, {30})
            # 同分并列：两个满分就是两个第 1（NOI 的成绩单也是这么排）
            self.assertEqual([row.rank for row in rows], [1, 1])
        finally:
            fixture.stop()


class TestPointValuesOnTheWire(unittest.TestCase):
    """按测试点分值给分（NOI 式），端到端。

    分值决定了这次提交的满分：3 个点各 10 分就是 30 分制。**两条路共用这一把
    尺子** —— 判题器报分值就直接累加，只报通过数就按通过比例折到同样的 30 分上，
    所以"全对"在两种判题器下都是该题满分。端到端钉一次，是因为这里同时牵动
    ``TestCase.points`` → ``JudgeReport`` → ``Submission`` → 榜单载荷 → 界面，
    只在单元层各测一半会漏掉"中间某一跳没带上分值"。
    """

    def _submit(self, fixture, judge, name: str = "张三") -> dict:
        fixture.server._judge = judge
        client = fixture.client(fixture.new_device_id(), name)
        self.addCleanup(client.close)
        client.connect()
        client.drain_snapshot()
        client.submit("P0001", "cpp", "int main(){}")
        return client.wait_for_verdict()

    def test_three_cases_of_ten_points_make_a_thirty_point_problem(self):
        """3 个点各 10 分：满分 30，全过就是 30 分 —— 不是 100 分。"""
        fixture = ServerFixture(
            problems=[make_problem(samples=0, hidden=3)]).start()
        self.addCleanup(fixture.stop)
        verdict = self._submit(fixture, fake_scored_judge(10, 3, 3))
        self.assertEqual((verdict["passed"], verdict["total"]), (3, 3))
        self.assertEqual(verdict["score"], 30)
        self.assertEqual(verdict["possible"], 30)

    def test_uneven_point_values_are_summed_not_counted(self):
        """分值不等时按**分值**累加，而不是数通过了几个点。

        三个点分别是 50 / 30 / 20 分，只过第一个就是 50 分，
        而不是"过了 1/3 → 33 分"。
        """
        fixture = ServerFixture(
            problems=[make_problem(samples=0, hidden=3,
                                   points=[50, 30, 20])]).start()
        self.addCleanup(fixture.stop)
        verdict = self._submit(fixture, fake_scored_judge([50, 30, 20], 1, 3))
        self.assertEqual(verdict["passed"], 1)
        self.assertEqual(verdict["score"], 50)
        self.assertEqual(verdict["possible"], 100)

    def test_the_board_carries_the_point_based_denominator(self):
        """榜上的满分跟着测试点分值走 —— 学生要靠它把"10 分"读成"满分 50"."""
        fixture = ServerFixture(
            problems=[make_problem(samples=0, hidden=5)]).start()
        self.addCleanup(fixture.stop)
        self._submit(fixture, fake_scored_judge(10, 1, 5))

        payload = fixture.session.leaderboard_payload()
        self.assertEqual(payload["max_total_score"], 50)
        self.assertEqual(payload["overall"][0]["score"], 10)
        row = payload["per_problem"]["P0001"][0]
        self.assertEqual(row["score"], 10)
        self.assertEqual(row["possible"], 50)

    def test_a_judge_without_point_values_still_scores_by_ratio(self):
        """没分值的判题器按通过比例折算，分母仍是**该题分值之和**。

        这条兼容路径不能断：外部接入的判题器可以只填 verdict 与通过数。
        但它必须和 NOI 路径落在同一刻度上 —— 4 个点的题满分 40，过一半就是
        20，而不是老的"40 分制里考出 50"。
        """
        fixture = ServerFixture(
            problems=[make_problem(samples=0, hidden=4)]).start()
        self.addCleanup(fixture.stop)
        verdict = self._submit(fixture, fake_judge(2, 4))
        self.assertEqual(verdict["score"], 20)
        self.assertEqual(verdict["possible"], 40)
        # 榜上的分母也是 40：分子分母同刻度
        payload = fixture.session.leaderboard_payload()
        self.assertEqual(payload["max_total_score"], 40)
        self.assertEqual(payload["overall"][0]["score"], 20)

    def test_a_full_score_problem_shows_up_as_solved(self):
        """每题满分不再是 100，"已解决"必须按**本题**满分判断。"""
        fixture = ServerFixture(
            problems=[make_problem(samples=0, hidden=3)]).start()
        self.addCleanup(fixture.stop)
        self._submit(fixture, fake_scored_judge(10, 3, 3))
        row = fixture.session.overall_ranking()[0]
        self.assertEqual(row.solved, 1)
        self.assertEqual(row.per_problem["P0001"], 30)


class TestExamMode(unittest.TestCase):
    """考试模式：全程封榜，结束后统一放榜。"""

    def setUp(self):
        self.fixture = ServerFixture(judge=fake_judge(2, 2),
                                     mode=RoomMode.EXAM).start()
        self.addCleanup(self.fixture.stop)
        self.session = self.fixture.session

    def test_mode_travels_with_the_summary(self):
        client = self.fixture.client("DEV00001", "张三")
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.exam["mode"], "exam")
        self.assertEqual(client.exam["mode_label"], "考试模式")
        self.assertFalse(client.leaderboard_published)

    def test_board_stays_empty_while_running(self):
        """判完也不许广播全榜 —— 否则"考试模式"名存实亡。"""
        log = MessageLog()
        author = self.fixture.client("DEV00001", "张三", on_message=log)
        self.addCleanup(author.close)
        author.connect()
        author.start_listener()

        author.submit("P0001", "cpp", "int main(){}")
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.VERDICT
                              and not m.get("pending") for k, m in items), 6.0),
            "没有等到判定结果")

        boards = log.of(protocol.MessageKind.LEADERBOARD)
        self.assertTrue(boards, "一次榜都没推")
        for board in boards:
            self.assertFalse(board["published"])
            self.assertEqual(board["overall"], [])
            self.assertEqual(board["per_problem"], {})
            self.assertIn("统一公布", board["withheld_reason"])

        # 但"你自己那一行"要私下给到本人 —— 自己的成绩不该也看不到
        myself = [board.get("myself") for board in boards if board.get("myself")]
        self.assertTrue(myself, "封榜期间至少要给本人看自己那一行")
        self.assertEqual(myself[-1]["username"], "张三")
        self.assertEqual(myself[-1]["device_id"], "DEV00001")
        self.assertEqual(myself[-1]["score"], 30)       # P0001 = 3 点 × 10 分

    def test_board_is_published_once_the_exam_is_over(self):
        log = MessageLog()
        client = self.fixture.client("DEV00001", "张三", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()

        client.submit("P0001", "cpp", "int main(){}")
        self.assertTrue(log.wait_until(lambda items: any(
            k == protocol.MessageKind.VERDICT and not m.get("pending")
            for k, m in items)))

        # 到点：结束时刻挪到过去并取消宽限，等 ticker 统一放榜
        self.session.ends_at = datetime.now() - timedelta(seconds=1)
        self.session.grace_seconds = 0

        self.assertTrue(log.wait_until(lambda items: any(
            k == protocol.MessageKind.LEADERBOARD
            and m.get("published") and m.get("overall")
            for k, m in items)), "考试结束后没有统一放榜")

        final = [m for k, m in log.snapshot()
                 if k == protocol.MessageKind.LEADERBOARD and m.get("published")]
        self.assertEqual(final[-1]["overall"][0]["username"], "张三")
        self.assertEqual(final[-1]["overall"][0]["score"], 30)

    def test_published_event_fires_for_the_host_ui(self):
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2), mode=RoomMode.EXAM)
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            fixture.session.ends_at = datetime.now() - timedelta(seconds=1)
            fixture.session.grace_seconds = 0
            deadline = time.time() + 6
            while time.time() < deadline and "published" not in seen:
                time.sleep(0.05)
            self.assertIn("published", seen)
        finally:
            fixture.stop()

    def test_practice_mode_never_emits_published(self):
        """练习模式的榜一开始就是公开的，不该有"放榜"这个时刻。"""
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2), mode=RoomMode.PRACTICE)
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            time.sleep(1.0)
            self.assertNotIn("published", seen)
        finally:
            fixture.stop()


class TestLanguagePolicyOnTheWire(unittest.TestCase):
    """被本场禁掉的语言，服务端必须自己拒。

    **界面上的置灰不是安全边界。** 学生端是可以被改的（改 exe、改内存、直接
    发包都行），所以这里的用例刻意绕开界面 —— 直接照着协议发一个不被允许的
    语言。主机要是"照单收下"，这一条就会红。
    """

    def _fixture(self, policy: ExamPolicy) -> ServerFixture:
        fixture = ServerFixture(judge=fake_judge(2, 2), policy=policy).start()
        self.addCleanup(fixture.stop)
        return fixture

    def test_disallowed_language_is_rejected(self):
        fixture = self._fixture(ExamPolicy(allowed_languages=("cpp",)))
        client = fixture.client("DEV00001", "张三")
        self.addCleanup(client.close)
        client.connect()
        client.drain_snapshot()

        client.submit("P0001", "java", "class Main {}")
        message = expect_kind(client, protocol.MessageKind.ERROR)
        self.assertIn("只收", message["message"])
        self.assertEqual(fixture.session.submissions, [],
                         "被拒的提交不该落进会话里")

    def test_allowed_language_goes_through(self):
        fixture = self._fixture(ExamPolicy(allowed_languages=("cpp",)))
        client = fixture.client("DEV00001", "张三")
        self.addCleanup(client.close)
        client.connect()
        client.drain_snapshot()

        client.submit("P0001", "cpp", "int main(){}")
        self.assertEqual(client.wait_for_verdict()["verdict"], "AC")

    def test_an_unrestricted_room_accepts_every_language(self):
        fixture = self._fixture(ExamPolicy())
        client = fixture.client("DEV00001", "张三")
        self.addCleanup(client.close)
        client.connect()
        client.drain_snapshot()

        client.submit("P0001", "java", "class Main {}")
        self.assertEqual(client.wait_for_verdict()["verdict"], "AC")


class TestRenameBroadcast(unittest.TestCase):
    """改名经一帧 EXAM 广播到学生端，且**不踢人**。"""

    def test_rename_reaches_connected_clients(self):
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        self.addCleanup(fixture.stop)
        log = MessageLog()
        client = fixture.client("DEV00001", "张三", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()

        fixture.server.rename("第三次模拟赛")
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.EXAM
                              and m.get("title") == "第三次模拟赛"
                              for k, m in items), 6.0),
            "改名没有随 EXAM 帧送达学生端")

        # 改完名连接仍然可用 —— 房间号与密钥都跟 title 无关，改名不该把人踢下线
        client.submit("P0001", "cpp", "int main(){}")
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.VERDICT
                              and not m.get("pending") for k, m in items), 6.0),
            "改名之后同一连接就不能用了")
        self.assertFalse(client.closed)

    def test_rename_trims_and_reports_an_event(self):
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2))
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            self.assertEqual(fixture.server.rename("  新名字  "), "新名字")
            self.assertEqual(fixture.session.title, "新名字")
            self.assertIn("renamed", seen)
        finally:
            fixture.stop()


class TestLeaderboardSwitchedOff(unittest.TestCase):
    """关掉放榜：练习模式的实时榜也不再出现，全链路一致。"""

    def test_practice_room_stays_withheld_when_switched_off(self):
        fixture = ServerFixture(judge=fake_judge(2, 2), mode=RoomMode.PRACTICE,
                                show_leaderboard=False).start()
        self.addCleanup(fixture.stop)
        log = MessageLog()
        client = fixture.client("DEV00001", "张三", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()

        client.submit("P0001", "cpp", "int main(){}")
        self.assertTrue(log.wait_until(lambda items: any(
            k == protocol.MessageKind.VERDICT and not m.get("pending")
            for k, m in items)))

        boards = log.of(protocol.MessageKind.LEADERBOARD)
        self.assertTrue(boards, "一次榜都没推")
        for board in boards:
            self.assertFalse(board["published"])
            self.assertEqual(board["overall"], [])
            self.assertEqual(board["per_problem"], {})
            self.assertEqual(board["withheld_reason"], "本场不公布榜单")
        # 关掉的是"公开"，不是"连自己都看不到"
        myself = [b.get("myself") for b in boards if b.get("myself")]
        self.assertTrue(myself, "关掉放榜也要让本人看到自己那一行")
        self.assertEqual(myself[-1]["score"], 30)       # P0001 = 3 点 × 10 分

    def test_no_published_event_when_switched_off(self):
        """没有榜可放时，不该通知主机界面"已放榜" —— 那会让人以为学生看到了。"""
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2), mode=RoomMode.EXAM,
                                show_leaderboard=False)
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            fixture.session.ends_at = datetime.now() - timedelta(seconds=1)
            fixture.session.grace_seconds = 0
            time.sleep(1.0)
            self.assertNotIn("published", seen)
        finally:
            fixture.stop()


class TestStartBroadcast(unittest.TestCase):
    """开考由**主机说出来**，不是让学生自己看表。

    学生机的系统时钟不可信（时区没设对、对时失败、故意改表都会中招），
    一旦按本地时间判定，同一间教室里会同时出现"有人提前进场"和"有人永远
    进不去"。所以主机在开考那一刻要主动广播一帧 ``START``。
    """

    def test_start_frame_unlocks_submission(self):
        fixture = ServerFixture(judge=fake_judge(2, 2), starts_in=60)
        self.addCleanup(fixture.stop)
        fixture.start()
        log = MessageLog()
        client = fixture.client("DEV00001", "早到", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()
        self.assertEqual(client.exam["state"], ExamState.PENDING.value)

        self.assertTrue(fixture.server.start_exam())
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.START
                              for k, _ in items), 6.0),
            "提前开考没有广播 START 帧")
        start = log.of(protocol.MessageKind.START)[-1]
        self.assertEqual(start["reason"], "host")
        self.assertEqual(start["state"], ExamState.RUNNING.value)

        # 不用等下一帧周期状态：开考帧本身就该把提交解锁
        self.assertEqual(client.exam["state"], ExamState.RUNNING.value)
        client.submit("P0001", "cpp", "int main(){}")
        self.assertTrue(log.wait_until(lambda items: any(
            k == protocol.MessageKind.VERDICT and not m.get("pending")
            for k, m in items), 6.0), "开考之后仍然交不上卷")

    def test_start_frame_is_isomorphic_to_exam(self):
        """``START`` 比 ``EXAM`` 只多一个 ``reason``。

        两帧同构是客户端"只维护一份 ``self.exam``"的前提。哪天有人往 START
        里塞一个 EXAM 没有的字段（或反过来），界面就会在两个来源之间分叉，
        而且症状只会在"到点开考"这一条路径上出现 —— 平时根本跑不到。
        """
        fixture = ServerFixture(judge=fake_judge(2, 2), starts_in=60)
        self.addCleanup(fixture.stop)
        fixture.start()
        log = MessageLog()
        client = fixture.client("DEV00001", "早到", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.EXAM
                              for k, _ in items), 6.0))
        exam_frame = log.of(protocol.MessageKind.EXAM)[-1]

        fixture.server.start_exam()
        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.START
                              for k, _ in items), 6.0))
        start_frame = log.of(protocol.MessageKind.START)[-1]
        self.assertEqual(set(start_frame), set(exam_frame) | {"reason"})

    def test_deadline_is_announced_exactly_once(self):
        """到点那一刻主机自己播报，而且只播一次。

        客户端不轮询、不重连，只靠这一帧；所以"播两次"比"不播"更糟 ——
        学生端的现场记录里会写下两遍"已开考"。
        """
        fixture = ServerFixture(judge=fake_judge(2, 2), starts_in=2)
        self.addCleanup(fixture.stop)
        fixture.start()
        log = MessageLog()
        client = fixture.client("DEV00001", "等开考", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()
        self.assertIs(fixture.session.state(), ExamState.PENDING)

        self.assertTrue(log.wait_until(
            lambda items: any(k == protocol.MessageKind.START
                              for k, _ in items), 8.0),
            "到点了还不见 START 帧")
        starts = log.of(protocol.MessageKind.START)
        self.assertEqual(starts[0]["reason"], "deadline")
        self.assertEqual(starts[0]["state"], ExamState.RUNNING.value)

        time.sleep(1.2)
        self.assertEqual(len(log.of(protocol.MessageKind.START)), 1,
                         "开考帧被重复播报")
        self.assertEqual(client.exam["state"], ExamState.RUNNING.value)

    def test_start_exam_is_a_no_op_once_running(self):
        """已经开考再点「开始考试」：返回 False，也不该发事件。"""
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2), starts_in=-5)
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            self.assertFalse(fixture.server.start_exam())
            self.assertNotIn("exam_started", seen)
        finally:
            fixture.stop()


class TestForceCollect(unittest.TestCase):
    """到点强制收卷：主机下发收卷指令，客户端据此自动提交并锁住编辑器。"""

    def test_collect_is_broadcast_at_the_deadline(self):
        fixture = ServerFixture(judge=fake_judge(2, 2), force_collect=True,
                                duration=120).start()
        try:
            log = MessageLog()
            client = fixture.client("DEV00001", "张三", on_message=log)
            self.addCleanup(client.close)
            client.connect()
            client.start_listener()

            # 到点：放在半秒后，宽限留 3 秒，确保 ticker 能观察到"剩余 0"
            fixture.session.ends_at = datetime.now() + timedelta(seconds=0.5)
            fixture.session.grace_seconds = 3

            self.assertTrue(
                log.wait_until(lambda items: any(
                    kind == protocol.MessageKind.COLLECT
                    for kind, _ in items), 8.0),
                "到点没有收到收卷指令")
            order = log.of(protocol.MessageKind.COLLECT)[-1]
            self.assertEqual(order["reason"], "deadline")
            self.assertTrue(order["force_collect"])
            self.assertIsNotNone(client.collect_request)
        finally:
            fixture.stop()

    def test_no_collect_when_force_collect_is_off(self):
        """关掉强制收卷就不该下发收卷指令 —— 到点只是不再收新提交。"""
        fixture = ServerFixture(judge=fake_judge(2, 2), force_collect=False,
                                duration=120).start()
        try:
            log = MessageLog()
            client = fixture.client("DEV00001", "张三", on_message=log)
            self.addCleanup(client.close)
            client.connect()
            client.start_listener()

            fixture.session.ends_at = datetime.now() + timedelta(seconds=0.4)
            fixture.session.grace_seconds = 1
            time.sleep(2.0)
            self.assertEqual(log.of(protocol.MessageKind.COLLECT), [])
            # 到点之后仍然收不到新提交（宽限期也已走完）
            self.assertFalse(fixture.session.accepts_submissions())
        finally:
            fixture.stop()

    def test_late_joiner_gets_the_collect_order(self):
        """截止后才连上来的客户端也要被要求交卷，否则它会一直停在"进行中"。"""
        fixture = ServerFixture(judge=fake_judge(2, 2), force_collect=True,
                                duration=1).start()
        try:
            fixture.session.starts_at = datetime.now() - timedelta(minutes=5)
            fixture.session.ends_at = datetime.now() - timedelta(seconds=1)
            fixture.session.grace_seconds = 30

            log = MessageLog()
            client = fixture.client("DEV00001", "迟到", on_message=log)
            self.addCleanup(client.close)
            client.connect()
            client.start_listener()

            self.assertTrue(log.wait_until(lambda items: any(
                kind == protocol.MessageKind.COLLECT for kind, _ in items), 4.0),
                "迟到的客户端没有收到收卷指令")
        finally:
            fixture.stop()

    def test_host_can_collect_early(self):
        """老师提前收卷走的是同一条路径，只是原因不同。"""
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        try:
            log = MessageLog()
            client = fixture.client("DEV00001", "张三", on_message=log)
            self.addCleanup(client.close)
            client.connect()
            client.start_listener()

            fixture.server.collect_from_all("host")
            self.assertTrue(log.wait_until(lambda items: any(
                kind == protocol.MessageKind.COLLECT for kind, _ in items), 4.0))
            self.assertEqual(log.of(protocol.MessageKind.COLLECT)[-1]["reason"],
                             "host")
        finally:
            fixture.stop()

    def test_forced_submission_is_accepted_and_marked(self):
        """自动交上来的卷子必须能进得来，并且在榜上标注为自动收卷。"""
        fixture = ServerFixture(judge=fake_judge(2, 2), force_collect=True,
                                duration=120).start()
        try:
            client = fixture.client("DEV00001", "张三")
            self.addCleanup(client.close)
            client.connect()
            client.drain_snapshot()

            fixture.session.ends_at = datetime.now() - timedelta(seconds=1)
            fixture.session.grace_seconds = 30      # 仍在宽限期内

            client.submit("P0001", "cpp", "// 自动交的", forced=True)
            verdict = client.wait_for_verdict()
            self.assertEqual(verdict["verdict"], "AC")
            self.assertTrue(verdict["forced"])
            self.assertTrue(fixture.session.submissions[0].forced)
        finally:
            fixture.stop()


class TestTimedExam(unittest.TestCase):
    """限时：到点收卷。"""

    def test_submissions_rejected_after_exam_ends(self):
        """人已经连着、测验中途到点：连接不断，但不再收卷子。

        这才是现场真正会发生的情形 —— 到点时没人会断开重连。
        """
        fixture = ServerFixture(judge=fake_judge(2, 2), duration=60).start()
        try:
            client = fixture.client("DEV00001", "准时交")
            client.connect()
            client.drain_snapshot()

            # 到点收卷：结束时刻挪到过去，并取消宽限
            fixture.session.ends_at = datetime.now() - timedelta(seconds=1)
            fixture.session.grace_seconds = 0

            client.submit("P0001", "cpp", "int main(){}")
            message = expect_kind(client, protocol.MessageKind.ERROR)
            self.assertIn("结束", message["message"])
            self.assertEqual(fixture.session.submissions, [])
            client.close()
        finally:
            fixture.stop()

    def test_pending_exam_accepts_connection_but_blocks_submission(self):
        """还没开始：能进来、能看到题面，但交不了卷，且提示语不能是"已结束"。"""
        fixture = ServerFixture(duration=60)
        fixture.session.starts_at = datetime.now() + timedelta(minutes=5)
        fixture.session.ends_at = (fixture.session.starts_at
                                   + timedelta(minutes=1))
        fixture.start()
        try:
            client = fixture.client("DEV00001", "提前到")
            client.connect()
            client.drain_snapshot()
            self.assertEqual(client.exam["state"], ExamState.PENDING.value)

            client.submit("P0001", "cpp", "int main(){}")
            message = expect_kind(client, protocol.MessageKind.ERROR)
            self.assertIn("尚未开始", message["message"])
            self.assertEqual(fixture.session.submissions, [])
            client.close()
        finally:
            fixture.stop()

    def test_untimed_practice_room_has_no_deadline(self):
        """不限时的练习房：`remaining_seconds` 是 None，客户端显示"不限时"。"""
        fixture = ServerFixture(duration=0, mode=RoomMode.PRACTICE).start()
        try:
            client = fixture.client("DEV00001", "随手练")
            client.connect()
            client.drain_snapshot()
            self.assertFalse(client.timed)
            self.assertIsNone(client.remaining_seconds())
            self.assertEqual(client.exam["state"], ExamState.RUNNING.value)
            client.close()
        finally:
            fixture.stop()


class TestSyncWaiting(unittest.TestCase):
    """同步等待必须有上限。

    这一组是拿一次真实事故换来的：全量回归在某个用例上挂了将近三个小时才
    被外部超时掐断。根因是握手之后套接字被设成**阻塞无超时** —— 长连接本来
    就该这样，但同步用法是"一问一答"，对端不回就变成永久卡死，而"卡死"在
    现象上很难和"跑得慢"区分开来。所以同步等待一律要有上限，而且要用**时间**
    兜底：主机会周期性推状态与榜单，拿"最多跳过多条消息"当上限是不成立的。
    """

    def test_wait_for_gives_up_when_the_host_stays_silent(self):
        """对端一直不说话时，等下去要有头，并且连接就地作废。"""
        fixture = ServerFixture(judge=fake_judge(2, 2), status_interval=30.0,
                                leaderboard_interval=30.0).start()
        try:
            client = fixture.client(fixture.new_device_id(), "等待")
            client.connect()
            client.drain_snapshot()

            started = time.monotonic()
            with self.assertRaises(protocol.ProtocolError):
                # PONG 只在客户端发了 PING 之后才回，这里永远不会来
                client.wait_for(protocol.MessageKind.PONG, limit=50, timeout=0.6)
            self.assertLess(time.monotonic() - started, 10.0,
                            "等待必须按时间收手，不能拖到消息条数用尽")
            self.assertTrue(client.closed, "读超时之后这条连接不能再用了")
        finally:
            fixture.stop()

    def test_wait_for_verdict_is_bounded_by_time_not_by_message_count(self):
        """等评测结果用时间兜底：主机话多不代表等到了结果。"""
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        try:
            client = fixture.client(fixture.new_device_id(), "等待")
            client.connect()
            client.drain_snapshot()

            started = time.monotonic()
            with self.assertRaises(protocol.ProtocolError):
                # 一次提交都不发，却一直有状态/榜单在推 —— 旧实现会老老实实
                # 数满 400 条消息才放弃，新实现到点就收手
                client.wait_for_verdict(timeout=1.0)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 20.0, f"等了 {elapsed:.1f} 秒，太久")
        finally:
            fixture.stop()


class TestServerLifecycle(unittest.TestCase):
    def test_start_stop_is_idempotent_enough(self):
        fixture = ServerFixture()
        fixture.start()
        self.assertTrue(fixture.server.running)
        self.assertGreater(fixture.server.port, 0)
        fixture.stop()
        self.assertFalse(fixture.server.running)

    def test_lan_addresses_returns_something(self):
        fixture = ServerFixture().start()
        try:
            self.assertIsInstance(fixture.server.lan_addresses(), list)
        finally:
            fixture.stop()

    def test_connected_devices_is_tracked(self):
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        try:
            client = fixture.client("ABCD2345", "张三")
            client.connect()
            client.drain_snapshot()
            self.assertIn("ABCD2345", fixture.server.connected_devices())
            client.close()
        finally:
            fixture.stop()

    def test_events_are_reported(self):
        seen: list[str] = []
        session = ExamSession("S-EVT", duration_minutes=5)
        session.set_problems([build_exam_problem(make_problem())])
        server = ExamServer(session, [make_problem()],
                            config=ServerConfig(bind_host="127.0.0.1", port=0),
                            judge=fake_judge(2, 2),
                            on_event=lambda name, _payload: seen.append(name))
        server.start()
        try:
            client = ExamClient("127.0.0.1", server.port, session.room_code,
                                "DEV00001", "张三")
            client.connect()
            client.drain_snapshot()
            client.submit("P0001", "cpp", "x")
            client.wait_for_verdict()
            client.close()
        finally:
            server.stop()
        self.assertIn("started", seen)
        self.assertIn("joined", seen)
        self.assertIn("submitted", seen)
        self.assertIn("judged", seen)
        self.assertIn("stopped", seen)

    def test_replaced_event_fires_on_reconnect(self):
        seen: list[str] = []
        fixture = ServerFixture(judge=fake_judge(2, 2))
        fixture.server._on_event = lambda name, _p: seen.append(name)
        fixture.start()
        try:
            first = fixture.client("ABCD2345", "张三")
            first.connect()
            first.drain_snapshot()
            second = fixture.client("ABCD2345", "张三")
            second.connect()
            second.drain_snapshot()
            self.assertIn("replaced", seen)
            first.close()
            second.close()
        finally:
            fixture.stop()


class TestStoppedServer(unittest.TestCase):
    """主机停掉之后，客户端应当收到明确的断开，而不是无限等待。"""

    def test_listener_reports_disconnect(self):
        fixture = ServerFixture(judge=fake_judge(2, 2)).start()
        reason: list[str] = []
        client = fixture.client("DEV00001", "张三",
                                on_disconnect=reason.append)
        client.connect()
        client.start_listener()
        fixture.stop()
        for _ in range(100):
            if reason:
                break
            time.sleep(0.05)
        self.assertTrue(reason, "主机停掉后客户端没有收到断开通知")
        client.close()


class TestAccountEntryOverTheWire(BaseCase):
    """账号进场走真 TCP：谁是谁由名单说了算。"""

    ROSTER = [
        {"account": "zhangsan", "name": "张三", "passcode": "135790"},
        {"account": "lisi", "name": "李四", "passcode": "246810"},
    ]

    def account_fixture(self, **kwargs) -> ServerFixture:
        return ServerFixture(entry_mode=EntryMode.ACCOUNT, roster=self.ROSTER,
                             **kwargs).start()

    def test_the_right_credentials_get_in_under_the_roster_name(self):
        """学生报什么都不算数 —— 名字是主机按名单回填的。"""
        fixture = self.account_fixture()
        client = fixture.account_client(fixture.new_device_id(), "ZhangSan",
                                        "135790")
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.welcome["username"], "张三")
        self.assertEqual(client.entry_mode, "account")
        self.assertEqual(client.welcome["exam"]["entry_mode"], "account")

    def test_the_room_code_is_not_needed_at_all(self):
        """这是这条路径的卖点：账号+口令就是全部凭据。"""
        fixture = self.account_fixture()
        client = ExamClient("127.0.0.1", fixture.server.port, "999999",
                            fixture.new_device_id(), "", account="zhangsan",
                            passcode="135790")
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.welcome["username"], "张三")

    def test_a_wrong_passcode_is_refused(self):
        fixture = self.account_fixture()
        client = fixture.account_client(fixture.new_device_id(), "zhangsan",
                                        "000000")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        self.assertIn("账号", str(caught.exception))

    def test_an_account_outside_the_roster_is_refused(self):
        fixture = self.account_fixture()
        client = fixture.account_client(fixture.new_device_id(), "wangwu",
                                        "135790")
        self.addCleanup(client.close)
        with self.assertRaises(HandshakeRejected) as caught:
            client.connect()
        # 故意**不区分**"没有这个账号"和"密码不对"：把两种情况说成一句话，
        # 别人就没法靠回答的差异把名单一个个试出来。
        self.assertIn("账号", str(caught.exception))

    def test_a_second_machine_is_refused_for_the_same_account(self):
        """两个人用同一个账号同时做题 —— 榜上会出现两行一样的名字。"""
        fixture = self.account_fixture()
        first = fixture.account_client("DEV00001", "zhangsan", "135790")
        first.connect()
        second = fixture.account_client("DEV00002", "zhangsan", "135790")
        self.addCleanup(second.close)
        with self.assertRaises(HandshakeRejected) as caught:
            second.connect()
        self.assertIn("另一台机器", str(caught.exception))

    def test_reconnecting_from_the_same_machine_still_works(self):
        """断线重连是最常见的正常路径，账号进场也不能误伤它。"""
        fixture = self.account_fixture()
        first = fixture.account_client("DEV00001", "zhangsan", "135790")
        first.connect()
        first.close()
        again = fixture.account_client("DEV00001", "zhangsan", "135790")
        self.addCleanup(again.close)
        again.connect()
        self.assertEqual(again.welcome["username"], "张三")

    def test_the_room_wide_passcode_is_a_second_factor(self):
        """"账号模式 + 全场口令"：两个都对才进得来。"""
        fixture = self.account_fixture(password="kaochang")
        good = fixture.account_client(fixture.new_device_id(), "zhangsan",
                                      "135790", password="kaochang")
        self.addCleanup(good.close)
        good.connect()
        self.assertEqual(good.welcome["username"], "张三")

        missing = fixture.account_client(fixture.new_device_id(), "zhangsan",
                                         "135790", password="")
        self.addCleanup(missing.close)
        with self.assertRaises(HandshakeRejected) as caught:
            missing.connect()
        self.assertIn("考场口令", str(caught.exception))

    def test_the_account_binds_into_the_archive(self):
        fixture = self.account_fixture()
        client = fixture.account_client(fixture.new_device_id(), "lisi",
                                        "246810")
        self.addCleanup(client.close)
        client.connect()
        rows = fixture.session.archive_record()["participants"]
        self.assertEqual(rows[0].get("account"), "lisi")


class TestAwayLockOverTheWire(unittest.TestCase):
    """离场锁屏走真 TCP：盖住屏幕这件事的两端。"""

    def setUp(self):
        self.fixture = ServerFixture(entry_mode=EntryMode.ACCOUNT,
                                     judge=None).start()
        self.addCleanup(self.fixture.stop)
        self.session = self.fixture.session
        self.session.set_contestants([
            {"account": "zhangsan", "name": "张三", "passcode": "135790"},
            {"account": "lisi", "name": "李四", "passcode": "246810"},
        ])
        self.log = MessageLog()
        self.client = self.fixture.account_client(
            "DEV00001", "zhangsan", "135790", on_message=self.log)
        self.client.connect()
        self.client.start_listener()

    def tearDown(self):
        self.client.close()

    def wait_for(self, kind: str, timeout: float = 4.0,
                 until=None) -> dict | None:
        """等到一条（可选：满足 ``until`` 条件的）``kind`` 消息。

        **用时间兜底，不用条数** —— 主机每 0.2 秒推一次状态/榜单，
        拿条数当上限会让一台忙碌的主机看起来像"永远等不到"。
        """
        def ready(snapshot) -> bool:
            return any(got == kind and (until is None or until(message))
                       for got, message in snapshot)

        self.log.wait_until(ready, timeout=timeout)
        matches = [message for message in self.log.of(kind)
                   if until is None or until(message)]
        return matches[-1] if matches else None

    def test_the_student_can_cover_their_own_screen(self):
        """「离开一下」只许锁自己 —— 这条路径主机不问口令。"""
        self.client.request_lock()
        self.wait_for(protocol.MessageKind.EXAM,
                      until=lambda m: any(
                          row.get("device_id") == "DEV00001" and row.get("locked")
                          for row in (m.get("participants") or [])))
        self.assertTrue(self.client.locked)
        self.assertIn("DEV00001", self.session.locked_devices())

    def test_unlocking_with_the_personal_passcode_opens_it(self):
        """口令**发到主机校验**，本机不比对。"""
        self.client.request_lock()
        self.wait_for(protocol.MessageKind.EXAM)

        self.client.request_unlock("000000")
        reply = self.wait_for(protocol.MessageKind.UNLOCKED)
        self.assertIsNotNone(reply)
        self.assertFalse(reply["ok"])
        self.assertIn("4", reply["message"])
        self.assertTrue(self.client.locked)      # 屏幕还盖着

        self.client.request_unlock("135790")
        reply = self.wait_for(protocol.MessageKind.UNLOCKED,
                              until=lambda m: m.get("ok"))
        self.assertIsNotNone(reply, "第二次应当解锁成功")
        self.assertFalse(self.client.locked)

    def test_asking_to_unlock_is_not_a_key(self):
        """"把 locked 改成 false 再发一次"不是万能钥匙。"""
        self.client.request_lock()
        self.wait_for(protocol.MessageKind.EXAM)

        self.client.send({"kind": protocol.MessageKind.LOCK, "locked": False})
        reply = self.wait_for(protocol.MessageKind.ERROR)
        self.assertIsNotNone(reply, "主机应当拒绝客户端自己解锁")
        self.assertTrue(self.client.locked)
        self.assertIn("DEV00001", self.session.locked_devices())

    def test_the_host_can_lock_and_free_someone(self):
        """老师远程下锁：**不需要口令** —— 他手上就有名单。"""
        self.assertTrue(self.fixture.server.set_locked("DEV00001"))
        reply = self.wait_for(protocol.MessageKind.LOCK)
        self.assertIsNotNone(reply)
        self.assertTrue(reply["locked"])
        self.assertTrue(self.client.locked)

        self.assertTrue(self.fixture.server.set_locked("DEV00001", False))
        freed = self.wait_for(protocol.MessageKind.LOCK,
                              until=lambda m: not m.get("locked"))
        self.assertIsNotNone(freed, "老师应当能远程解除")
        self.assertFalse(self.client.locked)

    def test_the_host_can_lock_everyone_at_once(self):
        client2 = self.fixture.account_client("DEV00002", "lisi", "246810")
        self.addCleanup(client2.close)
        client2.connect()
        client2.start_listener()

        self.assertEqual(self.fixture.server.lock_all(), 2)
        reply = self.wait_for(protocol.MessageKind.LOCK)
        self.assertIsNotNone(reply)
        self.assertTrue(self.client.locked)

    def test_guessing_runs_out(self):
        """没有上限的话，一台学生机就是一个在线爆破接口。"""
        self.client.request_lock()
        self.wait_for(protocol.MessageKind.EXAM)
        for attempt in range(MAX_UNLOCK_ATTEMPTS):
            self.client.request_unlock("000000")
            reply = self.wait_for(
                protocol.MessageKind.UNLOCKED,
                until=lambda m: "还可以试" in m.get("message", "")
                or "用完" in m.get("message", ""))
            self.assertIsNotNone(reply, f"第 {attempt + 1} 次应当被计数")
        self.client.request_unlock("135790")
        reply = self.wait_for(protocol.MessageKind.UNLOCKED,
                              until=lambda m: "老师" in m.get("message", ""))
        self.assertIsNotNone(reply, "错满之后连对口令也不再放行")
        self.assertFalse(reply["ok"])
        self.assertTrue(self.client.locked)

    def test_a_room_code_session_unlocks_with_the_room_password(self):
        """房间号进场没有个人口令 —— 解锁认的是全场口令。"""
        fixture = ServerFixture(password="kaochang").start()
        self.addCleanup(fixture.stop)
        log = MessageLog()
        client = fixture.client("DEV00001", "张三", on_message=log)
        self.addCleanup(client.close)
        client.connect()
        client.start_listener()
        client.request_lock()
        log.wait_until(
            lambda snap: any(got == protocol.MessageKind.EXAM for got, _ in snap))
        client.request_unlock("kaochang")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            replies = log.of(protocol.MessageKind.UNLOCKED)
            if replies:
                break
            time.sleep(0.05)
        self.assertTrue(log.of(protocol.MessageKind.UNLOCKED)[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
