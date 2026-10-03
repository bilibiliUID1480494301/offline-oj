"""编译器配置面板。

两个动作的区别值得说清楚，界面上也分开提示：

* **自动检测** —— 快速找到候选路径（查 PATH、注册表、常见目录）并填进输入框。
  只跑 ``--version``，几百毫秒；
* **验证可用性** —— 真编译并真运行一段最小程序。耗时几秒到十几秒，但能发现
  "版本号打得出、实际编译失败"的情况（32/64 位不匹配、缺 DLL、权限受限）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.compilers import KEY_LABELS, CompilerInfo
from ...core.validation import PathValidator
from ...win32 import process as win_process
from ..theme import (
    CAPTION_SPACING,
    GROUP_MARGINS,
    PAGE_MARGINS,
    ROW_SPACING,
    SECTION_SPACING,
    Palette,
)
from ..widgets import OutputView, PathPicker
from ..workers import CompilerDetectWorker, CompilerVerifyWorker
from .base import Panel

#: 配置键顺序（与对话框、状态栏一致：编译期依赖在前）
KEYS = ("cpp", "c", "python", "javac", "java")

HINTS = {
    "cpp": "g++ / clang++，编译 .cpp",
    "c": "gcc / clang，编译 .c",
    "python": "python.exe，解释执行 .py",
    "javac": "javac.exe，编译 .java",
    "java": "java.exe，运行 class",
}


class CompilersPanel(Panel):
    """工具链路径配置与验证。"""

    def _build(self) -> None:
        self.pickers: dict[str, PathPicker] = {}
        self.infos: dict[str, CompilerInfo | None] = {}
        self._detect_worker: CompilerDetectWorker | None = None
        self._verify_worker: CompilerVerifyWorker | None = None

        title = QLabel("工具链配置")
        title.setProperty("role", "h2")
        subtitle = QLabel(
            "本机安装的编译器路径。首次使用请先点「自动检测」，确认可用后「保存配置」。"
        )
        subtitle.setProperty("muted", True)
        subtitle.setWordWrap(True)

        # ---- 路径表单 ----
        group = QGroupBox("解释器 / 编译器路径")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setContentsMargins(*GROUP_MARGINS)
        form.setSpacing(ROW_SPACING)
        for key in KEYS:
            picker = PathPicker(
                caption=f"选择 {KEY_LABELS[key]}",
                file_filter="可执行文件 (*.exe *.bat *.cmd);;所有文件 (*)",
            )
            picker.changed.connect(lambda _text, k=key: self._on_path_edited(k))
            self.pickers[key] = picker
            # 名称加粗、提示用小字。以前是塞进同一个 QLabel 用 \n 拼的，
            # 结果是提示文字跟着一起加粗 —— 两行一样重的字，读起来分不出主次。
            caption = QWidget()
            caption_layout = QVBoxLayout(caption)
            caption_layout.setContentsMargins(0, 0, 0, 0)
            caption_layout.setSpacing(CAPTION_SPACING)
            name_label = QLabel(KEY_LABELS[key])
            name_label.setProperty("role", "strong")
            hint_label = QLabel(HINTS[key])
            hint_label.setProperty("role", "caption")
            hint_label.setWordWrap(True)
            caption_layout.addWidget(name_label)
            caption_layout.addWidget(hint_label)
            form.addRow(caption, picker)

        # ---- 按钮 ----
        self.detect_button = QPushButton("自动检测")
        self.detect_button.setToolTip("在 PATH、注册表与常见安装目录中寻找工具链")
        self.detect_button.clicked.connect(self.auto_detect)

        self.verify_button = QPushButton("验证可用性")
        self.verify_button.setToolTip("真实编译并运行一段最小程序，耗时数秒")
        self.verify_button.clicked.connect(self.verify)

        self.save_button = QPushButton("保存配置")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)

        self.reveal_button = QPushButton("打开数据目录")
        self.reveal_button.setProperty("flat", True)
        self.reveal_button.clicked.connect(
            lambda: win_process.reveal_in_explorer(self.ctx.paths.data_root)
        )

        buttons = QHBoxLayout()
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.verify_button)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        buttons.addWidget(self.reveal_button)

        # ---- 结果区 ----
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)   # 不确定进度

        self.output = OutputView(self.palette)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(SECTION_SPACING)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(group)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("验证结果"), 0)
        layout.addWidget(self.output, 1)

        self.load_from_settings()

    # ---- 数据同步 ---------------------------------------------------------

    def load_from_settings(self) -> None:
        for key, picker in self.pickers.items():
            path = self.ctx.settings.compiler_path(key)
            picker.set_path(path)
            if path:
                self._refresh_status(key, path)
            else:
                picker.set_status("未配置", self.palette.text_muted)

    def save(self) -> None:
        """把输入框里的路径写回设置。"""
        invalid: list[str] = []
        for key, picker in self.pickers.items():
            path = picker.path()
            self.ctx.settings.set_compiler_path(key, path)
            if path:
                valid, message = PathValidator.validate_executable(path)
                if not valid:
                    invalid.append(f"{KEY_LABELS[key]}：{message}")
        self.ctx.settings.save()
        self.status("编译器配置已保存")

        if invalid:
            self.notify.emit("warning", "以下路径已保存，但看起来有问题：\n\n" + "\n".join(invalid))
        self.output.append_line("配置已保存", self.palette.success)

    def on_settings_changed(self) -> None:
        self.load_from_settings()

    def _refresh_status(self, key: str, path: str) -> None:
        valid, message = PathValidator.validate_executable(path)
        picker = self.pickers[key]
        if valid:
            picker.set_status("待验证", self.palette.warning, tooltip="点「验证可用性」确认能否真正编译运行")
        else:
            picker.set_status("无效", self.palette.danger, tooltip=message)

    def _on_path_edited(self, key: str) -> None:
        path = self.pickers[key].path()
        self.ctx.settings.set_compiler_path(key, path)
        if path:
            self._refresh_status(key, path)
        else:
            self.pickers[key].set_status("未配置", self.palette.text_muted)

    # ---- 自动检测 ---------------------------------------------------------

    def auto_detect(self) -> None:
        if self._worker_running():
            return
        self.set_busy(True, "正在自动检测编译器…")
        self.output.append_line("开始自动检测…", self.palette.info)

        worker = CompilerDetectWorker(KEYS, self)
        worker.progress.connect(self.status)
        worker.detected.connect(self._on_detected)
        worker.done.connect(self._on_detect_done)
        worker.finished.connect(lambda: self.set_busy(False))
        self._detect_worker = worker
        worker.start()

    def _on_detected(self, key: str, info: object) -> None:
        label = KEY_LABELS.get(key, key)
        if isinstance(info, CompilerInfo):
            self.infos[key] = info
            self.pickers[key].set_path(info.path)
            self.pickers[key].set_status("待验证", self.palette.warning,
                                         tooltip="自动检测到的路径，建议点「验证可用性」")
            self.ctx.settings.set_compiler_path(key, info.path)
            self.output.append_line(f"✓ {label}: {info.path}", self.palette.success)
            self.output.append_line(f"    {info.version}", self.palette.text_muted)
        else:
            self.infos[key] = None
            self.pickers[key].set_status("未找到", self.palette.danger)
            self.output.append_line(f"✗ {label}: 未在本机找到", self.palette.warning)

    def _on_detect_done(self, result: dict) -> None:
        found = sum(1 for value in result.values() if value is not None)
        self.ctx.settings.save()
        self.output.append_line(f"检测完成，找到 {found}/{len(KEYS)} 个工具链",
                                self.palette.info, bold=True)
        self.status(f"检测完成：{found}/{len(KEYS)}")
        if found:
            self.output.append_line("建议接着点「验证可用性」做一次真编译检查。",
                                    self.palette.text_muted)

    # ---- 验证 -------------------------------------------------------------

    def verify(self) -> None:
        if self._worker_running():
            return
        self.set_busy(True, "正在验证工具链…")
        self.output.append_line("开始验证（真实编译并运行）…", self.palette.info)

        worker = CompilerVerifyWorker(self._current_paths(), self.ctx.paths.work_dir, self)
        worker.progress.connect(self.status)
        worker.verified.connect(self._on_verified)
        worker.done.connect(self._on_verify_done)
        worker.finished.connect(lambda: self.set_busy(False))
        self._verify_worker = worker
        worker.start()

    def _on_verified(self, key: str, ok: bool, message: str) -> None:
        label = KEY_LABELS.get(key, key)
        if ok:
            self.pickers[key].set_status("可用", self.palette.success, tooltip=message)
            self.output.append_line(f"✓ {label}: 可用", self.palette.success)
            self.infos[key] = (self.infos.get(key) or
                               CompilerInfo(key=key, path=self.pickers[key].path()))
            if self.infos[key] is not None:
                self.infos[key] = self.infos[key].with_status(True, message)
        else:
            self.pickers[key].set_status("不可用", self.palette.danger, tooltip=message)
            self.output.append_line(f"✗ {label}: {message}", self.palette.danger)

    def _on_verify_done(self, results: dict) -> None:
        ok_count = sum(1 for ok, _ in results.values() if ok)
        self.output.append_line(f"验证完成：{ok_count}/{len(results)} 项可用",
                                self.palette.info, bold=True)
        self.status(f"验证完成：{ok_count}/{len(results)} 项可用")

    # ---- 工具 -------------------------------------------------------------

    def _current_paths(self) -> dict[str, str]:
        return {key: picker.path() for key, picker in self.pickers.items()}

    def _worker_running(self) -> bool:
        for worker in (self._detect_worker, self._verify_worker):
            if worker is not None and worker.isRunning():
                self.status("已有检测任务在进行，请稍候…")
                return True
        return False

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.progress.setVisible(busy)
        for button in (self.detect_button, self.verify_button, self.save_button):
            button.setEnabled(not busy)
        if busy and message:
            self.status(message)

    def apply_palette(self, palette: Palette) -> None:
        super().apply_palette(palette)
        if hasattr(self, "output"):
            self.output.set_palette_theme(palette)

    def on_closing(self) -> bool:
        for worker in (self._detect_worker, self._verify_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(3000)
        return True
