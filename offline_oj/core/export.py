"""成绩单与提交明细的导出（txt / csv / xlsx / docx / pdf）。

**本模块不 import PySide6** —— 这是 ``core/`` 层的硬约定（见 ``core/__init__.py``），
所以它只产出**纯数据与纯文本**。五个格式里四个（txt / csv / xlsx / docx）在
这里落地，PDF 那一步交给 UI 侧：这里先给出 :func:`html_report`，由
``ui/export_pdf.py`` 用 ``QPdfWriter`` + ``QTextDocument`` 渲染。

**它也不 import ``net/``**。场次模式的显示名（"练习模式" / "考试模式"）由界面
填进 :attr:`ExportData.mode_label` —— 反向依赖会把这层从"纯数据"变成"网络层
的附庸"，而这个模块的全部价值就在于它能脱离界面与网络单测。

为什么不引第三方库
------------------
PDF 的中文要嵌字体（几 MB TTF），不嵌就是一片方块；而 Qt 自带 ``QPdfWriter``，
中文用系统字体天然正常，**零新依赖**。同理 xlsx / docx 也不用 ``openpyxl`` /
``python-docx``：后者依赖 ``lxml`` 这个编译扩展，而
``packaging/offline_oj.spec`` 明确把 ``PIL`` 排除了（``reportlab`` 恰好依赖 PIL）。
xlsx 就是一个 zip 加几个 XML，docx 也是 —— 手写一百多行，换掉三个编译依赖，
这笔账很划算。

对外入口
--------
* :func:`export` —— 按格式导出成绩单 / 提交明细，返回写出的文件列表；
* :func:`export_bundle` —— 「完整档案包」（xlsx + txt + 每人一个源码文件）；
* :func:`html_report` —— 给界面侧渲染 PDF 用。

数据来源可以是**测验档案**（:meth:`ExportData.from_archive`）也可以是
**进行中的这一场**（:meth:`ExportData.from_session_payload`）。两条路都只喂
普通 dict —— 与 ``core/records.py`` 同一个套路。
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from xml.sax.saxutils import escape as _escape

#: 导出内容。可以单独导，也可以一起导。
SECTION_SCORES = "scores"
SECTION_SUBMISSIONS = "submissions"
ALL_SECTIONS = (SECTION_SCORES, SECTION_SUBMISSIONS)

#: 核心层能直接落地的格式。``pdf`` 由界面侧渲染，见模块开头。
FORMATS = ("txt", "csv", "xlsx", "docx", "pdf")

SECTION_TITLES = {
    SECTION_SCORES: "成绩单",
    SECTION_SUBMISSIONS: "提交明细",
}

#: 语言 → 源码文件后缀（「完整档案包」里按人落源码时用）。
LANGUAGE_SUFFIX = {"cpp": ".cpp", "c": ".c", "python": ".py", "java": ".java"}

#: csv / txt 一律带 BOM：不带的话 Excel 双击打开中文全是乱码，而这个项目的
#: 头号使用场景就是"老师拿到文件直接双击看"。
_ENCODING = "utf-8-sig"

_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):?(\d{2})?")
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------


@dataclass
class ExportData:
    """导出用的一份数据。字段全部有默认值 —— 缺什么都能导，缺的列留空。

    ``leaderboard`` 是 ``ExamSession.archive_leaderboard()`` 那个形状
    （``{"max_total_score": …, "overall": [...], "per_problem": {...}}``）。
    **档案里缺榜单快照时不能因此罢工**：成绩单会退化成"按提交重算"，
    并在页首写明这一点。
    """

    title: str = ""
    mode: str = ""
    #: 模式的**显示名**。由界面填（``RoomMode.label``）—— 本层不 import net。
    mode_label: str = ""
    started_at: str = ""
    ended_at: str = ""
    problems: list[dict[str, Any]] = field(default_factory=list)
    participants: list[dict[str, Any]] = field(default_factory=list)
    submissions: list[dict[str, Any]] = field(default_factory=list)
    leaderboard: dict[str, Any] = field(default_factory=dict)
    max_total_score: int = 0
    keep_code: bool = False

    @classmethod
    def from_archive(cls, archive: Any) -> "ExportData":
        """从 :class:`offline_oj.core.records.ExamArchive` 建。

        只读属性、不调方法 —— 传一个形状对得上的替身进来也能用。
        """
        record = getattr(archive, "record", None)
        if record is None:
            return cls()
        return cls.from_mapping({
            "title": record.title,
            "mode": record.mode,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "problems": record.problems,
            "participants": record.participants,
            "submissions": archive.submissions,
            "leaderboard": archive.leaderboard,
            "max_total_score": record.max_total_score,
            "keep_code": bool(record.keep_code),
        })

    @classmethod
    def from_session_payload(cls, record: dict[str, Any],
                             submissions: Sequence[dict[str, Any]],
                             leaderboard: dict[str, Any] | None = None) -> "ExportData":
        """从 ``ExamSession.archive_record()`` / ``archive_submissions()`` 一路建。

        三个参数就是主机端"进行中的这一场"现成的三个 dict 产出 ——
        面板不需要为导出另准备一份数据。
        """
        return cls.from_mapping({
            "title": record.get("title", ""),
            "mode": record.get("mode", ""),
            "started_at": record.get("started_at", ""),
            "ended_at": record.get("ended_at", ""),
            "problems": record.get("problems") or [],
            "participants": record.get("participants") or [],
            "submissions": list(submissions),
            "leaderboard": leaderboard or {},
            "max_total_score": record.get("max_total_score", 0) or 0,
            "keep_code": bool(record.get("keep_code", True)),
        })

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "ExportData":
        """容错构造：未知字段忽略、缺字段取默认、类型不对就当空。"""
        def _list(key: str) -> list[dict[str, Any]]:
            raw = payload.get(key)
            if not isinstance(raw, list):
                return []
            return [item for item in raw if isinstance(item, dict)]

        def _text(key: str) -> str:
            value = payload.get(key)
            return "" if value is None else str(value)

        def _int(key: str) -> int:
            try:
                return int(payload.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        board = payload.get("leaderboard")
        return cls(
            title=_text("title"),
            mode=_text("mode"),
            mode_label=_text("mode_label"),
            started_at=_text("started_at"),
            ended_at=_text("ended_at"),
            problems=_list("problems"),
            participants=_list("participants"),
            submissions=_list("submissions"),
            leaderboard=board if isinstance(board, dict) else {},
            max_total_score=_int("max_total_score"),
            keep_code=bool(payload.get("keep_code", False)),
        )

    # -- 派生信息 ---------------------------------------------------------

    @property
    def has_leaderboard(self) -> bool:
        return bool(self.leaderboard.get("overall"))

    def problem_ids(self) -> list[str]:
        """题目顺序：优先档案里的题目快照，它才代表"当时学生看到的顺序"。"""
        ids = [str(item.get("id", "")) for item in self.problems if item.get("id")]
        if ids:
            return ids
        seen: list[str] = []
        for item in self.submissions:
            pid = str(item.get("problem_id", ""))
            if pid and pid not in seen:
                seen.append(pid)
        return seen

    def points_of(self, problem_id: str) -> int:
        """本题满分。题目快照里没有就退回"该题提交里见过的最大 possible"。"""
        for item in self.problems:
            if str(item.get("id", "")) == problem_id:
                try:
                    return int(item.get("points", 0) or 0)
                except (TypeError, ValueError):
                    return 0
        best = 0
        for item in self.submissions:
            if str(item.get("problem_id", "")) != problem_id:
                continue
            try:
                best = max(best, int(item.get("possible", 0) or 0))
            except (TypeError, ValueError):
                continue
        return best

    def full_marks(self) -> int:
        """整场满分：优先榜单快照里记的，其次各题满分之和。"""
        if self.max_total_score:
            return self.max_total_score
        return sum(self.points_of(pid) for pid in self.problem_ids())

    def problem_titles(self) -> dict[str, str]:
        titles = {str(item.get("id", "")): str(item.get("title", ""))
                  for item in self.problems if item.get("id")}
        for pid in self.problem_ids():
            titles.setdefault(pid, "")
        return titles


# ---------------------------------------------------------------------------
# 名次
# ---------------------------------------------------------------------------


def competition_ranks(scores: Sequence[int]) -> list[int]:
    """名次序列：**同分并列，下一名跳号**（NOI 式）。

    ``[300, 300, 200, 100, 100]`` → ``[1, 1, 3, 4, 4]``。

    这里的实现与 ``net.session.competition_ranks`` **刻意是两份** ——
    ``core/`` 不该为了抄一个六行函数去 import 网络层。两个真源之间的代价用测试
    钉住：``test_export.py`` 里拿一批分数交叉比对，任何一边改了规则都会红。
    """
    ranks: list[int] = []
    for index, score in enumerate(scores):
        if index and score == scores[index - 1]:
            ranks.append(ranks[-1])
        else:
            ranks.append(index + 1)
    return ranks


def _scores_from_submissions(data: ExportData) -> list[dict[str, Any]]:
    """没有榜单快照时，按"每题取最高分"重算一张成绩单。

    规则与 ``ExamSession.overall_ranking()`` 一致：一个人一题只算最高的那次提交。
    拿不到榜单时宁可重算并写明，也好过导出一张空表。
    """
    ids = data.problem_ids()
    people: dict[str, dict[str, Any]] = {}
    for item in data.submissions:
        key = str(item.get("device_id", "")) or str(item.get("username", ""))
        person = people.setdefault(key, {
            "device_id": str(item.get("device_id", "")),
            "username": str(item.get("username", "")),
            "per_problem": {}, "submit_count": 0,
        })
        person["submit_count"] += 1
        pid = str(item.get("problem_id", ""))
        if not pid:
            continue
        try:
            score = int(item.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        # 「最高分」而不是「最后一次」：这是榜单规则，不是随便挑一个
        if score > person["per_problem"].get(pid, 0):
            person["per_problem"][pid] = score

    rows = list(people.values())
    for person in rows:
        per = person["per_problem"]
        person["score"] = sum(per.get(pid, 0) for pid in ids)
        person["solved"] = sum(1 for pid in ids
                               if per.get(pid, 0) > 0 and per.get(pid, 0) >= data.points_of(pid))
    # 排序：总分降序 → 解题数降序 → 姓名。**名次不用耗时破平**（NOI 式同分并列）
    rows.sort(key=lambda item: (-item["score"], -item["solved"], item["username"]))
    for person, rank in zip(rows, competition_ranks([item["score"] for item in rows])):
        person["rank"] = rank
    return rows


def overall_rows(data: ExportData) -> list[dict[str, Any]]:
    """总分榜的行。没有快照就用重算的那一份。"""
    raw = data.leaderboard.get("overall")
    if isinstance(raw, list):
        rows = [item for item in raw if isinstance(item, dict)]
        if rows:
            return rows
    return _scores_from_submissions(data)


# ---------------------------------------------------------------------------
# 单元格格式化
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """把一个值写成单元格文本。``None`` → 空串，浮点去掉多余的 ``.0``。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "是" if value else ""
    if isinstance(value, float):
        # 12.0 写成 "12"：导出是给人看的，不是给程序读的
        return str(int(value)) if value.is_integer() else f"{value:g}"
    return str(value)


