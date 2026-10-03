"""打包脚本的回归测试。

这一组检查看着琐碎，但每一条都对应一次"打包根本跑不起来"或"跑起来是坏的"
真实故障 —— 而且都不是靠读代码看出来的，是在真机上跑一遍才炸出来的：

* ``build.ps1`` 没有 BOM → PowerShell 5.1 按 ANSI 读 → 中文乱码 → 报"字符串缺少
  终止符"这种**看起来像语法写错**的错误（PowerShell 7 又完全正常，于是更难查）；
* ``build.ps1`` 里一行 ``[regex]::Escape(a -replace b, c)`` 被解析成两个参数 →
  「找不到 Escape 的重载」，版本校验这一步必崩，**整条构建链从来没跑通过**；
* ``installer.iss`` 没有 BOM → Inno Setup 按 ANSI 读 → 安装界面和快捷方式的中文乱码；
* 版本号在三处各写了一份，谁漏改都会让安装包版本和程序版本对不上。

这些都抓得住、也值得抓住，所以放在这里，而不是留在"下次记得注意"。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import offline_oj  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
BOM = b"\xef\xbb\xbf"

#: 没有 BOM 就会被按系统 ANSI 代码页读取、中文随之乱码的文件
NEEDS_BOM = ("build.ps1", "installer.iss")


def read_text(name: str) -> str:
    """按 UTF-8 读取打包文件，自动去掉 BOM。"""
    return (PACKAGING / name).read_bytes().decode("utf-8-sig")


class BomEncodingTest(unittest.TestCase):
    def test_files_that_need_a_bom_have_one(self):
        for name in NEEDS_BOM:
            with self.subTest(file=name):
                raw = (PACKAGING / name).read_bytes()
                self.assertTrue(raw.startswith(BOM),
                                f"packaging/{name} 缺少 UTF-8 BOM —— "
                                "Windows PowerShell 5.1 / Inno Setup 会按 ANSI 读，中文将变成乱码")

    def test_bom_files_are_valid_utf8(self):
        for name in NEEDS_BOM:
            with self.subTest(file=name):
                raw = (PACKAGING / name).read_bytes()
                raw[len(BOM):].decode("utf-8")  # 解码失败会直接抛出

    def test_the_bom_is_actually_needed(self):
        """确认这两份文件里真的有非 ASCII 字符 —— 否则 BOM 也就无从谈起。"""
        for name in NEEDS_BOM:
            with self.subTest(file=name):
                text = read_text(name)
                self.assertTrue(any(ord(ch) > 0x7F for ch in text),
                                f"packaging/{name} 里没有任何非 ASCII 字符")


class VersionConsistencyTest(unittest.TestCase):
    """版本号在 ``offline_oj`` / ``version_info.txt`` / ``installer.iss`` 各写一份。"""

    def test_installer_matches_the_app(self):
        match = re.search(r'^#define\s+AppVersion\s+"([^"]+)"', read_text("installer.iss"),
                          re.MULTILINE)
        self.assertIsNotNone(match, "installer.iss 里找不到 AppVersion 定义")
        self.assertEqual(match.group(1), offline_oj.__version__,
                         "安装包版本与 offline_oj.__version__ 不一致")

    def test_version_resource_matches_the_app(self):
        text = read_text("version_info.txt")
        expected = "(" + ", ".join(str(part) for part in offline_oj.__version_info__) + ")"
        for key in ("filevers", "prodvers"):
            self.assertIn(f"{key}={expected}", text,
                          f"version_info.txt 的 {key} 应为 {expected}")
        dotted = ".".join(str(part) for part in offline_oj.__version_info__)
        self.assertIn(f"'{dotted}'", text, f"version_info.txt 里找不到版本字符串 {dotted}")

    def test_semantic_version_matches_version_info(self):
        """``"2.0.0"`` 与 ``(2, 0, 0, 0)`` 必须是同一个版本（低位补零后相等）。"""
        parts = tuple(int(part) for part in offline_oj.__version__.split("."))
        padded = parts + (0,) * max(0, 4 - len(parts))
        self.assertEqual(padded, tuple(offline_oj.__version_info__))


if __name__ == "__main__":
    unittest.main(verbosity=2)
