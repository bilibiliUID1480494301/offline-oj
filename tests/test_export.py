"""导出层（``core/export.py``）的测试。

这一层最容易出的**不是**逻辑错，而是"文件生成了、但打开是坏的"：
xlsx / docx 都是手写 OOXML，元素顺序错一个、控制字符没掐掉，Excel / Word 就
说"文件已损坏"，而生成代码这边**一个异常都不会有**。所以断言的重点是
**产物能不能被解回结构化数据**，不是"文件存在"。
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from offline_oj.core import export as ex

# 直接导入 core.models.TestCase 会在模块级触发 PytestCollectionWarning
# （"以 Test 开头 + 带 __init__"），这里用不到它，不导入。

# ======================================================================
# 样例数据
# ======================================================================


def sample(**overrides) -> ex.ExportData:
    payload = {
        "title": "期末模拟",
        "mode": "exam",
        "mode_label": "考试模式",
        "started_at": "2026-09-19T15:00:00",
        "ended_at": "2026-09-19T17:00:00",
        "problems": [
            {"id": "P0001", "title": "两数求和", "points": 50},
            {"id": "P0002", "title": "回文判断", "points": 30},
        ],
        "participants": [{"device_id": "AAAA1111", "username": "甲"},
                         {"device_id": "BBBB2222", "username": "乙"}],
        "submissions": [
            {"serial": 1, "device_id": "AAAA1111", "username": "甲",
             "problem_id": "P0001", "language": "cpp", "attempt": 1,
             "verdict": "AC", "passed": 3, "total": 3, "score": 50, "possible": 50,
             "time_ms": 12.5, "memory_mb": 3.4, "submitted_at": "2026-09-19T15:10:00",
             "code": "int main(){}", "optimized": True},
            {"serial": 2, "device_id": "BBBB2222", "username": "乙",
             "problem_id": "P0001", "language": "python", "attempt": 1,
             "verdict": "WA", "passed": 1, "total": 3, "score": 16, "possible": 50,
             "time_ms": 30.0, "memory_mb": 8.0,
             "submitted_at": "2026-09-19T15:12:00", "forced": True,
             "code": "print(1)"},
        ],
        "leaderboard": {
            "max_total_score": 80,
            "overall": [
                {"rank": 1, "device_id": "AAAA1111", "username": "甲", "score": 50,
                 "solved": 1, "per_problem": {"P0001": 50, "P0002": 0},
                 "total_time_ms": 12.5, "total_memory_mb": 3.4, "submit_count": 1},
                {"rank": 2, "device_id": "BBBB2222", "username": "乙", "score": 16,
                 "solved": 0, "per_problem": {"P0001": 16, "P0002": 0},
                 "total_time_ms": 30.0, "total_memory_mb": 8.0, "submit_count": 1},
            ],
            "per_problem": {},
        },
        "max_total_score": 80,
        "keep_code": True,
    }
    payload.update(overrides)
    return ex.ExportData.from_mapping(payload)


def body_xml(path: Path, part: str) -> ET.Element:
    """从 zip 里取出一个部件并**真正解析**它 —— 能解析才说明没写坏。"""
    with zipfile.ZipFile(path) as bundle:
        return ET.fromstring(bundle.read(part))


# ======================================================================
# 名次
# ======================================================================


class TestRanking:
    def test_ties_share_a_rank_and_the_next_one_skips(self):
        assert ex.competition_ranks([300, 300, 200, 100, 100]) == [1, 1, 3, 4, 4]

    def test_a_single_score_is_first(self):
        assert ex.competition_ranks([10]) == [1]

    def test_nobody_is_ranked(self):
        assert ex.competition_ranks([]) == []

    def test_all_equal_are_all_first(self):
        assert ex.competition_ranks([5, 5, 5]) == [1, 1, 1]


class TestRankingAgreesWithTheNetworkLayer:
    """两处实现必须给出同一串名次。

    ``core/`` 不 import ``net/``，所以 ``competition_ranks`` 是**第二份**实现。
    这里钉住它们：任何一边改了规则（比如"耗时破平"）都会红，而不会变成
    "榜单和导出成绩单名次对不上"这种没人查得动的现象。
    """

    @pytest.mark.parametrize("scores", [
        [],
        [100],
        [100, 100],
        [100, 90, 80],
        [300, 300, 200, 100, 100],
        [0, 0, 0, 0],
        [5, 5, 4, 3, 3, 3, 1],
    ])
    def test_same_as_net_session(self, scores):
        from offline_oj.net.session import competition_ranks as theirs

        assert ex.competition_ranks(scores) == theirs(scores), scores


# ======================================================================
# 表格
# ======================================================================


class TestScoreTable:
    def test_header_has_one_column_per_problem_with_its_points(self):
        header, _ = ex.score_table(sample())
        assert "两数求和（50）" in header
        assert "回文判断（30）" in header
        assert header[:3] == ["排名", "姓名", "设备"]
        assert header[-1] == "提交次数"

    def test_every_row_matches_the_header_length(self):
        header, rows = ex.score_table(sample())
        assert rows and all(len(row) == len(header) for row in rows)

    def test_scores_line_up_with_their_problem(self):
        header, rows = ex.score_table(sample())
        first = header.index("两数求和（50）")
        assert rows[0][first] == "50"
        assert rows[1][first] == "16"

    def test_a_problem_nobody_solved_still_gets_a_column(self):
        """没人做过的题也要有列 —— 否则成绩单上看不出"这题没人得分"。"""
        header, rows = ex.score_table(sample())
        index = header.index("回文判断（30）")
        assert all(row[index] == "0" for row in rows)

    def test_full_marks_column_comes_from_the_board_snapshot(self):
        header, rows = ex.score_table(sample())
        assert all(row[header.index("满分")] == "80" for row in rows)


class TestFallbackWithoutLeaderboard:
    """档案里没有榜单快照时不能罢工 —— 也不能假装数据是全的。"""

    def test_scores_are_recomputed_from_the_submissions(self):
        data = sample(leaderboard={})
        header, rows = ex.score_table(data)
        assert len(rows) == 2
        # 甲 50 分，乙 16 分；名次照样是 1 / 2
        assert [row[header.index("总分")] for row in rows] == ["50", "16"]
        assert [row[0] for row in rows] == ["1", "2"]

    def test_the_best_attempt_wins_not_the_last_one(self):
        """同一个人同一题交两次（先 50 后 10），算 50。"""
        data = sample(leaderboard={}, submissions=[
            {"serial": 1, "device_id": "A", "username": "甲", "problem_id": "P0001",
             "score": 50, "possible": 50},
            {"serial": 2, "device_id": "A", "username": "甲", "problem_id": "P0001",
             "score": 10, "possible": 50},
        ])
        header, rows = ex.score_table(data)
        assert rows[0][header.index("总分")] == "50"

    def test_ties_are_still_tied_after_recomputing(self):
        data = sample(leaderboard={}, submissions=[
            {"serial": 1, "device_id": "A", "username": "甲", "problem_id": "P0001",
             "score": 50, "possible": 50},
            {"serial": 2, "device_id": "B", "username": "乙", "problem_id": "P0001",
             "score": 50, "possible": 50},
        ])
        header, rows = ex.score_table(data)
        assert [row[0] for row in rows] == ["1", "1"]

    def test_the_report_says_that_it_recomputed(self):
        data = sample(leaderboard={})
        assert ex.NO_BOARD_NOTICE in ex.text_report(data, [ex.SECTION_SCORES])
        assert ex.NO_BOARD_NOTICE in ex.html_report(data, [ex.SECTION_SCORES])

    def test_no_notice_when_a_snapshot_is_present(self):
        data = sample()
        assert ex.NO_BOARD_NOTICE not in ex.text_report(data, [ex.SECTION_SCORES])


class TestSubmissionTable:
    def test_header_and_width_are_consistent(self):
        header, rows = ex.submission_table(sample())
        assert rows and all(len(row) == len(header) for row in rows)

    def test_rows_are_sorted_by_serial(self):
        data = sample(submissions=list(reversed(sample().submissions)))
        header, rows = ex.submission_table(data)
        assert [row[0] for row in rows] == ["1", "2"]

    def test_the_language_is_spelled_out(self):
        header, rows = ex.submission_table(sample())
        values = [row[header.index("语言")] for row in rows]
        assert values == ["C++", "Python"]

    def test_the_attempt_reads_as_a_sentence(self):
        header, rows = ex.submission_table(sample())
        assert [row[header.index("第几次")] for row in rows] == ["第 1 次", "第 1 次"]

    def test_forced_and_optimized_are_marked(self):
        """「自动收卷」和 O2 都得看得出来 —— 事后再看"他为什么 TLE"要靠它们。"""
        header, rows = ex.submission_table(sample())
        first, second = rows
        assert first[header.index("O2")] == "是"
        assert first[header.index("来源")] == ""
        assert second[header.index("来源")] == "自动收卷"
        assert second[header.index("O2")] == ""

    def test_passed_over_total_is_written_as_a_pair(self):
        header, rows = ex.submission_table(sample())
        assert [row[header.index("通过/总数")] for row in rows] == ["3/3", "1/3"]

    def test_the_problem_title_is_looked_up_not_invented(self):
        header, rows = ex.submission_table(sample())
        assert all(row[header.index("题名")] == "两数求和" for row in rows)


class TestCellFormatting:
    def test_integral_floats_lose_their_tail(self):
        assert ex._cell(12.0) == "12"
        assert ex._cell(12.5) == "12.5"

    def test_none_and_empty_become_an_empty_cell(self):
        assert ex._cell(None) == ""
        assert ex._cell("") == ""

    def test_a_true_flag_reads_as_yes_and_false_as_nothing(self):
        # 空单元格比"否"干净：一列里绝大多数都是"否"的时候，只有"是"值得占位置
        assert ex._cell(True) == "是"
        assert ex._cell(False) == ""

    def test_iso_timestamps_become_readable(self):
        assert ex.stamp_text("2026-09-19T15:00:00") == "2026-09-19 15:00:00"

    def test_a_timestamp_without_seconds_is_tolerated(self):
        assert ex.stamp_text("2026-09-19T15:00") == "2026-09-19 15:00"

    def test_an_unparseable_timestamp_is_kept_verbatim(self):
        assert ex.stamp_text("看不懂") == "看不懂"


# ======================================================================
# 等宽对齐（中文是双宽）
# ======================================================================


class TestAlignment:
    def test_chinese_counts_as_two_columns(self):
        assert ex.display_width("中文") == 4
        assert ex.display_width("ab") == 2

    def test_cells_of_equal_display_width_end_at_the_same_place(self):
        """这一条钉的是"表不会歪"。

        用 ``str.ljust`` 的写法在这里会失败：``甲`` 和 ``abcd`` 的 ``len()`` 是
        1 和 4，补出来一个宽一个窄，第二列就会一列靠左一列靠右。所以这里直接
        写出期望的**每一行**（含那个两空格分隔的横线行），不做"宽度差不超过 N"
        这种松判 —— 松判恰恰漏掉"某一行歪了但整体还在容差内"。
        """
        lines = ex.align_table(["姓名", "分"],
                               [["甲", "1"], ["abcd", "22"], ["中文名", "333"]])
        assert lines == [
            "姓名    分",
            "------  ---",
            "甲      1",
            "abcd    22",
            "中文名  333",
        ]

    def test_no_line_keeps_trailing_whitespace(self):
        """行尾留一堆空格，粘到别处就是一片空白。"""
        header, rows = ex.score_table(sample())
        for line in ex.align_table(header, rows):
            assert line == line.rstrip(), repr(line)


# ======================================================================
# txt / csv
# ======================================================================


class TestTextAndCsv:
    def test_the_text_report_holds_both_tables(self, tmp_path):
        data = sample()
        text = ex.text_report(data, ex.ALL_SECTIONS)
        assert "统计" not in text
        assert "成绩单" in text and "提交明细" in text
        assert "期末模拟" in text and "考试模式" in text

    def test_the_text_file_starts_with_a_bom(self, tmp_path):
        """没有 BOM，Excel 双击打开中文全是乱码。"""
        target = ex.export(tmp_path / "a.txt", sample(), "txt")[0]
        assert target.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_csv_gets_one_file_per_section(self, tmp_path):
        written = ex.export(tmp_path / "o.csv", sample(), "csv")
        assert {path.name for path in written} == {"o-成绩单.csv", "o-提交明细.csv"}
        assert all(path.exists() for path in written)

    def test_the_csv_is_readable_by_the_standard_module(self, tmp_path):
        target = [p for p in ex.export(tmp_path / "o.csv", sample(), "csv")
                  if "成绩单" in p.name][0]
        with target.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert rows[0][:3] == ["排名", "姓名", "设备"]
        assert len(rows) == 3          # 表头 + 两行

    def test_a_single_section_can_be_requested(self, tmp_path):
        written = ex.export(tmp_path / "o.csv", sample(), "csv",
                            sections=[ex.SECTION_SCORES])
        assert [path.name for path in written] == ["o-成绩单.csv"]

    def test_the_section_order_does_not_depend_on_the_input_order(self, tmp_path):
        """传集合进来时顺序本来是随机的，导出的两张表不能跟着换位置。"""
        written = ex.export(tmp_path / "o.csv", sample(), "csv",
                            sections={ex.SECTION_SUBMISSIONS, ex.SECTION_SCORES})
        assert [path.name for path in written] == ["o-成绩单.csv", "o-提交明细.csv"]


# ======================================================================
# xlsx
# ======================================================================


class TestXlsx:
    def test_it_is_a_zip_with_the_required_parts(self, tmp_path):
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        with zipfile.ZipFile(target) as bundle:
            names = set(bundle.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"} <= names

    def test_every_part_is_well_formed_xml(self, tmp_path):
        """写得歪一点 Excel 就说"文件已损坏"，而生成端不报任何错。"""
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        with zipfile.ZipFile(target) as bundle:
            for name in bundle.namelist():
                if name.endswith(".xml") or name.endswith(".rels"):
                    ET.fromstring(bundle.read(name))

    def test_the_two_sheets_are_named_after_the_sections(self, tmp_path):
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        workbook = body_xml(target, "xl/workbook.xml")
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        names = [node.get("name") for node in workbook.iter(f"{namespace}sheet")]
        assert names == ["成绩单", "提交明细"]

    def test_the_cells_land_in_the_right_column_letters(self, tmp_path):
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        sheet = body_xml(target, "xl/worksheets/sheet1.xml")
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        refs = [cell.get("r") for cell in sheet.iter(f"{namespace}c")]
        assert "A1" in refs and "B1" in refs and "C1" in refs
        # 表头 1 行 + 2 个选手，第三行不该存在
        assert "A4" not in refs

    def test_the_element_order_excel_insists_on(self, tmp_path):
        """OOXML 要求 ``sheetViews`` → ``cols`` → ``sheetData``，顺序错了就是"文件损坏"。"""
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert -1 < text.index("<sheetViews>") < text.index("<cols>") < text.index("<sheetData>")

    def test_numbers_stay_numbers_and_text_stays_text(self, tmp_path):
        """分数存成文本的话，Excel 里就没法求和排序了。"""
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "<v>50</v>" in text                    # 甲的总分是数字格
        assert 't="inlineStr"' in text                # 姓名是文本格

    def test_control_characters_from_compiler_output_are_stripped(self, tmp_path):
        """``message`` 里带 ``\\x0b`` 会让整份 xlsx 被判成损坏。"""
        data = sample(submissions=[{
            "serial": 1, "username": "甲", "problem_id": "P0001",
            "verdict": "CE", "message": "error:\x0b broken\x00",
        }])
        target = ex.export(tmp_path / "a.xlsx", data, "xlsx")[0]
        # 能解析就说明控制字符被清干净了
        body_xml(target, "xl/worksheets/sheet2.xml")

    def test_the_header_row_is_frozen(self, tmp_path):
        target = ex.export(tmp_path / "a.xlsx", sample(), "xlsx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert 'state="frozen"' in text

    def test_a_sheet_name_illegal_in_ooxml_is_replaced(self, tmp_path):
        """``[]:*?/\\`` 在 OOXML 里禁止，报错却发生在"Excel 打开时"。"""
        assert ex._sheet_name("a[b]:c*d?e/f\\g", 0) == "a-b-c-d-e-f-g"

    def test_a_sheet_name_is_capped_at_31_characters(self, tmp_path):
        assert len(ex._sheet_name("标" * 60, 0)) == 31

    def test_an_empty_sheet_name_gets_a_fallback(self):
        assert ex._sheet_name("   ", 2) == "表3"

    def test_column_names_roll_over_past_z(self):
        assert ex._column_name(0) == "A"
        assert ex._column_name(25) == "Z"
        assert ex._column_name(26) == "AA"
        assert ex._column_name(27) == "AB"


# ======================================================================
# docx
# ======================================================================


class TestDocx:
    def test_it_is_a_zip_with_the_required_parts(self, tmp_path):
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        with zipfile.ZipFile(target) as bundle:
            names = set(bundle.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names

    def test_every_part_is_well_formed_xml(self, tmp_path):
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        with zipfile.ZipFile(target) as bundle:
            for name in bundle.namelist():
                ET.fromstring(bundle.read(name))

    def test_the_title_and_both_tables_are_in_the_body(self, tmp_path):
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("word/document.xml").decode("utf-8")
        assert "期末模拟" in text
        assert text.count("<w:tbl>") == 2

    def test_bold_is_written_on_the_run_not_the_paragraph(self, tmp_path):
        """写在段落属性里 Word 是不认的 —— XML 合法，但排版静默不生效。"""
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("word/document.xml").decode("utf-8")
        assert "<w:r><w:rPr><w:b/>" in text

    def test_table_borders_are_inlined(self, tmp_path):
        """不引 styles.xml 就没有 TableGrid 可用，而 Word 对找不到的样式是静默不画线。"""
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        with zipfile.ZipFile(target) as bundle:
            text = bundle.read("word/document.xml").decode("utf-8")
        assert "<w:tblBorders>" in text and "insideH" in text

    def test_the_grid_has_one_column_per_header_cell(self, tmp_path):
        target = ex.export(tmp_path / "a.docx", sample(), "docx")[0]
        header, _ = ex.score_table(sample())
        sheet = body_xml(target, "word/document.xml")
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        grids = list(sheet.iter(f"{namespace}tblGrid"))
        assert len(grids[0]) == len(header)

    def test_control_characters_are_stripped_here_too(self, tmp_path):
        data = sample(submissions=[{
            "serial": 1, "username": "甲\x0b", "problem_id": "P1", "verdict": "AC"}])
        target = ex.export(tmp_path / "a.docx", data, "docx")[0]
        body_xml(target, "word/document.xml")


# ======================================================================
# HTML（PDF 的输入）
# ======================================================================


class TestHtmlReport:
    def test_it_has_a_table_for_each_section(self):
        html = ex.html_report(sample(), ex.ALL_SECTIONS)
        assert html.count("<table") == 2
        assert html.startswith("<html><body>")

    def test_special_characters_are_escaped(self):
        """题名里带 ``<`` 会让 HTML 结构坏掉，而 QTextDocument 会静默吃掉半张表。"""
        data = sample(problems=[{"id": "P1", "title": "<b>x</b> & y", "points": 10}])
        html = ex.html_report(data, [ex.SECTION_SCORES])
        assert "&lt;b&gt;" in html and "&amp;" in html
        assert "<b>x</b>" not in html

    def test_newlines_in_a_cell_become_line_breaks(self):
        data = sample(submissions=[{"serial": 1, "username": "甲\na", "verdict": "AC"}])
        html = ex.html_report(data, [ex.SECTION_SUBMISSIONS])
        assert "<br>" in html


# ======================================================================
# 完整档案包
# ======================================================================


class TestBundle:
    def test_it_carries_the_tables_and_the_source_code(self, tmp_path):
        target = ex.export_bundle(tmp_path / "b.zip", sample())
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
        assert any(name.endswith(".xlsx") for name in names)
        assert any(name.endswith(".txt") for name in names)
        assert {"sources/1-甲-P0001.cpp", "sources/2-乙-P0001.py"} <= set(names)

    def test_the_source_content_is_the_real_source(self, tmp_path):
        target = ex.export_bundle(tmp_path / "b.zip", sample())
        with zipfile.ZipFile(target) as bundle:
            assert bundle.read("sources/1-甲-P0001.cpp").decode() == "int main(){}"

    def test_the_xlsx_inside_is_usable(self, tmp_path):
        target = ex.export_bundle(tmp_path / "b.zip", sample())
        with zipfile.ZipFile(target) as bundle:
            payload = bundle.read([n for n in bundle.namelist()
                                   if n.endswith(".xlsx")][0])
        with zipfile.ZipFile(io.BytesIO(payload)) as inner:
            ET.fromstring(inner.read("xl/workbook.xml"))

    def test_a_bundle_without_code_says_so_instead_of_being_silent(self, tmp_path):
        """老师打开压缩包看不到代码，得知道是"没存"而不是"导出坏了"。"""
        data = sample(submissions=[{"serial": 1, "username": "甲",
                                    "problem_id": "P1", "verdict": "AC"}])
        target = ex.export_bundle(tmp_path / "b.zip", data)
        with zipfile.ZipFile(target) as bundle:
            assert "说明.txt" in bundle.namelist()
            assert not [n for n in bundle.namelist() if n.startswith("sources/")]

    def test_a_repeated_submission_does_not_overwrite_the_earlier_one(self, tmp_path):
        """同一个人同一题交两次，只按"人-题"命名会互相覆盖，最后只剩一份。"""
        data = sample(submissions=[
            {"serial": 1, "device_id": "A", "username": "甲", "problem_id": "P1",
             "language": "cpp", "verdict": "WA", "code": "first"},
            {"serial": 2, "device_id": "A", "username": "甲", "problem_id": "P1",
             "language": "cpp", "verdict": "AC", "code": "second"},
        ])
        target = ex.export_bundle(tmp_path / "b.zip", data)
        with zipfile.ZipFile(target) as bundle:
            sources = {name: bundle.read(name).decode()
                       for name in bundle.namelist() if name.startswith("sources/")}
        assert set(sources.values()) == {"first", "second"}

    def test_export_sources_writes_one_file_per_submission(self, tmp_path):
        count = ex.export_sources(tmp_path / "src", sample())
        assert count == 2
        assert {p.name for p in (tmp_path / "src").iterdir()} == {
            "1-甲-P0001.cpp", "2-乙-P0001.py"}

    def test_export_sources_skips_entries_without_code(self, tmp_path):
        data = sample(submissions=[{"serial": 1, "username": "甲", "problem_id": "P1",
                                    "verdict": "AC"}])
        assert ex.export_sources(tmp_path / "src", data) == 0


# ======================================================================
# 落盘要么完整、要么不存在
# ======================================================================


class TestAtomicWrite:
    """`zipfile` / `QPdfWriter` 都是**先建文件再填内容**。

    直接往目标名字上写，失败时磁盘上会留下一个"文件名和位置都对、但打不开"
    的东西 —— 老师以为导出成功了，考前打开才发现是坏的。
    """

    def test_the_target_does_not_exist_while_it_is_being_written(self, tmp_path):
        target = tmp_path / "out.txt"
        seen: dict[str, bool] = {}

        def produce(partial: Path) -> None:
            seen["exists"] = target.exists()
            partial.write_text("x", encoding="utf-8")

        ex.atomic_write(target, produce)
        assert seen["exists"] is False, "写到一半时目标名字就已经存在了"
        assert target.read_text(encoding="utf-8") == "x"

    def test_a_failure_removes_the_partial(self, tmp_path):
        def produce(partial: Path) -> None:
            partial.write_text("half", encoding="utf-8")
            raise OSError("磁盘满了")

        with pytest.raises(OSError):
            ex.atomic_write(tmp_path / "out.txt", produce)
        assert not (tmp_path / "out.txt").exists()
        assert not list(tmp_path.glob(f"*{ex.PARTIAL_SUFFIX}")), "留下了 .partial"

    def test_a_failed_reexport_does_not_destroy_the_previous_one(self, tmp_path):
        """重新导出失败，上一次成功的那份必须原样留着。"""
        target = tmp_path / "out.txt"
        target.write_text("上一版", encoding="utf-8")

        def produce(partial: Path) -> None:
            raise OSError("boom")

        with pytest.raises(OSError):
            ex.atomic_write(target, produce)
        assert target.read_text(encoding="utf-8") == "上一版"

    def test_every_writer_goes_through_it(self, tmp_path):
        """四个写出器都要么产出完整文件、要么什么都不留。"""
        data = sample()
        assert ex.export(tmp_path / "a.txt", data, "txt")[0].exists()
        assert ex.export(tmp_path / "a.xlsx", data, "xlsx")[0].exists()
        assert ex.export(tmp_path / "a.docx", data, "docx")[0].exists()
        assert ex.export_bundle(tmp_path / "a.zip", data).exists()
        assert not list(tmp_path.glob(f"*{ex.PARTIAL_SUFFIX}"))


# ======================================================================
# 文件名
# ======================================================================


class TestNaming:
    def test_the_stem_pairs_the_time_with_the_title(self):
        assert ex.default_stem(sample()) == "20260919150000-期末模拟"

    def test_a_missing_title_falls_back(self):
        assert ex.default_stem(sample(title="", started_at="")) == "未命名场次"

    def test_a_title_full_of_path_characters_is_cleaned(self):
        stem = ex.default_stem(sample(title="a/b:c*d?e", started_at=""))
        assert stem == "a-b-c-d-e"

    def test_a_person_named_with_slashes_cannot_escape_the_folder(self, tmp_path):
        """学生把自己的名字填成路径片段，源码文件不能跑到导出目录外面去。"""
        data = sample(submissions=[{"serial": 1, "username": "../../evil",
                                    "problem_id": "P1", "language": "cpp",
                                    "verdict": "AC", "code": "x"}])
        ex.export_sources(tmp_path / "src", data)
        written = list((tmp_path / "src").iterdir())
        assert len(written) == 1
        assert written[0].parent == tmp_path / "src"


# ======================================================================
# 入口的分派
# ======================================================================


class TestExportDispatch:
    def test_pdf_is_refused_with_a_pointer_to_the_ui_layer(self):
        """PDF 要走 QPdfWriter，只能在界面侧 —— 这里必须明确拒绝而不是静默跳过。"""
        with pytest.raises(ValueError) as info:
            ex.export("x.pdf", sample(), "pdf")
        assert "pdf" in str(info.value)

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError):
            ex.export("x.rtf", sample(), "rtf")

    def test_the_target_directory_is_created(self, tmp_path):
        target = ex.export(tmp_path / "deep" / "nested" / "a.txt", sample(), "txt")[0]
        assert target.exists()

    def test_an_empty_export_still_produces_a_file(self, tmp_path):
        """一个人都没交也要出文件 —— 报"没有数据"是界面的事，不是这里罢工。"""
        empty = ex.ExportData()
        assert ex.export(tmp_path / "a.txt", empty, "txt")[0].exists()
        assert ex.export(tmp_path / "a.xlsx", empty, "xlsx")[0].exists()
        assert ex.export(tmp_path / "a.docx", empty, "docx")[0].exists()


# ======================================================================
# 容错构造
# ======================================================================


class TestTolerantConstruction:
    def test_unknown_fields_are_ignored(self):
        data = ex.ExportData.from_mapping({"title": "t", "未来字段": 1})
        assert data.title == "t"

    def test_a_wrong_type_becomes_empty_rather_than_raising(self):
        data = ex.ExportData.from_mapping({"problems": "不是列表",
                                           "submissions": None,
                                           "leaderboard": [1, 2],
                                           "max_total_score": "abc"})
        assert data.problems == [] and data.submissions == []
        assert data.leaderboard == {} and data.max_total_score == 0

    def test_the_points_fall_back_to_what_the_submissions_claim(self):
        """题目快照丢了，至少要能从"该题见过的最大 possible"看出满分。"""
        data = ex.ExportData.from_mapping({
            "problems": [],
            "submissions": [{"problem_id": "P1", "possible": 30, "score": 10}]})
        assert data.points_of("P1") == 30

    def test_full_marks_adds_up_the_problems_when_the_board_is_missing(self):
        data = ex.ExportData.from_mapping({
            "problems": [{"id": "P1", "points": 50}, {"id": "P2", "points": 30}],
            "max_total_score": 0})
        assert data.full_marks() == 80

    def test_the_problem_order_comes_from_the_snapshot(self):
        data = sample()
        assert data.problem_ids() == ["P0001", "P0002"]

    def test_without_a_snapshot_the_order_follows_the_first_appearance(self):
        data = ex.ExportData.from_mapping({"submissions": [
            {"problem_id": "P2"}, {"problem_id": "P1"}, {"problem_id": "P2"}]})
        assert data.problem_ids() == ["P2", "P1"]


class TestBuildingFromRealSources:
    def test_an_archive_round_trips_into_export_data(self, tmp_path):
        """真档案 → 导出数据 → 成绩单，走一遍完整链路。"""
        from offline_oj.core import records

        record = records.SessionRecord(
            title="期末模拟", mode="exam", started_at="2026-09-19T15:00:00",
            problems=[{"id": "P0001", "title": "A+B", "points": 50}],
            participants=[{"username": "甲", "device_id": "AAAA"}],
            submission_count=1, max_total_score=50)
        records.write_archive(
            tmp_path, stamp="20260919-150000", record=record,
            submissions=[{"serial": 1, "device_id": "AAAA", "username": "甲",
                          "problem_id": "P0001", "score": 50, "possible": 50,
                          "verdict": "AC", "code": "x", "submitted_at": "2026-09-19T15:10:00"}],
            leaderboard={"max_total_score": 50, "overall": [
                {"rank": 1, "device_id": "AAAA", "username": "甲", "score": 50,
                 "per_problem": {"P0001": 50}, "submit_count": 1}], "per_problem": {}})
        archive = records.read_archive(
            next(p for p in tmp_path.iterdir() if p.is_dir()))

        data = ex.ExportData.from_archive(archive)
        assert data.title == "期末模拟"
        header, rows = ex.score_table(data)
        assert rows[0][header.index("总分")] == "50"
        # 源码跟着档案一路走到导出
        assert ex.export_sources(tmp_path / "src", data) == 1
