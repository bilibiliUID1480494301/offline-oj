"""整场测验的档案：写盘、读回、列举。

一场局域网测验结束后，主机把手里的东西整份留下来：

```
%LOCALAPPDATA%\\OfflineOJ\\exams\\<20260919-173045>-<题目或测验名>\\
    session.json        场次元数据（标题/模式/策略/时间/参与者/题目快照）
    submissions.jsonl   全部提交，含源码与判定（一行一份，追加友好）
    leaderboard.json    收卷时的榜单快照
    archive.json        版本号 + 每个文件的校验和
```

**为什么源码放在 jsonl 里而不是一人一个文件**：一份新文件就要多一次落盘，
一次测验几百份提交就是几百次；而且半途失败会留下"有文件但没写完"的档案。
一行一份 JSON 只写一次、只 rename 一次，要么整份在、要么整份不在。
需要"每人一个源码文件"时由导出（``core/export.py``）现拆，那是另一件事。

**这一层不 import ``net/``。** 它只吃 dict、吐 dict —— 记录内容由
``ExamSession`` 那边自己序列化（见 ``ExamSession.archive_record()``）。
好处是可以在完全没有网络栈的情况下测它。

**档案与"本机练习流水"不是一回事**：后者是 ``submissions/history.jsonl``
（单人练习、上限 500 条、没有场次概念），两者刻意分目录存放。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

#: 档案格式版本。**必备字段** —— 将来改结构时靠它决定怎么读旧档案。
#: 读档一律"未知字段忽略、缺字段取默认"，所以只有语义变了才需要升版本。
ARCHIVE_VERSION = 1

MANIFEST_FILE = "archive.json"
SESSION_FILE = "session.json"
SUBMISSIONS_FILE = "submissions.jsonl"
LEADERBOARD_FILE = "leaderboard.json"

#: 正在写的那份档案的临时后缀。写完改名成正式目录，中途失败就留在原地。
PARTIAL_SUFFIX = ".partial"

#: 档案含学生源码的隐私提示。**文案只放这一处** —— 列表页、导出对话框、
#: 关于页要是各写一份，迟早会有一处说"只存成绩"，而实际存了源码。
SOURCE_NOTICE = "这份档案包含全部学生源码，请按学校的数据管理要求保管。"

#: 目录名里不允许出现的字符（Windows 保留字符 + 控制字符）
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
#: 目录名里"看起来像空白"的一律并成一个横线
_SPACES = re.compile(r"[\s\u3000]+")
#: 目录名的可读部分最长多少个字符（太长 + 满路径会顶到 MAX_PATH）
SLUG_LIMIT = 40


class ArchiveError(Exception):
    """档案读不出来。文案直接给用户看。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SessionRecord:
    """``session.json``：这一场是什么、都有谁、考了哪几题。

    字段全部有默认值，读档时缺哪个都不炸 —— 旧版本写的档案没有新字段是常态。
    """

    session_id: str = ""
    title: str = ""
    #: ``RoomMode`` 的值（"practice" / "exam"）。**显示名由界面按枚举取**，
    #: 这里不存 label —— 存了就有两份真相，改文案时必漏一处。
    mode: str = "exam"
    #: 房间秘密的**单向索引**（``sha256(房间号[:口令])[:16]``）。
    #: 刻意不存房间号本身：老师不需要靠它认档案，而文件名与档案里散落凭据
    #: 只会让学生机被翻看时多一条线索。
    room_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    archived_at: str = ""
    duration_minutes: int = 0
    grace_seconds: int = 30
    force_collect: bool = False
    allow_resubmit: bool = True
    show_leaderboard: bool = True
    #: ``ExamPolicy.to_dict()``（默认策略是 ``{}``）
    policy: dict[str, Any] = field(default_factory=dict)
    #: 题目**快照**（``ExamProblemView.to_dict()``）。存快照而不是只存 ID：
    #: 题目日后被改动或删掉时，档案还得说得出当时学生看到的是什么。
    #: 注意视图里**只有样例**，正式测试点的期望输出从来不在这条链上。
    problems: list[dict[str, Any]] = field(default_factory=list)
    participants: list[dict[str, Any]] = field(default_factory=list)
    max_total_score: int = 0
    submission_count: int = 0
    #: 这份档案里到底有没有源码。列表页据此标出来，别让人以为"没源码 = 坏了"。
    keep_code: bool = True

    def to_dict(self) -> dict[str, Any]:
        # 档案是"一次性写、长期读"，这里不玩"只写非默认值"那一套：
        # 全部字段写全，读回来才不受"当时默认值是多少"的影响。
        return {
            "session_id": self.session_id,
            "title": self.title,
            "mode": self.mode,
            "room_id": self.room_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "archived_at": self.archived_at,
            "duration_minutes": self.duration_minutes,
            "grace_seconds": self.grace_seconds,
            "force_collect": self.force_collect,
            "allow_resubmit": self.allow_resubmit,
            "show_leaderboard": self.show_leaderboard,
            "policy": dict(self.policy),
            "problems": [dict(item) for item in self.problems],
            "participants": [dict(item) for item in self.participants],
            "max_total_score": self.max_total_score,
            "submission_count": self.submission_count,
            "keep_code": self.keep_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        """**未知字段直接忽略**，缺字段取默认 —— 这是读档的兼容契约。"""

        def _int(key: str, fallback: int = 0) -> int:
            try:
                return int(data.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def _list(key: str) -> list[dict[str, Any]]:
            raw = data.get(key) or []
            return [dict(item) for item in raw if isinstance(item, dict)] \
                if isinstance(raw, list) else []

        policy = data.get("policy")
        return cls(
            session_id=str(data.get("session_id", "") or ""),
            title=str(data.get("title", "") or ""),
            mode=str(data.get("mode", "exam") or "exam"),
            room_id=str(data.get("room_id", "") or ""),
            started_at=str(data.get("started_at", "") or ""),
            ended_at=str(data.get("ended_at", "") or ""),
            archived_at=str(data.get("archived_at", "") or ""),
            duration_minutes=_int("duration_minutes"),
            grace_seconds=_int("grace_seconds", 30),
            force_collect=bool(data.get("force_collect")),
            allow_resubmit=bool(data.get("allow_resubmit", True)),
            show_leaderboard=bool(data.get("show_leaderboard", True)),
            policy=dict(policy) if isinstance(policy, dict) else {},
            problems=_list("problems"),
            participants=_list("participants"),
            max_total_score=_int("max_total_score"),
            submission_count=_int("submission_count"),
            keep_code=bool(data.get("keep_code", True)),
        )


@dataclass
class ArchiveManifest:
    """``archive.json``：版本号 + 每个文件的校验和。"""

    version: int = ARCHIVE_VERSION
    created_at: str = ""
    keep_code: bool = True
    #: 文件名 → sha256。空表示这份档案没写校验和（不该发生，但读档不因此失败）。
    files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "keep_code": self.keep_code,
            "files": dict(self.files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchiveManifest":
        files = data.get("files")
        try:
            version = int(data.get("version", ARCHIVE_VERSION))
        except (TypeError, ValueError):
            version = ARCHIVE_VERSION
        return cls(
            version=version,
            created_at=str(data.get("created_at", "") or ""),
            keep_code=bool(data.get("keep_code", True)),
            files={str(k): str(v) for k, v in files.items()}
            if isinstance(files, dict) else {},
        )


@dataclass
class ArchiveSummary:
    """列表页的一行。**只读两个小文件**，不碰 jsonl。"""

    directory: Path
    record: SessionRecord
    manifest: ArchiveManifest
    #: 非空 = 这份档案读不出来（版本不认识、文件缺失……）。
    #: 列表里照样显示它，并把这句原话写在行上 —— 静默跳过等于"档案不见了"。
    broken: str = ""

    @property
    def has_code(self) -> bool:
        return bool(self.record.keep_code and self.manifest.keep_code)

    @property
    def stamp(self) -> str:
        """目录名里的时间戳部分（``20260919-173045``），列表按它倒序。"""
        return self.directory.name.split("-", 1)[0]

    @property
    def label(self) -> str:
        """列表里那一行的主文本。"""
        who = len(self.record.participants)
        return (f"{self.record.title or '（未命名场次）'} · "
                f"{len(self.record.problems)} 题 · "
                f"{who} 人 / {self.record.submission_count} 份提交")


@dataclass
class ExamArchive:
    """读回内存的一份档案。"""

    directory: Path
    manifest: ArchiveManifest
    record: SessionRecord
    submissions: list[dict[str, Any]] = field(default_factory=list)
    leaderboard: dict[str, Any] = field(default_factory=dict)
    #: jsonl 里跳过的坏行数（半行写入、手工改坏）。跳过而不是整份读不出来。
    skipped_lines: int = 0
    #: 校验和是否全部对上。对不上照样能用，但界面要说出来。
    integrity_ok: bool = True

    @property
    def has_code(self) -> bool:
        return bool(self.record.keep_code and self.manifest.keep_code)

    def codes(self) -> list[dict[str, str]]:
        """带源码的那几份，供导出"每人一个源码文件"用。"""
        return [item for item in self.submissions
                if isinstance(item.get("code"), str) and item.get("code")]


# ---------------------------------------------------------------------------
# 文件名
# ---------------------------------------------------------------------------


def _slug(value: str, limit: int = SLUG_LIMIT) -> str:
    """把任意文本压成一个能当文件名用的片段。

    ``_UNSAFE`` 里的字符在 Windows 上**直接让 ``mkdir`` 报 `WinError 123`**
    （"文件名、目录名或卷标语法不正确"），而且报的是整个路径，看着像根目录
    不存在。所以入口处一律先过这道筛子，别指望调用方自觉。
    """
    text = _SPACES.sub("-", _UNSAFE.sub("-", (value or "").strip()))
    return text.strip("-.")[:limit].strip("-.")


def archive_directory_name(stamp: str, title: str, session_id: str = "") -> str:
    """给一场测验起个目录名：``20260919-173045-期末模拟``。

    ``stamp`` 一般由调用方格式化好（``%Y%m%d-%H%M%S``）—— 时间格式化只放一处，
    这里不碰时区与本地时间。但**它也照样过一遍非法字符筛子**：``SessionRecord``
    里的时间本来就是 ISO 串，顺手写 ``started.isoformat()`` 太自然了，而那个
    串里有冒号 —— 不筛的话存档会以一个"路径语法不正确"静默失败。

    标题同理，并且**截断到 40 字符**：Windows 的路径上限是 260，档案根目录
    本身已经不短，再叠一个长标题就会在深目录下写入失败，而失败信息只会说
    "找不到路径"。
    """
    head = _slug(stamp, limit=32)
    tail = _slug(title) or _slug(session_id, limit=8) or "session"
    return f"{head}-{tail}" if head else tail


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def write_archive(
    root: str | Path,
    *,
    stamp: str,
    record: SessionRecord | dict[str, Any],
    submissions: Sequence[dict[str, Any]],
    leaderboard: dict[str, Any] | None = None,
    keep_code: bool = True,
    created_at: str = "",
) -> Path:
    """写一份档案，返回它的目录。

    ``record`` 允许直接给 ``dict`` —— ``ExamSession.archive_record()`` 吐的就是
    一个 dict，这样 ``net/session.py`` 不必反过来 import 本模块（它到目前为止
    只依赖标准库，那是它作为"纯逻辑层"的分层属性，值得保住）。

    ``keep_code=False`` 时提交里不含 ``code`` 键（调用方给的 dict 里就不该有），
    元数据里的 ``keep_code`` 也会置 False —— 于是读档方一眼能分清
    "这份档案没有源码"和"源码丢了"。

    整个目录先写成 ``<名字>.partial``，全部落盘后再改名 —— 中途失败留下的
    是 ``.partial`` 而不是一个缺文件的正式档案，列表页不会把它当成绩看。

    **``keep_code=False`` 时还会在这里再抠一次源码。** ``ExamSession``
    那边已经在源头抠过，这里重复一次是有意的：那个开关是一句隐私承诺，而这是
    最后一道碰文件的闸门。信任"每个调用方都会记得传"的代价是，一处疏漏就把学生
    代码写进了盘里，而且全程不报错。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if not keep_code:
        submissions = [{key: value for key, value in item.items() if key != "code"}
                       for item in submissions]
    source = _as_record(record)
    base = archive_directory_name(stamp, source.title, source.session_id)
    target = root / base
    index = 2
    while target.exists():
        target = root / f"{base}-{index}"
        index += 1

    record = _copy_record(source, keep_code=keep_code,
                          count=len(submissions), created_at=created_at)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    if partial.exists():
        # 上一轮中途失败留下的残骸。改名挪开而不是删 —— 真出事时它是唯一证据。
        stale = partial.with_name(f"{partial.name}-{index}")
        partial.rename(stale)

    try:
        partial.mkdir(parents=True)
        (partial / SESSION_FILE).write_text(_dumps(record.to_dict()),
                                            encoding="utf-8")
        (partial / SUBMISSIONS_FILE).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n"
                    for item in submissions), encoding="utf-8")
        (partial / LEADERBOARD_FILE).write_text(
            _dumps(leaderboard or {}), encoding="utf-8")

        manifest = ArchiveManifest(
            version=ARCHIVE_VERSION,
            created_at=created_at or datetime.now().isoformat(timespec="seconds"),
            keep_code=keep_code,
            files={name: _sha256_file(partial / name)
                   for name in (SESSION_FILE, SUBMISSIONS_FILE, LEADERBOARD_FILE)},
        )
        (partial / MANIFEST_FILE).write_text(_dumps(manifest.to_dict()),
                                            encoding="utf-8")
        partial.rename(target)
    except Exception:
        log.exception("写档案失败：%s", target)
        # 留给现场的是 .partial 而不是半个正式档案
        raise
    log.info("测验档案已保存：%s（%d 份提交，含源码=%s）",
             target, len(submissions), keep_code)
    return target


def _as_record(value: SessionRecord | dict[str, Any]) -> SessionRecord:
    """允许调用方直接给 dict —— 见 :func:`write_archive` 里对分层的说明。"""
    if isinstance(value, SessionRecord):
        return value
    if isinstance(value, dict):
        return SessionRecord.from_dict(value)
    raise TypeError(f"record 要 SessionRecord 或 dict，收到 {type(value).__name__}")


def _copy_record(record: SessionRecord, *, keep_code: bool, count: int,
                 created_at: str) -> SessionRecord:
    """不就地改调用方那份记录 —— 面板可能还要拿它刷新界面。"""
    return SessionRecord(
        session_id=record.session_id,
        title=record.title,
        mode=record.mode,
        room_id=record.room_id,
        started_at=record.started_at,
        ended_at=record.ended_at,
        archived_at=created_at or datetime.now().isoformat(timespec="seconds"),
        duration_minutes=record.duration_minutes,
        grace_seconds=record.grace_seconds,
        force_collect=record.force_collect,
        allow_resubmit=record.allow_resubmit,
        show_leaderboard=record.show_leaderboard,
        policy=dict(record.policy),
        problems=[dict(item) for item in record.problems],
        participants=[dict(item) for item in record.participants],
        max_total_score=record.max_total_score,
        submission_count=count,
        keep_code=keep_code,
    )


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArchiveError(f"档案缺少 {path.name}，读不出来") from exc
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"{path.name} 读不出来：{exc}") from exc
    if not isinstance(payload, dict):
        raise ArchiveError(f"{path.name} 的内容不是一个对象")
    return payload


def read_archive(directory: str | Path) -> ExamArchive:
    """读回一份档案。

    只有 ``session.json`` 读不出来才算失败（那是骨架，没有它什么都做不了）。
    其余文件缺失或校验和对不上都**尽量读**，把问题记在
    :attr:`ExamArchive.integrity_ok` / :attr:`ExamArchive.skipped_lines` 上，
    由界面去说 —— 一份"少了榜单但有全部提交"的档案，比一个异常有用得多。
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ArchiveError(f"不是一份档案：{directory}")

    manifest_path = directory / MANIFEST_FILE
    manifest = (ArchiveManifest.from_dict(_read_json(manifest_path))
                if manifest_path.exists() else ArchiveManifest(files={}))
    record = SessionRecord.from_dict(_read_json(directory / SESSION_FILE))

    archive = ExamArchive(directory=directory, manifest=manifest, record=record)

    if manifest_path.exists() and manifest.files:
        for name, expected in manifest.files.items():
            path = directory / name
            if not path.exists() or _sha256_file(path) != expected:
                archive.integrity_ok = False
                log.warning("档案校验和对不上：%s / %s", directory.name, name)

    submissions_path = directory / SUBMISSIONS_FILE
    if submissions_path.exists():
        try:
            text = submissions_path.read_text(encoding="utf-8")
        except OSError:
            archive.integrity_ok = False
            text = ""
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                archive.skipped_lines += 1
                continue
            if isinstance(item, dict):
                archive.submissions.append(item)
            else:
                archive.skipped_lines += 1
        if archive.skipped_lines:
            archive.integrity_ok = False
    else:
        archive.integrity_ok = False

    board_path = directory / LEADERBOARD_FILE
    if board_path.exists():
        try:
            archive.leaderboard = _read_json(board_path)
        except ArchiveError:
            archive.integrity_ok = False
    return archive


def list_archives(root: str | Path) -> list[ArchiveSummary]:
    """列出全部档案，**按场次时间倒序**（最近的排最前）。

    读不出来的那一份也留在列表里并带上原因：档案列表里少了它，老师的判断是
    "这一场没存下来"，而真相可能是"存下来了但版本不认识" —— 两件事的处理
    方式完全不同。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    summaries: list[ArchiveSummary] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.endswith(PARTIAL_SUFFIX):
            continue
        manifest_path = entry / MANIFEST_FILE
        if not manifest_path.exists():
            # 不是档案目录（误放进来的别的东西），不列表、也不报错
            continue
        try:
            manifest = ArchiveManifest.from_dict(_read_json(manifest_path))
            record = SessionRecord.from_dict(_read_json(entry / SESSION_FILE))
        except ArchiveError as exc:
            summaries.append(ArchiveSummary(
                directory=entry, record=SessionRecord(), manifest=ArchiveManifest(),
                broken=str(exc)))
            continue
        if manifest.version > ARCHIVE_VERSION:
            summaries.append(ArchiveSummary(
                directory=entry, record=record, manifest=manifest,
                broken=f"档案版本 {manifest.version} 比本程序新，请升级后再打开"))
            continue
        summaries.append(ArchiveSummary(directory=entry, record=record,
                                        manifest=manifest))
    summaries.sort(key=lambda item: (item.record.started_at, item.directory.name),
                   reverse=True)
    return summaries


def delete_archive(directory: str | Path) -> bool:
    """删掉一份档案。返回是否真的删了。

    **先确认它真是一份档案**（目录里有 ``archive.json``）再动手。这道检查不是
    多余的谨慎：调用方传进来的路径来自界面上的列表，一旦哪里拼错变成数据根目录
    或它的上级，``rmtree`` 会把题库、设置、练习记录一起带走。
    """
    directory = Path(directory)
    if not directory.is_dir() or not (directory / MANIFEST_FILE).exists():
        log.warning("拒绝删除：不像是一份档案 —— %s", directory)
        return False
    shutil.rmtree(directory, ignore_errors=True)
    log.info("档案已删除：%s", directory)
    return True


def total_bytes(root: str | Path) -> int:
    """档案根目录占了多少字节（设置页/列表页显示用）。"""
    root = Path(root)
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def iter_archive_dirs(root: str | Path) -> Iterable[Path]:
    """档案目录（含读不出来的），给"清理"这类批量操作当输入。"""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted((entry for entry in root.iterdir()
                   if entry.is_dir() and not entry.name.endswith(PARTIAL_SUFFIX)
                   and (entry / MANIFEST_FILE).exists()),
                  key=lambda item: item.name)