def _serial(item: dict[str, Any]) -> int:
    try:
        return int(item.get("serial", 0) or 0)
    except (TypeError, ValueError):
        return 0


def stamp_text(value: Any) -> str:
    """ISO 串 → ``2026-09-19 17:30:45``。看不懂的原样留着，别丢信息。"""
    text = _cell(value)
    if not text:
        return ""
    match = _TIME_RE.match(text)
    if not match:
        return text
    clock = f"{match.group(4)}:{match.group(5)}"
    if match.group(6):
        clock += f":{match.group(6)}"
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)} {clock}"


def _language_label(value: Any) -> str:
    text = _cell(value)
    return {"cpp": "C++", "c": "C", "python": "Python", "java": "Java"}.get(text, text)


def _attempt_label(value: Any) -> str:
    text = _cell(value)
    return f"第 {text} 次" if text else ""


# ---------------------------------------------------------------------------
# 两张表
# ---------------------------------------------------------------------------


def score_table(data: ExportData) -> tuple[list[str], list[list[str]]]:
    """成绩单：一行一人，列是各题得分 + 总分 + 排名。"""
    ids = data.problem_ids()
    titles = data.problem_titles()
    full = data.full_marks()

    header = ["排名", "姓名", "设备"]
    for pid in ids:
        points = data.points_of(pid)
        label = titles.get(pid) or pid
        header.append(f"{label}（{points}）" if points else label)
    header += ["总分", "满分", "解题数", "总耗时(ms)", "总内存(MB)", "提交次数"]

    rows: list[list[str]] = []
    for item in overall_rows(data):
        per = item.get("per_problem")
        per = per if isinstance(per, dict) else {}
        row = [_cell(item.get("rank")),
               _cell(item.get("username")),
               _cell(item.get("device_id"))]
        row += [_cell(per.get(pid, 0)) for pid in ids]
        row += [_cell(item.get("score")),
                _cell(full),
                _cell(item.get("solved", "")),
                _cell(item.get("total_time_ms", "")),
                _cell(item.get("total_memory_mb", "")),
                _cell(item.get("submit_count", ""))]
        rows.append(row)
    return header, rows


