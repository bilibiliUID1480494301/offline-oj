"""房间会话、房间号、设备 ID 与排名的测试。

排名是需求里明确点名的东西（"同题提交的分以及时间空间排名"），也是**最容易
算错又最难发现算错**的一类逻辑：错一点点，榜面看起来仍然"有条理"，
只是顺序不对，现场没人会立刻察觉。所以三条排序规则各写一条用例钉死。

身份部分测的是那条明确的设计决定：**身份是设备八位 ID，用户名只是标签**。
于是"同名两人"必须都能进来，而"同一台机器断线重连"必须还能进来。
"""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta

from offline_oj.core.models import Language
from offline_oj.net.identity import DeviceIdentity
from offline_oj.net.session import (
    DEVICE_ID_ALPHABET, DEVICE_ID_LENGTH, MAX_SCORE_PER_PROBLEM,
    MAX_UNLOCK_ATTEMPTS, MAX_USERNAME_LENGTH, ROOM_CODE_LENGTH, EntryMode,
    ExamPolicy, ExamProblemView, ExamSession, ExamState, JoinError,
    Participant, RoomMode, Submission, competition_ranks, generate_device_id,
    generate_room_code, normalize_device_id, normalize_room_code, room_id,
    room_secret,
)

#: 一份最小名单：账号进场那一组用例都用它。
#: 真名单的文件形态（CSV 导入、落盘）由 ``tests/test_roster.py`` 覆盖 ——
#: 这一层只认"若干行 dict"，不必也不该依赖 ``core/roster.py``。
ROSTER_ROWS = [
    {"account": "zhangsan", "name": "张三", "passcode": "135790", "seat": "A-01"},
    {"account": "lisi", "name": "李四", "passcode": "246810", "seat": "A-02"},
]


def device_of(name: str) -> str:
    """把测试里的用户名映射成一个稳定的合法设备 ID。

    sha1 的前 8 位十六进制只含 0-9 与 A-F，天然落在字符集内，
    而且同一个名字每次得到同一个 ID —— 用例读起来仍然是"张三怎么样"。
    """
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:DEVICE_ID_LENGTH].upper()


def make_problem(problem_id: str = "P0001") -> ExamProblemView:
    return ExamProblemView(id=problem_id, title=f"题目 {problem_id}",
                           description="读入两个整数，输出它们的和。",
                           time_limit=1000, memory_limit=256,
                           samples=[{"input": "1 2\n", "output": "3\n"}])


def make_session(*, problems: int = 1, duration: int = 60,
                 mode: RoomMode = RoomMode.EXAM,
                 starts_at: datetime | None = None,
                 **kwargs) -> ExamSession:
    exam = ExamSession("S-TEST", title="单元测试", mode=mode,
                       duration_minutes=duration,
                       starts_at=starts_at or datetime.now() - timedelta(seconds=1),
                       **kwargs)
    exam.set_problems(make_problem(f"P000{i}") for i in range(1, problems + 1))
    return exam


def submit(exam: ExamSession, username: str, problem_id: str, *,
           passed: int, total: int = 10, time_ms: float, memory_mb: float,
           at: datetime | None = None, verdict: str | None = None,
           language: str = "cpp", attempt: int | None = None) -> Submission:
    """造一次"已经判完"的提交。

    给分走的是 :meth:`Submission.ratio_score` —— 也就是判题器**没给**测试点
    分值时的兼容路径，等价于"total 个测试点各 10 分、满分 100"这种常见形状。
    于是 ``passed=7, total=10`` 就是 70 分，与"按测试点给分"的结果一致；
    测试点分值不等的那种题（5 个点各 20 分之类）由
    ``test_net_lan.py`` 用带分值的假判题器端到端验。
    """
    record = exam.new_submission(device_of(username), username, problem_id,
                                 language, "// code")
    if attempt is not None:
        record.attempt = attempt
    record.submitted_at = at or datetime.now()
    record.passed = passed
    record.total = total
    record.possible = MAX_SCORE_PER_PROBLEM
    record.time_ms = time_ms
    record.memory_mb = memory_mb
    record.verdict = verdict or ("AC" if passed == total else "WA")
    record.score = record.ratio_score()
    record.judged_at = record.submitted_at
    exam.record_submission(record)
    exam.participants.setdefault(
        device_of(username), Participant(device_id=device_of(username),
                                         username=username))
    return record


class TestRoomCode(unittest.TestCase):
    """房间号：6 位数字，就是唯一凭据。"""

    def test_generated_codes_are_six_digits(self):
        for _ in range(200):
            code = generate_room_code()
            self.assertEqual(len(code), ROOM_CODE_LENGTH)
            self.assertTrue(code.isdigit(), code)

    def test_generated_codes_never_start_with_zero(self):
        """首位非 0 —— 口头报号时"零一二三四五"会有歧义，也容易被抄掉。"""
        for _ in range(200):
            self.assertNotEqual(generate_room_code()[0], "0")

    def test_codes_are_not_obviously_repeating(self):
        codes = {generate_room_code() for _ in range(200)}
        self.assertGreater(len(codes), 190)

    def test_normalize_strips_whitespace_and_fullwidth_digits(self):
        """中文输入法下极易打出全角数字；不做转换就会出现"明明对却进不去"。"""
        self.assertEqual(normalize_room_code(" 123 456 "), "123456")
        self.assertEqual(normalize_room_code("１２３４５６"), "123456")
        self.assertEqual(normalize_room_code("\t123\n456"), "123456")

    def test_normalize_does_not_pad_or_truncate(self):
        """位数不对就该报"房间号是 6 位"，而不是悄悄猜一个。"""
        self.assertEqual(normalize_room_code("12345"), "12345")
        self.assertEqual(normalize_room_code("1234567"), "1234567")

    def test_room_code_never_appears_in_the_summary(self):
        """summary 会原样广播给每个客户端，凭据绝不能进它。"""
        exam = make_session(duration=1, room_code="246810", password="s3cret")
        dumped = str(exam.summary())
        self.assertNotIn("246810", dumped)
        self.assertNotIn("s3cret", dumped)
        self.assertTrue(exam.summary()["password_required"])

    def test_room_code_is_auto_generated_when_blank(self):
        exam = ExamSession("S")
        self.assertEqual(len(exam.room_code), ROOM_CODE_LENGTH)

    def test_secret_and_fingerprint(self):
        self.assertEqual(room_secret("123456"), "123456")
        self.assertEqual(room_secret("123456", "ab"), "123456:ab")
        # 口令区分大小写，不做折叠，否则白白削掉一大截熵
        self.assertNotEqual(room_secret("123456", "Ab"), room_secret("123456", "ab"))
        # 分隔符挡住"房间号末尾 + 口令开头"的拼接歧义
        self.assertNotEqual(room_secret("123456", "7"), room_secret("1234567", ""))
        self.assertNotEqual(room_id("123456"), room_id("123457"))
        self.assertNotEqual(room_id("123456"), room_id("123456", "pw"))

    def test_fingerprint_is_not_the_secret(self):
        """线上只走单向索引：抓到它也拿不到房间号。"""
        exam = make_session(room_code="246810")
        self.assertNotIn("246810", exam.fingerprint)
        self.assertEqual(len(exam.fingerprint), 16)


