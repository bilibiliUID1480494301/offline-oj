"""局域网测验的真实 TCP 端到端验收。

与 ``tests/test_net_lan.py`` 的分工
-----------------------------------
单元测试跑在回环上、覆盖面广，但它验的是"某条分支对不对"。这个脚本只做一件事：
**把整条链路按老师上课的先后顺序真跑一遍**，并且每一步都打印人看得懂的证据。
它面向的失败模式是"每条分支单测都过，凑起来老师就是开不了房"。

覆盖的场景
----------
1. 房间号不上线 —— 手工走前两帧明文，确认线上找不到房间号/口令的字样；
2. 房间号错 / 口令错被拒，且拒绝理由里不泄漏正确房间号；
3. 同名不同设备 ID 的两位学生同时在场，靠设备 ID 区分；
4. 提交 → 收判定 → 重复提交记第 2 次；
5. 同一设备 ID 再连一次＝顶号，旧连接被断开；
6. 练习模式榜单实时；考试模式封榜，结束后统一放榜；
7. 不允许重复提交时第二次被拒；
8. 到点收卷指令能广播到在线客户端；
9. 上述所有推送载荷里都不含房间号、口令与**源码** —— 源码只上行到主机内存，
   主机端的「提交与代码」页读的就是它。

判题默认用**替身**（直接给满分报告），这样在没有编译器的机器上也能跑；
加 ``--real`` 则走真实工具链，验"代码真的被编译执行了"。

用法::

    python tools\\lan_e2e.py
    python tools\\lan_e2e.py --real
    python tools\\lan_e2e.py --keep          # 失败时保留输出便于排查
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from offline_oj.core.judge import JudgeReport, TestOutcome                # noqa: E402
from offline_oj.core.models import Language, Problem, TestCase, Verdict   # noqa: E402
from offline_oj.net import protocol                                       # noqa: E402
from offline_oj.net.client import ExamClient                              # noqa: E402
from offline_oj.net.server import ExamServer, ServerConfig, build_exam_problem  # noqa: E402
from offline_oj.net.session import (                                      # noqa: E402
    ExamSession,
    RoomMode,
    room_id,
)

PASS = "  [OK]  "
FAIL = "  [!!]  "

#: 供人眼复核的场景编号
_step = 0


def step(title: str) -> None:
    global _step
    _step += 1
    print(f"\n[{_step}] {title}")


def ok(message: str) -> None:
    print(PASS + message)


def fail(message: str) -> None:
    print(FAIL + message)
    raise SystemExit(1)


def expect(condition: bool, message: str) -> None:
    if condition:
        ok(message)
    else:
        fail(message)


# ---------------------------------------------------------------------------
# 素材
# ---------------------------------------------------------------------------


def connection_is_dead(client: ExamClient, attempts: int = 6) -> bool:
    """这条连接是不是真的已经断了。

    **不能用 ``client.closed`` 判断。** 那个标志是给异步用法（读取线程）用的，
    同步用法里没人去置它。真正可靠的判据是"再拿它收发一次会不会报错" ——
    主机已经 `shutdown` 掉的套接字，下一次读会立刻失败。
    缓冲区里可能还剩几条推送，所以要多试几次。
    """
    for _ in range(attempts):
        try:
            if client.recv() is None:
                return True
        except Exception:                                  # noqa: BLE001
            return True
    return False


def build_problems() -> list[Problem]:
    """两道自带样例的题。样例是刻意的 —— 只有样例会下发给客户端。"""
    return [
        Problem(
            id="P0001", title="两数求和",
            description="读入两个整数，输出它们的和。", slug="sum-two",
            time_limit=1000, memory_limit=128,
            testcases=[TestCase(input="1 2\n", output="3\n", sample=True),
                       TestCase(input="100 200\n", output="300\n")],
        ),
        Problem(
            id="P0002", title="回文判断",
            description="判断给定字符串是否回文。", slug="palindrome",
            time_limit=2000, memory_limit=256,
            testcases=[TestCase(input="level\n", output="yes\n", sample=True)],
        ),
    ]


def stub_judge(problem, code, language) -> JudgeReport:
    """替身判题器：不碰工具链，直接按代码内容给分。

    代码里带 ``HALF`` 就只通过一半测试点。这样做是为了让"低分不顶掉高分"
    这条规则真的能被验到 —— 每次都返回同一个满分报告的话，取最高分那条
    逻辑等于没测。
    """
    total = max(1, len(problem.testcases))
    if "HALF" in code:
        passed = max(1, total // 2)
    else:
        passed = total
    verdict = Verdict.AC if passed == total else Verdict.WA
    report = JudgeReport(problem_id=problem.id, problem_title=problem.title,
                         language=Language.from_value(language),
                         verdict=verdict)
    for index in range(1, total + 1):
        report.outcomes.append(TestOutcome(
            index=index, total=total,
            verdict=Verdict.AC if index <= passed else Verdict.WA,
            time_ms=9.0 * index, memory_mb=2.5))
    return report


def real_judge_factory(compiler_paths: dict[str, str], work_root: str):
    """真判题器：与主机端默认路径完全一致。"""
    from offline_oj.core.judge import Judge
    from offline_oj.core.runners import make_runner

    def judge(problem, code, language) -> JudgeReport:
        runner = make_runner(Language.from_value(language), compiler_paths,
                             work_root)
        return Judge(runner).judge(problem, code)

    return judge


def detect_python() -> dict[str, str]:
    """找出一个能用的 Python 解释器。

    先按主机端那套探测逻辑找 PATH 上的，找不到就退回"正在跑这个脚本的解释器" ——
    后者一定存在，而且它本来就是本程序支持的 Python 3。
    """
    from offline_oj.core.compilers import CompilerDetector

    info = CompilerDetector.detect("python")
    if info is not None:
        print(f"  探测到 Python：{info.path}（{info.version}）")
        return {"python": info.path}
    print(f"  PATH 上没有找到 Python，改用当前解释器：{sys.executable}")
    return {"python": sys.executable}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def walk_strings(node) -> list[str]:
    """把一个 JSON 结构里所有字符串取出来（递归），用于"载荷里有没有秘密"的检查。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [item for value in node.values() for item in walk_strings(value)]
    if isinstance(node, list):
        return [item for value in node for item in walk_strings(value)]
    return []


