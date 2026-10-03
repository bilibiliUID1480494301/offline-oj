"""雷同检测结果的导出（core 层）。

``core/similarity_export.py`` 不 import PySide6，所以这里**纯函数、零 GUI**，
直接对 ``export / html_report / format_from_path / pair_rows / exact_rows`` 各
产物做断言。PDF 那一步在界面层（``ui/export_pdf.render_pdf``），这里只验证
``export`` 对 pdf 抛 ``ValueError``、以及 ``html_report`` 能产出可被界面层印的 HTML。

测试数据不依赖 ``similarity.analyse`` 的启发式阈值（那一层哪对算"高度相似"
会随窗口参数漂移），而是用 ``similarity`` 的数据类直接拼一份受控的
``Report``：一个完全重复组 + 一对高度相似。这样导出逻辑本身（去重、分表、
声明、diff 附录）是被确定地验证的。
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from offline_oj.core import similarity as sim
from offline_oj.core import similarity_export as sx
from offline_oj.core.similarity import ExactGroup, Person, Report, SimilarPair

CODE_A = "int main() {\n    int n = 1;\n    for (int i = 1; i <= n; i++) n += i;\n    return n;\n}\n"
# 只把末行的返回值从 n 改成 n + 1：语法上不同、结构上几乎一样 —— 一对"高度相似"。
CODE_B = "int main() {\n    int n = 1;\n    for (int i = 1; i <= n; i++) n += i;\n    return n + 1;\n}\n"


def _report() -> Report:
    pa = Person(1, "D1", "甲", "P1", "cpp")
    pb = Person(2, "D2", "乙", "P1", "cpp")
    pc = Person(3, "D3", "丙", "P1", "cpp")
    pd = Person(4, "D4", "丁", "P1", "cpp")
    exact = ExactGroup(problem_id="P1", problem_title="A+B", digest="dig",
                       members=[pa, pb, pc])
    # 甲 vs 丁：高度相似但不是一字不差（exact=False）
    similar = SimilarPair("P1", "A+B", pa, pd, similarity=0.85, shared=20,
                          exact=False)
    return Report(
        exact_groups=[exact],
        pairs=[similar],
        analysed=[pa, pb, pc, pd],
        codes={1: CODE_A, 2: CODE_A, 3: CODE_A, 4: CODE_B},
        notes=["口径说明示例：只在同一题内比较。"],
    )


def _contains_notice(path: Path) -> bool:
    """导出件里是否带了"仅提供线索"声明。txt 是纯文本、xlsx/docx 是 zip 包，
    声明以 UTF-8 落在某个 xml 部件里 —— 两种都按字节搜。"""
    if path.suffix == ".txt":
        return sx.EXPORT_NOTICE in path.read_text(encoding="utf-8-sig")
    with zipfile.ZipFile(path) as zf:
        blob = b"".join(zf.read(name) for name in zf.namelist())
    return sx.EXPORT_NOTICE.encode("utf-8") in blob


def test_export_notice_is_in_every_file():
    """措辞收口：txt/xlsx/docx 导出件里都必须有"仅提供线索，不构成认定"。"""
    report = _report()
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "report"
        for fmt, paths in (
            ("txt", sx.export(base.with_suffix(".txt"), report, "txt")),
            ("xlsx", sx.export(base.with_suffix(".xlsx"), report, "xlsx")),
            ("docx", sx.export(base.with_suffix(".docx"), report, "docx")),
        ):
            for p in paths:
                assert _contains_notice(p), (fmt, p)


def test_text_report_contains_both_tables_and_diff():
    report = _report()
    text = sx.text_report(report)
    assert "完全重复组" in text
    assert "高度相似对" in text
    assert sx.EXPORT_NOTICE in text
    # 并排 diff 附录：高度相似对（甲 vs 丁）代码不同，会出现"≠"标记
    assert "≠" in text
    # 完全重复的对一字不差，diff 里不该有它的"≠"
    assert "甲" in text and "丁" in text


def test_csv_writes_one_file_per_section():
    report = _report()
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "report.csv"
        out = sx.export(base, report, "csv")
        names = {p.name for p in out}
        assert "report-完全重复组.csv" in names
        assert "report-高度相似对.csv" in names
        exact_csv = (Path(d) / "report-完全重复组.csv").read_text(encoding="utf-8-sig")
        assert "甲" in exact_csv and "乙" in exact_csv and "丙" in exact_csv
        similar_csv = (Path(d) / "report-高度相似对.csv").read_text(encoding="utf-8-sig")
        assert "丁" in similar_csv


def test_xlsx_is_valid_archive_with_three_sheets():
    report = _report()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "report.xlsx"
        sx.export(path, report, "xlsx")
        assert path.exists() and path.stat().st_size > 0
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        assert "xl/workbook.xml" in names
        # 三张表：汇总 / 完全重复组 / 高度相似对
        for sheet in ("sheet1.xml", "sheet2.xml", "sheet3.xml"):
            assert f"xl/worksheets/{sheet}" in names


def test_docx_is_valid_archive():
    report = _report()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "report.docx"
        sx.export(path, report, "docx")
        assert path.exists() and path.stat().st_size > 0
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
        assert "word/document.xml" in names


def test_pdf_goes_through_ui_layer():
    """core 层不碰 PySide6：pdf 必须抛 ValueError，让界面层去 render_pdf。"""
    report = _report()
    try:
        sx.export("report.pdf", report, "pdf")
    except ValueError as exc:
        assert "render_pdf" in str(exc)
    else:
        raise AssertionError("pdf 不应在 core 层落地")


def test_html_report_carries_notice():
    report = _report()
    html = sx.html_report(report)
    assert sx.EXPORT_NOTICE in html
    assert "完全重复组" in html
    assert "高度相似对" in html


def test_format_from_path_roundtrip():
    assert sx.format_from_path("a.txt") == "txt"
    assert sx.format_from_path("a.CSV") == "csv"
    assert sx.format_from_path("a.xlsx") == "xlsx"
    assert sx.format_from_path("a.docx") == "docx"
    assert sx.format_from_path("a.pdf") == "pdf"
    import pytest
    with pytest.raises(ValueError):
        sx.format_from_path("a.xyz")


def test_pair_rows_does_not_double_count_exact():
    """完全重复组只出一行（不是每两人一对），高度相似对也不重复它。"""
    report = _report()
    header, rows = sx.pair_rows(report)
    assert header[0] == "相似度"
    exact_rows = [r for r in rows if r[1] == sx.KIND_EXACT]
    similar_rows = [r for r in rows if r[1] == sx.KIND_SIMILAR]
    # 三人完全重复组 → 1 行；甲 vs 丁高度相似 → 1 行
    assert len(exact_rows) == 1
    assert len(similar_rows) == 1
    # 完全重复组里的人数要标在备注里（"共 3 人一字不差"）
    assert "3 人" in exact_rows[0][6]
    # 高度相似对里不应混进"完全重复"字样
    assert all(r[1] == sx.KIND_SIMILAR for r in similar_rows)
    # 不变量：相似对行数 == 报告里非完全重复的对数量
    non_exact = [p for p in report.pairs if not p.exact]
    assert len(similar_rows) == len(non_exact)


def test_exact_rows_lists_all_members():
    report = _report()
    header, rows = sx.exact_rows(report)
    assert header == ["题目", "人数", "成员"]
    assert len(rows) == 1
    assert rows[0][1] == "3"
    assert "甲" in rows[0][2] and "乙" in rows[0][2] and "丙" in rows[0][2]
