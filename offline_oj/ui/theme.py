"""主题、字体与配色。

做 Windows 桌面应用时常被忽略的一点：**字体**。Qt 默认在 Windows 上用
"Segoe UI 9pt"，但中文界面需要回退到 "Microsoft YaHei UI"，否则部分控件里的
中文会用宋体渲染，看起来像十几年前的软件。这里显式给出字体回退链。

配色上采用接近 Windows 11 Fluent 的中性色板，并提供浅色/深色两套。
深色不是把浅色反相，而是分别调过对比度，保证正文对比度达到 WCAG AA。
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

#: 界面字体回退链（Windows 中文环境）
UI_FONT_FAMILIES = ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC")
#: 等宽字体回退链（代码编辑器）
MONO_FONT_FAMILIES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "Monaco", "Courier New")

# ---------------------------------------------------------------------------
# 间距节奏
# ---------------------------------------------------------------------------
# 整个界面**只允许用下面这几档**，不要再就地发明新数字。
#
# 这不是洁癖。改这一版之前，六个面板的"最外层页面边距"同时存在 10 / 12 / 14 / 16
# 四种取值，行距有 2 / 4 / 6 / 8 / 9 / 10 / 12 七种 —— 后果是切选项卡时正文的
# 左右边缘会横向跳一下，而任何一个面板单独看都挑不出毛病，所以长期没人发现。
# 统一之后，"这个新面板该留多少边距"就不再是个需要判断的问题。
#
# 分档理由：页面最外层的横向留白要比纵向略大（纵向紧一点显得紧凑），
# 分组框内部顶部要留出标题的位置所以比底部多，对话框整体比页面紧一档。
PAGE_MARGINS = (16, 14, 16, 14)
"""页面最外层边距（面板的根布局 / 滚动区内容）。"""

GROUP_MARGINS = (14, 16, 14, 12)
"""``QGroupBox`` 内部边距：顶部多 4px 是给标题让位。"""

DIALOG_MARGINS = (12, 12, 12, 12)
"""对话框内部边距，比页面紧一档。"""

ROW_SPACING = 8
"""同一分组内的行距（表单一行到下一行）。"""

SECTION_SPACING = 12
"""分组与分组之间的距离。"""

TIGHT_SPACING = 6
"""标签与其输入控件之间、以及成组小控件之间的距离。"""

COLUMN_SPACING = 16
"""网格里需要一眼分开的两列之间的距离（如"键位 / 说明"这类对照表）。"""

HEADER_SPACING = 4
"""标题行与其下方正文之间的距离（如"语言 / 提交"这一行与代码编辑器）。"""

CAPTION_SPACING = 2
"""同一控件内上下两段文字之间的距离（如名称与其下方的小字说明）。"""

TOP_GAP = (0, 6, 0, 0)
"""单独给某个控件上方留一点空气，而不影响左右。"""


#: 判题结论的**徽标底色** —— 逐字对标洛谷。
#:
#: 这不是"挑一套好看的绿红黄"，而是**选手的肌肉记忆**：在洛谷刷过题的人，
#: 瞄一眼颜色就知道结果，不用读字母。所以色值照抄，不顺手调好看一点 ——
#: AC ``#52C41A`` / WA ``#E74C3C`` / RE ``#9D3DCF`` / CE ``#FADB14`` /
#: TLE·MLE·OLE ``#052242``。
#:
#: 深色主题只动了 TLE 那一档：``#052242`` 的相对亮度是 0.016，而深色面板
#: ``#2b2b2b`` 是 0.027 —— 两者几乎一样亮，色块会"消失"成一片黑。所以沿同一
#: 色相（≈215°）提亮到看得见为止。其余几档都是中间调，两套主题下都成立。
#:
#: 洛谷没有 IE 这一档（它的问题不在色表里），本项目用中性灰 ——
#: 评测机自己出的错不该用红绿去表达"做对 / 做错"。
VERDICT_BADGE_COLORS: dict[str, dict[str, str]] = {
    "light": {
        "AC": "#52c41a",
        "WA": "#e74c3c",
        "RE": "#9d3dcf",
        "CE": "#fadb14",
        "TLE": "#052242",
        "MLE": "#052242",
        "OLE": "#052242",
        "PE": "#3498db",
    },
    "dark": {
        "AC": "#52c41a",
        "WA": "#e74c3c",
        "RE": "#9d3dcf",
        "CE": "#fadb14",
        "TLE": "#4a6da0",
        "MLE": "#4a6da0",
        "OLE": "#4a6da0",
        "PE": "#3498db",
    },
}


@dataclass(frozen=True)
class Palette:
    """一套主题色。"""

    name: str
    is_dark: bool
    window: str
    surface: str
    surface_alt: str
    border: str
    text: str
    text_muted: str
    accent: str
    accent_text: str
    accent_hover: str
    accent_pressed: str
    selection: str
    # 语义色
    success: str
    danger: str
    warning: str
    info: str

    # ---- 判题结论配色（对标洛谷） ----
    def verdict_badge_color(self, verdict: str) -> str:
        """判题结论的徽标底色。没登记的一律中性灰。"""
        return VERDICT_BADGE_COLORS["dark" if self.is_dark else "light"].get(
            verdict, self.text_muted)

    def verdict_color(self, verdict: str) -> str:
        """判题结论的**前景色**（评测记录里的彩色文字、表格单元格）。

        返回的不是洛谷原值，而是洛谷色相在本主题下"当文字读得清"的那一档 ——
        原因见 :func:`_as_text_color`：``#fadb14`` 压在白底上当文字只有 1.4:1。
        """
        return _as_text_color(self.verdict_badge_color(verdict), self.surface)


LIGHT = Palette(
    name="light",
    is_dark=False,
    window="#f3f3f3",
    surface="#ffffff",
    surface_alt="#fafafa",
    border="#dcdcdc",
    text="#1b1b1b",
    text_muted="#6a6a6a",
    accent="#0f6cbd",
    accent_text="#ffffff",
    accent_hover="#1a7fd4",
    accent_pressed="#0c5896",
    selection="#cfe4f7",
    success="#107c41",
    danger="#c42b1c",
    warning="#9a6100",
    info="#0f6cbd",
)

DARK = Palette(
    name="dark",
    is_dark=True,
    window="#202020",
    surface="#2b2b2b",
    surface_alt="#262626",
    border="#3d3d3d",
    text="#f2f2f2",
    text_muted="#a6a6a6",
    accent="#4cc2ff",
    accent_text="#0a2b3d",
    accent_hover="#6ccdff",
    accent_pressed="#3aa8e0",
    selection="#0f6cbd",
    success="#4ade80",
    danger="#ff8a80",
    warning="#f0c060",
    info="#4cc2ff",
)

PALETTES = {LIGHT.name: LIGHT, DARK.name: DARK}


def resolve_palette(theme: str) -> Palette:
    """把设置里的主题名解析成调色板。

    ``system`` 会跟随 Windows"应用模式"（浅色/深色）。
    """
    name = (theme or "light").lower()
    if name == "system":
        return DARK if _system_prefers_dark() else LIGHT
    return PALETTES.get(name, LIGHT)


def _system_prefers_dark() -> bool:
    """读取 Windows 应用主题偏好。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return False


