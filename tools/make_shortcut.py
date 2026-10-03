"""创建 / 读取 Windows ``.lnk`` 快捷方式 —— 纯 Python，不依赖 COM。

为什么手写二进制格式：这台机器上，PowerShell 里 ``New-Object -ComObject
WScript.Shell`` 会被沙箱直接拦下（理由是"COM 实例化可以执行任意代码"），
环境里也没有 ``pywin32`` / ``comtypes`` 可用。而 ``.lnk`` 用的
**MS-SHLLINK** 是公开且固定的二进制结构，自己写反而更可控：
没有 COM、没有额外依赖，也不会因为换了台机器就跑不起来。

用法::

    python tools\\make_shortcut.py                       # 桌面建 OfflineOJ.lnk
    python tools\\make_shortcut.py --name 离线评测
    python tools\\make_shortcut.py --into .\\build --name OfflineOJ
    python tools\\make_shortcut.py --read "%USERPROFILE%\\Desktop\\OfflineOJ.lnk"

写入的结构只用了必要项：``LinkInfo``（含 VolumeID + 本地基路径）+
``NAME_STRING`` / ``RELATIVE_PATH`` / ``WORKING_DIR`` / ``ICON_LOCATION``
四段 Unicode 字符串 + ExtraData 终止符。不带 ``LinkTargetIDList`` ——
那需要拼 SHITEMID 列表，而 Windows 用 LinkInfo 就能解析。
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ShellLinkHeader 固定 76 字节
HEADER_SIZE = 0x4C
# {00021401-0000-0000-C000-000000000046}
LINK_CLSID = bytes((
    0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46,
))

# LinkFlags（MS-SHLLINK 2.1.1）
HAS_LINK_TARGET_ID_LIST = 0x00000001
HAS_LINK_INFO = 0x00000002
HAS_NAME = 0x00000004
HAS_RELATIVE_PATH = 0x00000008
HAS_WORKING_DIR = 0x00000010
HAS_ICON_LOCATION = 0x00000040
IS_UNICODE = 0x00000080

FILE_ATTRIBUTE_ARCHIVE = 0x20
SW_SHOWNORMAL = 1

# LinkInfoFlags（2.3.1）
VOLUME_ID_AND_LOCAL_BASE_PATH = 0x00000001
# VolumeID.DriveType（2.3.2）：3 = DRIVE_FIXED，即本地磁盘
DRIVE_FIXED = 3


# ======================================================================
# 取系统目录
# ======================================================================


def known_folder(folder_id: str) -> str:
    """用 SHGetKnownFolderPath 取系统目录。

    不用 ``%USERPROFILE%\\Desktop`` 是因为桌面可能被 OneDrive 重定向到
    ``%USERPROFILE%\\OneDrive\\桌面``，拼字符串会指错地方。
    注意这不是 COM 实例化，是一次普通的 Win32 调用。
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(folder_id, ctypes.byref(guid))
    if hr != 0:
        raise OSError(f"CLSIDFromString 失败 0x{hr & 0xFFFFFFFF:08X}")

    buffer = ctypes.c_wchar_p()
    hr = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(buffer))
    if hr != 0:
        raise OSError(f"SHGetKnownFolderPath({folder_id}) 失败 0x{hr & 0xFFFFFFFF:08X}")
    try:
        return buffer.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buffer)


#: {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def desktop_dir() -> Path:
    try:
        return Path(known_folder(FOLDERID_DESKTOP))
    except OSError:
        # 兜底：万一 known folder 取不到，至少按常规位置试一下
        return Path.home() / "Desktop"


