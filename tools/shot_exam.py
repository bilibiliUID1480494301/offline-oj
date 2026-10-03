"""局域网测验的界面走查截图。

把面板摆到"正在用"的状态再截图 —— 空面板看不出问题：房间号大字、榜单列宽、
题面里的文件输入输出提示、代码高亮、封榜提示，都只有跑起来才看得见。

用法::

    python tools\\shot_exam.py                 # 浅色，输出到 build\\screens
    python tools\\shot_exam.py --theme dark --out build\\screens-dark

判题走替身（不碰工具链），所以没有编译器的机器也能跑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 离屏渲染必须在导入 QtWidgets 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PORT = 54896

PROBLEMS = {
    "P0001": {
        "id": "P0001", "title": "两数求和", "slug": "sum-two",
        "description": ("## 题目描述\n输入两个整数，输出它们的和。\n\n"
                        "### 输入格式\n一行，两个以空格分隔的整数。\n\n"
                        "### 输出格式\n一行，一个整数：两数之和。\n\n"
                        "### 数据范围\n两数的绝对值均不超过 10 的 9 次方。\n"),
        "time_limit": 1000, "memory_limit": 128,
        "testcases": [{"input": "1 2\n", "output": "3\n", "sample": True},
                      {"input": "100 200\n", "output": "300\n"}],
    },
    "P0002": {
        "id": "P0002", "title": "回文判断", "slug": "palindrome",
        "description": "## 题目描述\n给定一个字符串，判断它是否是回文串。",
        "time_limit": 2000, "memory_limit": 256,
        "testcases": [{"input": "level\n", "output": "yes\n", "sample": True}],
    },
    "P0003": {
        "id": "P0003", "title": "最大子段和", "slug": "maxsub",
        "description": "## 题目描述\n求序列中连续子段的最大和。",
        "time_limit": 1500, "memory_limit": 256,
        "testcases": [{"input": "-2 1 -3 4 -1 2 1 -5 4\n", "output": "6\n"}],
    },
}

#: 三位同学：**故意两个都叫张三** —— 榜单靠设备 ID 区分同名的人，
#: 这一点在截图里要看得见。
STUDENTS = (("A7K2M9QX", "张三"), ("B4N8P2RT", "李四"), ("C9W3H5YZ", "张三"))

PAIR_CODE = """#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int a = 0, b = 0;
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}
"""

#: 少一个分号的一份，"编译"会失败。有它，「判定说明」页才走查得到 ——
#: 全 AC 的截图看不出那块地方放编译错误时读不读得清。
BROKEN_CODE = """#include <bits/stdc++.h>
using namespace std;

int main() {
    int a, b
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}
"""

#: 替身判题器给出的"编译器原文"。真 g++ 的输出比这个长，取一段形状相同的
#: （带行号、带脱字符定位）就够验证多行文本在那一栏里读不读得清。
COMPILE_MESSAGE = """main.cpp: In function 'int main()':
main.cpp:5:13: error: expected ',' or ';' before 'cin'
    5 |     int a, b
      |             ^
      |             ;
