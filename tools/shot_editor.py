"""用真实 windows 平台渲染编辑器，验证字体、着色与补全弹窗的实际观感。

    python tools/shot_editor.py

产物落在 ``build/screens/``，仅供人工走查，不参与测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from offline_oj.core.models import Language  # noqa: E402
from offline_oj.ui.theme import apply_palette, resolve_palette  # noqa: E402
from offline_oj.ui.widgets import CodeEditor  # noqa: E402

CODE = """#include <bits/stdc++.h>
using namespace std;

// A + B
int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int a = 0, b = 0;
    cin >> a >> b;
    long long total = 0;

    // 中文字体回退检查：注释里的汉字
    cout << a + b << endl;
    return 0;
}
"""

OUT = ROOT / "build" / "screens"


def build(app: QApplication, theme: str) -> tuple[QWidget, CodeEditor]:
    palette = resolve_palette(theme)
    apply_palette(app, palette)

    host = QWidget()
    host.setWindowTitle(f"OfflineOJ 代码编辑器 · {theme}")
    host.resize(1000, 430)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(10, 10, 10, 10)

    editor = CodeEditor(palette)
    editor.set_language(Language.CPP)
    layout.addWidget(editor)
    host.show()
    app.processEvents()
    return host, editor


def type_text(editor: CodeEditor, text: str) -> None:
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    editor.setTextCursor(cursor)
    editor.insertPlainText(text)
    editor._completer.try_complete()


def main() -> int:
    app = QApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for theme in ("light", "dark"):
        # 1) 纯高亮
        host, editor = build(app, theme)
        editor.setPlainText(CODE)
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        editor.setTextCursor(cursor)
        app.processEvents()
        name = f"editor_{theme}_highlight.png"
        editor.grab().save(str(OUT / name))
        written.append(name)

        # 2) 关键字候选
        editor.setPlainText(CODE)
        type_text(editor, "\n    vec")
        app.processEvents()
        popup = editor._completer.popup()
        editor.grab().save(str(OUT / f"editor_{theme}_popup_editor.png"))
        popup.grab().save(str(OUT / f"editor_{theme}_popup_keyword.png"))
        written.extend((f"editor_{theme}_popup_editor.png",
                        f"editor_{theme}_popup_keyword.png"))
        print(f"[{theme}] 关键字候选 {editor._completer.completionCount()} 条，"
              f"默认选中 {popup.currentIndex().data(Qt.DisplayRole)}")

        # 3) 片段候选 + 展开
        editor.setPlainText("")
        type_text(editor, "for")
        app.processEvents()
        popup.grab().save(str(OUT / f"editor_{theme}_popup_snippet.png"))
        written.append(f"editor_{theme}_popup_snippet.png")
        print(f"[{theme}] 片段候选 {editor._completer.completionCount()} 条")

        editor.setPlainText("")
        type_text(editor, "mai")
        editor._completer.insert("main")
        app.processEvents()
        editor.grab().save(str(OUT / f"editor_{theme}_snippet_expanded.png"))
        written.append(f"editor_{theme}_snippet_expanded.png")

        host.close()

    print("\n写出:")
    for name in written:
        print("  ", (OUT / name).relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