def leak_report(payload: dict, secrets: list[str]) -> list[str]:
    """逐字段找出哪个键把秘密带出去了。

    只回一句"泄漏了"是不够用的 —— 上一次真出现这种情况时，问题出在
    ``session_id`` 里顺带拼了房间号，而检查本身只说了"搜到了房间号"，
    排查绕了一大圈。把字段名打出来，下次一眼就能定位。
    """
    found: list[str] = []
    for key, value in payload.items():
        for text in walk_strings(value):
            for secret in secrets:
                if secret and secret in text:
                    found.append(f"{key}={text[:60]!r}")
                    break
    return found


# ---------------------------------------------------------------------------
# 会话装配
# ---------------------------------------------------------------------------


class Room:
    """一个开着的房间：会话 + 服务端 + 事件记录。"""

    def __init__(self, *, mode: RoomMode, room_code: str, password: str = "",
                 duration_minutes: int = 60, force_collect: bool = False,
                 allow_resubmit: bool = True, judge=None,
                 work_root: str = ".") -> None:
        self.problems = build_problems()
        self.session = ExamSession(
            # 与界面里一致：会话 ID 是时间戳。**绝不能顺带拼上房间号** ——
            # session_id 会随 CHALLENGE 与状态广播发出去，拼进去就等于把凭据放上了线。
            f"e2e-{datetime.now():%Y%m%d%H%M%S}", mode=mode, room_code=room_code,
            password=password, duration_minutes=duration_minutes,
            force_collect=force_collect, allow_resubmit=allow_resubmit,
        )
        self.session.set_problems(
            [build_exam_problem(problem) for problem in self.problems])
        self.events: list[tuple[str, dict]] = []
        self.server = ExamServer(
            self.session, self.problems,
            compiler_paths={}, work_root=work_root,
            config=ServerConfig(port=free_port()),
            judge=judge or stub_judge,
            on_event=lambda name, payload: self.events.append((name, payload)),
        )

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop(timeout=2.0)

    def client(self, device_id: str, username: str,
               password: str | None = None) -> ExamClient:
        return ExamClient("127.0.0.1", self.server.port, self.session.room_code,
                          device_id, username,
                          password=self.session.password if password is None
                          else password)

    def kinds(self) -> list[str]:
        return [name for name, _ in self.events]


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def check_room_code_never_on_the_wire() -> None:
    """手工走前两帧明文，确认房间号与口令都没有以明文出现在线上。"""
    step("房间号不上线：手工走前两帧明文")
    room_code, password = "864213", "Zq7-Test"
    room = Room(mode=RoomMode.PRACTICE, room_code=room_code, password=password)
    room.start()
    try:
        sock = socket.create_connection(("127.0.0.1", room.server.port), timeout=5)
        plain = protocol.PlainChannel(sock)
        hello = {"kind": protocol.MessageKind.HELLO,
                 "protocol_version": 2,
                 "room_id": room_id(room_code, password)}
        plain.send(hello)
        challenge, _ = plain.recv()
        sock.close()

        expect(challenge.get("kind") == protocol.MessageKind.CHALLENGE,
               f"明文握手走到 CHALLENGE，房间索引 {room.session.fingerprint}")
        expect(room.session.fingerprint == room_id(room_code, password),
               "上线的是房间秘密的单向索引，不是房间号本身")
        expect("salt" in challenge, "服务端下发了随机 salt（密钥据此派生）")

        leaked = leak_report(hello, [room_code, password])
        leaked += leak_report(challenge, [room_code, password])
        expect(not leaked, f"明文两帧里搜不到房间号与口令"
                           + (f"（泄漏字段：{leaked}）" if leaked else ""))
    finally:
        room.stop()