def submission_table(data: ExportData) -> tuple[list[str], list[list[str]]]:
    """提交明细：一行一次提交。"""
    titles = data.problem_titles()
    header = ["编号", "时间", "姓名", "设备", "题目", "题名", "语言", "第几次",
              "结论", "通过/总数", "得分", "满分", "耗时(ms)", "内存(MB)",
              "O2", "来源"]
    rows: list[list[str]] = []
    for item in sorted(data.submissions, key=_serial):
        pid = _cell(item.get("problem_id"))
        passed = _cell(item.get("passed", ""))
        total = _cell(item.get("total", ""))
        rows.append([
            _cell(item.get("serial")),
            stamp_text(item.get("submitted_at")),
            _cell(item.get("username")),
            _cell(item.get("device_id")),
            pid,
            titles.get(pid, ""),
            _language_label(item.get("language")),
            _attempt_label(item.get("attempt")),
            _cell(item.get("verdict")),
            f"{passed}/{total}" if passed else "",
            _cell(item.get("score")),
            _cell(item.get("possible", "")),
            _cell(item.get("time_ms", "")),
            _cell(item.get("memory_mb", "")),
            "是" if item.get("optimized") else "",
            "自动收卷" if item.get("forced") else "",
        ])
    return header, rows


def tables(data: ExportData,
           sections: Iterable[str]) -> list[tuple[str, list[str], list[list[str]]]]:
    """按 ``ALL_SECTIONS`` 的顺序产出 ``[(标题, 表头, 行), …]``。

    顺序固定而不是跟着调用方传进来的顺序走：`scollections` 传成集合时顺序是随机的，
    导出的两张表会莫名其妙地换位置。
    """
    wanted = set(sections)
    out: list[tuple[str, list[str], list[list[str]]]] = []
    for name in ALL_SECTIONS:
        if name not in wanted:
            continue
        header, rows = (score_table(data) if name == SECTION_SCORES
                        else submission_table(data))
        out.append((SECTION_TITLES[name], header, rows))
    return out


