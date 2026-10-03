"""验收冻结版产物：\"源码能跑\"不代表\"打包出来的 exe 能跑\"。

用法::

    python tools\\verify_frozen.py                       # 静态验收 dist\\OfflineOJ
    python tools\\verify_frozen.py --dist dist\\OfflineOJ
    python tools\\verify_frozen.py --launch              # 追加双实例运行验收

静态项（不启动进程，随时可跑）：

* **PE 头**：x64、子系统为 2（GUI，不该有控制台窗口）、DllCharacteristics 里的
  高熵地址/动态基址/NX 都该在 —— 少一个就说明改坏了 spec；
* **图标**：把 ``assets/oj_icon.ico`` 里每一帧的图像数据拿去 exe 里找。
  这条是真会坏的：``PIL.Image.save(sizes=[...])`` 那种写法只会写进一帧，
  任务栏图标糊成一团，而且**不报错**；
* **版本资源**：中英文版本串以 UTF-16LE 存放，按 UTF-16LE 找；
* **清单**：PerMonitorV2（高 DPI 不发虚）、longPathAware、asInvoker、Common-Controls；
* **面板模块真的在里面**：静态 ``import`` 的模块会被打进 PYZ，但一旦有人图省事写成
  ``importlib.import_module(f".{name}")``，那些面板在冻结版里就**整个消失** ——
  源码跑得好好的，exe 一开就少几个选项卡。这里逐个名字去产物里找。

``--launch`` 会真的起两次进程（离屏，不弹窗）：第一次应进入事件循环，
第二次应在 1 秒内唤出已有窗口后自行退出（单实例互斥体 + 管道激活）。
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 除面板外，必须出现在冻结产物里的关键模块。
#: 面板那一堆是**扫目录**得来的（见 required_modules），不手写 ——
#: 手写清单会随重构过期，而"少了一个面板"正是这里要抓的东西。
CRITICAL_MODULES = (
    "offline_oj.app",
    "offline_oj.ui.main_window",
    "offline_oj.ui.theme",
    "offline_oj.core.sandbox",
    "offline_oj.core.judge",
    "offline_oj.net.server",
    "offline_oj.net.client",
    "offline_oj.win32.process",
)


def required_modules() -> list[str]:
    """要验的模块名：关键模块 + 面板目录下扫到的一切。"""
    names = list(CRITICAL_MODULES)
    panels = ROOT / "offline_oj" / "ui" / "panels"
    for path in sorted(panels.glob("*.py")):
        if path.name == "__init__.py":
            continue
        names.append(f"offline_oj.ui.panels.{path.stem}")
    return names


MANIFEST_PROBES = (
    ("PerMonitorV2", "高 DPI 缩放声明"),
    ("longPathAware", "超过 260 字符的路径"),
    ("asInvoker", "不需要管理员权限"),
    ("Common-Controls", "现代控件外观"),
)

#: DllCharacteristics 位（必须开着）
DLL_FLAGS = (
    (0x0020, "HIGH_ENTROPY_VA"),
    (0x0040, "DYNAMIC_BASE"),
    (0x0100, "NX_COMPAT"),
)


class Failure(Exception):
    """一项验收没过。"""


def parse_pe(data: bytes) -> dict:
    if data[:2] != b"MZ":
        raise Failure("不是 PE 文件（没有 MZ 头）")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise Failure("PE 签名不对")
    coff = offset + 4
    machine, sections = struct.unpack_from("<HH", data, coff)
    optional = coff + 20
    size_of_optional = struct.unpack_from("<H", data, coff + 16)[0]
    magic = struct.unpack_from("<H", data, optional)[0]
    # 子系统在标准字段的固定偏移上，PE32 与 PE32+ 都是 68
    subsystem, dll_chars = struct.unpack_from("<HH", data, optional + 68)
    return {
        "machine": machine,
        "sections": sections,
        "magic": magic,
        "size_of_optional": size_of_optional,
        "subsystem": subsystem,
        "dll_chars": dll_chars,
    }


def ico_frames(path: Path) -> list[tuple[int, int, bytes]]:
    """拆 ICO 容器，返回 (宽, 高, 该帧字节)。"""
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    if reserved != 0 or kind != 1:
        raise Failure(f"{path.name} 不是 ICO（reserved={reserved} type={kind}）")
    frames = []
    for index in range(count):
        entry = 6 + index * 16
        width, height, _colors, _res, _planes, _bits, size, start = struct.unpack_from(
            "<BBBBHHII", raw, entry)
        frames.append((width or 256, height or 256, raw[start:start + size]))
    return frames


def check_static(dist: Path) -> list[str]:
    exe = dist / "OfflineOJ.exe"
    if not exe.exists():
        raise Failure(f"找不到产物 {exe}（先跑一次打包）")
    data = exe.read_bytes()
    lines: list[str] = []

    total = sum(item.stat().st_size for item in dist.rglob("*") if item.is_file())
    count = sum(1 for item in dist.rglob("*") if item.is_file())
    lines.append(f"产物 {exe.relative_to(ROOT)} · {len(data) / 1e6:.2f} MB · "
                 f"整目录 {total / 1e6:.1f} MB / {count} 个文件")

    pe = parse_pe(data)
    if pe["machine"] != 0x8664:
        raise Failure(f"不是 x64（machine=0x{pe['machine']:04x}）")
    if pe["magic"] != 0x20B:
        raise Failure(f"不是 PE32+（magic=0x{pe['magic']:04x}）")
    if pe["subsystem"] != 2:
        raise Failure(f"子系统应为 2（GUI），实际 {pe['subsystem']} —— "
                      f"3 是控制台，双击会弹黑窗")
    absent = [name for bit, name in DLL_FLAGS if not pe["dll_chars"] & bit]
    if absent:
        raise Failure(f"DllCharacteristics 缺 {', '.join(absent)}")
    lines.append(f"PE 头 x64 / PE32+ / 子系统 2（GUI）/ "
                 f"DllCharacteristics 0x{pe['dll_chars']:04x} ✓")

    icon = ROOT / "assets" / "oj_icon.ico"
    frames = ico_frames(icon)
    sizes = ", ".join(f"{w}×{h}" for w, h, _ in frames)
    missing = [f"{w}×{h}" for w, h, payload in frames if payload not in data]
    if missing:
        raise Failure(f"图标这些尺寸没进 exe：{', '.join(missing)} —— "
                      f"任务栏图标会糊，而且不会报错")
    lines.append(f"图标 {len(frames)} 帧全部在产物里（{sizes}）✓")

    version = _app_version()
    needle = version.encode("utf-16-le")
    if needle not in data:
        raise Failure(f"版本资源里找不到 {version}（UTF-16LE）")
    lines.append(f"版本资源 {version}（UTF-16LE）✓")

    for probe, why in MANIFEST_PROBES:
        if probe.encode() not in data:
            raise Failure(f"清单缺少 {probe}（{why}）")
    lines.append(f"清单 {' / '.join(p for p, _ in MANIFEST_PROBES)} ✓")

    absent = [name for name in required_modules() if name.encode() not in data]
    if absent:
        raise Failure("这些模块不在冻结产物里（动态导入的典型症状）：\n  "
                      + "\n  ".join(absent))
    lines.append(f"{len(required_modules())} 个关键模块（含全部面板）都在产物里 ✓")
    return lines


def _app_version() -> str:
    import offline_oj

    return offline_oj.__version__


def _run(args: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)


def check_launch(dist: Path) -> list[str]:
    """起两次：第一次进入事件循环，第二次唤出已有窗口后退出。"""
    exe = dist / "OfflineOJ.exe"
    home = ROOT / "build" / "frozen-check"
    if home.exists():
        import shutil

        shutil.rmtree(home)
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["OFFLINE_OJ_HOME"] = str(home)

    lines: list[str] = []
    first = _run([str(exe)], env)
    try:
        time.sleep(6.0)
        if first.poll() is not None:
            raise Failure(f"第一次启动就退了（退出码 {first.returncode}）："
                          f"{first.stdout.read().decode('utf-8', 'replace')[:800]}")
        lines.append("第一次启动：仍在事件循环里 ✓")

        started = time.monotonic()
        second = _run([str(exe)], env)
        try:
            code = second.wait(timeout=15)
        except subprocess.TimeoutExpired:
            second.kill()
            raise Failure("第二次启动 15 秒还没退出 —— 单实例互斥体没起作用，"
                          "或者没能唤出已有窗口") from None
        elapsed = time.monotonic() - started
        if code != 0:
            raise Failure(f"第二次启动退出码为 {code}（应为 0）")
        if first.poll() is not None:
            raise Failure("第二次启动把第一个实例弄死了 —— 应该只唤出它")
        lines.append(f"第二次启动：{elapsed:.2f}s 内退出（码 0），第一个实例还活着 ✓")
    finally:
        first.terminate()
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()

    log = home / "logs" / "app.log"
    if not log.exists():
        raise Failure(f"没有日志 {log} —— 数据目录没建起来")
    text = log.read_text(encoding="utf-8", errors="replace")
    errors = [line for line in text.splitlines() if " ERROR " in line or
              line.startswith("ERROR")]
    if errors:
        raise Failure("日志里有 ERROR：\n  " + "\n  ".join(errors[:10]))
    lines.append(f"全新数据目录已建起来，日志 {len(text.splitlines())} 行、ERROR 0 条 ✓")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结版产物验收")
    parser.add_argument("--dist", default=str(ROOT / "dist" / "OfflineOJ"))
    parser.add_argument("--launch", action="store_true",
                        help="追加双实例运行验收（会真的起进程，离屏不弹窗）")
    args = parser.parse_args()

    dist = Path(args.dist)
    try:
        lines = check_static(dist)
        if args.launch:
            lines.extend(check_launch(dist))
    except Failure as exc:
        print(f"验收失败：{exc}", file=sys.stderr)
        return 1

    print("\n".join(lines))
    print("\n冻结版验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