def check_wrong_credentials_rejected() -> None:
    """房间号错、口令错都要被拒，而且拒绝理由里不能泄漏正确房间号。"""
    step("凭据错误被拒，且不泄漏正确房间号")
    room = Room(mode=RoomMode.EXAM, room_code="753951", password="hunter2")
    room.start()
    try:
        for wrong_code, wrong_password, label in (
            ("753952", "hunter2", "房间号错一位"),
            ("753951", "hunter3", "口令错一个字符"),
        ):
            client = ExamClient("127.0.0.1", room.server.port, wrong_code,
                                "ABCD2345", "冒名者", password=wrong_password)
            try:
                client.connect()
                fail(f"{label}竟然连上了")
            except Exception as exc:                       # noqa: BLE001
                reason = str(exc)
                expect(room.session.room_code not in reason,
                       f"{label} → 「{reason}」（理由里不含正确房间号）")
            finally:
                client.close()
    finally:
        room.stop()


def check_practice_room() -> None:
    """练习模式的完整一趟：进场、提交、重复提交、同名区分、顶号、实时榜单。"""
    step("练习模式：两位同名同学同场，榜单实时")
    room = Room(mode=RoomMode.PRACTICE, room_code="135790")
    room.start()
    try:
        # 设备 ID 故意用小写 + 易混字符，验归一化
        alpha = room.client("a7k2m9qx", "张三")
        welcome = alpha.connect()
        alpha.drain_snapshot()
        expect(welcome.get("username") == "张三", "第一位同学进场，用户名张三")
        expect(alpha.device_id == "A7K2M9QX",
               f"设备 ID 已归一化：a7k2m9qx → {alpha.device_id}")
        expect([view.id for view in alpha.problems] == ["P0001", "P0002"],
               "题面已下发：P0001 / P1002")
        p1001 = alpha.problem("P0001")
        expect(p1001 is not None and len(p1001.samples) == 1,
               "只下发了样例测试点（正式测试点不出主机）")
        expect(p1001.slug == "sum-two" and not p1001.input_file,
               f"题面带上英文名 {p1001.slug}，本题是标准输入输出")

        beta = room.client("B4N8P2RT", "张三")          # 同名！
        beta.connect()
        beta.drain_snapshot()
        expect(len(room.session.participants) == 2,
               "两位同名同学都在场：靠设备 ID 区分，不靠用户名")

        alpha.submit("P0001", "cpp", "int main(){return 0;}")
        pending = alpha.wait_for(protocol.MessageKind.VERDICT)
        expect(pending.get("pending") is True and pending.get("attempt") == 1,
               f"提交即回执：「{pending.get('message')}」")
        got = alpha.wait_for_verdict(serial=pending["serial"])
        expect(got["verdict"] == "AC" and got["score"] == 100,
               f"判定结果：{got['verdict']} {got['passed']}/{got['total']} "
               f"{got['score']} 分 {got['time_ms']}ms/{got['memory_mb']}MB")

        # 第二次交一份只过一半的代码
        alpha.submit("P0001", "cpp", "// HALF\nint main(){return 0;}")
        half = alpha.wait_for_verdict()
        expect(half["attempt"] == 2 and half["score"] == 50,
               f"重复提交记第 {half['attempt']} 次，本次 {half['score']} 分"
               f"（{half['passed']}/{half['total']}）")

        # 顶号：同一设备 ID 再开一条连接
        before = len(room.session.participants)
        gamma = room.client("A7K2M9QX", "张三改名版")
        gamma.connect()
        gamma.drain_snapshot()
        expect(len(room.session.participants) == before,
               "同一设备 ID 重连不新增参与者（视为断线重连）")
        expect(gamma.username == "张三",
               f"重连时名字以第一次为准：「{gamma.username}」")
        expect("replaced" in room.kinds(), "主机记下了这次顶号事件")
        expect(connection_is_dead(alpha),
               "旧连接已被主机断开（再拿它收发会立刻报错）")
        gamma.close()

        # 练习模式：榜单全程实时公开，字段与界面上要画的一一对应
        board = room.session.leaderboard_payload()
        expect(board["published"] is True, "练习模式榜单始终公开")

        rows = board["overall"]
        expect(len(rows) == 2, f"总分榜 {len(rows)} 行：两位同学都在表里")
        top = rows[0]
        expect(top["username"] == "张三" and top["score"] == 100,
               f"首行 第{top['rank']}名 {top['username']} {top['score']} 分 · "
               f"设备 {top['device_id']} · 提交 {top['submit_count']} 次 · "
               f"{top['total_time_ms']}ms / {top['total_memory_mb']}MB · "
               f"最后提交 {top['last_submit_at']}")
        expect(any(row["score"] == 0 for row in rows),
               "一分未得的同学也在榜上（交了白卷也是信息，不该「榜上无名」）")
        expect(sum(1 for row in rows if row["username"] == "张三") == 2,
               "两位「张三」各占一行，设备 ID 不同")

        attempts = top.get("per_problem_attempts", {})
        expect(attempts.get("P0001") == 1,
               f"榜单标出计入总分的是第 {attempts.get('P0001')} 次提交")

        per = {row["device_id"]: row for row in board["per_problem"]["P0001"]}
        mine = per.get("A7K2M9QX")
        expect(mine is not None and mine["score"] == 100 and mine["attempt"] == 1,
               f"单题榜取最高分那次：第 {mine['attempt']} 次 {mine['score']} 分，"
               f"第 2 次的 50 分不顶掉它")
        beta.close()
    finally:
        room.stop()


