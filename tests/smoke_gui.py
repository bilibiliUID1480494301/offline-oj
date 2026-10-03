"""界面冒烟测试：离屏构建主窗口，检查面板能否正常装配与渲染。

用途有两个：

1. **CI 守卫** —— 新增面板或改了信号接线后，跑一次就能发现"窗口构造直接抛异常"
   这类低级错误，不需要人工点一遍；
2. **截图** —— ``--shot DIR`` 会把每个选项卡渲染成 PNG，方便做界面走查。

使用 ``QT_QPA_PLATFORM=offscreen``，因此无需显示器也能跑::

    python tests/smoke_gui.py --shot build/screens
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 离屏渲染必须在导入 QtWidgets 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def seed_repository(data_dir: Path) -> None:
    """放几道示例题，让界面有内容可渲染。"""
    problems = {
        "P0001": {
            "id": "P0001",
            "title": "两数求和",
            "description": ("## 题目描述\n输入两个整数，输出它们的和。\n\n"
                            "### 输入格式\n一行，两个以空格分隔的整数。\n\n"
                            "### 输出格式\n一行，一个整数：两数之和。\n\n"
                            "```python\na, b = map(int, input().split())\nprint(a + b)\n```\n"),
            "time_limit": 1000,
            "memory_limit": 128,
            "testcases": [
                {"input": "1 2\n", "output": "3\n"},
                {"input": "100 200\n", "output": "300\n"},
            ],
        },
        "P0002": {
            "id": "P0002",
            "title": "回文判断",
            "description": "## 题目描述\n给定一个字符串，判断它是否是回文串。",
            "time_limit": 2000,
            "memory_limit": 256,
            "testcases": [{"input": "level\n", "output": "yes\n"}],
        },
        "P0003": {
            "id": "P0003",
            "title": "最大子段和",
            "description": "## 题目描述\n求序列中连续子段的最大和。",
            "time_limit": 1500,
            "memory_limit": 256,
            "testcases": [{"input": "-2 1 -3 4 -1 2 1 -5 4\n", "output": "6\n"}],
        },
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "problems.json").write_text(
        json.dumps(problems, ensure_ascii=False, indent=2), encoding="utf-8")


#: 用来给编辑器截图/冒烟用的示例代码。刻意全 ASCII ——
#: 离屏平台的中文字体回退不可靠，中文注释会被渲染成空心方块，
#: 那是 fontconfig 的问题不是编辑器的问题，但会干扰走查。
SAMPLE_CODE = """#include <bits/stdc++.h>
using namespace std;

