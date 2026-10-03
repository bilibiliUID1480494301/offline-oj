"""Windows 目录规约。

遵循 Windows 应用的基本约定：**程序文件**与**用户数据**分离。

* 程序文件（只读）：可执行文件所在目录，或 PyInstaller 解包目录 ``sys._MEIPASS``。
* 用户数据（可写）：``%LOCALAPPDATA%\\OfflineOJ``。

之所以不把 ``problems.json`` 写在程序目录，是因为 Windows 应用的标准安装位置是
``C:\\Program Files``，该目录对普通用户只读，UAC 虚拟化会让写入静默重定向到
VirtualStore，导致"数据写了但读不到"的诡异现象。写入 ``%LOCALAPPDATA%``
既符合规范，也让多用户共用一台机器时各自拥有独立题库。

开发者/自动化测试可用环境变量 ``OFFLINE_OJ_HOME`` 覆盖数据根目录。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME

#: 覆盖数据根目录的环境变量（测试与绿色部署用）
ENV_HOME = "OFFLINE_OJ_HOME"

#: 目录叶子名，同时用于日志、安装包与开始菜单
DATA_DIR_NAME = APP_NAME


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """程序文件所在目录（只读）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读资源根目录。

    PyInstaller 单文件模式下资源被解包到 ``sys._MEIPASS``；单目录模式与源码运行
    时资源就在程序目录里。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def local_app_data() -> Path:
    """本机用户数据根目录 ``%LOCALAPPDATA%``。"""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()

    value = os.environ.get("LOCALAPPDATA")
    if value:
        return Path(value)
    # 非 Windows 或环境变量缺失时的兜底，保证导入期不会抛异常
    return Path.home() / "AppData" / "Local"


@dataclass(frozen=True)
class AppPaths:
    """应用用到的全部路径。所有可写路径都位于 :attr:`data_root` 之下。"""

    data_root: Path
    install_root: Path
    resource_root: Path

    # ---- 派生路径 ---------------------------------------------------------

    @property
    def problems_file(self) -> Path:
        """题库文件（题目 + 测试点）。"""
        return self.data_root / "problems.json"

    @property
    def settings_file(self) -> Path:
        """应用设置。"""
        return self.data_root / "settings.json"

    @property
    def device_file(self) -> Path:
        """本机测验身份（设备八位 ID）。

        与设置分开存：设备 ID 是学生在局域网测验里的身份，必须跨场次稳定，
        而设置是随时可能被"恢复默认"的东西 —— 放在一起，一次恢复默认
        就会让学生换个 ID，榜上的历史成绩就接不上了。
        """
        return self.data_root / "device.json"

    @property
    def resources_dir(self) -> Path:
        """题目描述里引用的图片等资源。"""
        return self.data_root / "problem_resources"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "app.log"

    @property
    def work_dir(self) -> Path:
        """编译与运行的临时工作区（首次使用时按需创建）。"""
        return self.data_root / "workspace"

    @property
    def submissions_dir(self) -> Path:
        """判题留档目录。"""
        return self.data_root / "submissions"

    @property
    def exams_dir(self) -> Path:
        """整场测验的档案根目录 —— 一场一个子目录（见 ``core/records.py``）。

        与 :attr:`submissions_dir` **分开放**，两者不是一回事：那个是本机单人
        练习的提交流水（上限 500 条，一行一次提交，没有"场次"这个概念），
        这里是"某一场局域网测验的完整快照"（含参与者、题目快照、榜单与全部
        提交的源码）。混在一起会让"清空练习记录"顺手把老师的考场档案删掉。
        """
        return self.data_root / "exams"

    @property
    def rosters_dir(self) -> Path:
        """选手名单根目录 —— 一份名单一个文件（见 ``core/roster.py``）。

        与 :attr:`device_file` 分开放：那个是**这台机器**的身份，这个是人
        （老师）准备的花名册。老师往往同时带好几个班，一个班一份。
        注意这里的文件含**明文个人口令**，属于记分册一级的敏感数据。
        """
        return self.data_root / "rosters"

    def roster_file(self, name: str) -> Path:
        """某一份名单的落盘位置。

        "名单名 → 文件名"的收尾（去非法字符、空名回落）由
        ``core.roster.safe_roster_name`` 一处负责，这里只是把它接到目录上；
        真正的落盘/读回也都在 ``core.roster``，本模块不认识名单的内容。
        """
        from .core.roster import ROSTER_SUFFIX, safe_roster_name
        return self.rosters_dir / (safe_roster_name(name) + ROSTER_SUFFIX)

    def roster_files(self) -> list[Path]:
        """列举已有名单，按文件名排序（顺序稳定，界面不会每次刷新乱跳）。"""
        if not self.rosters_dir.exists():
            return []
        return sorted(self.rosters_dir.glob("*.json"),
                      key=lambda item: item.name.lower())

    # ---- 资源定位 ---------------------------------------------------------

    def asset(self, name: str) -> Path:
        """定位随程序分发的只读资源（图标等）。"""
        return self.resource_root / "assets" / name

    def existing_asset(self, name: str) -> Path | None:
        path = self.asset(name)
        return path if path.exists() else None

    def ensure_layout(self) -> "AppPaths":
        """创建所有必需目录，返回自身以便链式调用。"""
        for directory in (
            self.data_root,
            self.resources_dir,
            self.logs_dir,
            self.work_dir,
            self.submissions_dir,
            self.exams_dir,
            self.rosters_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def image_search_paths(self) -> list[str]:
        """Markdown 预览检索图片时的搜索目录。"""
        return [str(self.resources_dir), str(self.data_root)]

    def as_dict(self) -> dict[str, str]:
        """用于"关于"对话框与日志诊断。"""
        return {
            "数据目录": str(self.data_root),
            "程序目录": str(self.install_root),
            "资源目录": str(self.resource_root),
            "日志文件": str(self.log_file),
        }


def build_paths() -> AppPaths:
    """按 Windows 约定构造路径集合（不创建目录，纯计算）。

    默认 ``%LOCALAPPDATA%\\OfflineOJ``；设置 ``OFFLINE_OJ_HOME`` 时直接使用该目录，
    方便测试隔离与便携部署。
    """
    base = local_app_data()
    # local_app_data() 在 ENV_HOME 存在时已返回覆盖值本身，不再追加子目录
    data_root = base if ENV_HOME in os.environ else base / DATA_DIR_NAME
    return AppPaths(
        data_root=data_root,
        install_root=program_dir(),
        resource_root=resource_dir(),
    )
