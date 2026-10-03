# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

选择 **onedir（文件夹模式）** 而不是 onefile：

* onefile 每次启动都要把上百 MB 的 Qt 运行时解包到临时目录，首屏要等 3~6 秒，
  且杀软会对"自解压到 Temp 的 exe"额外扫描；
* onedir 首屏约 1 秒，安装包里也更适合做增量更新。

用法::

    pyinstaller packaging/offline_oj.spec --noconfirm
"""

from pathlib import Path

# SPECPATH 是本 spec 文件所在目录（packaging/）
PACKAGING_DIR = Path(SPECPATH).resolve()
ROOT = PACKAGING_DIR.parent

APP_NAME = "OfflineOJ"
ICON = ROOT / "assets" / "oj_icon.ico"
MANIFEST = PACKAGING_DIR / "app.manifest"
VERSION_FILE = PACKAGING_DIR / "version_info.txt"

# ---- 打包进去的只读资源 -------------------------------------------------
# 目标目录 'assets' 与 offline_oj.paths 里的 resource_root/assets 约定一致
datas = []
if ICON.exists():
    datas.append((str(ICON), "assets"))
    png = ICON.with_suffix(".png")
    if png.exists():
        datas.append((str(png), "assets"))

# ---- 不打包的大块头 -----------------------------------------------------
# Qt 的 WebEngine / Quick / 3D 加起来就有 200 MB+，本项目一个都用不到。
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    # 科学计算栈：本程序不用，但环境里装了就会被打进去
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "tkinter",
    "PIL",
    "pytest",
]

a = Analysis(
    [str(PACKAGING_DIR / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 压缩容易触发杀软误报，不使用
    console=False,             # GUI 子系统：不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
    manifest=str(MANIFEST) if MANIFEST.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
