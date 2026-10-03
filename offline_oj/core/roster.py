"""选手名单：账号、姓名、个人口令、座位，以及"这一场谁用哪台机器"。

名单是**老师一个人的东西**，存在老师那台机器上：

```
%LOCALAPPDATA%\\OfflineOJ\\rosters\\<名单名>.json
```

它解决三个现场问题：

1. **账号进场**（:class:`offline_oj.net.session.EntryMode`）—— 学生输账号+口令，
   主机反查到名单上那一行，于是"谁是谁"不由学生自己说了算；
2. **绑定选手信息** —— 名单上的姓名、座位跟着成绩单走，不必再让学生手打名字；
3. **口令分发** —— 老师导入名单时批量生成口令，导出成一张待打印的纸条。

关于**明文口令**
----------------
``<名单名>.json`` 与导出的 CSV 里，个人口令是**明文**的。这是刻意取舍：
老师需要在考前一天把口令打印出来发给学生，也需要在有人忘带时当场念给他听。
做成只存哈希，这两件事就都做不了了。

代价是这份文件与一份记分册同级敏感。所以：

* 它**只落在老师那台机器**上，从不随任何帧上线（线协议里只有单向索引）；
* 界面上明写"这份文件里含明文口令，请按学校的数据管理要求保管"；
* 真要更强，办法是**每人一个长口令**而不是把文件加密 —— 文件加密挡不住
  能读到这台机器的人，而口令长度是实打实的。

为什么这一层不 import ``net/``
-------------------------------
``core/records.py`` 已经立过这条规矩，这里照办。但"账号怎么算同一个账号"
这条规则**必须与网络层逐字节一致**（不一致的现象是"名单上明明有你、密码也对，
就是进不去"），所以它落在 :mod:`offline_oj.credentials` 里，两边都 import 那一份。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..credentials import (ACCOUNT_MAX_LENGTH, PASSCODE_MAX_LENGTH,
                           credential_id, normalize_account,
                           normalize_passcode)

log = logging.getLogger(__name__)

__all__ = [
    "ROSTER_VERSION", "ROSTER_SUFFIX", "PASSCODE_NOTICE",
    "DEFAULT_PASSCODE_LENGTH", "ACCOUNT_COLUMNS", "CSV_HEADERS",
    "RosterError", "Contestant", "Roster",
    "generate_passcode", "safe_roster_name", "read_csv_text",
]

#: 名单文件格式版本。读旧文件一律"未知字段忽略、缺字段取默认"，
#: 所以只有语义真的变了才需要升它。
ROSTER_VERSION = 1

ROSTER_SUFFIX = ".json"

#: 生成口令的长度。**6 位纯数字**：学生要照着纸条在键盘上敲一遍，
#: 数字键比字母好找；6 位 × 10 种 = 100 万种，配上主机的握手限速
#: （``server.py`` 每个 IP 每个窗口只许几次握手）足够挡住现场乱试。
DEFAULT_PASSCODE_LENGTH = 6

#: 名单里含明文口令这件事，**文案只放这一处**。界面、导出提示、关于页
#: 各写一份的话，迟早有一处说"已加密"，而实际没有。
PASSCODE_NOTICE = "这份名单里含明文个人口令，请按学校的数据管理要求保管。"

#: 账号列在 CSV 里的几种常见写法。中文 Excel、手打表、从学籍系统导出的表
#: 用的表头都不一样，硬认一种会让"导入进来是空的"变成常态。
ACCOUNT_COLUMNS = ("账号", "帐号", "account", "学号", "考号", "用户名")
NAME_COLUMNS = ("姓名", "名字", "name", "学生", "选手")
PASSCODE_COLUMNS = ("密码", "口令", "passcode", "password", "个人密码")
SEAT_COLUMNS = ("座位", "座位号", "seat", "机位", "位置")
DEVICE_COLUMNS = ("设备", "设备id", "device_id", "机器")
NOTE_COLUMNS = ("备注", "note", "说明")

#: 导出 CSV 时的固定表头。导入认多种写法，**导出只写一种** ——
#: 导出再导入必须原样回来，那种地方不该有"看运气"的成分。
CSV_HEADERS = ("账号", "姓名", "密码", "座位")

#: 名单名里不能出现的字符（Windows 保留字符 + 控制字符），与档案目录名同源。
_UNSAFE_FILE = '[]:*?/\\|<>"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e\x0f'


class RosterError(Exception):
    """名单读不出来 / 存不下去。文案直接给用户看。"""


def generate_passcode(length: int = DEFAULT_PASSCODE_LENGTH) -> str:
    """生成一条个人口令：纯数字，首位不为 0（照着纸条念不会漏掉前导零）。"""
    length = max(4, min(12, int(length)))
    head = secrets.choice("123456789")
    tail = "".join(secrets.choice("0123456789") for _ in range(length - 1))
    return head + tail


def safe_roster_name(name: str) -> str:
    """把名单名变成能当文件名的样子。空则回落到"名单"。"""
    cleaned = "".join("" if char in _UNSAFE_FILE else char
                      for char in " ".join(str(name or "").split()))
    return cleaned[:40] or "名单"


def read_csv_text(data: bytes) -> str:
    """把 CSV 字节解成文本，**认 BOM 也认 GBK**。

    中文 Windows 的 Excel"另存为 CSV"默认写 GBK，而学生名册多半就是从 Excel
    出来的。只按 UTF-8 解会得到一串乱码名字，而且**不报错** —— 乱码的姓名
    照样能存进名单、照样能进场，只是榜上写的是一堆问号。所以这里按序试：
    UTF-8（带不带 BOM 都吃）→ GBK → UTF-8 宽松模式兜底（宁可替换几个字，
    也不要整个导入失败）。
    """
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


@dataclass
class Contestant:
    """名单里的一个人。

    字段全部有默认值 —— 手工在 JSON 里写一个只有账号的名单也该能读进来。
    """

    #: 归一化之后的账号（唯一键）。导入时归一化，之后一律用它比对。
    account: str = ""
    #: 显示用的姓名。空的就回落到账号。
    name: str = ""
    #: 个人口令。**明文** —— 理由见模块开头。
    passcode: str = ""
    #: 座位号，纯显示用（成绩单上多一列，方便老师按座位找人对卷）。
    seat: str = ""
    #: 上一场用过哪台机器。**只是提示**，不是本场的绑定：本场的绑定在
    #: ``ExamSession.bindings`` 里，开一场新的就重新绑。
    #: 留在这里是为了让老师能在名单上看到"这个人的机器换了"。
    device_id: str = ""
    note: str = ""

    def display_name(self) -> str:
        return self.name or self.account

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "name": self.name,
            "passcode": self.passcode,
            "seat": self.seat,
            "device_id": self.device_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Contestant":
        if not isinstance(data, dict):
            return cls()
        return cls(
            account=normalize_account(data.get("account", "")),
            name=" ".join(str(data.get("name", "") or "").split()),
            passcode=normalize_passcode(data.get("passcode", "")),
            seat=str(data.get("seat", "") or "").strip(),
            device_id=str(data.get("device_id", "") or "").strip().upper(),
            note=str(data.get("note", "") or "").strip(),
        )

    def to_row(self) -> dict[str, str]:
        """导出成 CSV 的一行（表头固定，见 :data:`CSV_HEADERS`）。"""
        return {"账号": self.account, "姓名": self.name,
                "密码": self.passcode, "座位": self.seat}


def _pick(row: Mapping[str, Any], names: Sequence[str]) -> str:
    """在一行里按几种可能的表头找值。**大小写与首尾空白都不计较。**"""
    folded = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in folded:
            return str(folded[name.lower()] or "")
    return ""


@dataclass
class Roster:
    """一整份名单。"""

    title: str = "名单"
    contestants: list[Contestant] = field(default_factory=list)
    saved_at: str = ""

    # ---- 查询 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.contestants)

    def __iter__(self):
        return iter(self.contestants)

    def by_account(self, account: str) -> Contestant | None:
        key = normalize_account(account)
        if not key:
            return None
        return next((item for item in self.contestants
                     if item.account == key), None)

    def by_device(self, device_id: str) -> Contestant | None:
        key = str(device_id or "").strip().upper()
        if not key:
            return None
        return next((item for item in self.contestants
                     if item.device_id == key), None)

    def names(self) -> list[str]:
        return [item.display_name() for item in self.contestants]

    def accounts(self) -> list[str]:
        return [item.account for item in self.contestants]

    def passcodes(self) -> dict[str, str]:
        """账号 → 口令。**只在两种地方用**：写进名单文件、导出打印纸条。"""
        return {item.account: item.passcode for item in self.contestants}

    def credential_index(self, room_password: str = "") -> dict[str, str]:
        """账号进场用的"索引 → 账号"表，交给主机的 ``ExamSession``。

        与 :meth:`offline_oj.net.session.ExamSession.credential_index` 算的是
        同一件事 —— 那一份由会话现算（它要用本场当时的口令），这一份给界面
        与测试用来核对"导入的这份名单能不能被认出来"。
        """
        return {credential_id(item.account, item.passcode, room_password): item.account
                for item in self.contestants}

    def verify(self, account: str, passcode: str) -> tuple[bool, str]:
        """核对账号口令，返回 ``(是否通过, 说明)``。说明直接给用户看。"""
        item = self.by_account(account)
        if item is None:
            return False, "账号不在名单里"
        if not item.passcode:
            return True, ""          # 个人口令留空 = 这个人只要账号就能进
        if normalize_passcode(passcode) == item.passcode:
            return True, ""
        return False, "口令不对"

    # ---- 增删改 -------------------------------------------------------------

    def add(self, contestant: Contestant) -> Contestant:
        """加入一个人。账号已存在时就地更新（"修正"而不是"再来一个"）。"""
        item = Contestant.from_dict(contestant.to_dict())
        existing = self.by_account(item.account)
        if existing is not None:
            existing.name = item.name or existing.name
            existing.passcode = item.passcode or existing.passcode
            existing.seat = item.seat or existing.seat
            existing.note = item.note or existing.note
            return existing
        if not item.account:
            raise RosterError("账号不能为空")
        self.contestants.append(item)
        return item

    def remove(self, account: str) -> bool:
        key = normalize_account(account)
        before = len(self.contestants)
        self.contestants = [item for item in self.contestants
                            if item.account != key]
        return len(self.contestants) != before

    def bind(self, account: str, device_id: str) -> bool:
        """记下"这个人上一场用的是哪台机器"。返回是否真的改了。"""
        item = self.by_account(account)
        if item is None:
            return False
        key = str(device_id or "").strip().upper()
        if item.device_id == key:
            return False
        item.device_id = key
        return True

    def unbind(self, account: str) -> bool:
        item = self.by_account(account)
        if item is None or not item.device_id:
            return False
        item.device_id = ""
        return True

    def fill_missing_passcodes(self, length: int = DEFAULT_PASSCODE_LENGTH) -> int:
        """给还没口令的人补生成，返回补了几条。"""
        filled = 0
        for item in self.contestants:
            if not item.passcode:
                item.passcode = generate_passcode(length)
                filled += 1
        return filled

    # ---- 导入导出 -----------------------------------------------------------

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]],
                  *, title: str = "名单") -> tuple["Roster", list[str]]:
        """从若干行构造名单，**并回报被丢掉的行**。

        导入是老师按下按钮就要看到结果的操作，不能"静默少收两个人" ——
        少的那个人往往要到考完试、发现成绩单上没他才暴露出来。
        所以返回值里带上每一条丢弃的原因，界面照它逐条显示。

        认多种表头（见 :data:`ACCOUNT_COLUMNS` 那一组常量），
        这样中文 Excel、手打表、学籍系统导出的表都能直接拖进来。
        """
        roster = cls(title=safe_roster_name(title))
        notes: list[str] = []
        seen: dict[str, int] = {}
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                notes.append(f"第 {index} 行不是一行表格，已跳过")
                continue
            account = normalize_account(_pick(row, ACCOUNT_COLUMNS))
            if not account:
                if any(str(value or "").strip() for value in row.values()):
                    notes.append(f"第 {index} 行没有账号，已跳过")
                continue
            if len(account) > ACCOUNT_MAX_LENGTH:
                notes.append(f"第 {index} 行账号太长（超过 {ACCOUNT_MAX_LENGTH} 字），已跳过")
                continue
            if account in seen:
                notes.append(f"第 {index} 行账号「{account}」与第 {seen[account]} 行重复，已跳过")
                continue
            passcode = normalize_passcode(_pick(row, PASSCODE_COLUMNS))
            if len(passcode) > PASSCODE_MAX_LENGTH:
                notes.append(f"第 {index} 行口令太长（超过 {PASSCODE_MAX_LENGTH} 字），已截断")
                passcode = passcode[:PASSCODE_MAX_LENGTH]
            seen[account] = index
            roster.contestants.append(Contestant(
                account=account,
                name=" ".join(_pick(row, NAME_COLUMNS).split()),
                passcode=passcode,
                seat=_pick(row, SEAT_COLUMNS).strip(),
                device_id=_pick(row, DEVICE_COLUMNS).strip().upper(),
                note=_pick(row, NOTE_COLUMNS).strip(),
            ))
        return roster, notes

    @classmethod
    def from_csv_text(cls, text: str, *, title: str = "名单"
                      ) -> tuple["Roster", list[str]]:
        """从 CSV 文本构造。空行交给 :meth:`from_rows` 统一判定丢弃原因。"""
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            return cls(title=safe_roster_name(title)), ["文件里没有表头，读不出任何一列"]
        return cls.from_rows(list(reader), title=title)

    @classmethod
    def load_csv(cls, path: str | os.PathLike[str],
                 *, title: str = "") -> tuple["Roster", list[str]]:
        """从 CSV 文件读。编码按 :func:`read_csv_text` 的规则试。"""
        file = Path(path)
        try:
            raw = file.read_bytes()
        except OSError as exc:
            raise RosterError(f"读不了这个文件：{exc}") from exc
        return cls.from_csv_text(read_csv_text(raw),
                                 title=title or file.stem)

    def to_rows(self) -> list[dict[str, str]]:
        return [item.to_row() for item in self.contestants]

    def to_csv_text(self) -> str:
        """导出成 CSV 文本。**带 BOM**，否则 Excel 打开是乱码。"""
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(CSV_HEADERS))
        writer.writeheader()
        for item in self.contestants:
            writer.writerow(item.to_row())
        return buffer.getvalue()

    def save_csv(self, path: str | os.PathLike[str]) -> Path:
        """把名单（含明文口令）导成 CSV，供打印或转交。原子替换写。"""
        target = Path(path)
        _atomic_write_text(target, "\ufeff" + self.to_csv_text())
        return target

    # ---- 落盘 ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": ROSTER_VERSION,
            "title": self.title,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "contestants": [item.to_dict() for item in self.contestants],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Roster":
        """从 JSON 构造。**任何不合法的地方都退回默认值，不抛异常。**
        名单文件被手工改坏、被同步工具截断都是可能的，读不动时最合理的处置是
        "当作空名单，让老师重新导入"，而不是让程序起不来。
        """
        if not isinstance(data, dict):
            return cls()
        rows = data.get("contestants")
        roster = cls(title=str(data.get("title", "") or "名单"),
                     saved_at=str(data.get("saved_at", "") or ""))
        if not isinstance(rows, list):
            return roster
        seen: set[str] = set()
        for row in rows:
            item = Contestant.from_dict(row)
            if not item.account or item.account in seen:
                continue
            seen.add(item.account)
            roster.contestants.append(item)
        return roster

    def save(self, path: str | os.PathLike[str]) -> Path:
        """写盘。**原子替换**（先写 ``.tmp`` 再 ``os.replace``）。"""
        target = Path(path)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        _atomic_write_text(target, payload + "\n")
        self.saved_at = self.to_dict()["saved_at"]
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Roster":
        """读盘。文件不存在返回空名单；内容坏了抛 :class:`RosterError`。

        与 :meth:`from_dict` 的分工：``from_dict`` 对**内容**宽容（老师手写的
        JSON 少一列不算错），``load`` 对**文件**诚实（打不开要说出来，
        不能让老师以为"这份名单是空的"而重新导入一遍、把已绑定的机器弄丢）。
        """
        file = Path(path)
        if not file.exists():
            return cls()
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RosterError(f"名单文件读不出来：{exc}") from exc
        roster = cls.from_dict(raw)
        if not roster.title or roster.title == "名单":
            roster.title = file.stem
        return roster

    @classmethod
    def default_file(cls, name: str, directory: str | os.PathLike[str]) -> Path:
        return Path(directory) / (safe_roster_name(name) + ROSTER_SUFFIX)


def _atomic_write_text(target: Path, text: str) -> None:
    """先写 ``.tmp`` 再改名。

    与题库、设备标识同一套做法：半截文件比没文件更糟 —— 名单少了一半，
    老师不会发现，直到考完试有人举手说"榜上没有我"。
    """
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RosterError(f"写不了这个文件：{exc}") from exc