// A + B
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int a = 0, b = 0;
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}
"""


def _free_port() -> int:
    """要一个当前空闲的端口号。

    比硬编码 8899 可靠 —— CI 机器上什么进程都可能占着端口。取出后立刻关掉、
    再交给服务端绑定，中间有一个理论上的竞争窗口，对冒烟测试来说足够了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _pump(app, seconds: float, until=None) -> bool:
    """在离屏环境里推进事件循环，直到条件成立或超时。

    这一步不能省：服务端线程发出来的事件是靠 Qt 的信号排队投递进界面线程的，
    不转事件循环，事件永远到不了控件上 —— 这不是 bug，是 Qt 的设计。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    app.processEvents()
    return True if until is None else bool(until())


def _wait(pred, seconds: float) -> bool:
    """线程侧的轮询等待。与 :func:`_pump` 相对：一个在界面线程推进事件循环，
    一个在工作线程里干等网络回执 —— 两边各看各的状态，谁也不阻塞谁。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def run_exam_smoke(window, app) -> list[str]:
    """局域网测验面板的端到端冒烟。

    这条链路值得单独跑一遍，因为它横跨了四层：面板 → ``ExamServer``
    （自带一堆线程）→ TCP + 加密帧 → ``ExamClient``。任何一层的接线错位，
    在界面上的表现都是"点了没反应"，而"没反应"恰恰是最难靠读代码发现的。
    """
    from offline_oj.core.judge import JudgeReport, TestOutcome
    from offline_oj.core.models import Language, Verdict
    from offline_oj.ui.main_window import TAB_EXAM
    from offline_oj.ui.panels.exam_panel import HOST_TAB_CODE

    checks: list[str] = []
    panel = window.exam_panel
    window.switch_tab(TAB_EXAM)
    panel.set_role("host")

    def fake_judge(problem, code, language) -> JudgeReport:
        """假判题器：不碰工具链，直接给一份满分报告。

        冒烟要验的是"界面能不能把结果画出来"，不是"g++ 能不能编译"。
        真去编译会把界面问题伪装成环境问题，在没有编译器的机器上直接假失败。
        """
        total = max(1, len(problem.testcases))
        report = JudgeReport(problem_id=problem.id, problem_title=problem.title,
                             language=Language.from_value(language),
                             verdict=Verdict.AC)
        for index in range(1, total + 1):
            report.outcomes.append(TestOutcome(
                index=index, total=total, verdict=Verdict.AC,
                time_ms=12.0, memory_mb=3.5))
        return report

    panel._judge = fake_judge
    panel._set_all_checked(True)
    panel.mode_combo.setCurrentIndex(0)        # 练习模式：榜单全程实时
    panel.room_code_edit.setText("135790")
    panel.duration_spin.setValue(0)            # 0 = 不限时
    panel.force_collect_check.setChecked(False)
    panel.resubmit_check.setChecked(True)
    panel.port_spin.setValue(_free_port())
    app.processEvents()

    picked = len(panel._checked_problem_ids())
    assert picked >= 3, f"选题列表没勾上：{picked}"
    checks.append(f"  测验选题: 勾选 {picked} 题")

    panel.open_room()
    app.processEvents()
    assert panel._server is not None and panel._server.running, "开启房间失败"
    port = panel._server.port
    assert panel.room_code_edit.text() == "135790"
    checks.append(f"  主机开启房间: 房间号 135790 · 端口 {port} · 「{panel.host_room_label.text()}」")

    # ---- 一个真客户端从另一条线程进场、提交、收判定 ----
    outcome: dict = {}

    def student() -> None:
        from offline_oj.net.client import ExamClient

        client = ExamClient("127.0.0.1", port, "135790", "ABCD2340", "线程同学")
        try:
            client.connect()
            client.drain_snapshot()
            outcome["problems"] = [view.id for view in client.problems]
            client.submit(client.problems[0].id, "cpp", SAMPLE_CODE)
            outcome["verdict"] = client.wait_for_verdict()
        except Exception as exc:                       # noqa: BLE001
            outcome["error"] = repr(exc)
        finally:
            client.close()

    worker = threading.Thread(target=student, daemon=True)
    worker.start()
    assert _pump(app, 30.0, lambda: "verdict" in outcome or "error" in outcome), \
        "客户端迟迟没有拿到判定结果"
    worker.join(5)
    assert "error" not in outcome, f"线程客户端失败：{outcome}"
    served = outcome["verdict"]
    assert served.get("verdict") == "AC", served
    # 判题器不给测试点分值 → 走"按通过比例折算"，分母是该题的分值之和。
    # 这条同时验了两件事：分值跟着题走（不是恒定的 100），且"全过 = 满分"。
    assert served.get("possible", 0) > 0, served
    assert served.get("score") == served["possible"], served
    assert served.get("attempt") == 1, served
    checks.append(f"  客户端进场: 收到 {len(outcome['problems'])} 题 · "
                  f"第 {served['attempt']} 次 → {served['verdict']} "
                  f"{served['passed']}/{served['total']} · "
                  f"{served['score']}/{served['possible']} 分 · "
                  f"{served['time_ms']}ms/{served['memory_mb']}MB")

    # 主机榜单/名单要跟着更新 —— 事件是跨线程排队过来的，这一条正好验它
    assert _pump(app, 10.0, lambda: panel.host_overall_table.rowCount() >= 1), \
        "主机榜单没有随判定结果刷新"
    assert panel.host_roster_table.rowCount() >= 1, "主机名单是空的"
    assert panel.host_overall_table.item(0, 1).text() == "线程同学"
    assert panel.host_overall_table.item(0, 2).text() == "ABCD2340"
    checks.append(f"  主机榜单: 第 {panel.host_overall_table.item(0, 0).text()} 名 · "
                  f"{panel.host_overall_table.item(0, 1).text()} · "
                  f"设备 {panel.host_overall_table.item(0, 2).text()} · "
                  f"{panel.host_overall_table.item(0, 3).text()} 分")

    # 主机端的「提交与代码」页要读得到刚才那份源码。它不在任何载荷里 ——
    # 学生的机器上根本没有它（见 test_net_lan），这里验的是另一半：
    # 主机内存里那份完整的提交，界面取得到、显示得出来。
    panel.host_tabs.setCurrentIndex(HOST_TAB_CODE)
    app.processEvents()
    assert panel.host_code_table.rowCount() >= 1, "提交列表是空的"
    assert panel.host_code_view.toPlainText() == SAMPLE_CODE, \
        "主机端读不到学生交上来的源码"
    assert "线程同学" in panel.host_code_caption.text()
    checks.append(f"  主机端读码: 编号 {panel.host_code_table.item(0, 0).text()} · "
                  f"{panel.host_code_caption.text()}")

    # ---- 学生端：面板自己以学生身份进场，把界面接线也走一遍 ----
    panel.set_role("student")
    panel.address_edit.setText("127.0.0.1")
    panel.port_edit.setValue(port)
    panel.student_code_edit.setText("135790")
    panel.student_name_edit.setText("面板同学")
    panel.toggle_join()
    assert _pump(app, 30.0, lambda: panel._client is not None), "面板学生端没有连上"
    assert panel.join_button.text() == "离开房间"
    checks.append(f"  学生端加入: 设备 ID {panel._client.device_id} · "
                  f"用户名 {panel._client.username}")

    assert _pump(app, 10.0, lambda: panel.student_problem_list.count() >= 1), \
        "学生端没收到题目列表"
    panel.student_problem_list.setCurrentRow(0)
    app.processEvents()
    problem_id = panel._current_problem_id
    assert problem_id, "选中题目后没有记录题目 ID"
    assert problem_id in panel.statement_view.toPlainText(), "题面没有渲染出来"
    checks.append(f"  学生端题面: {problem_id} 已渲染"
                  f"（{panel.student_problem_list.count()} 题可选）")

    panel.code_editor.setPlainText(SAMPLE_CODE)
    panel.submit_code()
    assert _pump(app, 30.0, lambda: panel.mine_table.rowCount() >= 1
                 and panel.mine_table.item(0, 3).text() == "AC"), \
        "学生端的提交没有拿到 AC"
    checks.append(f"  学生端提交: {problem_id} "
                  f"{panel.mine_table.item(0, 2).text()} → "
                  f"{panel.mine_table.item(0, 3).text()} "
                  f"{panel.mine_table.item(0, 4).text()} · "
                  f"{panel.mine_table.item(0, 5).text()} 分")
    assert panel.submit_button.isEnabled(), "练习模式进行中应当还能继续提交"

    assert _pump(app, 20.0, lambda: panel.student_board_table.rowCount() >= 2), \
        "学生端排行榜没有收到全榜"
    checks.append(f"  学生端榜单: {panel.student_board_table.rowCount()} 行")

    # 封榜分支：考试模式封榜时榜单是空的，界面得给出原因，不能只给一张空表。
    # 带上 max_total_score，验的正是"得分与分母同刻度"（20/60，不是 100/60）。
    panel._render_student_board({
        "published": False,
        "withheld_reason": "考试模式下成绩将在测验结束后统一公布",
        "max_total_score": 60,
        "myself": {"rank": 2, "score": 20}})
    app.processEvents()
    assert panel.student_board_table.rowCount() == 1
    reason = panel.student_board_table.item(0, 0).text()
    assert "统一公布" in reason and "你自己" in reason, reason
    assert "20/60 分" in reason, reason
    checks.append("  封榜提示: 空表换成原因说明，并带上「你自己」那一行")

    # 到点强制收卷：自动交一次 + 锁编辑器
    panel._editor_locked = False
    panel.code_editor.setReadOnly(False)
    panel.code_editor.setPlainText(SAMPLE_CODE)
    panel._on_collect({"reason": "deadline", "force_collect": True})
    app.processEvents()
    assert panel._editor_locked and panel.code_editor.isReadOnly(), \
        "强制收卷之后编辑器没有锁定"
    assert _pump(app, 30.0, lambda: panel.mine_table.rowCount() >= 2), \
        "强制收卷没有自动把代码交上去"
    tag = panel.mine_table.item(1, 2).text()
    assert "自动" in tag, tag
    checks.append(f"  到点收卷: 编辑器已锁定 · 自动提交记为「{tag}」")

    # 收尾：房间与学生端都要关干净。留一个开着的房间，window.close() 会弹
    # 模态确认框，而离屏环境里没人点得到它（这正是真实用户会看到的正确行为）
    panel.leave_room()
    app.processEvents()
    panel.close_room(quiet=True)
    app.processEvents()
    assert panel._server is None and panel._client is None
    assert panel.on_closing() is True, "没有房间在跑时关闭钩子不该拦下退出"
    checks.append("  收尾: 学生端已离开 · 房间已关闭 · 关闭钩子放行")

    # 关房自动留档 → 回看 → 删除，走一整圈。
    # 这条链子全是"不报错"的类型：存档失败会被 try/except 吞掉（绝不能拦下关房），
    # 所以只有真去读一遍才算验过。
    from offline_oj.ui.panels.exam_panel import HOST_TAB_ARCHIVE, HOST_TAB_CODE

    panel.host_tabs.setCurrentIndex(HOST_TAB_ARCHIVE)
    app.processEvents()
    assert len(panel._archives) == 1, f"关房没有留下档案：{len(panel._archives)}"
    item = panel._archives[0]
    assert item.record.submission_count >= 2, item.record.submission_count
    assert item.has_code, "默认应当连源码一起留档"
    checks.append(f"  关房留档: {len(panel._archives)} 份 · "
                  f"{item.record.submission_count} 份提交 · "
                  f"{'含源码' if item.has_code else '仅成绩'}")

    panel.archive_table.selectRow(0)
    app.processEvents()
    panel.open_selected_archive()
    app.processEvents()
    assert panel._archive is not None, "打开档案没有读进内存"
    assert panel.host_tabs.currentIndex() == HOST_TAB_CODE, "打开档案没有切到「提交与代码」"
    # 注意：关房后整个主机页被收起（面板回到"不在房间里"的空态），所以这里
    # 只能看控件**自己**的显示标志。isVisible() 会被祖先连带判 False，
    # 那是关房的正确行为，不是"提示没出来"。
    assert not panel.code_scope_label.isHidden(), "回看档案时没有说明这是哪一场"
    assert panel.code_scope_label.text().startswith("档案 ·"), \
        panel.code_scope_label.text()
    assert any(sub.code for sub in panel._archive_submissions), "档案里没有源码"
    checks.append("  回看档案: 已切到「提交与代码」· 作用域提示已点亮")

    panel.close_archive()
    app.processEvents()
    assert panel._archive is None and panel.code_scope_label.isHidden()
    checks.append("  退出回看: 数据源换回进行中的这一场")

    # 删除要过一道确认框；离屏环境点不到模态框，替掉确认方法
    panel.archive_table.selectRow(0)
    app.processEvents()
    panel._confirm_delete = lambda label, count: True
    panel.delete_selected_archive()
    app.processEvents()
    assert panel._archives == [], "删除后列表没有刷新"
    checks.append("  删除档案: 确认后已从列表移除")
    return checks