class TestDeviceId(unittest.TestCase):
    """设备八位 ID：身份，必须唯一且跨场次稳定。"""

    def test_generated_ids_avoid_confusable_characters(self):
        """字符集里不能有 I/L/O/U —— 它们和 1/0/V 手抄时必错。"""
        for char in "ILOU":
            self.assertNotIn(char, DEVICE_ID_ALPHABET)
        for _ in range(50):
            device_id = generate_device_id()
            self.assertEqual(len(device_id), DEVICE_ID_LENGTH)
            self.assertTrue(set(device_id) <= set(DEVICE_ID_ALPHABET))

    def test_alphabet_is_thirty_two_characters(self):
        """32 个字符 = 5 bit/字符，8 位正好整数 40 bit。"""
        self.assertEqual(len(DEVICE_ID_ALPHABET), 32)
        self.assertEqual(len(set(DEVICE_ID_ALPHABET)), 32)

    def test_ids_are_unique_within_reasonable_sample(self):
        """同一场里撞号必须可以忽略：这里抽 5000 个，一个都不许重。"""
        self.assertEqual(len({generate_device_id() for _ in range(5000)}), 5000)

    def test_normalize_folds_confusable_letters(self):
        self.assertEqual(normalize_device_id("OIL"), "011")
        self.assertEqual(normalize_device_id("abcd"), "ABCD")
        self.assertEqual(normalize_device_id("OiLo"), "0110")
        self.assertEqual(normalize_device_id("u"), "V")

    def test_normalize_output_stays_inside_the_alphabet(self):
        for raw in ("oil", "O1L", "  a b c 1 2 3 ", "AbC123xy", "uuu"):
            cleaned = normalize_device_id(raw)
            self.assertTrue(set(cleaned) <= set(DEVICE_ID_ALPHABET),
                            f"{raw!r} 归一出集合外字符: {cleaned!r}")

    def test_normalize_strips_whitespace(self):
        self.assertEqual(normalize_device_id("  ab cd \n 23 45 "), "ABCD2345")

    def test_normalize_never_corrupts_a_valid_id(self):
        for _ in range(200):
            device_id = generate_device_id()
            self.assertEqual(normalize_device_id(device_id), device_id)
            self.assertEqual(normalize_device_id(normalize_device_id(device_id)),
                             device_id)


