"""Visual Studio (MSVC) 工具链定位与环境组装。

为什么需要这一块：``cl.exe`` 不是普通的独立编译器。它靠 ``INCLUDE`` / ``LIB`` /
``PATH`` 三个环境变量去找标准库头文件与导入库，**没有这套环境时连
``#include <iostream>`` 都过不了**（报 ``C1034: iostream: 不包括路径集``）。
只把 ``cl.exe`` 的路径填进配置是没用的。

官方给的初始化方式是跑 ``VC\\Auxiliary\\Build\\vcvars64.bat``，但那条路对宿主
程序非常不友好：

1. 它是批处理，必须经 ``cmd.exe`` 执行。而 ``subprocess`` 的 ``list2cmdline``
   会把内层引号转义成 ``\\"``，**``cmd.exe`` 不认反斜杠转义**，于是路径带空格的
   ``"C:\\Program Files\\..."`` 直接执行失败，报"不是内部或外部命令"。
2. ``shell=True`` 也救不了 —— 它会在外面再套一层引号。
3. 唯一可用的办法是临时写一个无空格路径的包装 ``.bat``。实测能work，
   但**单次要 100 秒左右**，因为 vcvars 内部大量调用 ``reg.exe`` 去探测
   SDK / .NET / 旧工具集版本。

所以这里自己拼：``vswhere.exe`` 定位 VS 安装根目录，注册表读 Windows SDK 根，
再按 VS 2015 以来非常稳定的目录布局算出三个变量。**实测 50 毫秒级**，
不经过 ``cmd.exe``，也不依赖 ``reg.exe``。

正确性由"真的编译一个程序并运行它"来兜底 —— 见 :func:`probe`。
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import re
import sys
from pathlib import Path

from .process import run_silent
from .volumes import expand_drive_templates

log = logging.getLogger(__name__)

#: 编译器可执行名
COMPILER_NAME = "cl.exe"

#: 找到 VS 安装时要求的组件 —— 只有装了 C++ 生成工具才有 cl.exe
VC_TOOLS_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"

#: 兼容旧版的组件 ID（VS 2017 之前）
_LEGACY_COMPONENTS = (
    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "Microsoft.VisualStudio.Component.VC.Tools",
)

#: vswhere 的标准位置
_VSWHERE_TEMPLATES = (
    r"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe",
    r"%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe",
    r"{drive}Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe",
    r"{drive}Program Files\Microsoft Visual Studio\Installer\vswhere.exe",
)

#: vswhere 不可用时的兜底扫描（VS 2017 以后都是这个布局）
_VS_SCAN_TEMPLATES = (
    r"{drive}Program Files\Microsoft Visual Studio\*",
    r"{drive}Program Files (x86)\Microsoft Visual Studio\*",
)

#: 版本号在横幅里的样子。**必须同时认英文和中文** —— cl.exe 的横幅是本地化的，
#: 中文 Windows 上打出来是「用于 x64 的 Microsoft (R) C/C++ 优化编译器 19.44.35228 版」，
#: 只匹配英文 ``Version\s+...`` 会永远解析不出东西。
_VERSION_PATTERNS = (
    re.compile(r"Version\s+(\d+(?:\.\d+)+)", re.I),      # 英文：Version 19.44.35228
    re.compile(r"(\d+(?:\.\d+)+)\s*版"),                  # 中文：19.44.35228 版
    re.compile(r"(\d+\.\d+\.\d{4,})"),                    # 兜底：任意 主.次.修订 形态
)
#: 目标架构**不从横幅里取** —— "for x64" 同样是本地化的（中文是「用于 x64 的」）。
#: 直接从 ``bin\Hostx64\x64\cl.exe`` 的父目录名读，天然与语言无关。

#: 主版本号 -> 产品名（用于把 19.44.35207 显示成"VS 2022"）
_PRODUCT_BY_MAJOR = {
    19: "Visual Studio 2015+",
    18: "Visual Studio 2013",
    17: "Visual Studio 2012",
    16: "Visual Studio 2010",
    15: "Visual Studio 2008",
    14: "Visual Studio 2005",
}


def _product_name(version: tuple[int, ...]) -> str:
    """把 MSVC 编译器版本映射成 Visual Studio 产品名。"""
    if not version:
        return "Visual Studio"
    major, minor = version[0], (version[1] if len(version) > 1 else 0)
    if major != 19:
        return _PRODUCT_BY_MAJOR.get(major, "Visual Studio")
    # 19.x 的次版本号与 VS 大版本对应
    if minor >= 40:
        return "Visual Studio 2022"
    if minor >= 30:
        return "Visual Studio 2022"
    if minor >= 20:
        return "Visual Studio 2019"
    if minor >= 10:
        return "Visual Studio 2017"
    return "Visual Studio 2015"


def _host_target_pairs() -> tuple[tuple[str, str], ...]:
    """按本机架构给出优先尝试的 (宿主目录, 目标架构) 组合。

    评测产物要在本机跑，所以优先原生组合（x64 机器上要 x64 产物）。
    """
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return (("Hostarm64", "arm64"), ("Hostx64", "x64"), ("Hostx86", "x86"))
    return (("Hostx64", "x64"), ("Hostx86", "x86"))


def _is_compiler(path: str | os.PathLike[str]) -> bool:
    return Path(path).name.lower() == COMPILER_NAME


def is_msvc(path: str | os.PathLike[str] | None) -> bool:
    """这个编译器路径是不是 MSVC 的 ``cl.exe``。

    只按文件名判断，不看路径 —— cl.exe 可能来自 VS 安装目录、Build Tools，
    也可能被用户复制到别处。GCC / clang 不会叫这个名字。
    """
    return bool(path) and Path(str(path)).name.lower() == COMPILER_NAME


# ----------------------------------------------------------------------
# 定位
# ----------------------------------------------------------------------

def find_vswhere() -> str | None:
    """找 ``vswhere.exe``。它是 VS 2017 以后随安装器一起装的，用于查询安装位置。"""
    for template in _VSWHERE_TEMPLATES:
        for raw in expand_drive_templates((template,)):
            candidate = Path(os.path.expandvars(raw))
            if candidate.is_file():
                return str(candidate)
    return None


@functools.lru_cache(maxsize=1)
def visual_studio_roots() -> tuple[str, ...]:
    """所有装了 C++ 生成工具的 Visual Studio 安装根目录，新的在前。"""
    roots: list[str] = []

    vswhere = find_vswhere()
    if vswhere:
        # 不带 -latest，这样同时装了 2019 和 2022 时两个都能拿到，
        # 后面再按工具集版本挑最新的
        command = [vswhere, "-products", "*", "-prerelease", "-property",
                   "installationPath", "-nologo"]
        for component in _LEGACY_COMPONENTS:
            completed = run_silent([*command, "-requires", component], timeout=20)
            if completed.returncode != 0 or not completed.stdout:
                continue
            for line in completed.stdout.splitlines():
                path = line.strip().strip('"')
                if path and Path(path).is_dir() and path not in roots:
                    roots.append(path)
            if roots:
                break
        # 组件过滤失败时再退一步：不过滤，但要求目录里真的有 VC\Tools\MSVC
        if not roots:
            completed = run_silent(command, timeout=20)
            for line in (completed.stdout or "").splitlines():
                path = line.strip().strip('"')
                if path and (Path(path) / "VC" / "Tools" / "MSVC").is_dir():
                    roots.append(path)

    if not roots:
        from glob import glob

        for template in _VS_SCAN_TEMPLATES:
            for pattern in expand_drive_templates((template,)):
                for raw in sorted(glob(os.path.expandvars(pattern)), reverse=True):
                    candidate = Path(raw)
                    if ((candidate / "VC" / "Tools" / "MSVC").is_dir()
                            and str(candidate) not in roots):
                        roots.append(str(candidate))

    if roots:
        log.info("找到 %d 个 Visual Studio 安装: %s", len(roots), roots)
    else:
        log.debug("没有找到可用的 Visual Studio（含 C++ 生成工具）")
    return tuple(roots)


def tools_dir(vs_root: str | os.PathLike[str]) -> str | None:
    """某个 VS 安装里版本最高的 MSVC 工具集目录。

    形如 ``...\\VC\\Tools\\MSVC\\14.44.35207``；同一安装里可能并存多个工具集，
    取版本号最高的那个。
    """
    root = Path(vs_root) / "VC" / "Tools" / "MSVC"
    if not root.is_dir():
        return None

    best: tuple[tuple[int, ...], str] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        parts = child.name.split(".")
        try:
            key = tuple(int(part) for part in parts[:3])
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, str(child))
    return best[1] if best else None


def tools_dir_of_compiler(compiler: str | os.PathLike[str]) -> str | None:
    """从 ``cl.exe`` 的路径反推工具集目录。

    布局是 ``<VS>\\VC\\Tools\\MSVC\\<版本>\\bin\\<Host>\\<目标>\\cl.exe``，
    所以 ``cl.exe`` 往上数四层就是工具集目录。
    """
    path = Path(compiler)
    if not _is_compiler(path):
        return None
    try:
        parent = path.parents[3]
    except IndexError:
        return None
    return str(parent) if (parent / "include").is_dir() else None


def find_compiler(vs_root: str | None = None) -> str | None:
    """返回最佳的 ``cl.exe`` 路径，找不到返回 ``None``。"""
    roots = (vs_root,) if vs_root else visual_studio_roots()
    for root in roots:
        resolved = tools_dir(root)
        if not resolved:
            continue
        for host, target in _host_target_pairs():
            candidate = Path(resolved) / "bin" / host / target / COMPILER_NAME
            if candidate.is_file():
                log.info("检测到 MSVC: %s", candidate)
                return str(candidate)
    return None


def find_compilers() -> tuple[str, ...]:
    """所有可用的 ``cl.exe``，用于让界面展示多个候选。"""
    found: list[str] = []
    for root in visual_studio_roots():
        resolved = tools_dir(root)
        if not resolved:
            continue
        for host, target in _host_target_pairs():
            candidate = Path(resolved) / "bin" / host / target / COMPILER_NAME
            if candidate.is_file() and str(candidate) not in found:
                found.append(str(candidate))
    return tuple(found)


# ----------------------------------------------------------------------
# Windows SDK
# ----------------------------------------------------------------------

_SDK_REGISTRY_KEYS = (
    r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots",
)


@functools.lru_cache(maxsize=1)
def windows_sdk() -> tuple[str | None, str | None]:
    """返回 ``(KitsRoot10, 版本号)``，例如 ``("C:\\\\Program Files (x86)\\\\Windows Kits\\\\10\\\\", "10.0.26100.0")``。

    用 ``winreg`` 直接读，不经过 ``reg.exe`` —— 后者在某些环境下会被安全策略拦住。
    """
    if sys.platform != "win32":
        return None, None
    try:
        import winreg
    except ImportError:
        return None, None

    root: str | None = None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey in _SDK_REGISTRY_KEYS:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "KitsRoot10")
            except OSError:
                continue
            if value and Path(str(value)).is_dir():
                root = str(value)
                break
        if root:
            break

    if not root:
        # 注册表里没有就猜默认位置
        for template in (r"%ProgramFiles(x86)%\Windows Kits\10",
                         r"%ProgramFiles%\Windows Kits\10"):
            expanded = Path(os.path.expandvars(template))
            if (expanded / "Include").is_dir():
                root = str(expanded) + os.sep
                break
    if not root:
        return None, None

    include_dir = Path(root) / "Include"
    best: tuple[tuple[int, ...], str] | None = None
    if include_dir.is_dir():
        for child in include_dir.iterdir():
            if not child.is_dir() or not child.name[:1].isdigit():
                continue
            try:
                key = tuple(int(part) for part in child.name.split("."))
            except ValueError:
                continue
            if best is None or key > best[0]:
                best = (key, child.name)
    return root, (best[1] if best else None)


# ----------------------------------------------------------------------
# 环境组装
# ----------------------------------------------------------------------

def build_environment(compiler: str) -> dict[str, str] | None:
    """为 ``cl.exe`` 组装 ``INCLUDE`` / ``LIB`` / ``PATH``。

    成功时返回一份**完整的环境字典**（在 ``os.environ`` 基础上覆盖），
    失败返回 ``None``。不可用时调用方应把它当作"该编译器无法使用"。
    """
    resolved = tools_dir_of_compiler(compiler)
    if not resolved:
        log.warning("无法从路径反推 MSVC 工具集目录: %s", compiler)
        return None
    tools = Path(resolved)
    target = Path(compiler).parent.name          # x64 / x86 / arm64
    host = Path(compiler).parent.parent.name     # HostX64 / HostX86

    includes: list[str] = []
    libs: list[str] = []

    toolset_include = tools / "include"
    if toolset_include.is_dir():
        includes.append(str(toolset_include))
    toolset_lib = tools / "lib" / target
    if toolset_lib.is_dir():
        libs.append(str(toolset_lib))
    # ATL / MFC 是可选组件，有就加上
    atlmfc_include = tools / "ATLMFC" / "include"
    if atlmfc_include.is_dir():
        includes.append(str(atlmfc_include))
    atlmfc_lib = tools / "ATLMFC" / "lib" / target
    if atlmfc_lib.is_dir():
        libs.append(str(atlmfc_lib))

    sdk_root, sdk_version = windows_sdk()
    if sdk_root and sdk_version:
        sdk_include = Path(sdk_root) / "Include" / sdk_version
        for name in ("ucrt", "shared", "um", "winrt", "cppwinrt"):
            candidate = sdk_include / name
            if candidate.is_dir():
                includes.append(str(candidate))
        sdk_lib = Path(sdk_root) / "Lib" / sdk_version
        for name in ("ucrt", "um"):
            candidate = sdk_lib / name / target
            if candidate.is_dir():
                libs.append(str(candidate))

    if not includes or not libs:
        log.warning("MSVC 环境不完整（INCLUDE %d 段 / LIB %d 段）",
                    len(includes), len(libs))
        return None

    bin_dir = Path(compiler).parent
    environment = dict(os.environ)
    environment["INCLUDE"] = ";".join(includes)
    environment["LIB"] = ";".join(libs)
    environment["LIBPATH"] = ";".join(libs)
    environment["PATH"] = str(bin_dir) + os.pathsep + environment.get("PATH", "")
    environment["VCToolsInstallDir"] = str(tools) + os.sep
    environment["Platform"] = target
    if sdk_root:
        environment["WindowsSdkDir"] = sdk_root
    if sdk_version:
        environment["WindowsSDKVersion"] = sdk_version + os.sep
    environment.setdefault("VSCMD_ARG_HOST_ARCH", host.replace("Host", "").lower())
    environment.setdefault("VSCMD_ARG_TGT_ARCH", target)
    return environment


@functools.lru_cache(maxsize=8)
def cached_environment(compiler: str) -> dict[str, str] | None:
    """带缓存的 :func:`build_environment`。

    ``cl.exe`` 每编译一次都要这份环境，而读注册表 + 列目录虽然只要几十毫秒，
    也没必要每次判题都重来。缓存的 key 是编译器路径 —— 同一路径的工具集
    在一次运行内不会变。
    """
    return build_environment(compiler)


# ----------------------------------------------------------------------
# 版本
# ----------------------------------------------------------------------

def version_text(compiler: str) -> str:
    """取 ``cl.exe`` 的版本描述。

    ``cl`` 不像 gcc 那样支持 ``--version`` —— 不带参数运行会打印横幅与用法，
    退出码是 0（**注意：这与 gcc 的约定完全不同**）。

    两个坑：

    1. 横幅走的是 **stderr**，而 stdout 里放的是用法说明（非空）。
       早期写成 ``stdout or stderr`` 会取到用法说明，永远解析不到版本号。
    2. 横幅**随系统语言本地化**，中文 Windows 上是
       「用于 x64 的 Microsoft (R) C/C++ 优化编译器 19.44.35228 版」。
       所以既要看两个流，也要同时匹配中英文形态。

    目标架构不解析横幅，改从路径推 —— 见 :func:`architecture`。
    """
    completed = run_silent([compiler], timeout=15)
    if completed.returncode not in (0, 1):
        return ""
    # 两个流都要看：横幅在 stderr，用法在 stdout，不同版本/语言下分布还会变
    text = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    version = ""
    for pattern in _VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            version = match.group(1)
            break
    if not version:
        return ""
    try:
        parts = tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        parts = ()
    product = _product_name(parts)
    arch = architecture(compiler)
    suffix = f" · {arch}" if arch else ""
    return f"{version} ({product}{suffix})"


def architecture(compiler: str | os.PathLike[str]) -> str:
    """从 ``cl.exe`` 路径推目标架构（``x64`` / ``x86`` / ``arm64``）。

    比解析横幅可靠：横幅里那句 ``for x64`` 会随语言变成「用于 x64 的」，
    而目录名永远是 ``bin\\<Host>\\<目标>\\cl.exe``。
    """
    name = Path(compiler).parent.name.lower()
    return name if name in ("x64", "x86", "arm64", "arm") else ""


def version_tuple(text: str) -> tuple[int, ...]:
    """从 :func:`version_text` 的结果里取出可比较的数字元组。"""
    match = re.search(r"(\d+(?:\.\d+)+)", text or "")
    if not match:
        return ()
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return ()


def cpp_standard(version: tuple[int, ...]) -> str:
    """按 MSVC 工具集版本给出可用的 C++ 标准参数。

    为什么不照搬 GCC 那套"失败就降级"的阶梯：**MSVC 对不认识的 ``/std:`` 不报错**，
    只发一条 ``warning D9002``，退出码仍是 0，然后**静默按默认标准编译**
    （实测 ``/std:c++23`` 编出来的 ``_MSVC_LANG`` 是 201402，即 C++14）。
    也就是说"编过了"完全不能证明这个标准被接受了，靠失败来探测是行不通的。

    版本对应关系（MSVC 内部版本号 -> 语言标准）：

    * ``19.14`` 起（VS2017 15.7）支持 ``/std:c++17``
    * ``19.00`` 起（VS2015 Update 3）支持 ``/std:c++14``
    * 再早的版本（VS2013 及以前）没有 ``/std:`` 这一族参数，只能用默认标准
    """
    if not version:
        return ""
    if version[:2] >= (19, 14):
        return "/std:c++17"
    if version[:2] >= (19, 0):
        return "/std:c++14"
    return ""


def c_standard(version: tuple[int, ...]) -> str:
    """按 MSVC 工具集版本给出可用的 C 标准参数。

    ``/std:c11`` 与 ``/std:c17`` 都是 VS2019 16.7（``19.27``）才加上的。
    更早的版本没有选择余地，只能用默认（C89 + MS 扩展）—— 但这仍然比不加参数好不了，
    所以干脆不加，避免多一条 D9002 噪音。
    """
    if not version:
        return ""
    if version[:2] >= (19, 30):
        return "/std:c17"
    if version[:2] >= (19, 27):
        return "/std:c11"
    return ""


def ignored_options(completed) -> tuple[str, ...]:
    """从编译输出里挑出被 MSVC 忽略的选项（``warning D9002``）。

    用途：MSVC 忽略某个 ``/std:`` 时不会失败，只会警告。用它来把这些选项
    从缓存里剔除，免得每次编译都带一条注定被忽略的参数。

    注意 D9002 **走 stderr**，而编译错误走 stdout —— 与 GCC 正好相反，
    所以两个流都要看（见 :func:`offline_oj.core.runners.CRunner.compile`）。
    """
    text = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    ignored: list[str] = []
    for line in text.splitlines():
        if "D9002" not in line:
            continue
        for quoted in re.findall(r"[“\"'](/\S+?)[”\"']", line):
            if quoted not in ignored:
                ignored.append(quoted)
    return tuple(ignored)


def _normalize_version(parts: tuple[int, ...]) -> tuple[int, ...]:
    """把工具集目录里的 ``14.44.35207`` 归一成编译器版本 ``(19, 44)``。

    VS2015 之后工具集目录用 ``14.x``，而 ``cl.exe`` 自称 ``19.x``，
    两者靠这条对应关系换算 —— 否则版本比较会得出"VS2022 比 VS2015 老"的结论。
    """
    if not parts:
        return ()
    if parts[0] == 14:
        return (19, parts[1] if len(parts) > 1 else 0)
    if parts[0] == 19:
        return parts[:2] if len(parts) >= 2 else (19, 0)
    return parts[:2]


@functools.lru_cache(maxsize=8)
def toolset_version(compiler: str) -> tuple[int, ...]:
    """``cl.exe`` 的编译器版本元组，如 ``(19, 44)``。

    优先读横幅（那是编译器自己报的版本）；读不到再从工具集目录名反推。
    """
    banner = version_tuple(version_text(compiler))
    if banner:
        return _normalize_version(banner)
    resolved = tools_dir_of_compiler(compiler)
    if resolved:
        try:
            parts = tuple(int(chunk) for chunk in Path(resolved).name.split(".")[:3])
        except ValueError:
            return ()
        return _normalize_version(parts)
    return ()


@functools.lru_cache(maxsize=16)
def standard_flag(compiler: str, cpp: bool) -> str:
    """这台机器上该给 ``cl.exe`` 配哪个标准参数；不需要贡献时返回空串。"""
    version = toolset_version(compiler)
    return cpp_standard(version) if cpp else c_standard(version)


def probe(compiler: str) -> tuple[bool, str]:
    """真正编译并运行一个最小程序，确认这套环境可用。

    只检查"文件存在 + 能打印版本"是不够的：``cl.exe`` 没有 vcvars 环境时
    照样能打印版本，一编译就报 ``C1034``。所以这里必须实际编一次。
    """
    import subprocess
    import tempfile

    from .process import creation_flags

    environment = cached_environment(compiler)
    if environment is None:
        return False, "无法组装 MSVC 编译环境（INCLUDE / LIB 缺失）"

    with tempfile.TemporaryDirectory(prefix="oj_msvc_probe_") as folder:
        source = Path(folder) / "main.cpp"
        source.write_text(
            '#include <iostream>\nint main() { std::cout << "OJSELFTEST"; return 0; }\n',
            encoding="utf-8", newline="\n",
        )
        binary = Path(folder) / "main.exe"
        try:
            completed = subprocess.run(
                [compiler, "/nologo", "/EHsc", "/O2", "/MT", "/utf-8",
                 f"/Fe:{binary}", f"/Fo:{Path(folder) / 'main.obj'}", str(source)],
                cwd=folder, env=environment, capture_output=True, timeout=180,
                creationflags=creation_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"调用 cl.exe 失败: {exc}"

        if completed.returncode != 0 or not binary.is_file():
            output = (completed.stdout or b"").decode("mbcs", "replace") + \
                     (completed.stderr or b"").decode("mbcs", "replace")
            return False, output.strip()[:400] or "编译失败"

        try:
            run = subprocess.run([str(binary)], capture_output=True, timeout=30,
                                 creationflags=creation_flags())
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"运行编译产物失败: {exc}"
        if b"OJSELFTEST" not in run.stdout:
            return False, f"编译产物输出异常: {run.stdout[:120]!r}"
    return True, "编译并运行通过"


__all__ = [
    "COMPILER_NAME",
    "architecture",
    "build_environment",
    "c_standard",
    "cached_environment",
    "cpp_standard",
    "find_compiler",
    "find_compilers",
    "find_vswhere",
    "ignored_options",
    "is_msvc",
    "probe",
    "standard_flag",
    "tools_dir",
    "tools_dir_of_compiler",
    "toolset_version",
    "version_text",
    "version_tuple",
    "visual_studio_roots",
    "windows_sdk",
]
