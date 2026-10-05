"""``.lnk`` 写入器的回归测试。

这个工具是为了绕开"本机不许 COM 实例化"而手写二进制格式的，所以它的正确性
不能靠"我读得懂自己写的东西"来自证 —— 下面几条断言都对着**真实快捷方式**
量出来的事实：

1. **必须带 LinkTargetIDList**（flags 的 bit 0）。第一版没带，``os.startfile``
   直接报 ``WinError 1155``（没有关联）；拿资源管理器写的 Steam / OneDrive /
   爱奇艺三个 .lnk 一比才看到，它们都带着 200~430 字节的这段。
2. **``CountCharacters`` 不含结尾的 NUL。** 三个真实样本里
   ``'Steam'`` 是 5、``'D:\\Steam'`` 是 8 —— 都不含 NUL。
   这个写成含 NUL 也不会立刻报错，只会让后面几段字符串整体错位。
3. 头部固定 76 字节，CLSID 是 ``{00021401-...-000000000046}``。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.make_shortcut import (  # noqa: E402
    HAS_LINK_INFO,
    HAS_LINK_TARGET_ID_LIST,
    HAS_WORKING_DIR,
    HEADER_SIZE,
    IS_UNICODE,
    LINK_CLSID,
    _string_data,
    build_link,
    read_link,
)


@pytest.fixture
def target() -> Path:
    """拿解释器自己当目标 —— 一定存在，而且不依赖有没有打过包。"""
    return Path(sys.executable)


class TestHeader:
    def test_header_is_76_bytes_with_the_shortcut_clsid(self, target, tmp_path):
        raw = build_link(target, link_dir=tmp_path)
        assert struct.unpack_from("<I", raw, 0)[0] == HEADER_SIZE == 76
        assert raw[4:20] == LINK_CLSID

    def test_flags_include_the_target_id_list(self, target, tmp_path):
        """缺了这段，ShellExecute 报 1155，系统不认这个文件是快捷方式。"""
        raw = build_link(target, link_dir=tmp_path)
        (flags,) = struct.unpack_from("<I", raw, 20)
        for bit, name in ((HAS_LINK_TARGET_ID_LIST, "HasLinkTargetIDList"),
                          (HAS_LINK_INFO, "HasLinkInfo"),
                          (HAS_WORKING_DIR, "HasWorkingDir"),
                          (IS_UNICODE, "IsUnicode")):
            assert flags & bit, f"LinkFlags 少了 {name}（flags=0x{flags:08X}）"

    def test_target_id_list_is_present_and_terminated(self, target, tmp_path):
        raw = build_link(target, link_dir=tmp_path)
        (size,) = struct.unpack_from("<H", raw, HEADER_SIZE)
        assert size > 8, "PIDL 短得不像话，八成没从 shell 拿到真东西"
        # 结尾必须是 2 字节的 0，这是 IDList 的结束标志
        assert raw[HEADER_SIZE + 2 + size - 2:HEADER_SIZE + 2 + size] == b"\x00\x00"


class TestStringData:
    def test_count_excludes_the_terminator(self):
        """对着真实样本定的约定：'Steam' 的 CountCharacters 是 5，不是 6。"""
        blob = _string_data("Steam")
        (count,) = struct.unpack_from("<H", blob, 0)
        assert count == 5, "CountCharacters 应当是字符数，不含结尾的 NUL"
        assert blob[2:] == "Steam".encode("utf-16-le") + b"\x00\x00"

    def test_non_ascii_survives(self):
        blob = _string_data("离线评测")
        (count,) = struct.unpack_from("<H", blob, 0)
        assert count == 4
        assert blob[2:2 + count * 2].decode("utf-16-le") == "离线评测"

    def test_empty_string_is_allowed(self):
        assert _string_data("") == b"\x00\x00\x00\x00"


class TestRoundTrip:
    def test_read_back_matches_what_went_in(self, target, tmp_path):
        link = tmp_path / "回读.lnk"
        link.write_bytes(build_link(target, description="回归测试用",
                                    link_dir=tmp_path))
        info = read_link(link)
        assert info["name_string"] == "回归测试用"
        assert info["working_dir"] == str(target.parent)
        assert info["common_path_suffix"] == target.name
        assert info["local_base_path"] == str(target.parent)
        assert info["volume_drive_type"] == 3, "3 是 DRIVE_FIXED（本地磁盘）"
        assert info["icon_location"].endswith(",0")
        assert info["target_id_list_size"] > 8

    def test_relative_path_is_absolute_across_drives(self, target, tmp_path):
        """桌面在 C:、产物在 E: 时不可能有相对路径，要退回绝对路径。

        这正是本项目的实际情况（桌面 C:，dist 在 E:），
        真按相对路径写，资源管理器会解析不到目标。
        """
        link = tmp_path / "跨盘.lnk"
        link.write_bytes(build_link(target, link_dir=Path("D:/"), ))
        info = read_link(link)
        assert Path(info["relative_path"]).is_absolute() or \
            info["relative_path"] == str(target)


class TestRejectsJunk:
    def test_short_file_is_rejected(self, tmp_path):
        bad = tmp_path / "短.lnk"
        bad.write_bytes(b"MZ" + b"\x00" * 10)
        with pytest.raises(ValueError):
            read_link(bad)

    def test_wrong_header_size_is_rejected(self, tmp_path):
        bad = tmp_path / "伪.lnk"
        bad.write_bytes(struct.pack("<I", 0x4C) + b"\x00" * 200)
        with pytest.raises(ValueError, match="CLSID"):
            read_link(bad)
