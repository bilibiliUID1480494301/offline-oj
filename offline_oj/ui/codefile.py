"""代码文件的读写（「打开…」／「另存为…」）。

练习面板和测验面板（学生端）的编辑器都要这两个动作，所以读写、"该给什么
后缀"以及整套对话框流程都收在这里，不在两个面板里各写一遍。

三件不写清楚就会变成难查的怪问题的事：

* **BOM。** 记事本「另存为 UTF-8」默认带 BOM。按 ``utf-8`` 读进来，文件开头
  会多一个看不见的 ``\\ufeff`` —— 编译器的报错指在第 1 行的别处，怎么读都读不出
  原因。所以读一律用 ``utf-8-sig``：有 BOM 就剥掉，没有也照读。
* **不是 UTF-8 的文件。** 直接说清楚"这个文件不是 UTF-8"，而不是把一串
  ``UnicodeDecodeError`` 的十六进制摊给用户看。GBK 存的中文代码在中文 Windows
  上很常见，这是一定会发生的一类输入。
* **写。** 一律 ``utf-8`` 且**不写** BOM。判题工具链按 UTF-8 读源码
  （MSVC 那边还显式加了 ``/utf-8``），主动写 BOM 反而是制造问题。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ..core.models import Language

#: 「所有文件」过滤器。学生手里的文件名五花八门，不能只给当前语言一种。
ALL_FILES = "所有文件 (*)"

#: 各语言认得的源码后缀，用于拼过滤器。
#: 只列**常见**的：给 C++ 罗列五六个后缀会让过滤器长到读不下去。
SUFFIXES: dict[Language, tuple[str, ...]] = {
    Language.CPP: (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h"),
    Language.C: (".c", ".h"),
    Language.PYTHON: (".py", ".pyw"),
    Language.JAVA: (".java",),
}

#: 「另存为」默认文件名的主干。用 main 而不是"未命名"：判题按题目英文名
#: 命名源程序，学生手动存盘时取 main 最不容易和别的东西撞。
DEFAULT_STEM = "main"


class CodeFileError(Exception):
    """读写代码文件时的用户可读错误。消息直接拿去显示，不用再包装。"""


def suffixes(language: Language) -> tuple[str, ...]:
    """这门语言认得的后缀。"""
    return SUFFIXES.get(language, (language.suffix,))


def default_name(language: Language) -> str:
    """「另存为」对话框里的默认文件名。"""
    return f"{DEFAULT_STEM}{language.suffix}"


def file_filter(language: Language) -> str:
    """``QFileDialog`` 的过滤器串。当前语言排在最前面，最后兜一个「所有文件」。"""
    patterns = " ".join(f"*{suffix}" for suffix in suffixes(language))
    return f"{language.short} 源文件 ({patterns});;{ALL_FILES}"


def language_for_path(path: str) -> Language | None:
    """按后缀猜语言 —— 打开一个 ``.py`` 却还用 C++ 着色是说不通的。"""
    suffix = Path(path).suffix.lower()
    if not suffix:
        return None
    for language in Language:
        if suffix in suffixes(language):
            return language
    return None


def initial_directory(hint: str, *, fallback: str = "") -> str:
    """打开对话框时从哪个目录开始。

    传进来的可能是文件（上次存的那个）也可能是目录。指向一个**已经不在了**的
    路径时要**逐级往上**找一个真实存在的目录 —— 否则 Windows 会让对话框停在一个
    空目录里，看起来像"我的文件都不见了"。

    往上找而不是只看父目录：整个工作目录被搬走 / 改名时，父目录也不存在，
    这时候还退到父目录等于没退。
    """
    if not hint:
        return fallback
    path = Path(hint)
    if path.is_dir():
        return str(path)
    if path.is_file():
        return str(path.parent)
    for parent in path.parents:
        if parent.is_dir():
            return str(parent)
    return fallback


def read_code(path: str | os.PathLike[str]) -> str:
    """读一份源码。BOM 会被剥掉。

    :raises CodeFileError: 文件读不了或不是 UTF-8，消息可以直接显示给用户。
    """
    try:
        # ``utf-8-sig``：有 BOM 就剥掉，没有也照读 —— 不能顺手改成 ``utf-8``。
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CodeFileError(
            f"这个文件不是 UTF-8 编码（{os.path.basename(str(path))}）。\n"
            "用记事本打开 →「另存为」→ 编码选 UTF-8，再试一次。"
        ) from exc
    except OSError as exc:
        raise CodeFileError(f"读不了这个文件：{exc}") from exc


def write_code(path: str | os.PathLike[str], text: str) -> None:
    """把源码写到文件。一律 UTF-8、**不写 BOM**。

    换行统一成 ``\\r\\n``：Windows 上的记事本对只有 ``\\n`` 的文件会当成
    "一整行"显示。判题工具链两者都吃，所以这里只考虑"学生双击打开好不好看"。

    :raises CodeFileError: 写不进去（没有权限、目录不存在、被占用）。
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    try:
        Path(path).write_text(normalised, encoding="utf-8", newline="")
    except OSError as exc:
        raise CodeFileError(f"写不进去：{exc}") from exc


def file_label(path: str, *, dirty: bool) -> str:
    """编辑器标题行上那行小字：当前文件名，改过就加个后缀。

    ``path`` 为空返回空串 —— 没有关联文件时不摆一个"未命名"的占位，
    那会变成第三个说"这里没有东西"的地方，而这行字本来就是为了说"有文件"。
    """
    if not path:
        return ""
    name = os.path.basename(path)
    return f"{name}・未保存" if dirty else name