class TestJoin(unittest.TestCase):
    """进场与断线重连。身份是设备 ID，用户名只是标签。"""

    def setUp(self) -> None:
        self.exam = make_session()

    def test_join_creates_a_participant(self):
        participant = self.exam.join("ABCD2345", "张三")
        self.assertEqual(participant.device_id, "ABCD2345")
        self.assertEqual(participant.username, "张三")
        self.assertTrue(self.exam.has_device("abcd2345"))

    def test_same_device_can_reconnect(self):
        """断线重连必须能进来 —— 网卡松动、笔记本合盖、切 Wi-Fi 都得能回来。"""
        self.exam.join("ABCD2345", "张三")
        self.exam.join("ABCD2345", "张三")
        self.assertEqual(len(self.exam.participants), 1)

    def test_reconnect_keeps_the_first_username(self):
        """名字以第一次为准：老师是照名字点名的，中途悄悄改名更麻烦。"""
        self.exam.join("ABCD2345", "张三")
        again = self.exam.join("ABCD2345", "李四")
        self.assertEqual(again.username, "张三")

    def test_device_id_is_normalized_on_join(self):
        """用户手抄时把 O 打成 0、带空格，都要能无歧义地纠正回来。"""
        self.exam.join("abcd 2345", "张三")
        self.assertTrue(self.exam.has_device("ABCD2345"))

    def test_two_people_may_share_a_username(self):
        """同名两人靠设备 ID 区分。强行要求用户名唯一，
        会在"班里两个张三"时把第二个人挡在门外。"""
        first = self.exam.join("AAAA1111", "张三")
        second = self.exam.join("BBBB2222", "张三")
        self.assertEqual(len(self.exam.participants), 2)
        self.assertNotEqual(first.device_id, second.device_id)

    def test_ready_check(self):
        self.assertTrue(DeviceIdentity(device_id="ABCD2345").ready)
        self.assertFalse(DeviceIdentity(device_id="").ready)
        self.assertFalse(DeviceIdentity(device_id="ABCD234").ready)     # 7 位
        self.assertFalse(DeviceIdentity(device_id="ABCD234O").ready)    # 含 O

    def test_bad_device_ids_are_rejected(self):
        cases = {
            "": "不能为空",
            "   ": "不能为空",
            "ABC": "8 位",
            "ABCD23456": "8 位",
            "ABCD-234": "非法字符",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                with self.assertRaises(JoinError) as caught:
                    self.exam.join(raw, "张三")
                self.assertIn(expected, str(caught.exception))

    def test_bad_usernames_are_rejected(self):
        with self.assertRaises(JoinError) as caught:
            self.exam.join("ABCD2345", "   ")
        self.assertIn("用户名不能为空", str(caught.exception))

        with self.assertRaises(JoinError) as caught:
            self.exam.join("ABCD2345", "名" * (MAX_USERNAME_LENGTH + 1))
        self.assertIn("最长", str(caught.exception))

    def test_username_whitespace_is_collapsed(self):
        participant = self.exam.join("ABCD2345", "  张   三  ")
        self.assertEqual(participant.username, "张 三")
        # 看着一样的两个名字必须真的相等，否则榜上会出现两行"张 三"
        self.assertEqual(self.exam.join("ABCD2345", "张 三").username, "张 三")

    def test_replacement_is_recorded(self):
        """同一设备又连上来，主机要能察觉"旧连接被顶掉"这件事。"""
        self.exam.note_replacement("ABCD2345", "10.0.0.9:5000")
        self.assertEqual(len(self.exam.replacements), 1)
        self.assertEqual(self.exam.replacements[0]["device_id"], "ABCD2345")

    def test_connect_flags(self):
        self.exam.join("ABCD2345", "张三")
        self.exam.mark_connected("ABCD2345", "10.0.0.9:5000")
        self.assertTrue(self.exam.participant("ABCD2345").connected)
        self.exam.mark_disconnected("ABCD2345")
        self.assertFalse(self.exam.participant("ABCD2345").connected)


class TestExamTiming(unittest.TestCase):
    """状态与剩余时间只由两个时刻推导。"""

    def test_pending_state(self):
        exam = make_session(starts_at=datetime.now() + timedelta(minutes=10))
        self.assertIs(exam.state(), ExamState.PENDING)
        self.assertFalse(exam.accepts_submissions())
        self.assertGreater(exam.remaining_seconds(), 500)

    def test_running_state(self):
        exam = make_session(duration=60)
        self.assertIs(exam.state(), ExamState.RUNNING)
        self.assertTrue(exam.accepts_submissions())
        self.assertLessEqual(exam.remaining_seconds(), 3600)

    def test_ended_state(self):
        exam = make_session(duration=1)
        exam.starts_at = datetime.now() - timedelta(minutes=5)
        exam.ends_at = datetime.now() - timedelta(minutes=1)
        self.assertIs(exam.state(), ExamState.ENDED)
        self.assertEqual(exam.remaining_seconds(), 0)
        self.assertFalse(exam.accepts_submissions())

    def test_grace_period_still_accepts_late_frames(self):
        """刚过截止的那几十秒仍然收卷 —— 否则网络晚 0.2 秒就判迟到。"""
        exam = make_session(duration=1)
        exam.starts_at = datetime.now() - timedelta(minutes=5)
        exam.ends_at = datetime.now() - timedelta(seconds=5)
        self.assertIs(exam.state(), ExamState.ENDED)
        self.assertTrue(exam.accepts_submissions())

    def test_untimed_session_never_ends(self):
        """时长 0 = 不限时（练习模式的常用形态）。"""
        exam = make_session(duration=0)
        self.assertFalse(exam.timed)
        self.assertIsNone(exam.ends_at)
        self.assertIsNone(exam.remaining_seconds())
        self.assertIs(exam.state(), ExamState.RUNNING)
        self.assertTrue(exam.accepts_submissions())
        self.assertFalse(exam.closing_soon())

    def test_timed_session_reports_closing_soon(self):
        exam = make_session(duration=1)
        exam.ends_at = datetime.now() + timedelta(seconds=10)
        self.assertTrue(exam.closing_soon(30))
        exam.ends_at = datetime.now() + timedelta(minutes=5)
        self.assertFalse(exam.closing_soon(30))

    def test_pending_session_never_reports_closing_soon(self):
        """备考阶段不能喊"即将收卷"。

        备考期 :meth:`remaining_seconds` 给的是**距开考还有多久**；直接拿它去比
        "快收卷了"的阈值，会在开考前 30 秒报一次根本不存在的收卷提醒 ——
        学生还没开考就被催着交卷。
        """
        exam = make_session(duration=60,
                            starts_at=datetime.now() + timedelta(seconds=10))
        self.assertIs(exam.state(), ExamState.PENDING)
        self.assertLessEqual(exam.remaining_seconds(), 10)
        self.assertFalse(exam.closing_soon(30))

    def test_start_now_pulls_the_deadline_forward(self):
        """提前开考：时长跟着走 —— 老师想的是"早开始早结束"。"""
        exam = make_session(duration=30,
                            starts_at=datetime.now() + timedelta(minutes=10))
        self.assertTrue(exam.start_now())
        self.assertIs(exam.state(), ExamState.RUNNING)
        self.assertTrue(exam.accepts_submissions())
        self.assertGreater(exam.remaining_seconds(), 29 * 60)

    def test_start_now_on_an_untimed_session_invents_no_deadline(self):
        """不限时的房间提前开考时，不许凭空长出一个 ``ends_at``。"""
        exam = make_session(duration=0,
                            starts_at=datetime.now() + timedelta(minutes=10))
        self.assertIsNone(exam.ends_at)
        self.assertTrue(exam.start_now())
        self.assertIsNone(exam.ends_at)
        self.assertIsNone(exam.remaining_seconds())

    def test_start_now_reports_false_once_running(self):
        """已经开考就不动 —— 返回 ``False``，调用方据此决定要不要广播。"""
        exam = make_session(duration=30)
        before = exam.ends_at
        self.assertFalse(exam.start_now())
        self.assertEqual(exam.ends_at, before)

    def test_summary_reports_state_and_problems(self):
        exam = make_session(problems=3)
        summary = exam.summary()
        self.assertEqual(summary["state"], "running")
        self.assertEqual(len(summary["problems"]), 3)
        self.assertEqual(summary["problems"][0]["id"], "P0001")


class TestRoomMode(unittest.TestCase):
    """两种模式唯一的差别：榜单什么时候公开。"""

    def _exam(self, mode: RoomMode, **kwargs) -> ExamSession:
        exam = make_session(duration=1, mode=mode, **kwargs)
        exam.starts_at = datetime.now() - timedelta(minutes=5)
        exam.ends_at = datetime.now() - timedelta(minutes=1)
        return exam

    def test_practice_publishes_from_the_very_beginning(self):
        exam = make_session(duration=60, mode=RoomMode.PRACTICE)
        self.assertTrue(exam.leaderboard_visible())

    def test_exam_withholds_while_running(self):
        exam = make_session(duration=60, mode=RoomMode.EXAM)
        self.assertFalse(exam.leaderboard_visible())
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload()
        self.assertFalse(payload["published"])
        self.assertEqual(payload["overall"], [])
        self.assertEqual(payload["per_problem"], {})
        self.assertIn("统一公布", payload["withheld_reason"])

    def test_exam_stays_withheld_through_the_grace_period(self):
        """宽限期没走完就放榜，会让刚过截止还在补交的人看到自己没被算进去。"""
        exam = self._exam(RoomMode.EXAM)
        exam.ends_at = datetime.now() - timedelta(seconds=5)      # 在宽限期内
        self.assertFalse(exam.leaderboard_visible())

    def test_exam_publishes_after_the_grace_period(self):
        exam = self._exam(RoomMode.EXAM)
        self.assertTrue(exam.leaderboard_visible())
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload()
        self.assertTrue(payload["published"])
        self.assertEqual(payload["overall"][0]["username"], "张三")

    def test_exam_is_not_published_before_it_starts(self):
        exam = make_session(duration=60, mode=RoomMode.EXAM,
                            starts_at=datetime.now() + timedelta(minutes=10))
        self.assertFalse(exam.leaderboard_visible())

    def test_withheld_viewer_still_sees_their_own_row(self):
        """考试模式不公开别人的成绩，但不该连自己的排名都看不到。"""
        exam = make_session(duration=60, mode=RoomMode.EXAM)
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        submit(exam, "李四", "P0001", passed=4, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload(viewer=device_of("李四"))
        self.assertFalse(payload["published"])
        self.assertIsNotNone(payload["myself"])
        self.assertEqual(payload["myself"]["username"], "李四")

    def test_payload_reports_the_mode(self):
        practice = make_session(duration=60, mode=RoomMode.PRACTICE)
        payload = practice.leaderboard_payload()
        self.assertEqual(payload["mode"], "practice")
        self.assertEqual(payload["mode_label"], "练习模式")

    def test_mode_falls_back_to_exam_on_junk(self):
        self.assertIs(RoomMode.from_value("nonsense"), RoomMode.EXAM)
        self.assertIs(RoomMode.from_value("practice"), RoomMode.PRACTICE)


class TestRename(unittest.TestCase):
    """改房间名：只换一个显示标签，房间号与连接都不动。"""

    def test_rename_changes_the_title(self):
        exam = make_session()
        self.assertEqual(exam.rename("第三次模拟赛"), "第三次模拟赛")
        self.assertEqual(exam.title, "第三次模拟赛")

    def test_rename_trims_and_falls_back_on_blank(self):
        exam = make_session()
        self.assertEqual(exam.rename("  期中  "), "期中")
        self.assertEqual(exam.rename("   "), "局域网测验")
        self.assertEqual(exam.rename(""), "局域网测验")

    def test_rename_does_not_move_the_room_identity(self):
        """这条是名字改动的核心保证：房间秘密与 title 无关。

        哪一天有人"顺手"把 title 拼进 room_secret，改名就会让全教室同时掉线，
        而且现象是"改名之后连不上了"，跟标题看起来毫不相干 —— 极难查。
        把这条钉死在这里，比在注释里写一句更管用。
        """
        exam = make_session()
        before_fingerprint = exam.fingerprint
        before_secret = exam.secret
        exam.rename("换了个名字")
        self.assertEqual(exam.fingerprint, before_fingerprint)
        self.assertEqual(exam.secret, before_secret)
        self.assertEqual(exam.secret, room_secret(exam.room_code, exam.password))

    def test_rename_reaches_the_client_summary(self):
        """学生端的状态栏从 summary() 里读 title，改名必须同步过去。"""
        exam = make_session()
        exam.rename("新的名字")
        self.assertEqual(exam.summary()["title"], "新的名字")


class TestLeaderboardSwitch(unittest.TestCase):
    """不放榜开关：与 RoomMode 正交，关掉则**全程**不公开，连自己那条也只剩自己。"""

    def test_practice_only_publishes_while_the_switch_is_on(self):
        self.assertTrue(make_session(duration=60, mode=RoomMode.PRACTICE)
                        .leaderboard_visible())
        self.assertFalse(
            make_session(duration=60, mode=RoomMode.PRACTICE,
                         show_leaderboard=False).leaderboard_visible())

    def test_exam_never_publishes_when_switched_off(self):
        """哪怕过了截止、连收卷宽限也走完了，关掉开关就是不公开。"""
        exam = make_session(duration=1, mode=RoomMode.EXAM,
                            show_leaderboard=False)
        exam.starts_at = datetime.now() - timedelta(minutes=5)
        exam.ends_at = datetime.now() - timedelta(minutes=1)
        exam.grace_seconds = 0
        self.assertFalse(exam.leaderboard_visible())
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload()
        self.assertFalse(payload["published"])
        self.assertEqual(payload["overall"], [])
        self.assertEqual(payload["per_problem"], {})
        # 文案必须说"不公布"，不能是"稍后统一公布" —— 那会让人一直等
        self.assertEqual(payload["withheld_reason"], "本场不公布榜单")

    def test_the_switch_is_exported_to_clients(self):
        self.assertFalse(make_session(show_leaderboard=False)
                         .summary()["show_leaderboard"])
        self.assertTrue(make_session().summary()["show_leaderboard"])

    def test_own_row_is_still_visible_when_not_publishing(self):
        """不放榜不等于"连自己都看不到"：自己的成绩仍然是自己的数据。"""
        exam = make_session(duration=60, mode=RoomMode.EXAM,
                            show_leaderboard=False)
        submit(exam, "李四", "P0001", passed=4, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload(viewer=device_of("李四"))
        self.assertFalse(payload["published"])
        self.assertIsNotNone(payload["myself"])
        self.assertEqual(payload["myself"]["username"], "李四")


class TestExamPolicy(unittest.TestCase):
    """本场限制：默认全开，序列化只写非默认项。"""

    def test_default_allows_everything_and_serialises_to_emptiness(self):
        policy = ExamPolicy()
        self.assertTrue(policy.is_default())
        self.assertTrue(policy.allows_language(Language.JAVA))
        self.assertTrue(policy.allows_language("java"))
        # 默认策略落成 {}，这样既有线上载荷逐字节不变
        self.assertEqual(policy.to_dict(), {})

    def test_language_restriction(self):
        policy = ExamPolicy(allowed_languages=("cpp", "python"))
        self.assertFalse(policy.is_default())
        self.assertTrue(policy.allows_language(Language.CPP))
        self.assertTrue(policy.allows_language("python"))
        self.assertFalse(policy.allows_language(Language.JAVA))
        self.assertFalse(policy.allows_language("c"))

    def test_round_trip_through_to_dict(self):
        policy = ExamPolicy(allowed_languages=("c",), allow_copy_out=False,
                            lock_after_submit=True)
        self.assertEqual(ExamPolicy.from_dict(policy.to_dict()), policy)

    def test_from_dict_tolerates_junk(self):
        """读旧档 / 旧主机不带这个字段时都不该炸，一律回落到默认。"""
        for junk in (None, "nonsense", [], 42, {}):
            self.assertTrue(ExamPolicy.from_dict(junk).is_default(), junk)

    def test_session_defaults_to_an_open_policy(self):
        exam = make_session()
        self.assertTrue(exam.policy.is_default())
        self.assertEqual(exam.summary()["policy"], {})

    def test_session_carries_the_policy_to_clients(self):
        exam = make_session(policy=ExamPolicy(allowed_languages=("cpp",)))
        self.assertEqual(exam.summary()["policy"]["allowed_languages"], ["cpp"])

    def test_session_ignores_a_bogus_policy(self):
        self.assertTrue(make_session(policy="nonsense").policy.is_default())


class TestSubmitPolicy(unittest.TestCase):
    """重复提交的开关与"第几次提交"的计数。"""

    def test_attempt_counts_up(self):
        exam = make_session()
        first = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "a")
        exam.record_submission(first)
        second = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "b")
        self.assertEqual((first.attempt, second.attempt), (1, 2))

    def test_attempt_is_counted_per_problem(self):
        exam = make_session(problems=2)
        exam.record_submission(
            exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "a"))
        other = exam.new_submission("ABCD2345", "张三", "P0002", "cpp", "a")
        self.assertEqual(other.attempt, 1)

    def test_attempt_is_counted_per_device(self):
        exam = make_session()
        exam.record_submission(
            exam.new_submission("AAAA1111", "张三", "P0001", "cpp", "a"))
        mine = exam.new_submission("BBBB2222", "张三", "P0001", "cpp", "a")
        self.assertEqual(mine.attempt, 1)

    def test_resubmit_allowed_by_default(self):
        exam = make_session()
        self.assertTrue(exam.allow_resubmit)
        allowed, why = exam.can_submit("ABCD2345", "P0001")
        self.assertTrue(allowed)
        self.assertEqual(why, "")

    def test_resubmit_can_be_forbidden(self):
        exam = make_session(allow_resubmit=False)
        allowed, _ = exam.can_submit("ABCD2345", "P0001")
        self.assertTrue(allowed, "第一次提交必须放行")
        exam.record_submission(
            exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "a"))
        allowed, why = exam.can_submit("ABCD2345", "P0001")
        self.assertFalse(allowed)
        self.assertIn("不允许重复提交", why)

    def test_forbidden_resubmit_does_not_block_other_problems(self):
        exam = make_session(problems=2, allow_resubmit=False)
        exam.record_submission(
            exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "a"))
        allowed, _ = exam.can_submit("ABCD2345", "P0002")
        self.assertTrue(allowed)

    def test_policy_is_exported_to_clients(self):
        exam = make_session(allow_resubmit=False, force_collect=True)
        summary = exam.summary()
        self.assertFalse(summary["allow_resubmit"])
        self.assertTrue(summary["force_collect"])

    def test_forced_submission_is_marked(self):
        exam = make_session()
        forced = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "a",
                                     forced=True)
        exam.record_submission(forced)
        self.assertTrue(forced.forced)
        self.assertIn("自动收卷", forced.brief())
        self.assertTrue(forced.to_dict()["forced"])