def run_access_smoke(window, app) -> list[str]:
    """考场身份与管控的端到端冒烟：APP 内建名单 → 账号进场 → 离场锁屏。

    与 :func:`run_exam_smoke` 相对，这条链验的是新的一等公民：名单在 APP 里
    直接建（不经 Excel）、凭据从"房间号"换成"账号 + 个人口令"、学生锁屏防窥、
    老师在名单页替人解锁。任何一环接线错位，现场表现都是"进不去 / 盖不上 /
    解不开" —— 恰好都是读代码最难发现的那一类。
    """
    from PySide6.QtWidgets import QTableWidgetItem

    from offline_oj.net.session import EntryMode
    from offline_oj.ui.main_window import TAB_EXAM
    from offline_oj.ui.panels.exam_panel import HOST_TAB_ARCHIVE, HOST_TAB_ROSTER
    from offline_oj.ui.roster_dialog import RosterDialog

    checks: list[str] = []
    panel = window.exam_panel
    window.switch_tab(TAB_EXAM)
    panel.set_role("host")
    # 拆掉 notify 的模态接收者 —— 这条链会触发"名单里有选手没口令，已经
    # 补上了"的警告，而 MainWindow 的 notify 对 warning 也弹 QMessageBox，
    # 离屏环境里 exec() 永久挂死（与 pytest 夹具同一条纪律）。
    # 用例想看提示就自己接一个收集器。
    try:
        panel.notify.disconnect()
    except RuntimeError:
        pass                                   # 已经拆过了
    notes: list[tuple[str, str]] = []
    panel.notify.connect(lambda level, text: notes.append((level, text)))

    # ---- 开房守卫：账号进场没名单，必须被挡在开房前 ----
    index = panel.entry_mode_combo.findData(EntryMode.ACCOUNT)
    assert index >= 0, "进场方式下拉里没有「账号进场」项"
    panel.entry_mode_combo.setCurrentIndex(index)
    # 上一间房刚关，别赌同一个端口能立刻重绑（TIME_WAIT），换一个
    panel.port_spin.setValue(_free_port())
    app.processEvents()
    assert panel.roster_label.text() == "未选择名单", panel.roster_label.text()
    panel.open_room()
    app.processEvents()
    assert panel._server is None, "账号进场没名单不该开成房"
    assert any("名单" in text for _level, text in notes), \
        f"守卫提示没有送达：{notes}"
    checks.append("  开房守卫: 账号进场未选名单被挡下")

    # ---- APP 内直接建名单。走真编辑器，只是绕开 exec() —— 离屏点不了模态框。
    # 第二位故意不填口令，验开房前的自动补发。
    dialog = RosterDialog(panel.ctx)
    dialog.name_edit.setText("冒烟一班")
    for row_values in (("2025001", "名单同学", "246810", "01"),
                       ("2025002", "替补同学", "", "02")):
        row = dialog.table.rowCount()
        dialog.table.insertRow(row)
        for column, value in enumerate(row_values):
            dialog.table.setItem(row, column, QTableWidgetItem(value))
    dialog.save()
    roster = dialog.roster()
    assert roster is not None and len(roster) == 2, "名单保存失败"
    # 接到面板上：与 edit_roster() 收尾完全相同的三步（exec() 会挂死，绕开弹框）
    panel._roster = roster
    panel._roster_path = panel.ctx.paths.roster_file(roster.title)
    panel.ctx.settings.set("last_roster", roster.title)
    panel._refresh_roster_label()
    assert panel.roster_label.text() == "冒烟一班 · 2 人", panel.roster_label.text()
    checks.append("  APP 内建名单: 冒烟一班 · 2 人（第二位没填口令）")

    # ---- 开房：缺口令的人被自动补发，凭据换成账号 ----
    panel.open_room()
    app.processEvents()
    assert panel._server is not None, "有名单仍开不了房"
    assert panel._session.entry_mode is EntryMode.ACCOUNT
    port = panel._server.port
    spare = panel._roster.by_account("2025002")
    assert spare is not None and spare.passcode and spare.passcode[0] != "0", \
        "没口令的选手没有被自动补发"
    assert any("补上" in text for _level, text in notes), \
        f"自动补发没有提示老师：{notes}"
    checks.append(f"  账号进场开房: 缺口令已自动补发（{spare.passcode}）· 端口 {port}")

    # ---- 线程客户端走账号进场，把锁屏全链路拉一遍 ----
    outcome: dict = {}

    def account_student() -> None:
        from offline_oj.net.client import ExamClient

        client = ExamClient("127.0.0.1", port, device_id="BEAC2026",
                            account="2025001", passcode="246810")
        try:
            client.connect()
            client.drain_snapshot()
            # 之后的锁/解锁全靠主机推送 —— 不起读取线程的话，那些帧会堆在
            # 内核缓冲区里，client.locked 永远不会翻转（第一次跑就栽在这）
            client.start_listener()
            outcome["problems"] = len(client.problems)
            outcome["name"] = client.username      # 名字应由名单回填
            outcome["entry_mode"] = client.entry_mode
            client.request_lock()                  # 我要离开一下
            if not _wait(lambda: client.locked, 15):
                raise AssertionError("锁屏指令没有回到客户端")
            outcome["self_locked"] = True
            if not _wait(lambda: not client.locked, 30):
                raise AssertionError("老师解锁没有回到客户端")
            outcome["teacher_unlocked"] = True
            client.request_lock()                  # 再盖一次，这回自己输口令解
            if not _wait(lambda: client.locked, 15):
                raise AssertionError("第二次锁屏没有生效")
            client.request_unlock("246810")
            if not _wait(lambda: not client.locked, 15):
                raise AssertionError("个人口令解不开屏幕")
            outcome["self_unlocked"] = True
        except Exception as exc:                   # noqa: BLE001
            outcome["error"] = repr(exc)
        finally:
            client.close()

    worker = threading.Thread(target=account_student, daemon=True)
    worker.start()
    assert _pump(app, 15.0, lambda: outcome.get("self_locked")), \
        f"客户端没能自己锁上：{outcome}"
    # 主机名单页要看到「离开中」，老师再替他解开
    assert _pump(app, 10.0, lambda: panel.roster_hint.text() == "1 人离开中"), \
        f"主机没有随锁屏刷新：{panel.roster_hint.text()!r}"
    panel.host_tabs.setCurrentIndex(HOST_TAB_ROSTER)
    app.processEvents()
    assert panel.host_roster_table.rowCount() == 1, "名单页没有这一位选手"
    assert panel.host_roster_table.item(0, 1).text() == "名单同学", \
        "名单页的名字应由名单决定，而不是学生自报"
    assert panel.host_roster_table.item(0, 2).text() == "2025001", "名单页没显示账号"
    assert panel.host_roster_table.item(0, 4).text() == "离开中", "锁列没有跟着亮"
    # 选中与点击之间不留事件空隙：名单页每次刷新都会重排选中，见缝插针会点空
    panel.host_roster_table.selectRow(0)
    assert panel.unlock_one_button.isEnabled(), \
        "选中离开中的人后「让 TA 继续」应当可用"
    panel.unlock_one_button.click()
    assert _pump(app, 30.0, lambda: outcome.get("teacher_unlocked")), \
        f"老师解锁没有送达：{outcome}"
    assert _pump(app, 10.0, lambda: panel.roster_hint.text() == ""), \
        f"解锁后提示没有清掉：{panel.roster_hint.text()!r}"
    assert _pump(app, 30.0, lambda: outcome.get("self_unlocked")
                 or "error" in outcome), f"自己解锁没走通：{outcome}"
    worker.join(5)
    assert "error" not in outcome, f"账号客户端失败：{outcome}"
    assert outcome["entry_mode"] == EntryMode.ACCOUNT.value, outcome
    checks.append(f"  锁屏全链路: 自己锁 → 主机「1 人离开中」→ 老师解开 → "
                  f"自己再锁 → 个人口令解开 · {outcome['name']} · "
                  f"{outcome['problems']} 题 · 进场方式 {outcome['entry_mode']}")

    # ---- 口令不对进不来。主机刻意不区分"没这个账号"和"密码不对"（防枚举）----
    outcome2: dict = {}

    def bad_student() -> None:
        from offline_oj.net.client import ExamClient

        client = ExamClient("127.0.0.1", port, device_id="BEAC2027",
                            account="2025001", passcode="999999")
        try:
            client.connect()
        except Exception as exc:                   # noqa: BLE001
            outcome2["error"] = repr(exc)
        else:
            outcome2["connected"] = True
        finally:
            client.close()

    worker2 = threading.Thread(target=bad_student, daemon=True)
    worker2.start()
    worker2.join(10)
    assert "connected" not in outcome2, "错误口令不该放进来"
    assert "账号" in outcome2.get("error", ""), outcome2
    checks.append("  错口令: 被拒（话术不区分账号不存在与密码不对）")

    # ---- 面板自己以账号进场，界面接线（页签切换 + 盖板）也走一遍 ----
    panel.set_role("student")
    panel.address_edit.setText("127.0.0.1")
    panel.port_edit.setValue(port)
    index = panel.join_mode_combo.findData(EntryMode.ACCOUNT)
    assert index >= 0, "学生端进场下拉里没有「账号进场」项"
    panel.join_mode_combo.setCurrentIndex(index)
    app.processEvents()
    assert panel.join_cred_stack.currentIndex() == 1, "没切到「账号口令」页"
    panel.student_account_edit.setText("2025002")
    panel.student_passcode_edit.setText(spare.passcode)
    panel.toggle_join()
    assert _pump(app, 30.0, lambda: panel._client is not None), "面板学生端账号进场失败"
    assert panel.join_button.text() == "离开房间"
    assert panel._client.username == "替补同学", "名字应由名单回填"
    checks.append(f"  面板账号进场: 设备 {panel._client.device_id} · "
                  f"{panel._client.username}（名字来自名单）")

    assert panel.away_button.isEnabled(), "填了个人口令就应当可以锁屏"
    panel.away_button.click()
    assert _pump(app, 15.0, lambda: panel.student_stack.currentIndex() == 1), \
        "离场盖板没有盖上"
    panel.lock_passcode_edit.setText(spare.passcode)
    panel.unlock_screen()
    assert _pump(app, 15.0, lambda: panel.student_stack.currentIndex() == 0), \
        "输对口令没有解开盖板"
    checks.append("  离场盖板: 「离开一下」盖上 · 输个人口令继续答题")

    # ---- 收尾与留档：绑定关系（设备→账号）只进老师这边的档案 ----
    panel.leave_room()
    app.processEvents()
    panel.close_room(quiet=True)
    app.processEvents()
    assert panel._server is None and panel._client is None
    checks.append("  收尾: 学生端已离开 · 房间已关闭")

    panel.host_tabs.setCurrentIndex(HOST_TAB_ARCHIVE)
    app.processEvents()
    assert len(panel._archives) == 1, f"关房没有留下档案：{len(panel._archives)}"
    item = panel._archives[0]
    accounts = {str(p.get("account") or "") for p in item.record.participants}
    assert {"2025001", "2025002"} <= accounts, accounts
    checks.append("  留档: 档案里并进了账号（2025001 / 2025002），名单不外泄但成绩单够用")
    return checks


