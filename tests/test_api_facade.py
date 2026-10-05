# -*- coding: utf-8 -*-
"""offline_oj.api 高层函数接口测试（v2.1.1）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from offline_oj import api


PYTHON_HELLO = 'print("hello")\n'
PYTHON_OK = 'a, b = map(int, input().split())\nprint(a + b)\n'
PYTHON_WA = 'print("nope")\n'


def test_judge_python_ac(tmp_path):
    r = api.judge(
        PYTHON_OK,
        [{"input": "1 2\n", "output": "3\n"},
         {"input": "10 20\n", "output": "30\n"}],
        language="python",
        work_dir=tmp_path / "w1",
    )
    assert r["verdict"] == "AC"
    assert r["passed"] == 2 and r["total"] == 2
    assert r["compile_ok"] is True
    assert len(r["cases"]) == 2 and all(c["passed"] for c in r["cases"])


def test_judge_python_wa_and_detect(tmp_path):
    # 自动探测语言（不传 language）
    r = api.judge(PYTHON_WA, [("1 2\n", "3\n")], work_dir=tmp_path / "w2")
    assert r["language"] == "python"
    assert r["verdict"] == "WA"
    assert r["passed"] == 0 and r["total"] == 1


def test_judge_auto_detect_cpp_or_python(tmp_path):
    # #include 开头 → cpp；没有编译器时给出可读错误而不是崩
    code = '#include <iostream>\nint main() { std::cout << 1; }\n'
    try:
        r = api.judge(code, [("x\n", "1\n")], work_dir=tmp_path / "w3")
        assert r["language"] == "cpp"
    except RuntimeError as exc:
        assert "toolchain" in str(exc) or "工具链" in str(exc)


def test_judge_rejects_unknown_language(tmp_path):
    with pytest.raises(ValueError):
        api.judge(PYTHON_OK, [("1\n", "1\n")], language="fortran",
                  work_dir=tmp_path / "w4")


def test_detect_similarity():
    rows = [
        {"serial": 1, "username": "甲", "problem_id": "A",
         "language": "cpp", "code": "int main(){return 0;}"},
        {"serial": 2, "username": "乙", "problem_id": "A",
         "language": "cpp", "code": "int main(){return 0;}"},
        {"serial": 3, "username": "丙", "problem_id": "A",
         "language": "cpp", "code": "int main(){ /* 完全不同 */ return 1; }"},
    ]
    report = api.detect_similarity(rows)
    # 甲乙完全重复 → 至少一个 exact 组
    assert len(report.exact_groups) >= 1
    members = [p.username for p in report.exact_groups[0].members]
    assert set(members) == {"甲", "乙"}


def test_export_report_missing_dependency(tmp_path):
    """docx 依赖 python-docx：缺失时抛可读错误；txt 永远可用。"""
    from offline_oj.core.export import ExportData
    data = ExportData()  # 空数据也能导出 txt 骨架
    out = api.export_report(tmp_path / "report.txt", data, fmt="txt")
    assert Path(out[0]).exists()