class TestScoring(unittest.TestCase):
    """NOI 式的给分：**通过的测试点的分值之和**，满分是各点分值之和。

    一次提交的分数由主机在收尾时写死（``server._finish``）：判题器给了分值就
    按分值累加，没给（外部接入的判题器）才退回"按通过比例折算 100 分制"。
    这一组钉的是后者那条兼容路径，以及"满分不再是恒定的 100"这件事。
    """

    def test_proportional_score(self):
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=7, total=10,
                        time_ms=100, memory_mb=10)
        self.assertEqual(record.score, 70)

    def test_full_score(self):
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=10, total=10,
                        time_ms=100, memory_mb=10)
        self.assertEqual(record.score, 100)
        self.assertTrue(record.perfect)

    def test_zero_total_scores_zero_not_full_marks(self):
        """没有测试点是出题人的配置问题，不该变成送满分。"""
        record = Submission(serial=1, device_id="ABCD2345", username="张三",
                            problem_id="P0001", language="cpp", code="")
        self.assertEqual(record.ratio_score(), 0)

    def test_rounding(self):
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=1, total=3,
                        time_ms=1, memory_mb=1)
        self.assertEqual(record.score, 33)
        record2 = submit(exam, "张三", "P0001", passed=2, total=3,
                         time_ms=1, memory_mb=1)
        self.assertEqual(record2.score, 67)

    def test_the_denominator_is_the_point_sum_not_a_hundred(self):
        """满分跟着测试点分值走：5 个点各 10 分的题是 50 分制。

        这是"模仿 NOI"最要紧的一条 —— 分值一旦由测试点决定，光看"30 分"
        就读不出是 50 分里的还是 100 分里的，所以 ``possible`` 必须一起带着走。
        """
        exam = make_session()
        record = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "")
        record.passed, record.total = 3, 5
        record.possible = 50            # 5 个测试点 × 10 分
        record.score = 30               # 过了 3 个
        record.verdict = "WA"
        self.assertFalse(record.perfect)
        self.assertEqual(record.to_dict()["possible"], 50)

    def test_a_point_sum_beats_the_ratio_path(self):
        """有分值就按分值算，不再看通过比例。

        这里刻意让两者**不一致**（过 3/5 按比例是 60，按分值只有 30）：
        如果哪天有人把两条路径接反了，这条用例是唯一会红的。
        """
        exam = make_session()
        record = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "")
        record.passed, record.total = 3, 5
        record.possible, record.score = 50, 30
        self.assertNotEqual(record.score, record.ratio_score())
        self.assertEqual(record.ratio_score(), 60)