# ---------------------------------------------------------------------------
# 显示宽度（中文是双宽，len() 数不对）
# ---------------------------------------------------------------------------


def display_width(text: str) -> int:
    return sum(2 if _is_wide(char) else 1 for char in text)


def _is_wide(char: str) -> bool:
    code = ord(char)
    return (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6 or 0x20000 <= code <= 0x3FFFD)


def _pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# ---------------------------------------------------------------------------
# 纯文本 / HTML
# ---------------------------------------------------------------------------


def header_lines(data: ExportData) -> list[str]:
    """报告的抬头：场次名 + 一行元信息。"""
    lines = [data.title or "未命名场次"]
    parts = []
    if data.started_at:
        parts.append(stamp_text(data.started_at) + " 开始")
    if data.ended_at:
        parts.append(stamp_text(data.ended_at) + " 结束")
    if data.mode_label or data.mode:
        parts.append(data.mode_label or data.mode)
    parts.append(f"{len(data.participants)} 人 / {len(data.submissions)} 份提交")
    lines.append(" · ".join(parts))
    return lines


#: 缺榜单快照时的说明。**两处（文本/HTML）共用同一句话**，别各写一份。
NO_BOARD_NOTICE = "（档案里没有榜单快照，成绩单按「每题取最高分」重算）"


