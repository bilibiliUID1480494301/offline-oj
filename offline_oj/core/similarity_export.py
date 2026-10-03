"""雷同检测结果的导出（txt / csv / xlsx / docx / pdf）。

**本模块不 import PySide6、也不 import ``net/``** —— 与 ``core/export.py``
同一个套路：只产出纯数据与纯字符串，PDF 那一步交给 ``ui/export_pdf.py``
用 ``QPdfWriter`` 渲染（这里给 :func:`html_report`）。落地函数全部复用
``core/export.py`` 的零依赖写法（手写 OOXML / CSV / 原子写），不引入任何
新依赖。

导出的是**一份报告的固定内容**（摘要 + 完全重复组 + 高度相似对 + 口径说明），
不像成绩单那样要问"导哪几张表"，所以这里没有 section 勾选，只有一个格式选择。

关于措辞（这条是 #11 收口的一部分）
------------------------------------
报告指向具体的人，措辞像判决就会被直接拿去处理人。所以导出件**头部第一句**
永远是"本工具仅提供线索，不构成作弊认定"—— 这与对话框里常驻的
``REVIEW_NOTICE`` 是同一句话的两个落点，任何一处改了另一处也得跟着改。
"""

from __future__ import annotations

import io
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import export as ex
from . import similarity as sim

#: 导出件头部的声明。**与** ``ui/similarity_dialog.REVIEW_NOTICE`` **同一句话**，
#: 改一边另一边也要改。它出现在这里，是因为"导出成文件被人拿去打印/转发"时，
#: 屏幕上那条常驻提示已经看不到了，文件本身必须自证"我只是线索"。
EXPORT_NOTICE = ("本工具仅提供线索，不构成作弊认定；请打开双方源码人工复核后再下结论。")

#: 性质两词，与 ``ui/similarity_dialog`` 保持一致。
KIND_EXACT = "完全重复"
KIND_SIMILAR = "高度相似"

#: txt 里并排 diff 的截断行数（与对话框里的 ``DIFF_ROW_LIMIT`` 同量级）。
DIFF_ROW_LIMIT = 400