class TestCompetitionRanks(unittest.TestCase):
    """名次并列的算法本身（NOI / NOIP 官方成绩单的排法）。"""

    def test_equal_scores_share_the_rank_and_the_next_one_skips(self):
        self.assertEqual(competition_ranks([300, 300, 200, 100, 100]),
                         [1, 1, 3, 4, 4])

    def test_everyone_equal(self):
        self.assertEqual(competition_ranks([50, 50, 50]), [1, 1, 1])

    def test_no_ties_is_just_the_position(self):
        self.assertEqual(competition_ranks([90, 80, 70]), [1, 2, 3])

    def test_empty(self):
        self.assertEqual(competition_ranks([]), [])


class TestProblemRanking(unittest.TestCase):
    """单题排名：**同分并列**（NOI 式），同分者按耗时 → 内存 → 提交时间显示。

    耗时与内存仍然在榜上、也仍然决定同分者的先后，但它们**不再决定名次** ——
    那正是"同为 300 分，你第 3 我第 4"的来路，而那道分差往往只是评测机抖动。
    """

    def test_sorted_by_score_first(self):
        exam = make_session()
        submit(exam, "高分慢", "P0001", passed=10, time_ms=900, memory_mb=200)
        submit(exam, "低分快", "P0001", passed=4, time_ms=10, memory_mb=1)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["高分慢", "低分快"])
        self.assertEqual([r.rank for r in rows], [1, 2])

    def test_ties_share_the_rank(self):
        """同分就是同名次，不是"同分再比耗时排出前后"。"""
        exam = make_session()
        submit(exam, "甲", "P0001", passed=10, time_ms=500, memory_mb=10)
        submit(exam, "乙", "P0001", passed=10, time_ms=120, memory_mb=10)
        submit(exam, "丙", "P0001", passed=2, time_ms=20, memory_mb=1)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["乙", "甲", "丙"])
        self.assertEqual([r.rank for r in rows], [1, 1, 3],
                         "同分应当并列，下一名要跳号")

    def test_ties_still_show_the_faster_one_first(self):
        """并列归并列，"时间排名"这条需求还在：同分者按耗时先后显示。"""
        exam = make_session()
        submit(exam, "慢", "P0001", passed=10, time_ms=500, memory_mb=10)
        submit(exam, "快", "P0001", passed=10, time_ms=120, memory_mb=10)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["快", "慢"])
        self.assertEqual({r.rank for r in rows}, {1})

    def test_ties_ordered_by_memory_after_time(self):
        """同分同耗时比内存 —— 这条就是"空间排名"。"""
        exam = make_session()
        submit(exam, "吃内存", "P0001", passed=10, time_ms=100, memory_mb=250)
        submit(exam, "省内存", "P0001", passed=10, time_ms=100, memory_mb=12)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["省内存", "吃内存"])
        self.assertEqual({r.rank for r in rows}, {1})

    def test_all_equal_ordered_by_submission_time(self):
        """全同则先交的在前，且不依赖字典序这种偶然属性。"""
        base = datetime(2026, 1, 1, 9, 0, 0)
        exam = make_session()
        submit(exam, "晚交", "P0001", passed=10, time_ms=100, memory_mb=10,
               at=base + timedelta(minutes=5))
        submit(exam, "早交", "P0001", passed=10, time_ms=100, memory_mb=10,
               at=base)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["早交", "晚交"])
        self.assertEqual({r.rank for r in rows}, {1})

    def test_only_best_submission_counts(self):
        """同一人多次提交只取最高分那一次，而不是最后一次 —— NOI 也是取最高。"""
        exam = make_session()
        submit(exam, "张三", "P0001", passed=3, time_ms=50, memory_mb=5)
        submit(exam, "张三", "P0001", passed=10, time_ms=80, memory_mb=30)
        submit(exam, "张三", "P0001", passed=1, time_ms=10, memory_mb=1)
        rows = exam.problem_ranking("P0001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].submission.score, 100)

    def test_same_score_prefers_the_faster_submission(self):
        """同分时取最快的那次来显示 —— 名次反正并列，取哪个都不影响排名。"""
        exam = make_session()
        submit(exam, "张三", "P0001", passed=10, time_ms=900, memory_mb=10)
        submit(exam, "张三", "P0001", passed=10, time_ms=90, memory_mb=90)
        rows = exam.problem_ranking("P0001")
        self.assertEqual(rows[0].submission.time_ms, 90)

    def test_ranking_reports_which_attempt_counted(self):
        """需求里点名的"第几次提交"要真的出现在榜上。"""
        exam = make_session()
        submit(exam, "张三", "P0001", passed=3, time_ms=50, memory_mb=5)
        submit(exam, "张三", "P0001", passed=10, time_ms=80, memory_mb=30)
        row = exam.problem_ranking("P0001")[0]
        self.assertEqual(row.to_dict()["attempt"], 2)
        self.assertEqual(row.to_dict()["device_id"], device_of("张三"))

    def test_people_without_submission_are_absent(self):
        exam = make_session()
        exam.join("FFFF9999", "没交")
        submit(exam, "交了", "P0001", passed=5, time_ms=10, memory_mb=1)
        rows = exam.problem_ranking("P0001")
        self.assertEqual([r.username for r in rows], ["交了"])

    def test_problem_ranking_is_isolated_per_problem(self):
        exam = make_session(problems=2)
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        submit(exam, "李四", "P0002", passed=10, time_ms=10, memory_mb=1)
        self.assertEqual([r.username for r in exam.problem_ranking("P0001")],
                         ["张三"])
        self.assertEqual([r.username for r in exam.problem_ranking("P0002")],
                         ["李四"])


class TestOverallRanking(unittest.TestCase):
    """总分排名。"""

    def test_sums_best_scores_across_problems(self):
        exam = make_session(problems=3)
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        submit(exam, "张三", "P0002", passed=5, time_ms=10, memory_mb=1)
        submit(exam, "李四", "P0001", passed=10, time_ms=10, memory_mb=1)
        rows = exam.overall_ranking()
        board = {row.username: row for row in rows}
        self.assertEqual(board["张三"].score, 150)
        self.assertEqual(board["李四"].score, 100)
        self.assertEqual(board["张三"].rank, 1)

    def test_counts_solved_as_full_marks_only(self):
        exam = make_session(problems=2)
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        submit(exam, "张三", "P0002", passed=9, time_ms=10, memory_mb=1)
        row = exam.overall_ranking()[0]
        self.assertEqual(row.solved, 1)
        self.assertEqual(row.per_problem["P0002"], 90)

    def test_equal_totals_share_the_rank(self):
        """总分相同就是同名次，同分者按总耗时 → 总内存显示。

        "总分 → 总耗时 → 总内存 → 最后提交"这条链只决定**显示顺序**：
        名次一律并列，所以不会出现"同为 100 分，你第 1 我第 2"。
        """
        exam = make_session(problems=2)
        submit(exam, "慢", "P0001", passed=10, time_ms=800, memory_mb=10)
        submit(exam, "快", "P0001", passed=10, time_ms=200, memory_mb=10)
        rows = exam.overall_ranking()
        self.assertEqual([r.username for r in rows], ["快", "慢"])
        self.assertEqual([r.rank for r in rows], [1, 1])

        exam2 = make_session(problems=2)
        # 总分与总耗时都相同，比总内存
        submit(exam2, "吃内存", "P0001", passed=10, time_ms=100, memory_mb=250)
        submit(exam2, "省内存", "P0001", passed=10, time_ms=100, memory_mb=20)
        rows2 = exam2.overall_ranking()
        self.assertEqual([r.username for r in rows2], ["省内存", "吃内存"])
        self.assertEqual([r.rank for r in rows2], [1, 1])

    def test_ties_skip_the_next_rank(self):
        """并列之后下一个名次要跳号：100/100/50 → 1、1、3。"""
        exam = make_session(problems=2)
        submit(exam, "甲", "P0001", passed=10, time_ms=100, memory_mb=10)
        submit(exam, "乙", "P0001", passed=10, time_ms=120, memory_mb=10)
        submit(exam, "丙", "P0001", passed=5, time_ms=10, memory_mb=1)
        board = {row.username: row for row in exam.overall_ranking()}
        self.assertEqual(board["甲"].rank, 1)
        self.assertEqual(board["乙"].rank, 1)
        self.assertEqual(board["丙"].rank, 3)

    def test_two_problems_scores_still_add_up(self):
        """题内分值不等时，总分就是各题得分之和。"""
        exam = make_session(problems=2)
        first = submit(exam, "张三", "P0001", passed=10, total=10,
                       time_ms=10, memory_mb=1)
        second = submit(exam, "张三", "P0002", passed=3, total=5,
                        time_ms=10, memory_mb=1)
        second.possible, second.score = 50, 30
        row = exam.overall_ranking()[0]
        self.assertEqual(row.score, first.score + 30)
        self.assertEqual(row.per_problem["P0002"], 30)
        self.assertEqual(row.solved, 1)

    def test_every_participant_appears_even_with_zero(self):
        """交白卷也要上榜 —— "榜上无名"和"考了 0 分"在现场是两回事。"""
        exam = make_session()
        exam.join("FFFF9999", "白卷")
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        rows = exam.overall_ranking()
        self.assertEqual(len(rows), 2)
        board = {row.username: row for row in rows}
        self.assertEqual(board["白卷"].score, 0)
        self.assertEqual(board["白卷"].rank, 2)

    def test_same_name_stays_apart_in_the_board(self):
        """两个张三各占一行，设备 ID 不同 —— 这是"用户名可重复"的前提。"""
        exam = make_session()
        exam.join("AAAA1111", "张三")
        exam.join("BBBB2222", "张三")
        rows = exam.overall_ranking()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.device_id for row in rows},
                         {"AAAA1111", "BBBB2222"})

    def test_ranking_is_stable_for_identical_rows(self):
        """五项全同的人，顺序必须稳定可复现，不能每次刷新都变。"""
        base = datetime(2026, 1, 1, 9)
        order: list[list[str]] = []
        for _ in range(5):
            exam = make_session()
            for name in ("乙", "甲", "丙"):
                submit(exam, name, "P0001", passed=10, time_ms=100,
                       memory_mb=10, at=base)
            order.append([row.username for row in exam.overall_ranking()])
        self.assertEqual(len({tuple(item) for item in order}), 1)

    def test_row_carries_everything_the_board_must_show(self):
        """需求点名要说清楚：用户名、设备 ID、提交时间、时间与空间占用、得分、第几次。"""
        exam = make_session()
        submit(exam, "张三", "P0001", passed=7, time_ms=123.0, memory_mb=45.0)
        row = exam.overall_ranking()[0].to_dict()
        for key in ("rank", "username", "device_id", "score", "solved",
                    "total_time_ms", "total_memory_mb", "last_submit_at",
                    "per_problem", "per_problem_attempts", "submit_count"):
            self.assertIn(key, row)

    def test_problem_row_carries_the_denominator(self):
        """单题榜要带满分 —— 脱离分母看"37 分"读不出含义。"""
        exam = make_session()
        submit(exam, "张三", "P0001", passed=7, time_ms=1, memory_mb=1)
        row = exam.problem_ranking("P0001")[0].to_dict()
        self.assertEqual(row["score"], 70)
        self.assertEqual(row["possible"], MAX_SCORE_PER_PROBLEM)

    def test_leaderboard_payload_shape(self):
        exam = make_session(problems=2, mode=RoomMode.PRACTICE)
        submit(exam, "张三", "P0001", passed=10, time_ms=10, memory_mb=1)
        payload = exam.leaderboard_payload()
        self.assertEqual(payload["kind"], "leaderboard")
        self.assertTrue(payload["published"])
        self.assertEqual(set(payload["per_problem"]), {"P0001", "P0002"})
        self.assertEqual(payload["overall"][0]["username"], "张三")
        self.assertIn("server_time", payload)
        # 本场满分要跟着榜走：学生端靠它把"240 分"写成"240/300"
        self.assertEqual(payload["max_total_score"], 200)

    def test_max_total_score_follows_the_point_values(self):
        """满分由测试点分值决定，不再恒定 100 —— 榜上的分母也随之一变。"""
        exam = make_session(problems=2)
        exam.problems[0].points = 50
        exam.problems[1].points = 20
        self.assertEqual(exam.max_total_score, 70)

    def test_submission_without_testcases_excluded_from_ranking(self):
        """还没判完（total=0）的提交不能占坑，否则榜上会出现莫名其妙的 0 分。"""
        exam = make_session()
        pending = exam.new_submission("ABCD2345", "张三", "P0001", "cpp", "")
        exam.record_submission(pending)
        self.assertEqual(exam.problem_ranking("P0001"), [])