def check_exam_room() -> None:
    """考试模式：封榜 → 不允许重复提交 → 到点收卷 → 统一放榜。"""
    step("考试模式：封榜 → 收卷 → 统一放榜")
    room = Room(mode=RoomMode.EXAM, room_code="246810",
                duration_minutes=30, force_collect=True, allow_resubmit=False)
    room.start()
    try:
        client = room.client("C9W3H5YZ", "李四")
        client.connect()
        client.drain_snapshot()
        exam = dict(client.exam)
        expect(exam["leaderboard_published"] is False, "考试模式一开始就封榜")
        expect(exam["force_collect"] is True and exam["allow_resubmit"] is False,
               "到点强制收卷：开；允许重复提交：关")
        expect(exam["remaining_seconds"] is not None and
               exam["remaining_seconds"] <= 30 * 60,
               f"倒计时由主机下发：剩余 {exam['remaining_seconds']} 秒")

        sealed = client.leaderboard
        expect(sealed.get("published") is False and sealed.get("overall") == [],
               "封榜载荷里 overall 是空的")
        expect("withheld_reason" in sealed,
               f"封榜时给出原因：「{sealed.get('withheld_reason')}」")
        expect(isinstance(sealed.get("myself"), dict),
               "封榜时仍把「你自己那一行」单独带上")

        client.submit("P0001", "cpp", "int main(){return 0;}")
        first = client.wait_for_verdict()
        expect(first["attempt"] == 1 and first["verdict"] == "AC",
               f"第 1 次提交：{first['verdict']} {first['score']} 分")

        client.submit("P0001", "cpp", "int main(){return 0;}")
        rejected = client.wait_for(protocol.MessageKind.ERROR)
        expect("不允许重复提交" in str(rejected.get("message")),
               f"第二次被拒：「{rejected.get('message')}」")

        room.server.collect_from_all("deadline")
        collect = client.wait_for(protocol.MessageKind.COLLECT)
        expect(collect.get("reason") == "deadline" and
               collect.get("force_collect") is True,
               f"收到收卷指令：reason={collect.get('reason')} "
               f"force_collect={collect.get('force_collect')}")

        # 把截止时刻挪到现在之前，宽限走完就该统一放榜。
        # 主机的放榜检查走定时器（生产节奏是每 10 秒一次），所以这里给足 30 秒，
        # 但每次读都带上剩余预算 —— 同步读一旦超时会让连接作废，不能切成小段去轮询。
        room.session.ends_at = datetime.now() - timedelta(seconds=1)
        room.session.grace_seconds = 0
        deadline = time.monotonic() + 30
        published = None
        while published is None:
            budget = deadline - time.monotonic()
            if budget <= 0:
                break
            payload = client.wait_for(protocol.MessageKind.LEADERBOARD,
                                      timeout=budget)
            if payload.get("published"):
                published = payload
        expect(published is not None, "结束后自动统一放榜")
        expect(len(published["overall"]) == 1,
               f"放榜载荷 {len(published['overall'])} 行，"
               f"首行 {published['overall'][0]['username']} "
               f"{published['overall'][0]['score']} 分")
        expect(published["per_problem"].get("P0001"),
               "单题榜也一并下发")
        expect("published" in room.kinds(), "主机发出了 published 事件（仅考试模式）")
        client.close()
    finally:
        room.stop()


