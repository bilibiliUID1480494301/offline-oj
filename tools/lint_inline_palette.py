"""找出"把调色板色值内联进 setStyleSheet"的地方。

为什么值得单独查这一条：``QApplication.setStyleSheet`` 换主题时会**全量重刷**
所有控件，属性选择器（``QLabel[role="..."]``）会重新求值，但
``widget.setStyleSheet("color: #123456")`` 是控件自己的样式表，换主题不会动它。

于是产生一类很难发现的 bug：切到深色主题后，绝大多数控件都变了，
只有几处内联的地方还留着浅色主题的颜色 —— 而它们散落在上千行里，
靠读 diff 是看不出来的。

判据：``setStyleSheet(...)`` 的实参里出现了 ``palette`` / ``self.palette`` /
``self._palette`` 的属性访问，或者出现形如 ``#rrggbb`` 的字面色值。

**例外**（这些是正当用法，允许存在）：
  * 实参是动态变量（如 ``color`` 形参）—— 由调用方在运行时给值；
  * 控件自带刷新钩子（``set_palette_theme`` 或 ``apply_palette``），
    换主题时会被面板显式重刷；
  * 空字符串（用来清除样式）。

注意这个工具只能证明"控件提供了刷新钩子"，不能证明"面板真的调了它"。
后者由离屏冒烟测试里的换主题往返来兜（见 tests/smoke_gui.py）。

用法::

    python tools/lint_inline_palette.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "offline_oj" / "ui"

#: 控件自身提供的刷新钩子名。二者取其一即可，项目里并存是历史原因：
#: 早期控件叫 set_palette_theme，后来面板自己叫 apply_palette。
REFRESH_HOOKS = ("set_palette_theme", "apply_palette")

COLOR_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b")
PALETTE_ATTR = re.compile(r"\b(?:self\.)?_?palette\.\w+")


def _is_style_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "setStyleSheet"


def _mentions_palette_color(node: ast.Call) -> tuple[bool, str]:
    """判断这次 setStyleSheet 的实参是否写死了主题色。"""
    if not node.args:
        return False, ""
    arg = node.args[0]
    source = ast.unparse(arg)

    # 空字符串 = 清除样式，与主题无关
    if isinstance(arg, ast.Constant) and arg.value in ("", None):
        return False, ""

    hits = []
    if PALETTE_ATTR.search(source):
        hits.append("引用了 palette 属性")
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        if COLOR_LITERAL.search(arg.value):
            hits.append("写死了 #rrggbb")
    if isinstance(arg, ast.JoinedStr):
        for part in arg.values:
            if isinstance(part, ast.FormattedValue):
                inner = ast.unparse(part.value)
                if PALETTE_ATTR.search(inner):
                    hits.append("引用了 palette 属性")
            elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                if COLOR_LITERAL.search(part.value):
                    hits.append("写死了 #rrggbb")
    return bool(hits), "、".join(dict.fromkeys(hits))


def _enclosing_class(tree: ast.AST) -> dict[int, str]:
    """行号 → 所属类名。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner[id(child)] = node.name
    return owner


def main() -> int:
    findings: list[str] = []
    for path in sorted(UI_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append(f"{path.relative_to(ROOT)}:{exc.lineno}: 语法错误 {exc.msg}")
            continue

        # 收集带刷新钩子的类
        refreshing = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(
                isinstance(item, ast.FunctionDef) and item.name in REFRESH_HOOKS
                for item in node.body
            )
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_style_call(node):
                continue
            hardcoded, reason = _mentions_palette_color(node)
            if not hardcoded:
                continue

            # 找出这个调用所在的类（沿父链找最近的 ClassDef 是麻烦的，
            # 这里用行号近似：取该行所属的最近类）
            owner = ""
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                if cls.lineno <= node.lineno <= (cls.end_lineno or cls.lineno):
                    owner = cls.name
                    break
            if owner in refreshing:
                continue
            rel = path.relative_to(ROOT)
            findings.append(f"{rel}:{node.lineno}: {reason}（位于 {owner or '模块级'}）")

    if findings:
        print(f"发现 {len(findings)} 处内联主题色：")
        for item in findings:
            print("  " + item)
        print()
        print("改法：换成动态属性（theme.set_dynamic_property + QSS 里的属性选择器），")
        print("或让所在类实现 set_palette_theme 并由面板在 apply_palette 里调用。")
        return 1

    print("共 0 处内联主题色")
    return 0


if __name__ == "__main__":
    sys.exit(main())