class TestSubmissionRecord(unittest.TestCase):
    def test_brief_is_readable(self):
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=7, total=10,
                        time_ms=123.4, memory_mb=45.6)
        text = record.brief()
        self.assertIn("P0001", text)
        self.assertIn("70分", text)
        self.assertIn("第1次", text)

    def test_to_dict_is_json_safe(self):
        import json
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=7, total=10,
                        time_ms=1.0, memory_mb=2.0)
        json.dumps(record.to_dict(), ensure_ascii=False)

    def test_to_dict_never_carries_the_source(self):
        """载荷里永远没有源码 —— 「学生只有榜单」是在这里锁死的。

        判定结论、耗时、内存都要上线（榜单要用），只有 ``code`` 例外。
        主机端能看代码，靠的是它自己内存里那份 :class:`Submission`，
        而不是让源码绕一圈再传回来。
        """
        exam = make_session()
        record = submit(exam, "张三", "P0001", passed=1, total=1,
                        time_ms=1.0, memory_mb=2.0)
        record.code = "// SECRET-MARKER"

        payload = record.to_dict()

        self.assertNotIn("code", payload)
        self.assertNotIn("SECRET-MARKER", repr(payload))
        # 榜单要用的字段一个都不能少（漏了哪个，榜单上就会缺一列）
        for key in ("serial", "device_id", "username", "problem_id", "language",
                    "attempt", "verdict", "passed", "total", "score", "possible",
                    "time_ms", "memory_mb", "submitted_at", "judged_at"):
            self.assertIn(key, payload)


