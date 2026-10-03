"""选手名单：导入、导出、落盘、口令核对，以及"手打的账号能不能被认出来"。

这一层的用例全部**不建窗口、不开房间**（``core/roster.py`` 只吃 dict、只碰
文件），所以可以在没有任何 UI 与网络栈的情况下把它测穿。

用例盯的是几件"错了也不报错"的事：

* 导入 CSV 时**少收了人会不会说出来**（静默少一个人，要到考完才暴露）；
* 老师用中文 Excel 存的 GBK CSV 会不会被读成乱码姓名（乱码也能进场，只是榜上
  写的是一堆问号）；
* 名单文件写坏一半会不会被当成"这份名单是空的"；
* 以及最要紧的一条：**名单算出来的索引与网络层算出来的必须逐字节一致** ——
  不一致的现象是"名单上明明有你、密码也是对的，就是进不去"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.core import roster as roster_mod  # noqa: E402
from offline_oj.core.roster import (CSV_HEADERS, PASSCODE_NOTICE,  # noqa: E402
                                    ROSTER_VERSION, Contestant, Roster,
                                    RosterError, generate_passcode,
                                    read_csv_text, safe_roster_name)
from offline_oj.net import session as session_mod  # noqa: E402
from offline_oj.net.session import (EntryMode, ExamSession,  # noqa: E402
                                    credential_id, normalize_account)


def person(account: str, **extra) -> Contestant:
    data = {"account": account, "name": f"同学{account}", "passcode": "135790"}
    data.update(extra)
    return Contestant.from_dict(data)


def a_roster() -> Roster:
    return Roster(title="高一(3)班", contestants=[
        person("2026001", name="张三", seat="A-01"),
        person("2026002", name="李四", passcode="246810", seat="A-02"),
    ])


# ---------------------------------------------------------------------------
# 口令与文件名
# ---------------------------------------------------------------------------


class TestPasscodes:
    def test_a_generated_passcode_is_all_digits(self):
        for _ in range(50):
            code = generate_passcode(6)
            assert len(code) == 6
            assert code.isdigit()

    def test_it_never_starts_with_a_zero(self):
        """首位是 0 的话，口头念号或从纸条上抄都会漏掉前导零。"""
        for _ in range(200):
            assert generate_passcode(6)[0] != "0"

    def test_length_is_clamped_to_something_usable(self):
        assert len(generate_passcode(1)) == 4
        assert len(generate_passcode(99)) == 12

    def test_two_calls_do_not_agree(self):
        """用 secrets 而不是 random —— 撞上同一个口令的概率应当可以忽略。"""
        codes = {generate_passcode(8) for _ in range(50)}
        assert len(codes) > 45


class TestFileNames:
    def test_illegal_characters_are_dropped(self):
        assert safe_roster_name('高一<3>班:名单/2026') == "高一3班名单2026"

    def test_an_empty_name_falls_back(self):
        assert safe_roster_name("   ") == "名单"
        assert safe_roster_name("") == "名单"

    def test_it_is_not_unbounded(self):
        assert len(safe_roster_name("名" * 200)) == 40


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------


class TestCsvEncoding:
    def test_utf8_with_bom(self):
        assert read_csv_text("账号,姓名\n".encode("utf-8-sig")).startswith("账号")

    def test_plain_utf8(self):
        assert read_csv_text("账号,姓名\n".encode("utf-8")) == "账号,姓名\n"

    def test_gbk_from_chinese_excel(self):
        """中文 Windows 的 Excel"另存为 CSV"默认写 GBK，这是最常见的一种。"""
        text = read_csv_text("账号,姓名\n2026001,张三\n".encode("gbk"))
        assert "张三" in text

    def test_broken_bytes_do_not_raise(self):
        """宁可替换几个字，也不要让整个导入失败。"""
        assert read_csv_text(b"\xff\xfe\x00\x01") != ""


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


class TestImport:
    def test_a_plain_csv(self):
        text = "账号,姓名,密码,座位\n2026001,张三,135790,A-01\n"
        roster, notes = Roster.from_csv_text(text, title="一班")
        assert notes == []
        assert len(roster) == 1
        assert roster.contestants[0].seat == "A-01"
        assert roster.title == "一班"

    def test_alias_headers_from_other_systems(self):
        """学籍系统导出的表头各不相同，认死一种会让"导入进来是空的"变成常态。"""
        text = "学号,学生,passcode,机位\n2026001,张三,135790,A-01\n"
        roster, notes = Roster.from_csv_text(text)
        assert notes == []
        assert roster.contestants[0].account == "2026001"
        assert roster.contestants[0].passcode == "135790"

    def test_accounts_are_normalised(self):
        roster, _ = Roster.from_rows([{"账号": " ZhangSan ", "姓名": "张三"}])
        assert roster.contestants[0].account == "zhangsan"

    def test_duplicates_are_dropped_and_reported(self):
        text = "账号,姓名\n2026001,张三\n2026001,张三丰\n"
        roster, notes = Roster.from_csv_text(text)
        assert len(roster) == 1
        assert roster.contestants[0].name == "张三"
        assert any("重复" in note for note in notes)

    def test_a_case_only_duplicate_still_counts_as_a_duplicate(self):
        """ZhangSan 与 zhangsan 是同一个人 —— 否则名单里会出现两个看起来一样的账号。"""
        roster, notes = Roster.from_rows([{"账号": "ZhangSan"},
                                          {"账号": "zhangsan"}])
        assert len(roster) == 1
        assert notes

    def test_a_row_without_an_account_is_reported(self):
        text = "账号,姓名\n,张三\n"
        roster, notes = Roster.from_csv_text(text)
        assert len(roster) == 0
        assert any("没有账号" in note for note in notes)

    def test_utterly_blank_rows_are_silently_skipped(self):
        """Excel 拖出来的空行是常态，为它报一堆错只会淹没真正的问题。"""
        text = "账号,姓名\n,张三\n,\n"
        roster, notes = Roster.from_csv_text(text)
        assert len(roster) == 0
        assert len(notes) == 1

    def test_an_overlong_account_is_reported(self):
        roster, notes = Roster.from_rows([{"账号": "a" * 40}])
        assert len(roster) == 0
        assert any("太长" in note for note in notes)

    def test_an_overlong_passcode_is_truncated_not_dropped(self):
        roster, notes = Roster.from_rows([{"账号": "a1", "密码": "1" * 90}])
        assert len(roster) == 1
        assert len(roster.contestants[0].passcode) == 32
        assert any("截断" in note for note in notes)

    def test_a_file_without_a_header_explains_itself(self):
        roster, notes = Roster.from_csv_text("")
        assert len(roster) == 0
        assert notes and "表头" in notes[0]

    def test_loading_from_disk_uses_the_file_name_as_title(self, tmp_path):
        file = tmp_path / "高一3班.csv"
        file.write_text("账号,姓名\n2026001,张三\n", encoding="utf-8")
        roster, _ = Roster.load_csv(file)
        assert roster.title == "高一3班"

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(RosterError):
            Roster.load_csv(tmp_path / "没有这个.csv")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


class TestExport:
    def test_the_header_is_fixed(self):
        """导入认多种写法，**导出只写一种** —— 导出再导入必须原样回来。"""
        assert CSV_HEADERS == ("账号", "姓名", "密码", "座位")

    def test_a_csv_saved_to_disk_carries_a_bom(self, tmp_path):
        """没有 BOM 的话，Excel 双击打开就是乱码。"""
        target = a_roster().save_csv(tmp_path / "名单.csv")
        assert target.read_bytes().startswith("\ufeff".encode("utf-8"))

    def test_a_round_trip_keeps_everything(self, tmp_path):
        target = a_roster().save_csv(tmp_path / "名单.csv")
        back, notes = Roster.load_csv(target)
        assert notes == []
        assert back.accounts() == ["2026001", "2026002"]
        assert back.passcodes() == {"2026001": "135790", "2026002": "246810"}
        assert back.contestants[0].seat == "A-01"

    def test_it_writes_the_name_not_the_account(self):
        rows = a_roster().to_rows()
        assert rows[0]["姓名"] == "张三"
        assert rows[0]["密码"] == "135790"


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_a_json_round_trip(self, tmp_path):
        target = a_roster().save(tmp_path / "名单.json")
        raw = json.loads(target.read_text(encoding="utf-8"))
        assert raw["version"] == ROSTER_VERSION

        back = Roster.load(target)
        assert back.title == "高一(3)班"
        assert back.accounts() == ["2026001", "2026002"]
        assert back.contestants[1].name == "李四"

    def test_nothing_is_left_behind_on_disk(self, tmp_path):
        """原子替换：写完不该剩下 .tmp。剩下的那个文件会被下次误读。"""
        target = a_roster().save(tmp_path / "名单.json")
        assert target.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_a_missing_file_is_an_empty_roster(self, tmp_path):
        """第一次打开界面时没有名单是常态，不该弹错。"""
        assert len(Roster.load(tmp_path / "还没有.json")) == 0

    def test_a_corrupt_file_is_loud(self, tmp_path):
        """坏文件与空名单必须分得开 —— 否则老师会以为名单没了，重新导一遍。"""
        target = tmp_path / "坏.json"
        target.write_text("{ 这不是 json", encoding="utf-8")
        with pytest.raises(RosterError):
            Roster.load(target)

    def test_a_hand_written_json_with_missing_fields_still_reads(self):
        """老师手写的 JSON 少一列不算错，缺什么取默认。"""
        roster = Roster.from_dict({"contestants": [{"account": "a1"}]})
        assert roster.contestants[0].account == "a1"
        assert roster.contestants[0].name == ""
        assert roster.contestants[0].display_name() == "a1"

    def test_unknown_fields_are_ignored(self):
        roster = Roster.from_dict({"contestants": [{"account": "a1", "未来字段": 1}]})
        assert len(roster) == 1

    def test_garbage_reads_as_an_empty_roster(self):
        assert len(Roster.from_dict("不是字典")) == 0
        assert len(Roster.from_dict({"contestants": "也不是列表"})) == 0

    def test_duplicates_in_the_file_are_collapsed(self):
        roster = Roster.from_dict({"contestants": [{"account": "a1"},
                                                   {"account": "A1"}]})
        assert len(roster) == 1

    def test_saving_into_a_missing_directory_creates_it(self, tmp_path):
        target = a_roster().save(tmp_path / "深" / "一层" / "名单.json")
        assert target.exists()


class TestDisplayName:
    def test_a_missing_name_falls_back_to_the_account(self):
        assert Contestant(account="2026001").display_name() == "2026001"

    def test_a_real_name_wins(self):
        assert Contestant(account="2026001", name="张三").display_name() == "张三"


# ---------------------------------------------------------------------------
# 增删改
# ---------------------------------------------------------------------------


class TestEditing:
    def test_adding_the_same_account_updates_instead_of_duplicating(self):
        roster = a_roster()
        roster.add(person("2026001", name="张三丰", seat="B-09"))
        assert len(roster) == 2
        item = roster.by_account("2026001")
        assert item.name == "张三丰"
        assert item.seat == "B-09"

    def test_an_empty_update_keeps_what_was_there(self):
        """界面上的"编辑"对话框只改了座位，别把口令顺手清掉。"""
        roster = a_roster()
        roster.add(Contestant(account="2026001", seat="C-03"))
        assert roster.by_account("2026001").passcode == "135790"

    def test_a_blank_account_is_refused(self):
        with pytest.raises(RosterError):
            a_roster().add(Contestant(account="  "))

    def test_removing_is_reported(self):
        roster = a_roster()
        assert roster.remove("2026001") is True
        assert roster.remove("2026001") is False
        assert roster.accounts() == ["2026002"]

    def test_filling_missing_passcodes_only_fills_the_missing(self):
        roster = Roster(contestants=[
            Contestant(account="a1"), Contestant(account="a2", passcode="999999")])
        assert roster.fill_missing_passcodes() == 1
        assert roster.by_account("a2").passcode == "999999"
        assert len(roster.by_account("a1").passcode) == 6

    def test_binding_remembers_the_last_machine(self):
        roster = a_roster()
        assert roster.bind("2026001", "aaaa1111") is True
        assert roster.bind("2026001", "aaaa1111") is False
        assert roster.by_device("aaaa1111").account == "2026001"
        assert roster.unbind("2026001") is True
        assert roster.by_device("aaaa1111") is None


class TestVerify:
    def test_the_right_passcode_lets_you_in(self):
        assert a_roster().verify("2026001", "135790") == (True, "")

    def test_the_wrong_passcode_does_not(self):
        ok, why = a_roster().verify("2026001", "000000")
        assert not ok and why

    def test_an_unknown_account_says_so(self):
        ok, why = a_roster().verify("9999999", "135790")
        assert not ok and "名单" in why

    def test_an_empty_personal_passcode_means_account_only(self):
        roster = Roster(contestants=[Contestant(account="a1")])
        assert roster.verify("a1", "") == (True, "")

    def test_surrounding_whitespace_in_the_typed_passcode_is_forgiven(self):
        """从聊天软件复制口令时带上一两个空格是常态。"""
        assert a_roster().verify("2026001", " 135790 ")[0] is True

    def test_the_case_of_the_passcode_matters(self):
        """口令的熵全靠字符本身，不做大小写折叠。"""
        roster = Roster(contestants=[Contestant(account="a1", passcode="AbC123")])
        assert roster.verify("a1", "abc123")[0] is False


# ---------------------------------------------------------------------------
# 与网络层的一致性（这组用例是本文件存在的真正理由）
# ---------------------------------------------------------------------------


class TestAgreementWithTheNetworkLayer:
    def test_the_normaliser_is_literally_the_same_function(self):
        """禁止各写一份：不一致的现象是"名单上明明有你、密码也对，就是进不去"。"""
        assert roster_mod.normalize_account is session_mod.normalize_account

    @pytest.mark.parametrize("typed", [
        " ZhangSan ", "zhangsan", "ZHANGSAN", "张　三", "２０２６００１",
        "a\tb\n", "", "   ", "2026001",
    ])
    def test_a_hand_typed_account_normalises_the_same_on_both_sides(self, typed):
        assert normalize_account(typed) == session_mod.normalize_account(typed)

    def test_an_index_built_from_the_roster_is_the_one_the_session_looks_up(self):
        """名单算出的索引 → 会话必须反查得到人。这是"能进场"的唯一条件。"""
        roster = a_roster()
        session = ExamSession("s1", entry_mode=EntryMode.ACCOUNT)
        session.set_contestants([item.to_dict() for item in roster])

        for index, account in roster.credential_index().items():
            assert session.credential_of(index) is not None
            assert session.contestant(account) is not None

    def test_what_a_student_actually_types_reaches_the_contestant(self):
        """端到端：学生照着纸条打「大写+空格」的账号，也要落到名单那一行。"""
        roster = a_roster()
        session = ExamSession("s1", entry_mode=EntryMode.ACCOUNT)
        session.set_contestants([item.to_dict() for item in roster])

        index = credential_id(" 2026001 ", "135790")
        assert session.credential_of(index) is not None
        target = session.contestant(session.credential_index()[index])
        assert target["name"] == "张三"

    def test_a_wrong_passcode_produces_an_index_nobody_owns(self):
        session = ExamSession("s1", entry_mode=EntryMode.ACCOUNT)
        session.set_contestants([item.to_dict() for item in a_roster()])
        assert session.credential_of(credential_id("2026001", "000000")) is None

    def test_a_room_wide_passcode_is_folded_in(self):
        """"账号 + 全场口令"两个都对才推导得出同一把钥匙。"""
        roster = a_roster()
        session = ExamSession("s1", password="kaochang",
                              entry_mode=EntryMode.ACCOUNT)
        session.set_contestants([item.to_dict() for item in roster])

        assert session.credential_of(credential_id("2026001", "135790", "kaochang"))
        assert session.credential_of(credential_id("2026001", "135790")) is None
        assert session.credential_of(
            credential_id("2026001", "135790", "别的口令")) is None

    def test_the_room_code_path_is_untouched(self):
        """账号那套不许把老路径带坏：默认方式下索引仍是房间秘密的哈希。"""
        session = ExamSession("s1", room_code="123456", password="kaochang")
        assert session.credential_index() == {}
        assert session.credential_of(session.fingerprint) == session.secret
        assert session.credential_of("别的索引") is None


class TestNotice:
    def test_the_plaintext_warning_exists(self):
        """名单里是明文口令这件事必须有一句能显示的文案。"""
        assert "明文" in PASSCODE_NOTICE


# ---------------------------------------------------------------------------
# 目录规约
# ---------------------------------------------------------------------------


class TestPaths:
    def test_the_roster_directory_sits_under_the_data_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        paths = build_paths()
        assert paths.rosters_dir == tmp_path / "rosters"

    def test_it_is_created_with_the_rest_of_the_layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        build_paths().ensure_layout()
        assert (tmp_path / "rosters").is_dir()

    def test_it_is_not_the_same_directory_as_the_exam_archive(self, tmp_path, monkeypatch):
        """名单是"备战时准备的花名册"，档案是"考完留下的卷子"，清一个不该动另一个。"""
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        paths = build_paths()
        assert paths.rosters_dir != paths.exams_dir

    def test_a_name_with_reserved_characters_still_lands_somewhere_sane(self,
                                                                       tmp_path,
                                                                       monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        target = build_paths().roster_file("高一<3>班:2026")
        assert target.parent.name == "rosters"
        assert target.suffix == ".json"
        assert not set(target.stem) & set('<>:"/\\|?*')

    def test_an_empty_name_does_not_produce_a_nameless_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        assert build_paths().roster_file("   ").name == "名单.json"

    def test_listing_is_empty_before_anything_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        assert build_paths().roster_files() == []

    def test_listing_finds_what_was_saved_and_is_stable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        paths = build_paths().ensure_layout()
        for name in ("二班", "一班", "三班"):
            a_roster().save(paths.roster_file(name))
        # 临时文件不该混进列表：它下一瞬间就不存在了
        (paths.rosters_dir / "半截.json.tmp").write_text("{}", encoding="utf-8")
        assert [item.stem for item in paths.roster_files()] == ["一班", "三班", "二班"]

    def test_what_the_path_helper_produces_is_what_roster_loads(self,
                                                                tmp_path,
                                                                monkeypatch):
        """两处拼文件名的方式必须一致，否则"存了却找不到"。"""
        monkeypatch.setenv("OFFLINE_OJ_HOME", str(tmp_path))
        from offline_oj.paths import build_paths
        paths = build_paths().ensure_layout()
        a_roster().save(paths.roster_file("高一(3)班"))
        back = Roster.load(paths.roster_file("高一(3)班"))
        assert back.accounts() == ["2026001", "2026002"]