class CodeFileController:
    """把「打开…／另存为…」接到一个代码编辑器上。

    练习面板与测验面板（学生端）用的是同一套逻辑，差别只有三处：编辑器、
    当前语言从哪儿来、以及往哪儿报错。所以收成一个对象，两边各持有一个 ——
    复制一份的话，"BOM 忘了剥"这种修法就得改两遍，而漏掉的那一边不会报错，
    只会在某台机器上莫名其妙地编译失败。

    :param widget: 对话框的父窗口。``None`` 会让对话框没有宿主（离屏测试里
        会变成一个独立窗口），所以一定要传面板。
    :param editor: :class:`~offline_oj.ui.widgets.CodeEditor`
    :param settings: ``AppSettings``，用来记住上次的目录
    :param notify: ``Signal(str, str)``，``(级别, 消息)``
    :param label: 显示"当前文件・未保存"的 ``QLabel``，可以不传
    :param current_language: 取当前语言
    :param select_language: 按后缀切到对应语言；不传就只提示不改下拉框
    """

    def __init__(
        self,
        *,
        widget: QWidget,
        editor,
        settings,
        notify,
        current_language: Callable[[], Language],
        select_language: Callable[[Language], None] | None = None,
        label=None,
    ) -> None:
        self._widget = widget
        self._editor = editor
        self._settings = settings
        self._notify = notify
        self._current_language = current_language
        self._select_language = select_language
        self._label = label
        #: 当前代码来自（或存到）哪个文件；空串 = 还没和任何文件关联过。
        self._path = ""
        #: 上次读/写文件时编辑器里的内容，用来判断"有没有改动"。
        self._saved_text = ""

    # ---- 状态 -------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def is_dirty(self) -> bool:
        return self._editor.toPlainText() != self._saved_text

    def label_text(self) -> str:
        return file_label(self._path, dirty=self.is_dirty())

    def refresh_label(self) -> None:
        if self._label is not None:
            self._label.setText(self.label_text())

    def forget(self) -> None:
        """内容被整体换掉了（清空 / 插入模板），从此不再对应任何一个文件。"""
        self._path = ""
        self._saved_text = ""
        self.refresh_label()

    def adopt(self, path: str, text: str) -> None:
        """把 ``text`` 装进编辑器，并把它认成"这就是 ``path`` 里的内容"。

        顺序有讲究：``setPlainText`` 会**同步**触发内容变化回调（那份回调把状态
        标成"有改动"），所以文件关联和"已保存内容"必须在它**之后**落定，
        否则刚打开的文件一进来就显示成"未保存"。
        """
        guessed = language_for_path(path)
        if guessed is not None and guessed is not self._current_language():
            if self._select_language is not None:
                self._select_language(guessed)
            else:
                # 切不了也要说一声：用 C++ 的规则给 .py 着色，关键字会全错
                self._notify.emit(
                    "info", f"这个文件看起来是 {guessed.short}，"
                            f"记得把语言切过去。")
        self._editor.setPlainText(text)
        self._path = path
        self._saved_text = text
        self._remember_directory(path)
        self.refresh_label()

    # ---- 两个动作 ---------------------------------------------------------

    def open_file(self) -> bool:
        """从文件读一份代码进来。被取消或读失败返回 ``False``。"""
        if not self.confirm_discard("打开另一个文件"):
            return False
        path, _ = QFileDialog.getOpenFileName(
            self._widget, "打开代码文件", self._directory(),
            file_filter(self._current_language()))
        if not path:
            return False
        try:
            text = read_code(path)
        except CodeFileError as exc:
            # 消息本身就是给用户看的（含"改编码"的做法），不用再包一层
            self._notify.emit("error", str(exc))
            return False
        self.adopt(path, text)
        return True

    def save_as(self) -> bool:
        """把编辑器里的代码存成文件。被取消或写失败返回 ``False``。"""
        language = self._current_language()
        suggested = self._path or os.path.join(
            self._directory(), default_name(language))
        path, _ = QFileDialog.getSaveFileName(
            self._widget, "代码另存为", suggested, file_filter(language))
        if not path:
            return False
        text = self._editor.toPlainText()
        try:
            write_code(path, text)
        except CodeFileError as exc:
            self._notify.emit("error", str(exc))
            return False
        self._path = path
        self._saved_text = text
        self._remember_directory(path)
        self.refresh_label()
        return True

    def confirm_discard(self, action: str) -> bool:
        """编辑器里有没存过的内容时，问一句再往下走。

        只在"真的会丢东西"时才问：编辑器空着、或者内容和上次存的一致，
        就没有什么可丢的 —— 每次都弹一个"确定吗"会让这个提示失去意义。
        """
        if not self._editor.toPlainText().strip() or not self.is_dirty():
            return True
        box = QMessageBox(self._widget)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("代码还没有保存")
        box.setText(f"{action}会丢掉编辑器里未保存的代码。")
        box.setInformativeText("先用「另存为…」存一份，再继续？")
        proceed = box.addButton("不保存，继续", QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is proceed

    # ---- 内部 -------------------------------------------------------------

    def _directory(self) -> str:
        """对话框从哪个目录开始 —— 上次用过哪个就还从那儿开始。"""
        remembered = str(self._settings.get("code_directory", "") or "")
        return initial_directory(self._path or remembered, fallback=remembered)

    def _remember_directory(self, path: str) -> None:
        self._settings.set("code_directory", initial_directory(path))
