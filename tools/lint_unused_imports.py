"""极简未使用导入检查（stdlib AST，无需额外依赖）。

    python tools\\lint_unused_imports.py

三条"不报"的规则，都是为了别把噪声当信号：

* ``from __future__ import annotations`` 之类的 ``__future__`` 导入永远没有
  "被使用"的形式，但它们有实际作用 —— 早期版本把它们全报了出来，
  44 条告警里全是这一种，结果就是没人再看这个工具的输出；
* ``__all__`` 里列出的名字算已使用（重新导出）；
* 字符串注解（``"Foo"``）里出现的名字算已使用。

扫描范围包含 ``offline_oj`` / ``tests`` / ``packaging`` / ``tools`` ——
漏掉 ``tools`` 会让这个工具检查不了和它自己同级的那批脚本。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: 要扫描的顶层目录
ROOTS = ("offline_oj", "tests", "packaging", "tools")


def collect_imports(tree: ast.AST) -> dict[str, int]:
    """模块里所有被导入的名字 → 行号。``__future__`` 跳过。"""
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported.setdefault(name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported.setdefault(alias.asname or alias.name, node.lineno)
    return imported


def collect_used(tree: ast.AST) -> set[str]:
    """所有被引用到的名字，含属性链的根、字符串注解与 ``__all__``。"""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # 字符串形式的类型注解（"Foo"）以及 __all__ 的成员
            used.add(node.value)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets):
            for element in ast.walk(node.value):
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    used.add(element.value)
    return used


def check(path: Path) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # 语法错误的文件直接报出来，别静默跳过
        print(f"{path}: 语法错误，无法解析 —— {exc}")
        return []
    imported = collect_imports(tree)
    used = collect_used(tree)
    return [(name, line) for name, line in sorted(imported.items(), key=lambda kv: kv[1])
            if name not in used]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    files = [path for name in ROOTS for path in sorted((root / name).rglob("*.py"))]
    total = 0
    for path in files:
        unused = check(path)
        if unused:
            total += len(unused)
            print(f"{path.relative_to(root)}:")
            for name, line in unused:
                print(f"   L{line}: {name}")
    print(f"\n共 {total} 处未使用导入")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