def run_theme_smoke(window, app) -> list[str]:
    """换主题往返，并**按像素**验证配色真的换了。

    为什么不满足于"切主题没抛异常"：这一轮之前的缺陷全都是**不报错**的类型 ——
    内联写死的色值在换主题时不会重刷、深色主题下白字压亮色的对比度只有 2.3:1。
    两者都能让程序跑得欢快，只是界面没法看。

    做法：造一个徽标标签，用 ``apply_verdict_badge`` 上色，渲染出来取中心像素，
    和调色板里的色值直接比。属性选择器、repolish、``setStyleSheet`` 全量重刷
    这条链路只要有一环断了，这里就会对不上。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from offline_oj.ui.theme import DARK, LIGHT, apply_verdict_badge

    checks: list[str] = []

    badge = QLabel("AC", window)
    badge.setMinimumSize(48, 24)
    badge.setAlignment(Qt.AlignCenter)
    badge.show()
    app.processEvents()

    def badge_pixel() -> tuple[int, int, int]:
        """取徽标渲染结果的**众数**颜色，也就是底色。

        不能取中心单点：徽标文字正好居中，中心那一点采到的是字形本身
        （实测拿到的是白色前景而不是绿色底），于是"配色对不对"就验偏了。
        底色在整幅图里占绝大多数像素，取众数才是稳的。
        """
        badge.adjustSize()
        app.processEvents()
        image = badge.grab().toImage()
        tally: dict[tuple[int, int, int], int] = {}
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                key = (color.red(), color.green(), color.blue())
                tally[key] = tally.get(key, 0) + 1
        return max(tally.items(), key=lambda item: item[1])[0]

    def rgb(value: str) -> tuple[int, int, int]:
        raw = value.lstrip("#")
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]

    # 判题徽标配色对标洛谷。用 TLE 当探针而不是 AC：AC 的洛谷色在两套主题下
    # 是同一个值，切成深色也不会变，那样就验不出"动态属性有没有随主题重刷"。
    # TLE 的 ``#052242`` 在深色面板上会和面板糊成一片，必须提亮 —— 正好有差别。
    apply_verdict_badge(badge, "AC")
    app.processEvents()
    light_ac = badge_pixel()
    assert light_ac == rgb(LIGHT.verdict_badge_color("AC")), (
        f"浅色主题下 AC 徽标底色应为 {LIGHT.verdict_badge_color('AC')}，实际 {light_ac}")
    checks.append(f"  徽标配色·AC（浅色）: {LIGHT.verdict_badge_color('AC')} ✓")

    apply_verdict_badge(badge, "TLE")
    app.processEvents()
    light_tle = badge_pixel()
    assert light_tle == rgb(LIGHT.verdict_badge_color("TLE")), (
        f"浅色主题下 TLE 徽标底色应为 {LIGHT.verdict_badge_color('TLE')}，实际 {light_tle}")
    checks.append(f"  徽标配色·TLE（浅色）: {LIGHT.verdict_badge_color('TLE')} ✓")

    # 属性仍是 "tle"，这里不再设一次 —— 要验的正是"换主题能自己重刷"
    window.apply_theme(DARK, persist=False)
    app.processEvents()
    dark_tle = badge_pixel()
    assert dark_tle == rgb(DARK.verdict_badge_color("TLE")), (
        f"切到深色后 TLE 徽标底色应为 {DARK.verdict_badge_color('TLE')}，实际 {dark_tle}\n"
        f"  这说明动态属性没有随主题重刷")
    checks.append(f"  徽标配色·TLE（深色）: {DARK.verdict_badge_color('TLE')} ✓")
    assert light_tle != dark_tle, "TLE 的徽标底色在两套主题下应当不同"

    # 每个面板都要跟着换，一个有 is_dark 没变的就说明它的 apply_palette 漏了
    for panel in window._panels():
        name = type(panel).__name__
        assert panel.palette.is_dark, f"{name} 切到深色后 palette 没更新"
        assert panel.palette is DARK, f"{name} 拿到的不是同一个调色板对象"
    checks.append(f"  {len(window._panels())} 个面板的主题已同步")

    # 角色属性要能扛过换主题（属性没了，外观就退回默认，界面上看不出来）
    assert window.help_panel.note_label.property("role") == "danger"
    checks.append("  语义角色在换主题后仍然保留")

    window.apply_theme(LIGHT, persist=False)
    app.processEvents()
    assert badge_pixel() == light_tle, "切回浅色后徽标应回到原来的颜色"
    checks.append("  切回浅色: OK")

    badge.deleteLater()
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="界面冒烟测试")
    parser.add_argument("--shot", default="", metavar="DIR", help="把各选项卡截图保存到目录")
    parser.add_argument("--data-dir", default="", help="使用指定数据目录（默认临时目录）")
    parser.add_argument("--theme", default="light", choices=["light", "dark", "system"],
                        help="渲染时使用的主题")
    args = parser.parse_args()

    # Windows 上把输出重定向到文件时，stdout 的编码会退化成 GBK（代码页 936），
    # 而检查清单里有 ``✓`` 这类字符 —— 一次 print 就抛 UnicodeEncodeError。
    # 它发生在**所有断言都跑完之后**的打印阶段，于是"功能其实全过了、脚本却
    # 以异常收场"，非常容易被读成失败（这个坑真的踩过：exit code 与输出一起
    # 没了，只剩几张截图能证明跑到了哪一步）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass                                  # 老环境/被替换过的流：不阻塞主流程

    temporary = None
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
    else:
        temporary = tempfile.TemporaryDirectory(prefix="oj_smoke_")
        data_dir = Path(temporary.name)
    os.environ["OFFLINE_OJ_HOME"] = str(data_dir)

    from PySide6.QtWidgets import QApplication

    from offline_oj import APP_NAME, APP_ORGANIZATION, __version__
    from offline_oj.context import AppContext
    from offline_oj.paths import build_paths
    from offline_oj.ui.main_window import TAB_PROBLEMS, TAB_SOLVE, MainWindow

    seed_repository(data_dir)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORGANIZATION)
    app.setApplicationVersion(__version__)

    paths = build_paths().ensure_layout()
    ctx = AppContext.create(paths)
    ctx.settings.set("theme", args.theme)

    assert len(ctx.repository) == 3, f"示例题库加载失败：{len(ctx.repository)}"

    window = MainWindow(ctx)
    window.resize(1440, 900)
    window.show()
    app.processEvents()

    # 让存题面板载入第一道题，界面上才有内容
    window.switch_tab(TAB_PROBLEMS)
    window.problems_panel.load_problem("P0001")
    app.processEvents()

    checks: list[str] = []
    for index in range(window.tabs.count()):
        window.switch_tab(index)
        app.processEvents()
        name = window.tabs.tabText(index)
        checks.append(f"  面板 {index} {name}: OK")
        if args.shot:
            target_dir = Path(args.shot)
            target_dir.mkdir(parents=True, exist_ok=True)
            image = window.grab()
            file_name = f"{index}_{name}.png"
            if image.save(str(target_dir / file_name)):
                checks.append(f"    截图 {file_name}")

    # 写题面板：选一道题，确认描述与语言切换正常
    window.switch_tab(TAB_SOLVE)
    window.solve_panel.load_problem("P0001")
    app.processEvents()
    assert window.solve_panel.current_problem_id == "P0001"
    assert window.solve_panel.current_language() is not None
    checks.append("  写题面板载入题目: OK")

    # 代码编辑器：着色 + 代码提示
    from PySide6.QtGui import QTextCursor

    from offline_oj.core.models import Language

    editor = window.solve_panel.code_editor
    editor.set_language(Language.CPP)
    editor.setPlainText(SAMPLE_CODE)
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    editor.setTextCursor(cursor)
    editor.insertPlainText("    vec")
    app.processEvents()

    editor._completer.try_complete()
    app.processEvents()
    popup = editor._completer.popup()
    assert popup.isVisible(), "代码提示候选框没有弹出"
    count = editor._completer.completionCount()
    assert count > 0, "代码提示没有候选"
    # 候选框弹出时**不许**预选任何一项：一旦有当前项，编辑器里的回车就会被
    # 当成"采纳当前项"，于是写代码时回车不再换行（曾经就是这样）。
    assert not popup.currentIndex().isValid(), "候选框不该预选任何一项"
    checks.append(f"  代码提示: {count} 条候选，未预选 ✓")

    # 采纳一条片段，确认多行展开与光标位置
    editor._completer.insert("main")
    assert "int main() {" in editor.toPlainText()
    checks.append("  片段展开: OK")

    if args.shot:
        target_dir = Path(args.shot)
        target_dir.mkdir(parents=True, exist_ok=True)
        editor.setPlainText(SAMPLE_CODE)
        end = editor.textCursor()
        end.movePosition(QTextCursor.End)
        editor.setTextCursor(end)
        editor.insertPlainText("    vec")
        editor._completer.try_complete()
        app.processEvents()
        saved = [
            name for name, widget in (
                ("09_code_highlight.png", editor),
                ("10_code_completion.png", editor._completer.popup()),
            ) if widget.grab().save(str(target_dir / name))
        ]
        checks.append("    截图 " + " / ".join(saved))

    # 存题面板：编辑并校验
    window.switch_tab(TAB_PROBLEMS)
    panel = window.problems_panel
    panel.id_edit.setText("P9001")
    panel.title_edit.setText("冒烟测试题")
    panel.description_editor.setPlainText("## 描述\n测试用")
    panel.cases.set_count(2, [])
    problem = panel._collect()
    checks.append(f"  表单收集: {problem.id} / {len(problem.testcases)} 个测试点")

    # 判题方式：文件输入输出 + 自定义校验器，收集与回填都要对得上
    from offline_oj.core.models import IOMode, Language

    def pick(combo, value):
        index = combo.findData(value)
        assert index >= 0, f"下拉框里没有 {value!r}"
        combo.setCurrentIndex(index)

    pick(panel.io_mode_combo, IOMode.FILE.value)
    panel.input_file_edit.setText("a.in")
    panel.output_file_edit.setText("a.out")

    assert panel.input_file_edit.isEnabled(), "文件模式下输入文件名应当可编辑"
    pick(panel.io_mode_combo, IOMode.STDIO.value)
    assert not panel.input_file_edit.isEnabled(), "标准输入输出下文件名应当置灰"
    pick(panel.io_mode_combo, IOMode.FILE.value)
    assert (panel.input_file_edit.text(), panel.output_file_edit.text()) == ("a.in", "a.out"), \
        "来回切换输入输出方式不该冲掉已填的文件名"

    assert not panel.checker_group.isVisible(), "没勾校验器时不该显示校验器编辑区"
    panel.checker_check.setChecked(True)
    app.processEvents()
    assert panel.checker_group.isVisible(), "勾上校验器后编辑区应当出现"
    pick(panel.checker_language_combo, Language.PYTHON.value)
    panel.checker_editor.setPlainText("import sys\nsys.exit(0)\n")

    if args.shot:
        target_dir = Path(args.shot)
        target_dir.mkdir(parents=True, exist_ok=True)
        if window.grab().save(str(target_dir / "11_judge_config.png")):
            checks.append("    截图 11_judge_config.png")

    problem = panel._collect()
    judge = problem.judge
    assert judge.io_mode is IOMode.FILE, judge
    assert (judge.input_file, judge.output_file) == ("a.in", "a.out"), judge
    assert judge.uses_checker, judge
    assert judge.checker_language is Language.PYTHON, judge
    assert judge.checker.startswith("import sys"), judge
    checks.append(f"  判题方式收集: {problem.summary()}")

    # 存进题库再读回来，确认各项都能正确回填
    panel.ctx.repository.put(problem)
    panel.load_problem("P9001")
    app.processEvents()
    assert panel._collect().judge.to_dict() == judge.to_dict(), "判题方式回填不一致"
    assert panel.checker_editor.toPlainText().startswith("import sys")
    checks.append("  判题方式回填: OK")

    # 取消勾选后，配置里不该再出现校验器字段（老题库 JSON 才不会多出空壳）
    panel.checker_check.setChecked(False)
    plain = panel._collect()
    assert not plain.judge.uses_checker
    assert "checker" not in plain.judge.to_dict(), plain.judge.to_dict()
    checks.append("  取消校验器: 配置里不再出现 checker 字段")

    # 上面几步是真的改了表单，清掉脏标记，免得 closeEvent 弹出"是否保存"的模态框
    # ——离屏环境里没人点得到它。这里只是"不想保存这份草稿"，不是绕开缺陷。
    panel._set_dirty(False)

    checks.extend(run_exam_smoke(window, app))
    checks.extend(run_access_smoke(window, app))

    # 换主题必须保持"没改过"：以前语法高亮重排会发 textChanged，被面板当成
    # 用户编辑，于是关窗口时弹出保存确认，在离屏环境下把冒烟测试挂死。
    checks.extend(run_theme_smoke(window, app))
    assert not panel._dirty, "换主题不该把存题面板标成有未保存的修改"

    window.close()
    app.processEvents()

    print("\n".join(checks))
    print(f"\n冒烟测试通过 · 数据目录 {data_dir}")
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