def align_table(header: Sequence[str],
                rows: Sequence[Sequence[str]]) -> list[str]:
    """把一张表按**显示宽度**补齐成等宽文本。

    不能直接用 ``str.ljust``：中文占两个字符位，``len()`` 数成一个，
    整张表会从第一个中文字段之后开始歪。
    """
    widths = [display_width(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(cell))

    def _line(values: Sequence[str]) -> str:
        return "  ".join(_pad_to(cell, widths[index])
                         for index, cell in enumerate(values)).rstrip()

    lines = [_line(header), "  ".join("-" * width for width in widths)]
    lines += [_line(row) for row in rows]
    return lines


def text_report(data: ExportData, sections: Iterable[str]) -> str:
    """纯文本报告：抬头 + 各表。"""
    wanted = set(sections)
    blocks = header_lines(data)
    if not data.has_leaderboard and SECTION_SCORES in wanted:
        blocks.append(NO_BOARD_NOTICE)
    for title, header, rows in tables(data, sections):
        blocks.append("")
        blocks.append(title)
        blocks.append("-" * max(16, display_width(title)))
        blocks.extend(align_table(header, rows))
    return "\n".join(blocks) + "\n"


def html_report(data: ExportData, sections: Iterable[str]) -> str:
    """HTML 报告 —— PDF 的输入。

    只用 ``QTextDocument`` 支持得住的富文本子集：h1/h2/p/table。
    不要写 CSS 的 flex/grid，Qt 的富文本引擎不认。
    """
    wanted = set(sections)
    head = header_lines(data)
    body = [f"<h1>{_html(head[0])}</h1>", f"<p>{_html(head[1])}</p>"]
    if not data.has_leaderboard and SECTION_SCORES in wanted:
        body.append(f"<p><i>{_html(NO_BOARD_NOTICE)}</i></p>")
    for title, header, rows in tables(data, sections):
        body.append(f"<h2>{_html(title)}</h2>")
        body.append('<table border="1" cellspacing="0" cellpadding="4" width="100%">')
        body.append("<tr>" + "".join(f"<th>{_html(cell)}</th>" for cell in header) + "</tr>")
        for row in rows:
            body.append("<tr>" + "".join(f"<td>{_html(cell)}</td>" for cell in row) + "</tr>")
        body.append("</table>")
    body.append(f"<p><i>导出时间：{_escape(now_text())}</i></p>")
    return "<html><body>" + "\n".join(body) + "</body></html>"


def _html(text: str) -> str:
    return _escape(text).replace("\n", "<br>")


def now_text() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 落地：txt / csv / xlsx / docx
# ---------------------------------------------------------------------------


def safe_name(text: str, fallback: str = "导出") -> str:
    """去掉文件名里的保留字符（不截断 —— 那是调用方的事）。"""
    cleaned = _UNSAFE_RE.sub("-", (text or "").strip())
    return cleaned.strip("-.") or fallback


#: 写到一半的文件后缀。见 :func:`atomic_write`。
PARTIAL_SUFFIX = ".partial"


def atomic_write(target: str | Path,
                 produce: Callable[[Path], None]) -> Path:
    """先写 ``<名字>.partial``，写完整了再改名到目标。

    **直接往目标文件上写是危险的**：``zipfile`` / ``QPdfWriter`` 都会先建出文件
    再往里填内容，中途崩了（或者上面那个"导出"按钮被连点两下）就在磁盘上留下
    一个**看着像成功**的半成品 —— zip 与 xlsx 的中央目录在文件末尾，截断之后
    Excel 只会说"文件已损坏"，而老师那边看到的是"导出完成了、文件也在"。

    ``os.replace`` 在同一个卷上是原子的，于是目标文件只有两种状态：
    **不存在**，或者**是完整的**。这也是 ``core/records.py`` 写档案时用的同一招。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    try:
        produce(partial)
        os.replace(partial, target)
    except BaseException:
        # 失败就把半成品收掉，别让它冒充产物；连 KeyboardInterrupt 也一并处理，
        # 否则 Ctrl+C 之后磁盘上会多一个 .partial
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def write_text(path: str | Path, text: str) -> Path:
    return atomic_write(
        path, lambda target: target.write_text(text, encoding=_ENCODING, newline=""))


def write_csv(path: str | Path, header: Sequence[str],
              rows: Sequence[Sequence[str]]) -> Path:
    """带 BOM 的 CSV —— 不带的话 Excel 双击打开中文全是乱码。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    payload = buffer.getvalue()
    return atomic_write(
        path, lambda target: target.write_text(payload, encoding=_ENCODING, newline=""))


def _xml_text(value: Any) -> str:
    """XML 文本：转义 + 掐掉 XML 不认的控制字符。

    ``message`` 里是编译器的原始输出，带 ``\\x0b`` 之类的控制字符是常事，
    而它们会让整份 xlsx 被 Excel 判成"文件损坏"。
    """
    text = "".join(char for char in _cell(value)
                   if char in "\t\n\r" or ord(char) >= 0x20)
    return _escape(text)


def _column_name(index: int) -> str:
    """0 → A、25 → Z、26 → AA。"""
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(ord("A") + rest) + name
    return name


def _row_xml(number: int, cells: Sequence[Any], *, bold: bool = False) -> str:
    style = ' s="1"' if bold else ""
    out = [f'<row r="{number}">']
    for index, value in enumerate(cells):
        ref = f"{_column_name(index)}{number}"
        text = _cell(value)
        if text and _NUMBER_RE.match(text):
            out.append(f'<c r="{ref}"{style}><v>{text}</v></c>')
        elif text:
            out.append(f'<c r="{ref}"{style} t="inlineStr">'
                       f'<is><t xml:space="preserve">{_xml_text(text)}</t></is></c>')
        else:
            out.append(f'<c r="{ref}"{style}/>')
    out.append("</row>")
    return "".join(out)


def _sheet_xml(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """一张工作表。字符串走 ``inlineStr``，省掉 sharedStrings 那一套。

    OOXML 对元素的**顺序**有要求（``sheetViews`` → ``cols`` → ``sheetData``），
    顺序错了 Excel 会说文件损坏。
    """
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main">']
    # 冻结首行：几百行明细滚下去还知道哪列是什么
    parts.append('<sheetViews><sheetView workbookViewId="0">'
                 '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                 'state="frozen"/></sheetView></sheetViews>')
    widths = [display_width(_cell(cell)) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(_cell(cell)))
    parts.append("<cols>" + "".join(
        f'<col min="{index + 1}" max="{index + 1}" '
        f'width="{min(48, max(8, width + 4))}" customWidth="1"/>'
        for index, width in enumerate(widths)) + "</cols>")
    parts.append("<sheetData>")
    parts.append(_row_xml(1, header, bold=True))
    for offset, row in enumerate(rows):
        parts.append(_row_xml(offset + 2, row))
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


_STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs>'
    "</styleSheet>"
)


def _sheet_name(name: str, index: int) -> str:
    """工作表名：``[]:*?/\\`` 是 OOXML 禁止的，而且**报错发生在 Excel 打开时**
    （说文件损坏），排查起来毫无线索 —— 所以在这里就换掉。上限 31 字符。"""
    cleaned = re.sub(r"[\[\]:*?/\\]+", "-", (name or "").strip())
    cleaned = cleaned.strip("'")[:31]
    return cleaned or f"表{index + 1}"


def _content_types(count: int) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, count + 1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
            '.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>")


_ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
              'relationships"><Relationship Id="rId1" Type="http://schemas.'
              'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
              'Target="xl/workbook.xml"/></Relationships>')


def _workbook_xml(names: Sequence[str]) -> str:
    sheets = "".join(
        f'<sheet name="{_xml_text(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(names, start=1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheets}</sheets></workbook>")


def _workbook_rels(count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, count + 1))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships">{rels}<Relationship Id="rIdStyles" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/></Relationships>')


def _write_xlsx_into(bundle: zipfile.ZipFile,
                     sheets: Sequence[tuple[str, Sequence[str], Sequence[Sequence[str]]]]) -> None:
    """把 xlsx 的各个部件写进一个已经打开的 zip。"""
    names = [_sheet_name(item[0], index) for index, item in enumerate(sheets)]
    bundle.writestr("[Content_Types].xml", _content_types(len(sheets)))
    bundle.writestr("_rels/.rels", _ROOT_RELS)
    bundle.writestr("xl/workbook.xml", _workbook_xml(names))
    bundle.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
    bundle.writestr("xl/styles.xml", _STYLES_XML)
    for index, (_, header, rows) in enumerate(sheets, start=1):
        bundle.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(header, rows))


