"""把导出报告渲染成 PDF。

**为什么这一段在 ``ui/`` 而不是 ``core/export.py``。** ``core/`` 层的约定是
不 import PySide6（见 ``core/__init__.py``）—— 那一层要能在没有图形环境的机器上
单测，也要能被命令行入口复用。而 PDF 的渲染只能走 Qt：

* ``reportlab`` 要嵌中文字体（几 MB TTF），不嵌就是一片方块，而且它依赖 PIL，
  而 ``packaging/offline_oj.spec`` 明确把 PIL 排除了；
* Qt 自带的 ``QPdfWriter`` + ``QTextDocument`` 用系统字体渲染中文，
  **零新依赖**，装完就有。

所以分工是：``core/export.py`` 产出 HTML（纯字符串，可单测），这里负责
把它印成 PDF。两边都只碰自己该碰的东西。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: 正文用系统默认字体的字号（磅）。正文 10.5pt ≈ Word 的"五号"。
BODY_POINT_SIZE = 10.5


def render_pdf(html: str, path: str | Path, *, title: str = "",
               landscape: bool = True) -> Path:
    """把 ``html`` 印成 PDF 存到 ``path``，返回该路径。

    :param landscape: 默认横向。成绩单的列数随题目数增长，纵向 A4 几道题就装不下，
                      而**表格挤在一起比换页更难读**。
    :param title: 写进 PDF 元数据的标题（不是页面上那行字）。

    这里刻意**不做分页**：``QTextDocument.print_`` 会按纸张自动分页，而手工插
    ``page-break`` 需要知道"一张表多高"，那是排版引擎的活儿。曾经想过按行数
    硬切，结果比自动分页更难看 —— 表头只出现在第一页，后面几页全是没有列名的
    数字。
    """
    from ..core import export as table_export

    return table_export.atomic_write(
        path, lambda target: _write_pdf(html, target, title=title,
                                        landscape=landscape))


def _write_pdf(html: str, target: Path, *, title: str, landscape: bool) -> None:
    """真正落盘的那一步。由 :func:`render_pdf` 包在"先写 .partial 再改名"里 ——
    ``QPdfWriter`` 一构造就把文件建出来了，中途失败会留下一个 0 字节的 .pdf，
    双击打不开还会让人以为"这个导出功能是坏的"。"""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    writer = QPdfWriter(str(target))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageOrientation(QPageLayout.Landscape if landscape
                              else QPageLayout.Portrait)
    writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)
    # 96 DPI：与屏幕一致，字号换算才不会在纸上缩水
    writer.setResolution(96)
    if title:
        writer.setTitle(title)
    writer.setCreator("OfflineOJ")

    document = QTextDocument()
    # 文档自己的默认字体也定一下：HTML 里没写字号的段落会跟着它走，
    # 不设的话是 Qt 的默认值（比正文大一点），整份看起来松松垮垮
    document.setDefaultStyleSheet(
        f"body {{ font-size: {BODY_POINT_SIZE}pt; }}"
        "h1 { font-size: 17pt; } h2 { font-size: 13pt; }"
        "table { border-collapse: collapse; } th { background-color: #eeeeee; }")
    document.setHtml(html)
    document.print_(writer)


__all__ = ["BODY_POINT_SIZE", "render_pdf"]