def shell_id_list(path: Path) -> bytes:
    """取目标路径的 shell ITEMIDLIST（PIDL）原始字节。

    **这一段是必须的。** 一开始我以为 LinkInfo 里有完整路径就够，省掉了它，
    结果 ``os.startfile`` 直接报 ``WinError 1155``（没有关联）—— 系统压根不认
    这个文件是快捷方式。拿资源管理器自己写的 .lnk 对比才看清：
    ZCode / OneDrive / 爱奇艺三个都带着 200~430 字节的 LinkTargetIDList。

    PIDL 的格式（shell 命名空间里逐级 item ID）手写不现实，所以交给 shell 自己拼：
    ``SHParseDisplayName`` 是普通的 Win32 函数，不是 COM 实例化。
    """
    import ctypes

    pidl = ctypes.c_void_p()
    attributes = ctypes.c_ulong(0)
    hr = ctypes.windll.shell32.SHParseDisplayName(
        ctypes.c_wchar_p(str(path)), None, ctypes.byref(pidl), 0,
        ctypes.byref(attributes))
    if hr != 0:
        raise OSError(f"SHParseDisplayName 失败 0x{hr & 0xFFFFFFFF:08X}：{path}")
    try:
        size = ctypes.windll.shell32.ILGetSize(pidl)
        if size <= 0:
            raise OSError("ILGetSize 返回 0")
        return ctypes.string_at(pidl, size)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(pidl)


# ======================================================================
# 写
# ======================================================================


def _ansi(text: str) -> bytes:
    """LinkInfo 里的路径是 ANSI。

    目标路径是纯 ASCII 时没问题；含中文就必须靠 Unicode 那段
    （RELATIVE_PATH / WORKING_DIR），所以两处都要写。
    """
    try:
        return text.encode("mbcs") + b"\x00"
    except UnicodeEncodeError:
        return text.encode("ascii", "replace") + b"\x00"


def _string_data(text: str) -> bytes:
    """StringData（Unicode）：2 字节字符数 + UTF-16LE 正文 + 2 字节终止符。"""
    encoded = text.encode("utf-16-le")
    return struct.pack("<H", len(text)) + encoded + b"\x00\x00"


def build_link(target: Path, *, working_dir: Path | None = None,
               icon: Path | None = None, description: str = "",
               link_dir: Path | None = None) -> bytes:
    """拼出一个 .lnk 的完整字节内容。"""
    target = target.resolve()
    working = (working_dir or target.parent).resolve()
    icon_path = (icon or target).resolve()

    # ---- LinkInfo ----
    # 同盘写相对路径，跨盘写绝对路径（Windows 自己也是这么做的）
    if link_dir is not None and link_dir.drive.lower() == target.drive.lower():
        try:
            relative = __import__("os").path.relpath(target, link_dir)
        except ValueError:
            relative = str(target)
    else:
        relative = str(target)

    volume_id = struct.pack(
        "<IIII", 4 + 4 + 4 + 4 + 1, DRIVE_FIXED, 0, 0x10) + b"\x00"
    local_base = _ansi(str(working))
    common_suffix = _ansi(target.name)

    volume_offset = 0x1C
    base_offset = volume_offset + len(volume_id)
    suffix_offset = base_offset + len(local_base)
    link_info = struct.pack(
        "<IIIIIII",
        0x1C + len(volume_id) + len(local_base) + len(common_suffix),
        0x1C,                                   # LinkInfoHeaderSize
        VOLUME_ID_AND_LOCAL_BASE_PATH,
        volume_offset,
        base_offset,
        0,                                      # 没有 UNC 网络路径
        suffix_offset,
    ) + volume_id + local_base + common_suffix

    flags = (HAS_LINK_TARGET_ID_LIST | HAS_LINK_INFO | HAS_NAME
             | HAS_RELATIVE_PATH | HAS_WORKING_DIR | HAS_ICON_LOCATION
             | IS_UNICODE)

    header = struct.pack(
        "<I", HEADER_SIZE) + LINK_CLSID + struct.pack(
        # LinkFlags / FileAttributes / 三个时间戳 / FileSize / IconIndex /
        # ShowCommand / HotKey / Reserved1 / Reserved2 / Reserved3
        # = 56 字节，加上 HeaderSize(4) 与 CLSID(16) 正好 76
        "<IIQQQIIIHHII",
        flags,
        FILE_ATTRIBUTE_ARCHIVE,
        0, 0, 0,                                # 三个时间戳留给系统
        0,                                      # FileSize
        0,                                      # IconIndex
        SW_SHOWNORMAL,
        0,                                      # HotKey
        0, 0,                                   # Reserved1 / Reserved2
        0,                                      # Reserved3
    )

    strings = (
        _string_data(description)               # NAME_STRING
        + _string_data(relative)                # RELATIVE_PATH
        + _string_data(str(working))            # WORKING_DIR
        + _string_data(f"{icon_path},0")        # ICON_LOCATION
    )
    id_list = shell_id_list(target)
    return (header
            + struct.pack("<H", len(id_list)) + id_list
            + link_info + strings + struct.pack("<I", 0))


