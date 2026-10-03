"""测验档案：写盘、读回、列举、删除。

这一层的用例全部**不建窗口**：``core/records.py`` 只吃 dict、只碰文件，
所以可以在没有任何 UI 与网络栈的情况下把它测穿。GUI 那一侧由
``smoke_gui.py`` 与 ``test_exam_settings.py`` 覆盖。

用例盯的是几件"错了也不报错"的事：

* 档案里**到底有没有**学生源码（选了"只存成绩"就必须一个字节都不留）
* 半坏的档案还能不能读出剩下那部分（而不是整份报废）
* 删除**必须先确认它真是一份档案**（否则界面上一旦拼错路径就是删库）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.core import records  # noqa: E402

SECRET = "// SECRET-MARKER-7C1D"


def make_submission(serial: int = 1, *, code: str = "print(1)", **extra) -> dict:
    data = {
        "serial": serial,
        "device_id": f"DEV{serial:05d}",
        "username": f"同学{serial}",
        "problem_id": "P0001",
        "language": "python",
        "attempt": 1,
        "verdict": "AC",
        "passed": 2,
        "total": 2,
        "time_ms": 12.5,
        "memory_mb": 3.5,
        "score": 20,
        "possible": 20,
        "message": "全部通过",
        "submitted_at": "2026-09-19T17:00:00",
        "judged_at": "2026-09-19T17:00:01",
        "code": code,
    }
    data.update(extra)
    return data


def make_record(**extra) -> records.SessionRecord:
    payload = {
        "session_id": "20260919170000",
        "title": "期末模拟",
        "mode": "exam",
        "room_id": "a1b2c3d4e5f60718",
        "started_at": "2026-09-19T17:00:00",
        "ended_at": "2026-09-19T18:00:00",
        "duration_minutes": 60,
        "policy": {},
        "problems": [{"id": "P0001", "title": "A+B"}],
        "participants": [{"device_id": "DEV00001", "username": "同学1"}],
        "max_total_score": 100,
    }
    payload.update(extra)
    return records.SessionRecord.from_dict(payload)


def write_one(root: Path, *, keep_code: bool = True, **record_extra):
    submissions = [make_submission(1, code=f"{SECRET}\nprint(1)"),
                   make_submission(2, code="print(2)")]
    return records.write_archive(
        root, stamp="20260919-170000", record=make_record(**record_extra),
        submissions=submissions,
        leaderboard={"overall": [{"rank": 1, "score": 20}], "max_total_score": 100},
        keep_code=keep_code, created_at="2026-09-19T18:00:05")


# ======================================================================
# 目录名
# ======================================================================


class TestDirectoryName:
    def test_stamp_and_title(self):
        assert records.archive_directory_name("20260919-170000", "期末模拟") \
            == "20260919-170000-期末模拟"

    def test_path_characters_are_replaced(self):
        """标题里带 ``/`` 或 ``:`` 时不能真去建子目录 / 触发盘符歧义。"""
        name = records.archive_directory_name("20260919-170000", "A/B:C*D?E")
        assert "/" not in name and ":" not in name
        assert "*" not in name and "?" not in name

    def test_long_titles_are_capped(self):
        """Windows 路径上限 260，长标题会让写入失败在"找不到路径"上。"""
        name = records.archive_directory_name("20260919-170000", "标" * 200)
        assert len(name) <= len("20260919-170000-") + records.SLUG_LIMIT

    def test_empty_title_falls_back_to_the_session_id(self):
        name = records.archive_directory_name("20260919-170000", "   ", "abcdef01")
        assert name == "20260919-170000-abcdef01"

    def test_nothing_at_all_still_yields_a_name(self):
        assert records.archive_directory_name("", "", "") == "session"

    def test_an_iso_stamp_is_sanitised_too(self):
        """``started.isoformat()`` 是个太顺手的写法，而里面带冒号。

        冒号在 Windows 上会让 ``mkdir`` 直接报 ``WinError 123``（"文件名、
        目录名或卷标语法不正确"），报的还是整条路径 —— 看着像档案根目录不存在，
        实际是名字里有个冒号。所以 stamp 也必须过筛子。
        """
        name = records.archive_directory_name("2026-09-19T15:00:00", "期末模拟")
        assert ":" not in name
        assert name == "2026-09-19T15-00-00-期末模拟"

    def test_a_hostile_stamp_cannot_escape_the_root(self, tmp_path):
        """真有调用方把路径片段塞进 stamp，也不能跑到档案根目录外面去。"""
        target = records.write_archive(
            tmp_path, stamp="../../evil", record=make_record(),
            submissions=[make_submission(1)])
        assert target.parent == tmp_path
        assert target.is_dir()


# ======================================================================
# 写
# ======================================================================


class TestWriting:
    def test_the_four_files_are_there(self, tmp_path):
        target = write_one(tmp_path)
        assert target.is_dir()
        for name in (records.MANIFEST_FILE, records.SESSION_FILE,
                     records.SUBMISSIONS_FILE, records.LEADERBOARD_FILE):
            assert (target / name).exists(), name

    def test_the_manifest_carries_the_version_and_checksums(self, tmp_path):
        target = write_one(tmp_path)
        manifest = json.loads((target / records.MANIFEST_FILE).read_text("utf-8"))
        assert manifest["version"] == records.ARCHIVE_VERSION
        assert set(manifest["files"]) == {
            records.SESSION_FILE, records.SUBMISSIONS_FILE,
            records.LEADERBOARD_FILE}
        assert all(len(value) == 64 for value in manifest["files"].values())

    def test_one_line_per_submission(self, tmp_path):
        """一行一份：追加友好，也便于"读到一半崩了还能救回前面几份"。"""
        target = write_one(tmp_path)
        lines = (target / records.SUBMISSIONS_FILE).read_text("utf-8").splitlines()
        assert len(lines) == 2
        assert all(isinstance(json.loads(line), dict) for line in lines)

    def test_the_submission_count_lands_in_the_record(self, tmp_path):
        target = write_one(tmp_path)
        record = records.read_archive(target).record
        assert record.submission_count == 2
        assert record.max_total_score == 100

    def test_a_second_one_gets_a_different_directory(self, tmp_path):
        first = write_one(tmp_path)
        second = write_one(tmp_path)
        assert first != second
        assert first.is_dir() and second.is_dir()

    def test_the_directory_name_never_carries_the_room_code(self, tmp_path):
        """档案落在主机自己的盘上，但没有任何理由把凭据撒进文件名。"""
        target = write_one(tmp_path, title="期末模拟")
        assert "-期末模拟" in target.name
        text = (target / records.SESSION_FILE).read_text("utf-8")
        assert "room_code" not in text
        assert json.loads(text)["room_id"] == "a1b2c3d4e5f60718"


class TestPrivacySwitch:
    """「只存成绩不存源码」要真的做到一个字节都不留。"""

    def test_code_is_absent_from_the_bytes(self, tmp_path):
        """**调用方没抠、直接带着源码交给 write_archive** 也必须抠干净。

        真实路径上 ``ExamSession.archive_submissions(keep_code=False)`` 已经抠过
        一遍；这一条验的是"哪一天有人忘了"，也就是最后那道闸门本身。
        """
        target = write_one(tmp_path, keep_code=False)
        blob = (target / records.SUBMISSIONS_FILE).read_text("utf-8")
        assert "SECRET-MARKER" not in blob, "选了只存成绩，源码却还在盘上"
        assert all("code" not in json.loads(line)
                   for line in blob.splitlines())

    def test_the_scores_are_still_complete(self, tmp_path):
        """抠掉的只有源码：成绩、判定、耗时一个都不能少，否则导出成绩单没数据。"""
        target = write_one(tmp_path, keep_code=False)
        rows = records.read_archive(target).submissions
        assert [row["score"] for row in rows] == [20, 20]
        assert [row["verdict"] for row in rows] == ["AC", "AC"]

    def test_the_record_says_so(self, tmp_path):
        """要让读档方分得清"没存源码"和"源码丢了"。"""
        target = write_one(tmp_path, keep_code=False)
        archive = records.read_archive(target)
        assert archive.record.keep_code is False
        assert archive.has_code is False

    def test_the_default_keeps_the_code(self, tmp_path):
        archive = records.read_archive(write_one(tmp_path))
        assert archive.has_code is True
        assert archive.codes() and SECRET in archive.codes()[0]["code"]


# ======================================================================
# 读
# ======================================================================


class TestReading:
    def test_round_trip(self, tmp_path):
        archive = records.read_archive(write_one(tmp_path))
        assert archive.record.title == "期末模拟"
        assert archive.record.mode == "exam"
        assert archive.record.duration_minutes == 60
        assert len(archive.submissions) == 2
        assert archive.leaderboard["max_total_score"] == 100
        assert archive.integrity_ok is True
        assert archive.skipped_lines == 0

    def test_a_corrupt_line_is_skipped_not_fatal(self, tmp_path):
        """半行写入（断电、磁盘满）不该让整份档案读不出来。"""
        target = write_one(tmp_path)
        path = target / records.SUBMISSIONS_FILE
        path.write_text(path.read_text("utf-8") + '{"serial": 3, "device\n',
                        encoding="utf-8")
        archive = records.read_archive(target)
        assert len(archive.submissions) == 2
        assert archive.skipped_lines == 1
        assert archive.integrity_ok is False

    def test_a_checksum_mismatch_is_reported_but_still_readable(self, tmp_path):
        """"少了榜单但有全部提交"比一个异常有用得多。"""
        target = write_one(tmp_path)
        (target / records.LEADERBOARD_FILE).write_text('{"overall": []}',
                                                       encoding="utf-8")
        archive = records.read_archive(target)
        assert archive.integrity_ok is False
        assert len(archive.submissions) == 2
        assert archive.leaderboard == {"overall": []}

    def test_a_missing_session_file_is_an_error(self, tmp_path):
        """元数据是骨架：没有它连"这是哪一场"都不知道，只能报错。"""
        target = write_one(tmp_path)
        (target / records.SESSION_FILE).unlink()
        with pytest.raises(records.ArchiveError):
            records.read_archive(target)

    def test_a_missing_submission_file_is_tolerated(self, tmp_path):
        target = write_one(tmp_path)
        (target / records.SUBMISSIONS_FILE).unlink()
        archive = records.read_archive(target)
        assert archive.submissions == []
        assert archive.integrity_ok is False

    def test_unknown_fields_are_ignored(self, tmp_path):
        """将来加了字段、旧程序也要能读。"""
        target = write_one(tmp_path)
        path = target / records.SESSION_FILE
        payload = json.loads(path.read_text("utf-8"))
        payload["future_field"] = {"nested": [1, 2]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        assert records.read_archive(target).record.title == "期末模拟"

    def test_the_directory_must_exist(self, tmp_path):
        with pytest.raises(records.ArchiveError):
            records.read_archive(tmp_path / "没有这个目录")


class TestSubmissionRoundTrip:
    def test_the_archive_dict_has_code_and_to_dict_does_not(self, tmp_path):
        """两个方法的差别必须是**名字上的**，不是靠读注释才发现。

        ``Submission.to_dict()`` 跑在出站载荷上，它的契约是"绝不含 code"。
        """
        from offline_oj.net.session import Submission

        item = Submission.from_dict(make_submission(1, code="SECRET"))
        assert "code" not in item.to_dict()
        assert item.to_archive_dict()["code"] == "SECRET"

    def test_reading_back_a_submission(self):
        from offline_oj.net.session import Submission

        original = Submission.from_dict(make_submission(3, code="x = 1"))
        again = Submission.from_dict(original.to_archive_dict())
        assert again.serial == 3
        assert again.code == "x = 1"
        assert again.score == 20
        assert again.possible == 20
        assert again.verdict == "AC"
        assert again.submitted_at == original.submitted_at
        assert again.judged_at == original.judged_at

    def test_a_submission_without_code_is_fine(self):
        """``keep_code=False`` 的档案读回来的就是这种。"""
        from offline_oj.net.session import Submission

        payload = make_submission(4)
        payload.pop("code")
        assert Submission.from_dict(payload).code == ""

    def test_a_broken_timestamp_does_not_become_now(self):
        """把两年前的档案显示成今天考的，比时间空着更糟。"""
        from offline_oj.net.session import Submission

        item = Submission.from_dict(make_submission(5, judged_at="看不懂"))
        assert item.judged_at is None
        # submitted_at 在榜单排序里要参与比较，没有"空"的表示，只能回落到现在
        assert item.submitted_at is not None


# ======================================================================
# 列举
# ======================================================================


class TestListing:
    def test_newest_first(self, tmp_path):
        records.write_archive(
            tmp_path, stamp="20260918-090000",
            record=make_record(title="周一那场", started_at="2026-09-18T09:00:00"),
            submissions=[])
        records.write_archive(
            tmp_path, stamp="20260919-170000",
            record=make_record(title="周二那场", started_at="2026-09-19T17:00:00"),
            submissions=[])
        titles = [item.record.title for item in records.list_archives(tmp_path)]
        assert titles == ["周二那场", "周一那场"]

    def test_an_empty_root_is_not_an_error(self, tmp_path):
        assert records.list_archives(tmp_path / "还不存在") == []

    def test_a_foreign_directory_is_ignored(self, tmp_path):
        """档案根目录里被塞进别的东西时，列表不该报错也不该显示它。"""
        (tmp_path / "随手放进来的").mkdir()
        (tmp_path / "随手放进来的" / "笔记.txt").write_text("hi", encoding="utf-8")
        assert records.list_archives(tmp_path) == []

    def test_a_half_written_archive_is_not_listed(self, tmp_path):
        """``.partial`` 是写到一半的，列表页不能把它当成一场成绩。"""
        (tmp_path / "20260919-170000-半截.partial").mkdir()
        assert records.list_archives(tmp_path) == []

    def test_a_broken_archive_is_listed_with_a_reason(self, tmp_path):
        """静默跳过等于"档案不见了"，而真相可能是"版本不认识"。"""
        target = write_one(tmp_path)
        (target / records.SESSION_FILE).write_text("{ 这不是 JSON", encoding="utf-8")
        listed = records.list_archives(tmp_path)
        assert len(listed) == 1
        assert listed[0].broken
        assert "读不出来" in listed[0].broken

    def test_a_newer_version_says_upgrade(self, tmp_path):
        target = write_one(tmp_path)
        path = target / records.MANIFEST_FILE
        payload = json.loads(path.read_text("utf-8"))
        payload["version"] = records.ARCHIVE_VERSION + 5
        path.write_text(json.dumps(payload), encoding="utf-8")
        listed = records.list_archives(tmp_path)
        assert listed and "比本程序新" in listed[0].broken

    def test_the_label_reads_like_a_sentence(self, tmp_path):
        target = write_one(tmp_path)
        summary = records.list_archives(tmp_path)[0]
        assert summary.directory == target
        assert "期末模拟" in summary.label
        assert "1 人" in summary.label and "2 份提交" in summary.label

    def test_total_bytes_is_zero_for_a_missing_root(self, tmp_path):
        assert records.total_bytes(tmp_path / "还没有") == 0

    def test_total_bytes_counts_the_archives(self, tmp_path):
        write_one(tmp_path)
        assert records.total_bytes(tmp_path) > 0


# ======================================================================
# 删除
# ======================================================================


class TestDeleting:
    def test_it_refuses_a_directory_that_is_not_an_archive(self, tmp_path):
        """界面上一旦把路径拼错，这一条就是"题库还在不在"的分界线。"""
        important = tmp_path / "题库也在这儿"
        important.mkdir()
        (important / "problems.json").write_text("{}", encoding="utf-8")
        assert records.delete_archive(important) is False
        assert (important / "problems.json").exists()

    def test_it_refuses_a_missing_directory(self, tmp_path):
        assert records.delete_archive(tmp_path / "没有") is False

    def test_it_removes_a_real_archive(self, tmp_path):
        target = write_one(tmp_path)
        assert records.delete_archive(target) is True
        assert not target.exists()
        assert records.list_archives(tmp_path) == []