def check_no_secret_in_payloads() -> None:
    """把客户端**实际收到的每一条消息**逐个字符串扫一遍，确认房间号与口令没上线。

    扫的是客户端收到的帧，不是主机事件表 —— 后者里有一些只在本机流转的回调
    （比如"服务已启动"），拿它当"上线内容"会误报。想验"线上有什么"，
    就得老老实实看线上收到了什么。
    """
    step("接收侧全量扫描：房间号、口令与源码都不出现在任何推送里")
    code, password = "318462", "Se3cret!"
    room = Room(mode=RoomMode.PRACTICE, room_code=code, password=password)
    room.start()
    received: list[tuple[str, dict]] = []
    #: 源码里埋一个只可能来自它的字符串。判定结果、榜单、状态推送都要扫一遍 ——
    #: 「学生只有榜单」这条如果破了，最先在这里露出来。
    marker = "SECRET-MARKER-4Q7Z"
    secret_code = f"// {marker}\nint main(){{return 0;}}"
    try:
        client = room.client("D5V7X9ZQ", "王五")
        original = client._handle

        def spy(kind: str, message: dict) -> None:
            received.append((kind, message))
            original(kind, message)

        client._handle = spy                    # type: ignore[method-assign]
        client.connect()
        client.drain_snapshot()
        client.submit("P0001", "cpp", secret_code)
        client.wait_for_verdict()
        client.request_leaderboard()
        client.wait_for(protocol.MessageKind.LEADERBOARD)
        client.close()
        time.sleep(0.3)

        leaked = [f"{kind}.{item}" for kind, payload in received
                  for item in leak_report(payload, [code, password])]
        kinds = sorted({kind for kind, _ in received})
        expect(len(received) >= 4,
               f"共截获 {len(received)} 条推送：{'、'.join(kinds)}")
        expect(not leaked,
               "房间号与口令一次都没出现在收到的推送里"
               + (f"（泄漏：{leaked}）" if leaked else ""))
        expect(marker not in repr(received),
               "源码没有随任何推送回到学生这边（榜单里只有排名行）")
        expect(bool(room.session.submissions)
               and marker in room.session.submissions[0].code,
               "那份源码留在主机内存里 —— 主机端「提交与代码」页就是靠它")
    finally:
        room.stop()