class TestEntryMode(unittest.TestCase):
    """进场方式：作业方式二选一，默认必须与旧版本一致。"""

    def test_the_default_is_still_the_room_code(self):
        """老代码（不传这个参数）的行为必须逐字节不变。"""
        exam = make_session()
        self.assertIs(exam.entry_mode, EntryMode.ROOM_CODE)

    def test_the_label_is_what_goes_on_screen(self):
        self.assertEqual(EntryMode.ROOM_CODE.label, "房间号进场")
        self.assertEqual(EntryMode.ACCOUNT.label, "账号进场")

    def test_reading_the_value_back_tolerates_garbage(self):
        """读旧档案 / 手改过的载荷都不该炸，认不出来就回落到默认那一档。"""
        self.assertIs(EntryMode.from_value("account"), EntryMode.ACCOUNT)
        self.assertIs(EntryMode.from_value(EntryMode.ACCOUNT), EntryMode.ACCOUNT)
        self.assertIs(EntryMode.from_value("胡说八道"), EntryMode.ROOM_CODE)
        self.assertIs(EntryMode.from_value(None), EntryMode.ROOM_CODE)

    def test_a_room_code_still_exists_in_account_mode(self):
        """老师仍要一个短标识来口头称呼这一场，档案里的 room_id 也还是它。"""
        exam = make_session(entry_mode=EntryMode.ACCOUNT)
        self.assertEqual(len(exam.room_code), ROOM_CODE_LENGTH)


