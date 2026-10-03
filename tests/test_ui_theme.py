"""界面主题与"统一打磨"的回归测试。

这一组守的是**看起来没坏、其实坏了**的那一类问题 —— 它们不会抛异常，
只是让界面在某个主题下变得难读，或者在切主题时留下几处没跟上。三条主线：

1. **徽标前景色的对比度。** 深色主题的语义色是亮色（``#4ade80`` / ``#ff8a80``），
   以前徽标一律写死 ``color: #ffffff``，白字压亮色只有 2.3:1 的对比度 ——
   在深色背景上几乎看不清"AC"两个字。这条按 WCAG 算，不靠肉眼。
2. **没有内联主题色。** 控件自己的 ``setStyleSheet`` 不会在换主题时重刷，
   于是"切到深色后只有这几处还是浅色"。用 tools/lint_inline_palette.py 的
   同一套判据做断言，避免规则只活在工具里、测试里没人管。
3. **间距与行尾一致。** 间距散着写会让切选项卡时正文边缘横向跳动；
   行尾不一致则会让"只改几行"的编辑脚本把整个文件都变成改动。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from offline_oj.ui import theme  # noqa: E402

UI_DIR = ROOT / "offline_oj" / "ui"
PANEL_DIR = UI_DIR / "panels"

#: 面板必须用这些间距令牌，不许就地写数字。
PAGE_TOKEN = "setContentsMargins(*PAGE_MARGINS)"

#: 面板根布局以外的合法字面量（左侧/右侧留白这类布局细节，不属于间距节奏）。
ALLOWED_MARGIN_LITERALS = {
    "setContentsMargins(0, 0, 0, 0)",
    "setContentsMargins(0, 0, 8, 0)",      # 给滚动条让位
    "setContentsMargins(8, 0, 0, 0)",      # 分割器左侧留白
    "setContentsMargins(8, 4, 4, 0)",
    "setContentsMargins(8, 4, 8, 8)",
    "setContentsMargins(0, 0, 6, 0)",
}


def luminance(color: str) -> float:
    raw = color.lstrip("#")
    values = [int(raw[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    channels = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                for v in values]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    high, low = max(la, lb), min(la, lb)
    return (high + 0.05) / (low + 0.05)


def load_lint_module():
    """按路径加载 tools/lint_inline_palette.py（tools 不是包）。"""
    path = ROOT / "tools" / "lint_inline_palette.py"
    spec = importlib.util.spec_from_file_location("_lint_inline_palette", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BadgeContrastTest(unittest.TestCase):
    """徽标：前景色必须和背景拉得开。"""

    #: WCAG AA 对正文的要求
    MIN_RATIO = 4.5

    def test_every_semantic_color_gets_a_readable_foreground(self):
        for palette in (theme.LIGHT, theme.DARK):
            for name in ("accent", "success", "danger", "warning", "info",
                         "text", "text_muted"):
                with self.subTest(palette=palette.name, role=name):
                    background = getattr(palette, name)
                    foreground = theme._readable_on(background)
                    ratio = contrast(background, foreground)
                    self.assertGreaterEqual(
                        ratio, self.MIN_RATIO,
                        f"{palette.name}.{name} ({background}) 上用 {foreground} "
                        f"只有 {ratio:.2f}:1",
                    )

    def test_threshold_follows_the_crossover_not_the_midpoint(self):
        """分界点是两种前景色对比度相等之处（约 0.19），不是直觉上的 0.5。

        深色主题的危险色 ``#ff8a80`` 亮度约 0.41 —— 按 0.5 分界会挑到白字，
        而那正是最需要避免的一个组合。
        """
        self.assertEqual(theme._readable_on("#ff8a80"), "#101010")
        self.assertEqual(theme._readable_on("#b79cf0"), "#101010")
        self.assertEqual(theme._readable_on("#c42b1c"), "#ffffff")
        self.assertEqual(theme._readable_on("#107c41"), "#ffffff")

    def test_unknown_input_falls_back_to_white(self):
        for junk in ("", "#12", "not-a-color", "#zzzzzz"):
            with self.subTest(value=junk):
                self.assertEqual(theme._readable_on(junk), "#ffffff")


class SemanticRoleTest(unittest.TestCase):
    """面板依赖的语义角色必须在两套主题里都定义出来。"""

    REQUIRED = (
        'role="h1"',
        'role="h2"',
        'role="strong"',
        'role="display"',
        'role="caption"',
        'role="step"',
        'role="warn"',
        'role="danger"',
        'role="code"',
        'role="badge"',
        'tone="accent"',
        'tone="success"',
        'tone="warning"',
        'tone="danger"',
        'tone="muted"',
        'tone="none"',
        # 判题色调（对标洛谷）—— 由 _verdict_tone_rules 按表生成，
        # 少了任意一条都会让某个结论突然变成默认外观，且不报错。
        'tone="ac"',
        'tone="wa"',
        'tone="tle"',
        'tone="re"',
        'tone="ce"',
        'tone="pe"',
        'role="card"',
    )

    def test_roles_present_in_both_palettes(self):
        for palette in (theme.LIGHT, theme.DARK):
            css = theme.stylesheet(palette)
            for probe in self.REQUIRED:
                with self.subTest(palette=palette.name, role=probe):
                    self.assertIn(probe, css)

    def test_verdict_tone_covers_every_verdict(self):
        from offline_oj.core.models import Verdict

        for verdict in Verdict:
            with self.subTest(verdict=verdict.value):
                self.assertIn(verdict.value, theme.VERDICT_TONES)


class LuoguVerdictPaletteTest(unittest.TestCase):
    """判题配色对标洛谷。

    为什么要对到色值这一级：在洛谷刷过题的人，瞄一眼颜色就知道结果，不用读字母。
    "差不多是绿的"不算对标 —— 改色的意义正是这几位十六进制数。
    """

    LUOGU = {
        "AC": "#52c41a",
        "WA": "#e74c3c",
        "RE": "#9d3dcf",
        "CE": "#fadb14",
        "TLE": "#052242",
        "MLE": "#052242",
        "OLE": "#052242",
    }

    def test_badge_colors_are_luogu_colors_verbatim(self):
        """浅色主题的徽标底色必须**逐字**是洛谷色。"""
        for verdict, expected in self.LUOGU.items():
            with self.subTest(verdict=verdict):
                self.assertEqual(theme.LIGHT.verdict_badge_color(verdict), expected)

    def test_dark_theme_keeps_the_hue(self):
        """深色主题只许动明度，色相要留在原处。

        例外是 ``#052242``：它的相对亮度 0.016 和深色面板的 0.027 几乎一样，
        照搬色块会"消失"成一片黑，所以那一档提亮（色相不动）。
        """
        for verdict, expected in self.LUOGU.items():
            with self.subTest(verdict=verdict):
                if expected == "#052242":
                    continue
                self.assertEqual(theme.DARK.verdict_badge_color(verdict), expected)
        tle = theme.DARK.verdict_badge_color("TLE")
        self.assertEqual(tle, theme.DARK.verdict_badge_color("MLE"))
        self.assertEqual(tle, theme.DARK.verdict_badge_color("OLE"))
        self.assertGreater(theme.contrast_ratio(tle, theme.DARK.surface), 2.0,
                           "深色主题下的 TLE 色块和面板分不开")

    def test_badges_are_legible_in_both_themes(self):
        from offline_oj.core.models import Verdict

        for palette in (theme.LIGHT, theme.DARK):
            for verdict in Verdict:
                with self.subTest(palette=palette.name, verdict=verdict.value):
                    background = palette.verdict_badge_color(verdict.value)
                    foreground = theme._readable_on(background)
                    ratio = theme.contrast_ratio(foreground, background)
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        f"{verdict.value} 徽标上 {foreground} 压 {background} "
                        f"只有 {ratio:.2f}:1")

    def test_verdict_text_is_legible_on_the_panel(self):
        """评测记录里的结论是**彩色文字**，照着洛谷色值直接写会看不见。

        ``#fadb14`` 当白底上的文字只有 1.38:1、``#52c41a`` 只有 2.27:1 ——
        所以文字色走 :func:`theme._as_text_color`，色相留住、明度走到够。
        """
        from offline_oj.core.models import Verdict

        for palette in (theme.LIGHT, theme.DARK):
            for verdict in Verdict:
                with self.subTest(palette=palette.name, verdict=verdict.value):
                    color = palette.verdict_color(verdict.value)
                    ratio = theme.contrast_ratio(color, palette.surface)
                    self.assertGreaterEqual(
                        ratio, 4.5,
                        f"{palette.name} 下 {verdict.value} 的文字色 {color} 压面板 "
                        f"只有 {ratio:.2f}:1")

    def test_text_color_keeps_the_hue(self):
        """调亮/调暗只走明度轴，不能顺手换成另一个颜色。"""
        for palette in (theme.LIGHT, theme.DARK):
            for verdict in ("AC", "WA", "RE", "CE"):
                with self.subTest(palette=palette.name, verdict=verdict):
                    badge = palette.verdict_badge_color(verdict)
                    text = palette.verdict_color(verdict)
                    self.assertAlmostEqual(
                        self._hue(badge), self._hue(text), delta=2.0,
                        msg=f"{verdict} 的文字色 {text} 已经不是徽标色 {badge} 的色相了")

    def test_ce_is_not_invisible_on_white(self):
        """回归：CE 的名声是"黄"，而黄压白底是最糟的组合之一。"""
        ratio = theme.contrast_ratio(theme.LIGHT.verdict_color("CE"), theme.LIGHT.surface)
        self.assertGreaterEqual(ratio, 4.5)

    def test_unknown_verdict_is_neutral(self):
        for palette in (theme.LIGHT, theme.DARK):
            with self.subTest(palette=palette.name):
                self.assertEqual(palette.verdict_badge_color("ZZZ"), palette.text_muted)
                self.assertEqual(palette.verdict_color(""), palette.text_muted)

    @staticmethod
    def _hue(color: str) -> float:
        import colorsys

        raw = color.lstrip("#")
        rgb = [int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
        return colorsys.rgb_to_hls(*rgb)[0] * 360


class NoInlinePaletteTest(unittest.TestCase):
    """不许把调色板色值内联进 setStyleSheet。"""

    def test_no_inline_theme_colors(self):
        module = load_lint_module()
        found = self._scan(module)
        self.assertEqual(found, [], "以下位置把主题色内联了，切主题时不会重刷：\n"
                                   + "\n".join("  " + item for item in found))

    @staticmethod
    def _scan(module) -> list[str]:
        import ast

        findings: list[str] = []
        for path in sorted(module.UI_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            refreshing = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
                and any(
                    isinstance(item, ast.FunctionDef)
                    and item.name in module.REFRESH_HOOKS
                    for item in node.body
                )
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not module._is_style_call(node):
                    continue
                hardcoded, reason = module._mentions_palette_color(node)
                if not hardcoded:
                    continue
                owner = ""
                for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                    if cls.lineno <= node.lineno <= (cls.end_lineno or cls.lineno):
                        owner = cls.name
                        break
                if owner in refreshing:
                    continue
                findings.append(f"{path.relative_to(ROOT)}:{node.lineno}: {reason}")
        return findings


class SpacingRhythmTest(unittest.TestCase):
    """间距只用令牌，不许就地写数字。"""

    def _panels(self) -> list[Path]:
        """所有具体面板。

        排除 ``__init__.py``（只是导入）与 ``base.py``（抽象基类，本身不建布局）。
        """
        return [path for path in sorted(PANEL_DIR.glob("*.py"))
                if path.name not in ("__init__.py", "base.py")]

    def test_every_panel_uses_the_page_margin_token(self):
        for path in self._panels():
            with self.subTest(panel=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIn(
                    PAGE_TOKEN, source,
                    f"{path.name} 的根布局没有用 PAGE_MARGINS —— "
                    f"页面边距不一致会让切选项卡时正文边缘横向跳动",
                )

    def test_no_bare_margin_literals_outside_the_allowlist(self):
        import re

        pattern = re.compile(r"setContentsMargins\((\s*-?\d+(\s*,\s*-?\d+){3})\)")
        for path in self._panels():
            source = path.read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                literal = match.group(0)
                with self.subTest(panel=path.name, literal=literal):
                    self.assertIn(
                        literal, ALLOWED_MARGIN_LITERALS,
                        f"{path.name} 里出现了字面边距 {literal} —— "
                        f"请改用 theme 里的间距令牌",
                    )

    def test_no_bare_spacing_literals(self):
        import re

        pattern = re.compile(r"set(?:Vertical|Horizontal)?Spacing\(\d+\)")
        for path in self._panels():
            source = path.read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                with self.subTest(panel=path.name, literal=match.group(0)):
                    self.fail(f"{path.name} 里出现了字面行距 {match.group(0)} —— "
                              f"请改用 ROW_SPACING / SECTION_SPACING / TIGHT_SPACING")

    def test_tokens_are_complete(self):
        for name in ("PAGE_MARGINS", "GROUP_MARGINS", "DIALOG_MARGINS", "TOP_GAP"):
            with self.subTest(token=name):
                value = getattr(theme, name)
                self.assertEqual(len(value), 4, f"{name} 应该是四元组")
                self.assertTrue(all(isinstance(v, int) for v in value))
        for name in ("ROW_SPACING", "SECTION_SPACING", "TIGHT_SPACING", "COLUMN_SPACING"):
            with self.subTest(token=name):
                self.assertIsInstance(getattr(theme, name), int)


class LineEndingTest(unittest.TestCase):
    """源码一律 LF。

    这条是拿一次真实事故换来的：一次性编辑脚本里用了 ``Path.write_text``，
    它在 Windows 上会把 ``\\n`` 翻译成 ``\\r\\n``，于是"只改几行"变成了
    把整个文件的行尾都换掉。当时仓库里 5 个面板被整体转成 CRLF，
    而没有任何测试会发现 —— 直到下次有人对着 diff 数不清的改动发呆。
    """

    TREES = ("offline_oj", "tests", "tools")

    def test_sources_use_lf(self):
        offenders = []
        for base in self.TREES:
            for path in sorted((ROOT / base).rglob("*.py")):
                if b"\r\n" in path.read_bytes():
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "以下文件含 CRLF 行尾：\n  "
                                       + "\n  ".join(offenders))

    def test_no_file_mixes_crlf_and_lf(self):
        """同一个文件里两种行尾混用是最坏的情况 —— 编辑器会把差异越滚越大。"""
        for base in self.TREES:
            for path in sorted((ROOT / base).rglob("*.py")):
                raw = path.read_bytes()
                crlf = raw.count(b"\r\n")
                lf = raw.count(b"\n") - crlf
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertFalse(crlf and lf,
                                     f"{path.name} 同时含 {crlf} 个 CRLF 与 {lf} 个 LF")


if __name__ == "__main__":
    unittest.main()