# ---------------------------------------------------------------------------
# 真工具链
# ---------------------------------------------------------------------------


def check_real_toolchain(work_root: str) -> None:
    """--real：让真实工具链把代码编译执行一遍。"""
    step("真实工具链判题（Python，无需额外安装）")
    room = Room(mode=RoomMode.PRACTICE, room_code="112233",
                judge=real_judge_factory(detect_python(), work_root),
                work_root=work_root)
    room.start()
    try:
        client = room.client("E6W8Y2R4", "真编译")
        client.connect()
        client.drain_snapshot()
        client.submit("P0001", "python", "a, b = map(int, input().split())\n"
                                        "print(a + b)\n")
        verdict = client.wait_for_verdict()
        expect(verdict["verdict"] == "AC",
               f"真实判题：{verdict['verdict']} {verdict['passed']}/"
               f"{verdict['total']} {verdict['score']} 分 · "
               f"{verdict['message']}")

        client.submit("P0001", "python", "a, b = map(int, input().split())\n"
                                        "print(a + b + 1)\n")
        wrong = client.wait_for_verdict()
        expect(wrong["verdict"] == "WA",
               f"错误答案判 WA（{wrong['passed']}/{wrong['total']}），"
               f"失败测试点没有被算成通过")
        client.close()
    finally:
        room.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="局域网测验真实 TCP 验收")
    parser.add_argument("--real", action="store_true",
                        help="用真实工具链判题（默认用替身，便于无编译器环境）")
    parser.add_argument("--workdir", default="",
                        help="数据目录，默认用临时目录（不碰真实题库）")
    args = parser.parse_args()

    temporary = None
    if args.workdir:
        work_root = Path(args.workdir).resolve()
    else:
        temporary = tempfile.TemporaryDirectory(prefix="oj_lan_e2e_")
        work_root = Path(temporary.name)
    work_root.mkdir(parents=True, exist_ok=True)
    os.environ["OFFLINE_OJ_HOME"] = str(work_root)
    print(f"局域网测验端到端验收 · 工作目录 {work_root}")

    started = time.monotonic()
    check_room_code_never_on_the_wire()
    check_wrong_credentials_rejected()
    check_practice_room()
    check_exam_room()
    check_no_secret_in_payloads()
    if args.real:
        check_real_toolchain(str(work_root / "workspace"))
    else:
        print("\n（判题走替身；加 --real 可让真实工具链跑一遍）")

    elapsed = time.monotonic() - started
    print(f"\n全部通过 · {_step} 个场景 · 耗时 {elapsed:.1f} 秒")
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