class TestAccountEntry(unittest.TestCase):
    """账号进场：谁是谁由名单说了算，不由学生自己说了算。"""

    def account_session(self, *, rows=None, password="", **kwargs) -> ExamSession:
        exam = make_session(entry_mode=EntryMode.ACCOUNT, password=password,
                            **kwargs)
        exam.set_contestants(rows if rows is not None else ROSTER_ROWS)
        return exam

    def test_the_roster_is_keyed_by_the_normalised_account(self):
        exam = self.account_session(rows=[{"account": " ZhangSan ", "name": "张三"}])
        self.assertIn("zhangsan", exam.contestants)
        self.assertEqual(exam.contestant("ZHANGSAN")["name"], "张三")

    def test_a_case_only_duplicate_is_dropped(self):
        """两个看起来一样的账号同场出现，老师根本分不出谁是谁。"""
        exam = self.account_session(rows=[{"account": "a1", "name": "甲"},
                                          {"account": "A1", "name": "乙"}])
        self.assertEqual(len(exam.contestants), 1)
        self.assertEqual(exam.contestant("a1")["name"], "甲")

    def test_a_row_without_an_account_is_dropped(self):
        exam = self.account_session(rows=[{"account": "", "name": "无名"},
                                          {"account": "a1", "name": "甲"}])
        self.assertEqual(exam.contestants.keys(), {"a1"})

    def test_the_index_table_is_empty_outside_account_mode(self):
        exam = make_session()
        exam.set_contestants(ROSTER_ROWS)
        self.assertEqual(exam.credential_index(), {})

    def test_the_index_table_points_back_at_the_account(self):
        exam = self.account_session()
        table = exam.credential_index()
        self.assertIn("zhangsan", table.values())
        for index in table:
            self.assertTrue(exam.credential_of(index))

    def test_the_room_code_path_still_answers_to_its_own_index(self):
        """账号那套不许把老路径带坏。"""
        exam = make_session(room_code="123456", password="kaochang")
        self.assertEqual(exam.credential_of(exam.fingerprint), exam.secret)
        self.assertIsNone(exam.credential_of("别的索引"))

    def test_the_name_comes_from_the_roster(self):
        """学生报什么都不算数 —— 这条就是"绑定选手信息"。"""
        exam = self.account_session()
        person = exam.authenticate("zhangsan", device_of("张三"))
        self.assertEqual(person.username, "张三")
        self.assertEqual(exam.account_of(device_of("张三")), "zhangsan")

    def test_an_account_outside_the_roster_is_refused(self):
        exam = self.account_session()
        with self.assertRaises(JoinError):
            exam.authenticate("nobody", device_of("张三"))

    def test_a_second_person_cannot_reuse_the_same_machine(self):
        """换个人接着做：这台机器上的绑定还在，得先让老师解绑。"""
        exam = self.account_session()
        exam.authenticate("zhangsan", device_of("张三"))
        with self.assertRaises(JoinError):
            exam.authenticate("lisi", device_of("张三"))

    def test_the_same_account_cannot_come_from_a_second_machine(self):
        """两个人用同一个账号同时做题，榜上会出现两行一样的名字。"""
        exam = self.account_session()
        exam.authenticate("zhangsan", device_of("张三"))
        with self.assertRaises(JoinError):
            exam.authenticate("zhangsan", device_of("另一台机器"))

    def test_reconnecting_from_the_same_machine_is_never_blocked(self):
        """网卡松动、合盖、切 Wi-Fi —— 这是最常见的正常路径，绝不能误伤。"""
        exam = self.account_session()
        first = exam.authenticate("zhangsan", device_of("张三"))
        again = exam.authenticate("zhangsan", device_of("张三"))
        self.assertIs(first, again)
        self.assertEqual(len(exam.participants), 1)

    def test_unbinding_frees_both_the_machine_and_the_board_row(self):
        """只解一半会留下"榜上还挂着他、但他再也进不来"。"""
        exam = self.account_session()
        device = device_of("张三")
        exam.authenticate("zhangsan", device)

        self.assertTrue(exam.unbind_account("zhangsan"))
        self.assertEqual(exam.account_of(device), "")
        self.assertIsNone(exam.participant(device))
        self.assertFalse(exam.unbind_account("zhangsan"))

    def test_unbinding_by_device_works_too(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.authenticate("zhangsan", device)
        self.assertTrue(exam.unbind_device(device))
        self.assertFalse(exam.unbind_device(device))

    def test_the_account_never_leaks_into_the_broadcast(self):
        """``summary()`` 会原样发给全教室，名单是全班的学号，不该人手一份。"""
        exam = self.account_session()
        exam.authenticate("zhangsan", device_of("张三"))
        for person in exam.summary()["participants"]:
            self.assertNotIn("account", person)

    def test_the_account_does_reach_the_archive(self):
        """档案是老师一个人的东西，成绩单上要按学号列人。"""
        exam = self.account_session()
        device = device_of("张三")
        exam.authenticate("zhangsan", device)
        rows = exam.archive_record()["participants"]
        entry = next(row for row in rows if row["device_id"] == device)
        self.assertEqual(entry["account"], "zhangsan")

    def test_a_room_code_session_archives_without_an_account_field(self):
        exam = make_session()
        exam.join(device_of("张三"), "张三")
        for row in exam.archive_record()["participants"]:
            self.assertNotIn("account", row)


class TestAwayLock(unittest.TestCase):
    """离场锁屏：学生去上厕所，屏幕上摊着题面和代码，按一下就盖住。"""

    def account_session(self, *, password="", passcode="135790") -> ExamSession:
        exam = make_session(entry_mode=EntryMode.ACCOUNT, password=password)
        exam.set_contestants([{"account": "zhangsan", "name": "张三",
                               "passcode": passcode}])
        exam.authenticate("zhangsan", device_of("张三"))
        return exam

    def test_locking_and_unlocking_lists_the_person(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        self.assertEqual(exam.locked_devices(), [device])
        exam.set_locked(device, False)
        self.assertEqual(exam.locked_devices(), [])

    def test_locking_an_unknown_device_does_nothing(self):
        self.assertIsNone(make_session().set_locked("NOPE1111"))

    def test_the_personal_passcode_unlocks_it(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        ok, why = exam.verify_unlock(device, "135790")
        self.assertTrue(ok)
        self.assertEqual(why, "")
        self.assertFalse(exam.participant(device).locked)

    def test_the_room_wide_passcode_also_works(self):
        """双因子那档本该两个都对，但解锁挡的是隔壁同学的眼睛，不是本人。"""
        exam = self.account_session(password="kaochang")
        device = device_of("张三")
        exam.set_locked(device)
        self.assertTrue(exam.verify_unlock(device, "kaochang")[0])

    def test_a_wrong_passcode_keeps_it_locked_and_counts_down(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        ok, why = exam.verify_unlock(device, "000000")
        self.assertFalse(ok)
        self.assertIn("4", why)          # "还可以试 4 次"
        self.assertTrue(exam.participant(device).locked)

    def test_guessing_runs_out_and_then_only_the_teacher_can_free_you(self):
        """没有上限的话，一台学生机就是一个在线爆破接口。"""
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        for _ in range(MAX_UNLOCK_ATTEMPTS):
            self.assertFalse(exam.verify_unlock(device, "000000")[0])
        ok, why = exam.verify_unlock(device, "135790")
        self.assertFalse(ok)
        self.assertIn("老师", why)

    def test_the_teacher_freeing_you_restores_the_full_allowance(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        for _ in range(MAX_UNLOCK_ATTEMPTS):
            exam.verify_unlock(device, "000000")
        exam.set_locked(device, False)          # 老师放行
        self.assertEqual(exam.participant(device).unlock_failures, 0)

    def test_an_empty_typed_passcode_is_not_mistaken_for_a_match(self):
        """个人口令留空 = 只要账号就能进 —— 但那不等于空字符串能解开锁。"""
        exam = self.account_session(passcode="")
        device = device_of("张三")
        exam.set_locked(device)
        ok, why = exam.verify_unlock(device, "")
        self.assertFalse(ok)
        self.assertIn("老师", why)

    def test_a_session_without_any_passcode_says_so(self):
        exam = make_session()
        exam.join(device_of("张三"), "张三")
        device = device_of("张三")
        exam.set_locked(device)
        ok, why = exam.verify_unlock(device, "随便打的")
        self.assertFalse(ok)
        self.assertIn("老师", why)

    def test_unlocking_something_that_is_not_locked_is_not_an_error(self):
        """重复解锁别报错 —— 让人以为被拒绝，反而会去举手。"""
        exam = self.account_session()
        device = device_of("张三")
        self.assertEqual(exam.verify_unlock(device, "135790"), (True, ""))

    def test_an_unknown_device_cannot_unlock(self):
        ok, why = make_session().verify_unlock("NOPE1111", "135790")
        self.assertFalse(ok)
        self.assertIn("重新进场", why)

    def test_the_teacher_can_lock_everyone_at_once(self):
        exam = make_session()
        for name in ("张三", "李四"):
            exam.join(device_of(name), name)
        self.assertEqual(len(exam.set_locked_all()), 2)
        self.assertEqual(len(exam.locked_devices()), 2)
        exam.set_locked_all(False)
        self.assertEqual(exam.locked_devices(), [])

    def test_the_teacher_needs_no_passcode(self):
        """老师是监考者，他手上就有名单，没有什么需要向他证明的。"""
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        exam.set_locked(device, False)
        self.assertFalse(exam.participant(device).locked)

    def test_the_locked_flag_travels_to_the_host(self):
        """主机界面要能看见"谁离开中"。"""
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        row = next(item for item in exam.summary()["participants"]
                   if item["device_id"] == device)
        self.assertTrue(row["locked"])
        self.assertTrue(row["locked_at"])

    def test_unlocking_clears_the_timestamp(self):
        exam = self.account_session()
        device = device_of("张三")
        exam.set_locked(device)
        exam.verify_unlock(device, "135790")
        self.assertIsNone(exam.participant(device).locked_at)


class TestEntryModeInPayloads(unittest.TestCase):
    def test_the_summary_carries_the_entry_mode(self):
        """学生端要据此知道"这个房间认账号还是认房间号"。"""
        payload = make_session(entry_mode=EntryMode.ACCOUNT).summary()
        self.assertEqual(payload["entry_mode"], "account")
        self.assertEqual(payload["entry_mode_label"], "账号进场")

    def test_the_room_password_flag_is_a_different_thing(self):
        """``password_required`` 说的是"要不要全场口令"，与进场方式无关。"""
        exam = make_session(entry_mode=EntryMode.ACCOUNT, password="")
        self.assertFalse(exam.summary()["password_required"])
        exam = make_session(entry_mode=EntryMode.ACCOUNT, password="kaochang")
        self.assertTrue(exam.summary()["password_required"])

    def test_the_archive_records_the_entry_mode(self):
        exam = make_session(entry_mode=EntryMode.ACCOUNT)
        self.assertEqual(exam.archive_record()["entry_mode"], "account")


if __name__ == "__main__":
    unittest.main()