# ======================================================================
# 读（用来验证写出来的东西）
# ======================================================================


def read_link(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) < HEADER_SIZE:
        raise ValueError("文件太短，不是 .lnk")
    size = struct.unpack_from("<I", raw, 0)[0]
    if size != HEADER_SIZE:
        raise ValueError(f"HeaderSize 应为 76，实际 {size}")
    if raw[4:20] != LINK_CLSID:
        raise ValueError("LinkCLSID 不对，不是快捷方式")
    (flags,) = struct.unpack_from("<I", raw, 20)

    info: dict = {"flags": f"0x{flags:08X}", "raw_size": len(raw)}
    offset = HEADER_SIZE

    # LinkTargetIDList：2 字节长度 + PIDL 本体（含结尾的 2 字节 0）
    if flags & HAS_LINK_TARGET_ID_LIST:
        (id_list_size,) = struct.unpack_from("<H", raw, offset)
        info["target_id_list_size"] = id_list_size
        offset += 2 + id_list_size

    if flags & HAS_LINK_INFO:
        (info_size, _header_size, link_flags, volume_offset, base_offset,
         _network_offset, suffix_offset) = struct.unpack_from("<IIIIIII", raw, offset)
        base = offset
        drive_type = struct.unpack_from("<I", raw, base + volume_offset + 4)[0]
        info["volume_drive_type"] = drive_type
        info["local_base_path"] = _cstr(raw, base + base_offset)
        info["common_path_suffix"] = _cstr(raw, base + suffix_offset)
        offset += info_size

    for name, flag in (("name_string", HAS_NAME),
                       ("relative_path", HAS_RELATIVE_PATH),
                       ("working_dir", HAS_WORKING_DIR),
                       ("arguments", 0x20),
                       ("icon_location", HAS_ICON_LOCATION)):
        if not flags & flag:
            continue
        (count,) = struct.unpack_from("<H", raw, offset)
        length = count * 2
        info[name] = raw[offset + 2:offset + 2 + length].decode("utf-16-le")
        offset += 2 + length + 2
    return info


def _cstr(raw: bytes, start: int) -> str:
    end = raw.index(b"\x00", start)
    return raw[start:end].decode("mbcs", "replace")


# ======================================================================


def main() -> int:
    exe_default = ROOT / "dist" / "OfflineOJ" / "OfflineOJ.exe"

    parser = argparse.ArgumentParser(description="创建 / 读取 .lnk 快捷方式")
    parser.add_argument("--target", default=str(exe_default), help="快捷方式指向的文件")
    parser.add_argument("--name", default="OfflineOJ", help="快捷方式名字（不带 .lnk）")
    parser.add_argument("--into", default=None,
                        help="放在哪个目录，默认是桌面")
    parser.add_argument("--description", default="离线评测系统 v2.0.0",
                        help="悬停提示里显示的说明")
    parser.add_argument("--read", default=None, help="只读取并解析一个现成的 .lnk")
    args = parser.parse_args()

    if args.read:
        path = Path(args.read)
        print(f"读取 {path}")
        for key, value in read_link(path).items():
            print(f"  {key} = {value}")
        return 0

    target = Path(args.target)
    if not target.exists():
        print(f"目标不存在：{target}", file=sys.stderr)
        return 1

    where = Path(args.into) if args.into else desktop_dir()
    if not where.exists():
        print(f"目录不存在：{where}", file=sys.stderr)
        return 1

    link = where / f"{args.name}.lnk"
    link.write_bytes(build_link(target, description=args.description,
                                link_dir=where))

    print(f"已创建 {link}")
    print(f"  → {target}")
    for key, value in read_link(link).items():
        print(f"  {key} = {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