def write_xlsx(path: str | Path,
               sheets: Sequence[tuple[str, Sequence[str], Sequence[Sequence[str]]]]) -> Path:
    """手写 OOXML。``sheets`` 是 ``[(表名, 表头, 行), …]``。"""
    def produce(target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            _write_xlsx_into(bundle, sheets)

    return atomic_write(path, produce)


_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
    '.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType='
    '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>")

_DOCX_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
              'relationships"><Relationship Id="rId1" Type="http://schemas.'
              'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
              'Target="word/document.xml"/></Relationships>')

# 表格边框必须**内联写**：不引 styles.xml 就没有 TableGrid 样式可用，
# 而 Word 对"找不到的样式"是静默不画线 —— 表看起来就是一片没格子的文字。
_TABLE_BORDERS = (
    "<w:tblBorders>"
    + "".join(f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
              for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
    + "</w:tblBorders>")


def _docx_paragraph(text: str, *, half_points: int | None = None,
                    bold: bool = False) -> str:
    """一个段落。字号用**半磅**（``w:sz`` 的单位）：26 → 13pt。

    加粗必须写在**run 的 ``w:rPr``** 里。写在段落属性里 Word 是不认的 ——
    排版看着"没生效"，但 XML 完全合法，不报任何错。
    """
    run_props = []
    if bold:
        run_props.append("<w:b/>")
    if half_points:
        run_props.append(f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/>')
    props = f"<w:rPr>{''.join(run_props)}</w:rPr>" if run_props else ""
    return (f'<w:p><w:r>{props}'
            f'<w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r></w:p>')


def _docx_table(header: Sequence[str], rows: Sequence[Sequence[str]],
                width: int = 9000) -> str:
    columns = max(1, len(header))
    each = max(600, width // columns)
    grid = "".join(f'<w:gridCol w:w="{each}"/>' for _ in range(columns))

    def _cells(values: Sequence[Any], bold: bool) -> str:
        out = []
        for index in range(columns):
            value = values[index] if index < len(values) else ""
            out.append(f'<w:tc><w:tcPr><w:tcW w:w="{each}" w:type="dxa"/></w:tcPr>'
                       f"{_docx_paragraph(_cell(value), half_points=18, bold=bold)}</w:tc>")
        return "<w:tr>" + "".join(out) + "</w:tr>"

    body = [_cells(header, True)] + [_cells(row, False) for row in rows]
    return ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
            f"{_TABLE_BORDERS}</w:tblPr><w:tblGrid>{grid}</w:tblGrid>"
            + "".join(body) + "</w:tbl>")


def _docx_document(blocks: Sequence[tuple[str, Any]]) -> str:
    """``blocks`` 是 ``[("h1"|"h2"|"p", 文本) | ("table", (表头, 行)), …]``。

    ``h1`` / ``h2`` 不引用 Word 的 Heading 样式 —— 一份最小 docx 没有
    ``styles.xml``，引用了也找不到；直接给定字号加粗更可靠。
    """
    body: list[str] = []
    for kind, payload in blocks:
        if kind == "table":
            header, rows = payload
            body.append(_docx_table(header, rows))
        elif kind == "h1":
            body.append(_docx_paragraph(str(payload), half_points=32, bold=True))
        elif kind == "h2":
            body.append(_docx_paragraph(str(payload), half_points=26, bold=True))
        else:
            body.append(_docx_paragraph(str(payload)))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            + "".join(body)
            + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
            "</w:sectPr></w:body></w:document>")


def write_docx(path: str | Path, blocks: Sequence[tuple[str, Any]]) -> Path:
    """手写 OOXML。"""
    def produce(target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
            bundle.writestr("_rels/.rels", _DOCX_RELS)
            bundle.writestr("word/document.xml", _docx_document(blocks))

    return atomic_write(path, produce)


def docx_blocks(data: ExportData, sections: Iterable[str]) -> list[tuple[str, Any]]:
    """Word 的内容：标题、场次信息、各表。与 txt / HTML 共用同一批表数据。"""
    head = header_lines(data)
    blocks: list[tuple[str, Any]] = [("h1", head[0]), ("p", head[1])]
    if not data.has_leaderboard and SECTION_SCORES in set(sections):
        blocks.append(("p", NO_BOARD_NOTICE))
    for title, header, rows in tables(data, sections):
        blocks.append(("h2", title))
        blocks.append(("table", (header, rows)))
    blocks.append(("p", f"导出时间：{now_text()}"))
    return blocks


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def default_stem(data: ExportData) -> str:
    """默认文件名（不含后缀）。时间戳取 ``YYYYMMDDHHMMSS``。"""
    stamp = re.sub(r"[-:T ]", "", data.started_at)[:14]
    label = safe_name(data.title, "未命名场次")
    return f"{stamp}-{label}" if stamp else label


def _source_name(item: dict[str, Any], fallback_serial: int) -> str:
    """源码文件名。**带上提交编号**：同一个人同一题交过多次，只按"人-题"命名
    会互相覆盖，最后只剩一份 —— 而"第几次"恰恰是讲评时要看的。"""
    language = _cell(item.get("language"))
    who = safe_name(_cell(item.get("username")) or _cell(item.get("device_id")), "匿名")
    pid = safe_name(_cell(item.get("problem_id")), "T")
    serial = _cell(item.get("serial")) or str(fallback_serial)
    return f"{serial}-{who}-{pid}{LANGUAGE_SUFFIX.get(language, '.txt')}"


def _codes(data: ExportData) -> list[tuple[str, str]]:
    """``[(文件名, 源码), …]``，按提交编号排序。"""
    out: list[tuple[str, str]] = []
    for index, item in enumerate(sorted(data.submissions, key=_serial), start=1):
        code = item.get("code")
        if isinstance(code, str) and code:
            out.append((_source_name(item, index), code))
    return out


def export(path: str | Path, data: ExportData, fmt: str,
           sections: Iterable[str] = ALL_SECTIONS) -> list[Path]:
    """按格式导出，返回**写出的文件列表**。

    ``path`` 的语义按格式分两种：

    * **单文件格式**（txt / xlsx / docx）—— ``path`` 就是目标文件；
    * **csv** —— 每个 section 一个文件，``path`` 当**主干名**用，两个文件分别叫
      ``<主干>-成绩单.csv`` / ``<主干>-提交明细.csv``。把两张表塞进一个 csv 是
      做得到的，但那样没有任何一列是机器可用的。
    """
    wanted = [name for name in ALL_SECTIONS if name in set(sections)] or list(ALL_SECTIONS)
    target = Path(path)
    if fmt == "csv":
        stem = target.with_suffix("") if target.suffix.lower() == ".csv" else target
        return [write_csv(stem.parent / f"{stem.name}-{title}.csv", header, rows)
                for title, header, rows in tables(data, wanted)]
    if fmt == "txt":
        return [write_text(target, text_report(data, wanted))]
    if fmt == "xlsx":
        sheets = list(tables(data, wanted)) or []
        return [write_xlsx(target, sheets or [("空", ["（无数据）"], [])])]
    if fmt == "docx":
        return [write_docx(target, docx_blocks(data, wanted))]
    raise ValueError(
        f"核心层不支持这种导出格式：{fmt}（支持 txt / csv / xlsx / docx；"
        "pdf 由界面侧用 html_report 渲染）")


def export_sources(directory: str | Path, data: ExportData) -> int:
    """「每人一个源码文件」那一半，落到 ``directory``，返回写出的文件数。"""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    codes = _codes(data)
    for name, code in codes:
        (root / name).write_text(code, encoding="utf-8", newline="")
    return len(codes)


def no_code_notice() -> str:
    return ("这份导出里没有源码。\n"
            "可能的原因：导出时选择了「只存成绩不存代码」，"
            "或者档案本身没有保存源码。\n")


def export_bundle(path: str | Path, data: ExportData, *,
                  sections: Iterable[str] = ALL_SECTIONS) -> Path:
    """完整档案包：一份 xlsx + 一份 txt + ``sources/`` 下的每人源码。

    **源码全缺时不静默**：包里多一个 ``说明.txt``。老师打开压缩包就知道
    为什么没有代码，而不是以为导出坏了。
    """
    wanted = [name for name in ALL_SECTIONS if name in set(sections)] or list(ALL_SECTIONS)
    stem = default_stem(data)
    sheets = list(tables(data, wanted)) or [("空", ["（无数据）"], [])]
    codes = _codes(data)
    payload = text_report(data, wanted)

    def produce(target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as inner:
                _write_xlsx_into(inner, sheets)
            bundle.writestr(f"{stem}.xlsx", buffer.getvalue())
            bundle.writestr(f"{stem}.txt", payload)
            for name, code in codes:
                bundle.writestr(f"sources/{name}", code)
            if not codes:
                bundle.writestr("说明.txt", no_code_notice())

    return atomic_write(path, produce)


__all__ = [
    "ALL_SECTIONS", "FORMATS", "LANGUAGE_SUFFIX", "NO_BOARD_NOTICE",
    "SECTION_SCORES", "SECTION_SUBMISSIONS", "SECTION_TITLES", "ExportData",
    "PARTIAL_SUFFIX", "align_table", "atomic_write", "competition_ranks",
    "default_stem", "display_width",
    "docx_blocks", "export", "export_bundle", "export_sources", "header_lines",
    "html_report", "no_code_notice", "now_text", "overall_rows", "safe_name",
    "score_table", "stamp_text", "submission_table", "tables", "text_report",
    "write_csv", "write_docx", "write_text", "write_xlsx",
]
