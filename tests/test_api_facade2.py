# -*- coding: utf-8 -*-
"""offline_oj.api 第二批函数测试：题库/导入导出/档案/工具链/加密。"""

from __future__ import annotations

import json

import pytest

from offline_oj import api


PROBLEM_A = {
    "id": "P1001",
    "title": "A+B",
    "time_limit": 1000,
    "memory_limit": 256,
    "testcases": [{"input": "1 2\n", "output": "3\n"}],
}


def test_repo_crud(tmp_path):
    repo = api.open_repository(tmp_path / "repo")
    assert repo.count() == 0
    pid = repo.add(PROBLEM_A)
    assert pid == "P1001" and repo.count() == 1
    got = repo.get("P1001")
    assert got["title"] == "A+B" and got["testcases"][0]["input"] == "1 2\n"
    assert "P1001" in repo
    assert repo.search("A+B")  # 标题能搜到
    stats = repo.stats()
    assert stats["total"] == 1 and stats["testcases"] == 1
    # 重开同一目录，数据还在
    repo2 = api.open_repository(tmp_path / "repo")
    assert repo2.count() == 1
    assert repo2.remove("P1001") is True and repo2.count() == 0


def test_import_export_problems_roundtrip(tmp_path):
    repo = api.open_repository(tmp_path / "repo")
    repo.add(PROBLEM_A)
    zip_path = tmp_path / "problems.zip"
    n = api.export_problems(repo, zip_path)
    assert n == 1 and zip_path.exists()

    repo2 = api.open_repository(tmp_path / "repo2")
    report = api.import_problems(repo2, zip_path)
    assert report["imported"] == 1
    assert repo2.get("P1001")["title"] == "A+B"


def test_list_exam_archives_empty(tmp_path):
    assert api.list_exam_archives(tmp_path) == []


def test_check_toolchain():
    result = api.check_toolchain(keys=("python",))
    assert result["python"]["found"] is True
    assert result["python"]["works"] is True


def test_encrypt_decrypt_roundtrip():
    key = bytes(range(32))  # ChaCha20 32B
    blob = api.encrypt_message(key, "机密消息".encode("utf-8"))
    assert blob[:12] != api.encrypt_message(key, "机密消息".encode("utf-8"))[:12]  # nonce 随机
    assert api.decrypt_message(key, blob) == "机密消息".encode("utf-8")


def test_similarity_diff():
    diff = api.similarity_diff("int main(){return 0;}", "int main(){return 1;}",
                               language="cpp")
    assert isinstance(diff, list) and len(diff) > 0


def test_generate_passcode():
    code = api.generate_passcode(8)
    assert len(code) == 8