def ui_font(size: int = 9) -> QFont:
    """界面字体。"""
    font = QFont()
    font.setFamilies([family for family in UI_FONT_FAMILIES
                      if family in QFontDatabase.families()] or ["Segoe UI"])
    font.setPointSize(size)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def mono_font(size: int = 11) -> QFont:
    """代码字体。开启等宽连字候选（Cascadia Mono 支持）。"""
    available = QFontDatabase.families()
    families = [family for family in MONO_FONT_FAMILIES if family in available]
    font = QFont()
    font.setFamilies(families or ["Consolas", "Courier New"])
    font.setPointSize(size)
    font.setStyleHint(QFont.Monospace)
    font.setFixedPitch(True)
    return font


def apply_palette(app, palette: Palette) -> None:
    """给 QApplication 装上 Qt 调色板（影响原生控件绘制，如菜单、滚动条）。"""
    qt_palette = QPalette()
    roles = {
        QPalette.Window: palette.window,
        QPalette.WindowText: palette.text,
        QPalette.Base: palette.surface,
        QPalette.AlternateBase: palette.surface_alt,
        QPalette.Text: palette.text,
        QPalette.Button: palette.surface,
        QPalette.ButtonText: palette.text,
        QPalette.Highlight: palette.accent,
        QPalette.HighlightedText: palette.accent_text,
        QPalette.ToolTipBase: palette.surface,
        QPalette.ToolTipText: palette.text,
        QPalette.PlaceholderText: palette.text_muted,
        QPalette.Link: palette.accent,
        QPalette.BrightText: "#ffffff",
    }
    for role, color in roles.items():
        qt_palette.setColor(role, QColor(color))
    # 禁用态：文字明显变灰，避免"看起来能点其实不能点"
    disabled = QColor(palette.text_muted)
    disabled.setAlpha(140)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        qt_palette.setColor(QPalette.Disabled, role, disabled)
    app.setPalette(qt_palette)


