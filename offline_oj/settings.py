"""设置持久化。

设置存为 ``%LOCALAPPDATA%\\OfflineOJ\\settings.json``，写入采用"临时文件 + 原子替换"，
避免掉电或崩溃在写入中途留下半截 JSON 导致下次启动配置丢失。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 设置架构版本，字段含义发生不兼容变化时递增，并在此处做升级迁移
SETTINGS_SCHEMA = 2

#: 编译器键名 -> 中文名，供 UI 与设置复用
COMPILER_KEYS: tuple[str, ...] = ("cpp", "c", "python", "javac", "java")

DEFAULTS: dict[str, Any] = {
    "schema": SETTINGS_SCHEMA,
    # 编译器路径，键为 COMPILER_KEYS
    "compilers": {key: "" for key in COMPILER_KEYS},
    # 判题选项
    "security_mode": True,       # 提交前做危险调用静态检查
    "o2_optimization": True,     # C/C++ 编译开 -O2
    # 测验档案（关房时自动留档，见 core/records.py）
    "archive_on_close": True,    # 关房即把整场写进 exams/<时间戳>-<名字>/
    # 档案里要不要带学生源码。**关掉它，"导出档案包 / 雷同检测"就没有输入了**，
    # 但成绩、榜单、提交明细仍然完整 —— 有些学校不允许留存学生代码。
    "archive_keep_code": True,
    # 界面选项
    "theme": "light",            # light | dark | system
    "markdown_render": True,     # 题目描述以 Markdown 渲染
    "editor_font_size": 11,
    "editor_font_family": "Consolas",
    # 主窗口状态（base64 编码的 QByteArray，由 UI 层写入）
    "window_geometry": "",
    "window_state": "",
    "active_tab": 0,
    "last_language": "cpp",
    # 首次运行提示
    "welcome_shown": False,
    # 最近使用的目录
    "last_import_dir": "",
    "last_export_dir": "",
    # 上次「打开 / 另存为」代码文件用的目录。不记的话每次都要从头翻一遍目录树。
    "code_directory": "",
}


class AppSettings:
    """线程安全的设置容器。

    使用方式::

        settings = AppSettings(paths.settings_file).load()
        settings.set("theme", "dark")
        settings.save_if_dirty()
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))  # 深拷贝
        self._dirty = False

    # ---- 读写 -------------------------------------------------------------

    @property
    def file(self) -> Path:
        """设置文件路径（供 UI 展示 / 诊断）。"""
        return self._path

    def load(self) -> "AppSettings":
        """从磁盘读取设置；文件缺失或损坏时回落到默认值，绝不抛出。"""
        with self._lock:
            if not self._path.exists():
                self._dirty = True
                return self
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("设置文件根节点不是对象")
            except Exception as exc:  # 损坏文件不阻断启动
                log.warning("设置文件无法解析，已回落到默认值: %s", exc)
                self._quarantine()
                return self

            # 逐键合并：保留用户已有值，补齐新增默认值
            merged = json.loads(json.dumps(DEFAULTS))
            for key, value in raw.items():
                if key == "compilers" and isinstance(value, dict):
                    merged["compilers"].update(
                        {k: str(v) for k, v in value.items() if k in COMPILER_KEYS}
                    )
                else:
                    merged[key] = value
            self._data = merged
            self._migrate()
            return self

    def save(self) -> None:
        """原子写入磁盘。"""
        with self._lock:
            if not self._dirty:
                return
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self._path)  # 同分区内原子替换
                self._dirty = False
            except Exception as exc:
                log.error("保存设置失败: %s", exc)
                tmp.unlink(missing_ok=True)

    def save_if_dirty(self) -> None:
        self.save()

    # ---- 访问 -------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._data:
                return self._data[key]
            return DEFAULTS.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if self._data.get(key) != value:
                self._data[key] = value
                self._dirty = True

    def update(self, **values: Any) -> None:
        for key, value in values.items():
            self.set(key, value)

    def bool(self, key: str) -> bool:
        return bool(self.get(key))

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def reset(self) -> None:
        """恢复出厂设置（保留架构版本）。"""
        with self._lock:
            self._data = json.loads(json.dumps(DEFAULTS))
            self._dirty = True

    # ---- 编译器便捷接口 ---------------------------------------------------

    def compiler_path(self, key: str) -> str:
        return str(self.get("compilers", {}).get(key, "") or "")

    def set_compiler_path(self, key: str, path: str) -> None:
        with self._lock:
            compilers = dict(self._data.get("compilers", {}))
            if compilers.get(key) != path:
                compilers[key] = path
                self._data["compilers"] = compilers
                self._dirty = True

    def compiler_paths(self) -> dict[str, str]:
        return {key: self.compiler_path(key) for key in COMPILER_KEYS}

    # ---- 内部 -------------------------------------------------------------

    def _migrate(self) -> None:
        """旧版本设置升级。当前 v2 相比 v1 仅新增字段，无需转换。"""
        schema = self._data.get("schema")
        if schema != SETTINGS_SCHEMA:
            log.info("设置架构从 %s 升级到 %s", schema, SETTINGS_SCHEMA)
            self._data["schema"] = SETTINGS_SCHEMA
            self._dirty = True

    def _quarantine(self) -> None:
        """把损坏的设置文件改名留证，避免反复解析失败。"""
        try:
            broken = self._path.with_suffix(".json.broken")
            os.replace(self._path, broken)
            log.warning("损坏的设置文件已改名为 %s", broken)
        except Exception:
            pass