def _cell(value: Any) -> str:
    """单元格文本。**与 ``core/export._cell`` 同实现**—— 这里复一份，
    免得 import 一个下划线开头的私有函数。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "是" if value else ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    return str(value)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 数据 → 表
# ---------------------------------------------------------------------------

def summary_lines(report: sim.Report) -> list[str]:
    """报告抬头：一句话说清比了什么、出了什么。"""
    problems = len({person.problem_id for person in report.analysed})
    pairs = sum(1 for p in report.pairs if not p.exact)
    return [
        "雷同检测报告（供人工复核）",
        (f"比对 {len(report.analysed)} 份提交 · {problems} 道题 · "
         f"完全重复 {len(report.exact_groups)} 组 · 高度相似 {pairs} 对"),
        f"导出时间：{_now_text()}",
    ]


def exact_rows(report: sim.Report) -> tuple[list[str], list[list[str]]]:
    """完全重复组表：题目 / 人数 / 成员名单。"""
    header = ["题目", "人数", "成员"]
    rows: list[list[str]] = []
    for group in report.exact_groups:
        members = list(group.members)
        if len(members) < 2:
            continue
        roster = "、".join(m.display for m in members)
        rows.append([group.problem_title, str(len(members)), roster])
    return header, rows


def pair_rows(report: sim.Report) -> tuple[list[str], list[list[str]]]:
    """高度相似对表：相似度 / 性质 / 题目 / 选手 A / 选手 B / 共享指纹 / 备注。

    **完全重复的对不进这张表** —— 它们已经在「完全重复组」里说过了，
    同一件事说两遍会让人以为有两批人。
    """
    header = ["相似度", "性质", "题目", "选手 A", "选手 B", "共享指纹", "备注"]
    rows: list[list[str]] = []
    for group in report.exact_groups:
        members = list(group.members)
        if len(members) < 2:
            continue
        note = ("共 %d 人一字不差" % len(members)) if len(members) > 2 else "两人一字不差"
        second = (members[1].display if len(members) <= 2
                   else f"{members[1].display} 等 {len(members)} 人")
        rows.append(["100%", KIND_EXACT, group.problem_title,
                     members[0].display, second, "—", note])
    for pair in report.pairs:
        if pair.exact:
            continue
        rows.append([f"{pair.percent:.0f}%", KIND_SIMILAR, pair.problem_title,
                     pair.left.display, pair.right.display,
                     str(pair.shared), f"共享 {pair.shared} 个指纹"])
    return header, rows


def notes_lines(report: sim.Report) -> list[str]:
    """口径说明（逐条）。"""
    return list(report.notes)


def _diff_appendix(report: sim.Report) -> list[str]:
    """txt 专属：每对高度相似提交的并排 diff（截断）。

    只在 txt 里给—— xlsx/docx 是表格，塞不下 diff；而老师拿到文件最常做的
    是"顺手打开看一眼"，把证据直接附在后面比"再去点开软件看"实用。
    完全重复的对一字不差，diff 全是相同行，没信息量，跳过。
    """
    blocks: list[str] = []
    for pair in report.pairs:
        if pair.exact:
            continue
        left_code = report.codes.get(pair.left.serial, "")
        right_code = report.codes.get(pair.right.serial, "")
        language = pair.left.language or pair.right.language
        lines = sim.diff_lines(left_code, right_code, language=language, context=1)
        truncated = len(lines) > DIFF_ROW_LIMIT
        blocks.append("")
        blocks.append(f"· {pair.left.display}（#{pair.left.serial}） vs "
                      f"{pair.right.display}（#{pair.right.serial}）"
                      f"· {pair.problem_title} · {pair.percent:.0f}%")
        for line in lines[:DIFF_ROW_LIMIT]:
            tag = {sim.DIFF_EQUAL: "  ", sim.DIFF_CHANGED: "≠ ",
                   sim.DIFF_LEFT_ONLY: "- ", sim.DIFF_RIGHT_ONLY: "+ "}.get(line.tag, "  ")
            blocks.append(f"{tag}{line.left_text}  |  {line.right_text}")
        if truncated:
            blocks.append(f"… 差异过长，仅显示前 {DIFF_ROW_LIMIT} 行")
    return blocks


# ---------------------------------------------------------------------------
# 纯文本 / HTML
# ---------------------------------------------------------------------------

def text_report(report: sim.Report) -> str:
    """纯文本报告：抬头 + 声明 + 两张表 + 口径说明 + diff 附录。"""
    blocks = list(summary_lines(report))
    blocks.append("")
    blocks.append(EXPORT_NOTICE)
    blocks.append("")
    header, rows = exact_rows(report)
    blocks.append("完全重复组")
    blocks.extend(ex.align_table(header, rows) or ["（无）"])
    blocks.append("")
    header, rows = pair_rows(report)
    blocks.append("高度相似对")
    blocks.extend(ex.align_table(header, rows) or ["（无）"])
    blocks.append("")
    blocks.append("口径说明")
    blocks.extend(f"· {note}" for note in notes_lines(report))
    blocks.extend(_diff_appendix(report))
    return "\n".join(blocks) + "\n"


def html_report(report: sim.Report) -> str:
    """HTML 报告 —— PDF 的输入（``ui/export_pdf.render_pdf`` 用 ``QTextDocument`` 渲染）。"""
    from xml.sax.saxutils import escape as _escape
    body = [f"<h1>{_escape(summary_lines(report)[0])}</h1>"]
    body.append(f"<p>{_escape(summary_lines(report)[1])} · {_escape(summary_lines(report)[2])}</p>")
    body.append(f"<p><i>{_escape(EXPORT_NOTICE)}</i></p>")
    for title, header, rows in _sheets(report):
        body.append(f"<h2>{_escape(title)}</h2>")
        if not rows:
            body.append("<p>（无）</p>")
            continue
        body.append('<table border="1" cellspacing="0" cellpadding="4" width="100%">')
        body.append("<tr>" + "".join(f"<th>{_escape(c)}</th>" for c in header) + "</tr>")
        for row in rows:
            body.append("<tr>" + "".join(f"<td>{_escape(_cell(c))}</td>" for c in row) + "</tr>")
        body.append("</table>")
    body.append("<h2>口径说明</h2>")
    body.append("<p>" + "<br>".join(_escape(n) for n in notes_lines(report)) + "</p>")
    return "<html><body>" + "\n".join(body) + "</body></html>"


# ---------------------------------------------------------------------------
# 落地格式
# ---------------------------------------------------------------------------

def _sheets(report: sim.Report) -> list[tuple[str, list[str], list[list[str]]]]:
    """xlsx / docx / html 共用的三张表（汇总 / 完全重复组 / 高度相似对）。"""
    sums = [["比对提交数", str(len(report.analysed))],
            ["题目数", str(len({p.problem_id for p in report.analysed}))],
            ["完全重复组", str(len(report.exact_groups))],
            ["高度相似对", str(sum(1 for p in report.pairs if not p.exact))],
            ["导出时间", _now_text()],
            ["声明", EXPORT_NOTICE]]
    exact = exact_rows(report)
    pairs = pair_rows(report)
    return [("汇总", ["项目", "内容"], sums),
            ("完全重复组", list(exact[0]), exact[1]),
            ("高度相似对", list(pairs[0]), pairs[1])]


def xlsx_sheets(report: sim.Report) -> list[tuple[str, list[str], list[list[str]]]]:
    return _sheets(report)


def docx_blocks(report: sim.Report) -> list[tuple[str, Any]]:
    """Word 内容块。"""
    blocks: list[tuple[str, Any]] = [("h1", summary_lines(report)[0]),
                                      ("p", summary_lines(report)[1]),
                                      ("p", EXPORT_NOTICE)]
    for title, header, rows in _sheets(report):
        blocks.append(("h2", title))
        blocks.append(("table", (header, rows)))
    blocks.append(("h2", "口径说明"))
    blocks.append(("p", "\n".join(notes_lines(report))))
    return blocks


def csv_sections(report: sim.Report) -> dict[str, tuple[list[str], list[list[str]]]]:
    """CSV 每个内容一项一个文件（两张表塞一个文件就没有一列机器可读）。"""
    exact = exact_rows(report)
    pairs = pair_rows(report)
    return {"完全重复组": exact, "高度相似对": pairs}


def format_from_path(path: str | Path) -> str:
    """从文件名后缀推断导出格式（txt / csv / xlsx / docx / pdf）。"""
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in ("txt", "csv", "xlsx", "docx", "pdf"):
        return suffix
    raise ValueError(f"无法从后缀识别导出格式：{suffix!r}（支持 txt/csv/xlsx/docx/pdf）")


def export(path: str | Path, report: sim.Report, fmt: str) -> list[Path]:
    """按格式导出相似度报告，返回写出的文件列表。

    ``pdf`` 不走这里（``core/`` 不能 import PySide6）—— 调用方改用
    ``ui/export_pdf.render_pdf(html_report(report), path)``。其余四种格式
    全部复用 ``core/export.py`` 的零依赖落地。
    """
    target = Path(path)
    if fmt == "pdf":
        raise ValueError("pdf 由界面层用 html_report + render_pdf 渲染，"
                         "core 层不碰 PySide6")
    if fmt == "txt":
        return [ex.write_text(target, text_report(report))]
    if fmt == "csv":
        stem = target.with_suffix("")
        out = []
        for name, (header, rows) in csv_sections(report).items():
            out.append(ex.write_csv(stem.parent / f"{stem.name}-{name}.csv",
                                    header, rows))
        return out
    if fmt == "xlsx":
        return [ex.write_xlsx(target, xlsx_sheets(report) or [("空", ["（无数据）"], [])])]
    if fmt == "docx":
        return [ex.write_docx(target, docx_blocks(report))]
    raise ValueError(
        f"不支持这种导出格式：{fmt}（支持 txt/csv/xlsx/docx；pdf 由界面层渲染）")


__all__ = [
    "DIFF_ROW_LIMIT", "EXPORT_NOTICE", "KIND_EXACT", "KIND_SIMILAR",
    "csv_sections", "docx_blocks", "exact_rows", "export", "format_from_path",
    "html_report", "notes_lines", "pair_rows", "summary_lines",
    "text_report", "xlsx_sheets",
]