def _relative_luminance(color: str) -> float:
    """sRGB 相对亮度（WCAG 2.x，带伽马校正）。解析不了就按黑算。"""
    raw = color.lstrip("#")
    if len(raw) != 6:
        return 0.0
    try:
        values = [int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    except ValueError:
        return 0.0
    channels = [
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in values
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """两色的 WCAG 对比度，范围 1.0 ~ 21.0。"""
    one = _relative_luminance(foreground)
    other = _relative_luminance(background)
    lighter, darker = max(one, other), min(one, other)
    return (lighter + 0.05) / (darker + 0.05)


def _hls_hex(hue: float, lightness: float, saturation: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return "#{:02x}{:02x}{:02x}".format(
        round(red * 255), round(green * 255), round(blue * 255))


def _readable_on(color: str) -> str:
    """给一个背景色，返回压在上面读得清的正文色。

    为什么不能一律写 ``#ffffff``：深色主题的语义色是**亮**色
    （成功 ``#4ade80``、危险 ``#ff8a80``），白字压上去对比度只有 2.3:1 左右，
    基本看不清；而浅色主题的语义色是暗色，白字才对。同一个"徽标"在两套主题下
    需要相反的前景色，所以按亮度算，不靠人记住。

    阈值的来历：两个候选前景 ``#ffffff``（相对亮度 1.0）与 ``#101010``
    （约 0.0056）谁更好，取决于哪个对比度更高 ——

        (1.0 + 0.05) / (L + 0.05)  ==  (L + 0.05) / (0.0056 + 0.05)
        ⇒ (L + 0.05)² = 0.05838    ⇒ L ≈ 0.19

    所以分界点是 **0.19** 而不是直觉上的 0.5。按 0.5 会让 ``#ff8a80``（L=0.41）
    和 ``#b79cf0``（L=0.40）都挑到白字，正好是最需要避免的那两个。
    """
    return "#101010" if _relative_luminance(color) > 0.19 else "#ffffff"


def _as_text_color(color: str, background: str, minimum: float = 4.5) -> str:
    """把 ``color`` 调成"压在 ``background`` 上当**文字**读得清"的那一档。

    **色相不动**，只沿明度轴走到够对比度为止。

    为什么需要它：洛谷那几个判题色是给**色块**用的。``#fadb14``（CE 黄）当白底上的
    文字只有 1.4:1，``#52c41a``（AC 绿）只有 2.3:1 —— 而本项目的评测记录是
    **彩色文字**（表格单元格、行内结论标签），照搬色值等于把这两个结论变成看不见。

    所以分工是：徽标底色逐字用洛谷色（:data:`VERDICT_BADGE_COLORS`，色块大、
    前景色由 :func:`_readable_on` 另算），文字场景走这里。
    "AC 是绿的、CE 是黄的"这个身份留住了，可读性也留住了。

    ``minimum`` 默认 4.5:1，即 WCAG AA 对正文的要求。
    """
    if contrast_ratio(color, background) >= minimum:
        return color
    raw = color.lstrip("#")
    if len(raw) != 6:
        return color
    try:
        rgb = [int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    except ValueError:
        return color
    hue, lightness, saturation = colorsys.rgb_to_hls(*rgb)

    # 背景亮就往暗走、背景暗就往亮走。分 100 档逼近，结果可复现 —— 不用二分，
    # 是因为"够对比度的最小改动"和"刚好越过阈值"在浮点上不是一回事，步进更稳。
    steps = 100
    light_background = _relative_luminance(background) > 0.5
    for step in range(1, steps + 1):
        ratio = step / steps
        target = lightness * (1 - ratio) if light_background else lightness + (1 - lightness) * ratio
        candidate = _hls_hex(hue, target, saturation)
        if contrast_ratio(candidate, background) >= minimum:
            return candidate
    # 走到头都不够：退回黑白里更远的那个（实际用不到，留个确定的结果）
    return "#101010" if light_background else "#ffffff"


def set_dynamic_property(widget, name: str, value) -> None:
    """设置动态属性并让它**立刻**生效。

    必须 repolish：Qt 只在控件被重新 polish 的时候才重算属性选择器，
    运行时改了属性而不 repolish，现象是"属性和样式都写对了，界面就是不变" ——
    这种坑很难从代码上看出来，所以统一走这个函数，别在调用点手写。

    传 ``None`` 表示去掉该属性（回到没有这个属性的默认外观）。

    切主题时不需要调它：``QApplication.setStyleSheet`` 会全量重刷，
    属性选择器自然重新求值 —— 这正是用属性而不是内联样式的意义。
    """
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def stylesheet(palette: Palette, *, ui_point_size: int = 9) -> str:
    """生成 QSS。用 CSS 变量风格集中管理颜色，避免满屏硬编码色值。"""
    p = palette
    base = f"""
* {{
    outline: none;
}}

QWidget {{
    color: {p.text};
    background-color: transparent;
}}

QMainWindow, QDialog {{
    background-color: {p.window};
}}

QToolTip {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    padding: 4px 6px;
}}

/* ---------- 菜单栏 ---------- */
QMenuBar {{
    background-color: {p.window};
    border-bottom: 1px solid {p.border};
    padding: 1px 2px;
}}
QMenuBar::item {{
    padding: 5px 10px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background-color: {p.selection};
}}
QMenu {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 26px 6px 24px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {p.selection};
    color: {p.text};
}}
QMenu::separator {{
    height: 1px;
    background-color: {p.border};
    margin: 4px 8px;
}}

/* 这里原本还有一组 QToolBar / QToolButton 的规则。全局工具栏已经删掉了
   （见 main_window 开头的说明：每个按钮都和菜单 + 面板按钮重复），
   留着这套样式只会让后来的人以为"是不是有个工具栏没建出来"。 */

/* ---------- 状态栏 ---------- */
QStatusBar {{
    background-color: {p.window};
    border-top: 1px solid {p.border};
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 0 6px; }}

/* ---------- 选项卡 ---------- */
QTabWidget::pane {{
    border: 1px solid {p.border};
    background-color: {p.surface};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {p.window};
    color: {p.text_muted};
    padding: 7px 16px;
    margin-right: 2px;
    border: 1px solid {p.border};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:hover {{
    color: {p.text};
    background-color: {p.surface_alt};
}}
QTabBar::tab:selected {{
    background-color: {p.surface};
    color: {p.text};
    font-weight: 600;
    border-bottom: 2px solid {p.accent};
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 5px;
    padding: 5px 14px;
    min-height: 20px;
}}
QPushButton:hover {{
    background-color: {p.selection};
    border-color: {p.accent};
}}
QPushButton:pressed {{
    background-color: {p.accent};
    color: {p.accent_text};
    border-color: {p.accent_pressed};
}}
QPushButton:disabled {{
    background-color: {p.surface_alt};
    color: {p.text_muted};
    border-color: {p.border};
}}
QPushButton[variant="primary"] {{
    background-color: {p.accent};
    color: {p.accent_text};
    border: 1px solid {p.accent};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background-color: {p.accent_hover}; }}
QPushButton[variant="primary"]:pressed {{ background-color: {p.accent_pressed}; }}
QPushButton[variant="primary"]:disabled {{
    background-color: {p.border};
    color: {p.text_muted};
    border-color: {p.border};
}}
QPushButton[variant="danger"] {{ color: {p.danger}; }}
QPushButton[variant="danger"]:hover {{
    background-color: {p.danger};
    color: #ffffff;
    border-color: {p.danger};
}}
QPushButton[flat="true"] {{
    background: transparent;
    border: 1px solid transparent;
    padding: 4px 8px;
}}
QPushButton[flat="true"]:hover {{ background-color: {p.selection}; }}

/* ---------- 输入控件 ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {p.surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 5px;
    padding: 4px 6px;
    selection-background-color: {p.selection};
    selection-color: {p.text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus,
QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p.accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QComboBox:disabled {{
    background-color: {p.surface_alt};
    color: {p.text_muted};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    selection-background-color: {p.selection};
    selection-color: {p.text};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}

/* ---------- 列表 / 表格 ---------- */
QListWidget, QTreeWidget, QTableWidget, QListView, QTableView {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 5px;
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background-color: {p.surface_alt};
}}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{
    background-color: {p.selection};
    color: {p.text};
}}
QHeaderView::section {{
    background-color: {p.surface_alt};
    color: {p.text_muted};
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
    padding: 5px 8px;
    font-weight: 600;
}}

/* ---------- 分组框 ---------- */
QGroupBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    color: {p.text_muted};
}}

/* 卡片：一块比背景浅一档的独立区域（如"单个测试点"那种可增删的小块）。
   做成主题规则而不是由调用点内联写色值，是为了切主题时能自动重刷。 */
QFrame[role="card"] {{
    background-color: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 6px;
}}

/* ---------- 分割条 / 滚动条 ---------- */
QSplitter::handle {{
    background-color: {p.border};
}}
QSplitter::handle:hover {{
    background-color: {p.accent};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    min-height: 28px;
    border-radius: 6px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_muted}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    min-width: 28px;
    border-radius: 6px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 进度条 ---------- */
QProgressBar {{
    background-color: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 5px;
    text-align: center;
    height: 8px;
}}
QProgressBar::chunk {{
    background-color: {p.accent};
    border-radius: 4px;
}}

/* ---------- 语义标签 ---------- */
/* 一律用**动态属性**而不是 setStyleSheet。原因有二：
   一是属性选择器会在切主题时随 setStyleSheet 全量重刷，内联样式不会 ——
   凡是内联写死了色值的地方，深色主题下都会留着浅色主题的颜色；
   二是色值只在这里出现一次，不会散落到六个面板里各写一份。 */
QLabel[role="h1"] {{ font-size: {ui_point_size + 6}pt; font-weight: 700; }}
QLabel[role="h2"] {{ font-size: {ui_point_size + 3}pt; font-weight: 600; }}
QLabel[role="strong"] {{ font-weight: 600; }}
QLabel[role="display"] {{
    /* 需要被人从另一个屏幕前念出来的大字（房间号、地址）。 */
    font-size: {ui_point_size + 6}pt;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel[role="muted"], QLabel[muted="true"] {{ color: {p.text_muted}; }}
QLabel[role="warn"] {{ color: {p.warning}; font-weight: 600; }}
QLabel[role="danger"] {{ color: {p.danger}; font-weight: 600; }}
QLabel[role="code"] {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    background-color: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 2px 6px;
}}
QLabel[role="badge"] {{
    border-radius: 9px;
    padding: 1px 9px;
    font-weight: 600;
}}
QLabel[role="badge"][tone="accent"] {{
    background-color: {p.accent};
    color: {p.accent_text};
}}
QLabel[role="badge"][tone="success"] {{
    background-color: {p.success};
    color: {_readable_on(p.success)};
}}
QLabel[role="badge"][tone="warning"] {{
    background-color: {p.warning};
    color: {_readable_on(p.warning)};
}}
QLabel[role="badge"][tone="danger"] {{
    background-color: {p.danger};
    color: {_readable_on(p.danger)};
}}
QLabel[role="badge"][tone="muted"] {{
    background-color: {p.border};
    color: {p.text};
}}
QLabel[role="badge"][tone="none"] {{
    color: {p.text_muted};
    border: 1px solid {p.border};
}}

/* 小字说明（控件上方的小标题等） */
QLabel[role="caption"] {{ color: {p.text_muted}; font-size: {ui_point_size - 1}pt; }}

/* 步骤序号圆点。刻意不复用 role="badge"：那个带 9px 横向内边距，
   放在 22×22 的固定尺寸里会把数字挤没。 */
QLabel[role="step"] {{
    background-color: {p.accent};
    color: {p.accent_text};
    border-radius: 11px;
    font-weight: 700;
}}
"""
    # 判题徽标的色调规则按 VERDICT_TONES 生成（见 _verdict_tone_rules），
    # 而不是手写在上面那段里 —— 漏一条不会报错，只会让某个结论颜色不对。
    return base + _verdict_tone_rules(palette)


#: 判题结论 → 徽标色调。
#:
#: 每个结论一个独立色调，而不是复用 ``success`` / ``danger`` —— 复用的后果是
#: "洛谷的绿"和"本项目的成功色"必须二选一，那还不如分开：通用语义色继续服务
#: 别的地方（连接状态、警告条），判题色只管判题。``TLE`` / ``MLE`` / ``OLE``
#: 共用一个色调是**对的** —— 洛谷给这三者的就是同一个色值。
#:
#: ``IE`` 用 ``muted`` 是刻意的：它不是"做得对 / 做得错"，而是"评测机自己出了
#: 问题"，用红绿去表达会误导选手。
VERDICT_TONES = {
    "AC": "ac",
    "WA": "wa",
    "TLE": "tle",
    "MLE": "tle",
    "OLE": "tle",
    "RE": "re",
    "CE": "ce",
    "PE": "pe",
    "IE": "muted",
}

#: 手写 CSS 里已有的通用色调。判题色调不重复定义这几个。
_BASE_BADGE_TONES = ("accent", "success", "warning", "danger", "muted", "none")


def _verdict_tone_rules(palette: Palette) -> str:
    """按 :data:`VERDICT_TONES` 生成判题徽标的色调规则。

    手写七条 ``QLabel[role="badge"][tone="..."]`` 很容易漏掉一条，而漏掉的表现是
    "某个结论突然变成默认外观"——不报错、只是颜色不对，很难注意到。生成就没有这个口子。
    """
    rules: list[str] = []
    for tone in dict.fromkeys(VERDICT_TONES.values()):
        if tone in _BASE_BADGE_TONES:
            continue
        verdict = next(key for key, value in VERDICT_TONES.items() if value == tone)
        background = palette.verdict_badge_color(verdict)
        rules.append(
            f'QLabel[role="badge"][tone="{tone}"] {{\n'
            f'    background-color: {background};\n'
            f'    color: {_readable_on(background)};\n'
            f'}}'
        )
    return "\n".join(rules) + "\n"


def apply_verdict_badge(label, verdict: str | None) -> None:
    """把判题结论渲染成徽标外观；``None`` 表示"尚未提交"。

    以前这里是一段内联 QSS（``... color: #ffffff; border-radius: 9px; ...``），
    由调用点自己拼。那个写法有两个毛病：色值散在调用点、而且前景色一律白 ——
    深色主题的语义色是亮色（``#4ade80`` / ``#ff8a80``），白字压上去对比度只有
    2.3:1。改成属性之后前景色由 :func:`_readable_on` 按亮度算，两套主题都够看。
    """
    set_dynamic_property(label, "role", "badge")
    set_dynamic_property(label, "tone",
                         "none" if verdict is None else VERDICT_TONES.get(verdict, "muted"))
