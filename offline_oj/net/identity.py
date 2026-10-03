"""本机设备标识（设备八位 ID）的生成与持久化。

设备 ID 是学生在测验里的**身份**：排行榜上显示它、老师照它点名、同名的人靠它区分。
所以它必须**跨场次稳定** —— 每场重摇的话，学生认不出自己那一行，
"上次那道题我做对了"这种连续感也没了，老师念 ID 也没有意义。

存放位置：``%LOCALAPPDATA%\\OfflineOJ\\device.json``，与题库、设置并列。
不写进程序目录：``C:\\Program Files`` 对普通用户只读，UAC 虚拟化会把写入
静默重定向到 VirtualStore，现象是"写了但读不到"。

写盘用**先写 .tmp 再 os.replace 的原子替换**（与题库、设置同一套做法）。
半截文件比没文件更糟：读回来是个空 ID，而空 ID 会被主机拒绝，
表现出来是"明明进过一次，这次怎么都进不去"。
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .session import (DEVICE_ID_ALPHABET, DEVICE_ID_LENGTH, MAX_USERNAME_LENGTH,
                      generate_device_id, normalize_device_id)

log = logging.getLogger(__name__)

__all__ = ["DEVICE_FILE_NAME", "DeviceIdentity", "load_identity",
           "save_identity", "get_identity", "blank_identity"]

#: 文件名。放在数据根目录下，与 problems.json / settings.json 并列。
DEVICE_FILE_NAME = "device.json"


def _clean_username(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_USERNAME_LENGTH]


@dataclass
class DeviceIdentity:
    """本机的测验身份。"""

    #: 八位设备 ID；空串表示"还没生成"
    device_id: str = ""
    #: 上次用过的用户名，进场时预填，省得每次重打
    username: str = ""
    #: 本机名，方便老师在名单里认出是谁的机器。仅本机可见，不上报给他人之外的用途
    host: str = ""

    @property
    def ready(self) -> bool:
        """设备 ID 是否已经是一个合法的八位 ID。"""
        return (len(self.device_id) == DEVICE_ID_LENGTH
                and all(char in DEVICE_ID_ALPHABET for char in self.device_id))

    def to_dict(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "username": self.username,
                "host": self.host}

    @classmethod
    def from_dict(cls, data: Any) -> "DeviceIdentity":
        """从 JSON 构造。**任何不合法的地方都退回默认值，不抛异常。**

        这个文件是用户机器上的本地状态，被手工改坏、被同步工具截断、
        被别的版本写成别的结构都是可能的；而它读不出来时最合理的处置是
        "当作没有，重新生成一个"，而不是让整个程序起不来。
        """
        if not isinstance(data, dict):
            return cls()
        identity = cls(
            device_id=normalize_device_id(str(data.get("device_id", "") or "")),
            username=_clean_username(data.get("username")),
            host=str(data.get("host", "") or ""),
        )
        if not identity.ready:
            identity.device_id = ""
        return identity


def blank_identity(*, host: str | None = None) -> DeviceIdentity:
    """生成一个全新的设备标识（已带好本机名）。"""
    return DeviceIdentity(device_id=generate_device_id(),
                          host=host if host is not None else platform.node())


def load_identity(path: str | os.PathLike[str]) -> DeviceIdentity:
    """读设备标识。文件不存在或读不动就返回一个空白标识（**不落盘**）。"""
    file = Path(path)
    if not file.exists():
        return DeviceIdentity()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("设备标识读取失败（将重新生成）: %s", exc)
        return DeviceIdentity()
    identity = DeviceIdentity.from_dict(raw)
    if not identity.host:
        identity.host = platform.node()
    return identity


def save_identity(path: str | os.PathLike[str], identity: DeviceIdentity) -> bool:
    """写设备标识。返回是否成功。

    失败**只记日志不抛**：写不了这个文件是"下次进场换个新 ID"这种程度的问题，
    不该因为它让用户连测验都进不去。
    """
    file = Path(path)
    tmp = file.with_name(file.name + ".tmp")
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(identity.to_dict(), ensure_ascii=False, indent=2)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, file)          # 同分区内原子替换
        return True
    except OSError as exc:
        log.warning("设备标识写入失败: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def get_identity(path: str | os.PathLike[str]) -> DeviceIdentity:
    """读出本机标识；没有或已损坏就生成一个新的并立刻落盘。

    这是界面与客户端唯一该调用的入口 —— 保证"第一次运行后设备 ID 就固定下来了"。
    """
    identity = load_identity(path)
    if not identity.ready:
        identity.device_id = generate_device_id()
        if not identity.host:
            identity.host = platform.node()
        save_identity(path, identity)
        log.info("已生成本机设备 ID: %s", identity.device_id)
    return identity