main.cpp:6:5: error: 'cin' does not name a type
    6 |     cin >> a >> b;
      |     ^~~"""


def fake_judge(problem, code, language):
    """替身判题器：不碰工具链，直接给满分报告。

    唯一的例外是 :data:`BROKEN_CODE` —— 它判编译失败，好让截图里同时有
    "过了"和"没过"两种样子。
    """
    from offline_oj.core.judge import JudgeReport, TestOutcome
    from offline_oj.core.models import Language, Verdict

    if code.strip() == BROKEN_CODE.strip():
        return JudgeReport(problem_id=problem.id, problem_title=problem.title,
                           language=Language.from_value(language),
                           verdict=Verdict.CE, compile_ok=False,
                           compile_message=COMPILE_MESSAGE)

    total = max(1, len(problem.testcases))
    report = JudgeReport(problem_id=problem.id, problem_title=problem.title,
                         language=Language.from_value(language),
                         verdict=Verdict.AC)
    for index in range(1, total + 1):
        report.outcomes.append(TestOutcome(index=index, total=total,
                                           verdict=Verdict.AC,
                                           time_ms=11.0 * index, memory_mb=3.2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="局域网测验界面截图")
    parser.add_argument("--out", default="build/screens", help="截图输出目录")
    parser.add_argument("--theme", default="light", choices=["light", "dark"],
                        help="渲染时使用的主题")
    args = parser.parse_args()

    temporary = tempfile.TemporaryDirectory(prefix="oj_shot_exam_")
    os.environ["OFFLINE_OJ_HOME"] = temporary.name
    data = Path(temporary.name)
    data.mkdir(parents=True, exist_ok=True)
    (data / "problems.json").write_text(
        json.dumps(PROBLEMS, ensure_ascii=False, indent=2), encoding="utf-8")

    from PySide6.QtWidgets import QApplication

    from offline_oj import APP_NAME, APP_ORGANIZATION
    from offline_oj.context import AppContext
    from offline_oj.net.client import ExamClient
    from offline_oj.paths import build_paths
    from offline_oj.ui.main_window import TAB_EXAM, MainWindow
    # 页签下标一律走常量：插一个新页会把裸数字静默指到隔壁，截图不会报错，
    # 只会拍到别的页上 —— 那样"走查过了"就是假的。
    from offline_oj.ui.panels.exam_panel import (
        HOST_TAB_CODE,
        HOST_TAB_LOG,
        HOST_TAB_PROBLEM,
        HOST_TAB_ROSTER,
        STUDENT_TAB_BOARD,
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORGANIZATION)
    ctx = AppContext.create(build_paths().ensure_layout())
    ctx.settings.set("theme", args.theme)

    window = MainWindow(ctx)
    window.resize(1500, 940)
    window.show()
    app.processEvents()

    def pump(seconds: float, until=None) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            if until is not None and until():
                return True
            time.sleep(0.02)
        return True if until is None else bool(until())

    window.switch_tab(TAB_EXAM)
    app.processEvents()
    panel = window.exam_panel
    panel._judge = fake_judge               # 判题走替身，截图不需要工具链
    panel.room_code_edit.setText("135790")
    panel.mode_combo.setCurrentIndex(0)     # 练习模式：榜单实时更新
    panel.duration_spin.setValue(90)
    panel.force_collect_check.setChecked(True)
    panel.port_spin.setValue(PORT)
    panel.open_room()
    pump(2)

    def student(device_id: str, name: str, extra: str = "") -> None:
        client = ExamClient("127.0.0.1", PORT, "135790", device_id, name)
        try:
            client.connect()
            client.drain_snapshot()
            for problem_id in ("P0001", "P0002"):
                client.submit(problem_id, "cpp", PAIR_CODE)
                client.wait_for_verdict()
            if extra:
                client.submit("P0002", "cpp", extra)
                client.wait_for_verdict()
        except Exception as exc:                                # noqa: BLE001
            print("学生线程出错:", exc)
        time.sleep(25)          # 撑到截图拍完

    # 最后一位同学多交一份编译会失败的：截图里要有"没过"的样子
    for index, (device_id, name) in enumerate(STUDENTS):
        extra = BROKEN_CODE if index == len(STUDENTS) - 1 else ""
        threading.Thread(target=student, args=(device_id, name, extra),
                         daemon=True).start()
    expected = len(STUDENTS) * 2 + 1
    # 等三位都进了场、都判出结果 —— 榜上缺人或全是 0 分，截图就看不出榜单的样子了
    complete = pump(20, lambda: (panel.host_overall_table.rowCount() >= len(STUDENTS)
                                 and panel._server.judged_count >= expected))
    print(f"进场 {len(panel._session.participants)} 人 · "
          f"提交 {len(panel._session.submissions)} 份 · "
          f"总分榜 {panel.host_overall_table.rowCount()} 行 · "
          f"名单 {panel.host_roster_table.rowCount()} 行"
          + ("" if complete else "（等待超时，截图可能不完整）"))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    def shot(name: str) -> None:
        # 先转一会儿事件循环再抓图：填表和重绘都不是同步的，紧接着 grab()
        # 会把"还没画出来的那一行"漏掉 —— 截图上看起来像榜单少了一行。
        app.processEvents()
        time.sleep(0.3)
        app.processEvents()
        if window.grab().save(str(out / name)):
            saved.append(name)

    shot("20_exam_host_board.png")
    # 单独把表抓一张：离屏平台下整窗抓图偶尔会漏掉一行重绘，
    # 单抓控件能确认"数据到底画出来没有"
    if panel.host_overall_table.grab().save(str(out / "27_exam_host_table.png")):
        saved.append("27_exam_host_table.png")
    panel.host_tabs.setCurrentIndex(HOST_TAB_PROBLEM)      # 单题榜
    pump(0.5)
    shot("21_exam_host_problem.png")
    panel.host_tabs.setCurrentIndex(HOST_TAB_CODE)         # 提交与代码
    pump(0.5)
    shot("28_exam_host_code.png")
    # 判定说明那一页：最新提交（列表最上面那份）就是编译失败的那份。
    # 切页用控件本身，不用下标 —— 这一页只有两个页签，不值得再定一组常量。
    panel.host_code_tabs.setCurrentWidget(panel.host_verdict_view)
    pump(0.4)
    shot("29_exam_host_verdict.png")
    panel.host_code_tabs.setCurrentWidget(panel.host_code_view)
    pump(0.2)
    panel.host_tabs.setCurrentIndex(HOST_TAB_ROSTER)       # 名单
    pump(0.5)
    shot("22_exam_host_roster.png")
    panel.host_tabs.setCurrentIndex(HOST_TAB_LOG)          # 现场记录
    pump(0.5)
    shot("23_exam_host_log.png")

    # 学生端：面板自己以学生身份进场
    panel.set_role("student")
    panel.address_edit.setText("127.0.0.1")
    panel.port_edit.setValue(PORT)
    panel.student_code_edit.setText("135790")
    panel.student_name_edit.setText("王五")
    panel.toggle_join()
    pump(6)
    panel.student_problem_list.setCurrentRow(0)
    app.processEvents()
    panel.code_editor.setPlainText(PAIR_CODE)
    shot("24_exam_student_answer.png")

    panel.submit_code()
    pump(5)
    shot("25_exam_student_submitted.png")
    panel.student_tabs.setCurrentIndex(STUDENT_TAB_BOARD)   # 排行榜
    pump(4)
    shot("26_exam_student_board.png")

    panel.leave_room()
    panel.close_room(quiet=True)
    print("截图完成:", out, "→", "、".join(saved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
