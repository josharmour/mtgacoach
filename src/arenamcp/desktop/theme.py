"""Single source of truth for desktop colours, type sizes and control styling.

Every colour, font size and monospace face the desktop app uses is defined
here. Widgets never name a colour themselves: Qt widgets pick their look up
from the generated application stylesheet through dynamic properties
(``role``, ``tone``, ``variant``, ``state``, ``card`` ...), and rich-text
views build their HTML with :func:`span` / :func:`block`, which read the
active :class:`Tokens`. ``tests/test_desktop_theme.py`` fails on any hex
colour, ``QColor(`` or ``font-size`` outside this module.

Colour meaning is fixed across themes:
  * status (``good`` / ``warn`` / ``bad``) answers "is this OK?" only;
  * identity (``you`` / ``opp`` / ``convo``) says whose turn / which mode;
  * ``accent`` marks interactive state (focus, primary action, selection).
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, fields
from typing import Final

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from arenamcp import settings as _settings

THEME_SYSTEM: Final = "system"
THEME_DARK: Final = "dark"
THEME_LIGHT: Final = "light"
THEME_HIGH_CONTRAST: Final = "high-contrast"

THEME_LABELS: Final[dict[str, str]] = {
    THEME_SYSTEM: "System",
    THEME_DARK: "Dark",
    THEME_LIGHT: "Light",
    THEME_HIGH_CONTRAST: "High Contrast",
}

# Type scale in px. Nothing outside this module picks a font size.
TYPE_SCALE: Final[dict[str, int]] = {
    "caption": 11,
    "body": 13,
    "emphasis": 15,
    "title": 18,
    "display": 22,
}

_KNOWN_THEMES = frozenset(THEME_LABELS)
_THEME_SETTINGS_KEY: Final = "desktop_theme"


@dataclass(frozen=True)
class Tokens:
    name: str
    is_dark: bool
    bg: str
    surface: str
    surface_2: str
    border: str
    text: str
    muted: str
    accent: str
    on_accent: str
    good: str
    warn: str
    bad: str
    you: str
    opp: str
    convo: str
    line: int = 1
    radius: int = 6
    radius_card: int = 10

    def tint(self, tone: str, amount: float = 0.14) -> str:
        """``tone`` blended into the surface colour (for chip/strip fills)."""
        return mix(getattr(self, tone), self.surface, amount)


TONES: Final = ("text", "muted", "accent", "good", "warn", "bad", "you", "opp", "convo")

_PALETTES: Final[dict[str, Tokens]] = {
    THEME_DARK: Tokens(
        name=THEME_DARK,
        is_dark=True,
        bg="#14181d",
        surface="#1b2027",
        surface_2="#232a33",
        border="#313a45",
        text="#e6edf3",
        muted="#9aa5b1",
        accent="#58a6ff",
        on_accent="#0b1220",
        good="#3fb950",
        warn="#d29922",
        bad="#f85149",
        you="#39c5cf",
        opp="#f0883e",
        convo="#bc8cff",
    ),
    THEME_LIGHT: Tokens(
        name=THEME_LIGHT,
        is_dark=False,
        bg="#f6f8fa",
        surface="#ffffff",
        surface_2="#eef1f5",
        border="#d0d7de",
        text="#1f2328",
        muted="#59636e",
        accent="#0969da",
        on_accent="#ffffff",
        good="#1a7f37",
        warn="#9a6700",
        bad="#cf222e",
        you="#1b7c83",
        opp="#bc4c00",
        convo="#8250df",
    ),
    THEME_HIGH_CONTRAST: Tokens(
        name=THEME_HIGH_CONTRAST,
        is_dark=True,
        bg="#000000",
        surface="#000000",
        surface_2="#1a1a1a",
        border="#ffffff",
        text="#ffffff",
        muted="#d9d9d9",
        accent="#ffff00",
        on_accent="#000000",
        good="#3dff8a",
        warn="#ffa500",
        bad="#ff6b6b",
        you="#00e5ff",
        opp="#ff9e3d",
        convo="#ff7cff",
        line=2,
        radius=0,
        radius_card=0,
    ),
}

_CURRENT: Tokens | None = None
_SYSTEM_PALETTE: QPalette | None = None
_OS_SCHEME_HOOKED = False


class _ThemeBus(QObject):
    changed = Signal()


_BUS: _ThemeBus | None = None


def theme_bus() -> _ThemeBus:
    """Process-wide notifier; ``changed`` fires after a theme is applied."""
    global _BUS
    if _BUS is None:
        _BUS = _ThemeBus()
    return _BUS


def on_theme_changed(callback) -> None:
    """Call ``callback()`` after every theme switch (incl. OS light/dark flips).

    Pass a bound method of a QObject so the connection dies with it.
    """
    theme_bus().changed.connect(callback)


# ---------------------------------------------------------------------------
# Theme selection & persistence
# ---------------------------------------------------------------------------


def available_themes() -> list[tuple[str, str]]:
    return list(THEME_LABELS.items())


def normalize_theme_name(theme_name: str | None) -> str:
    if not theme_name:
        return THEME_SYSTEM
    normalized = str(theme_name).strip().lower()
    return normalized if normalized in _KNOWN_THEMES else THEME_SYSTEM


def load_saved_theme() -> str:
    settings = _settings.get_settings()
    file_theme = normalize_theme_name(settings.get(_THEME_SETTINGS_KEY, THEME_SYSTEM))
    native_theme = normalize_theme_name(QSettings().value(_THEME_SETTINGS_KEY, THEME_SYSTEM))

    if file_theme != THEME_SYSTEM:
        chosen = file_theme
    elif native_theme != THEME_SYSTEM:
        chosen = native_theme
    else:
        chosen = THEME_SYSTEM

    if file_theme != chosen:
        settings.set(_THEME_SETTINGS_KEY, chosen)
    if native_theme != chosen:
        native_settings = QSettings()
        native_settings.setValue(_THEME_SETTINGS_KEY, chosen)
        native_settings.sync()

    return chosen


def save_theme(theme_name: str | None) -> str:
    theme = normalize_theme_name(theme_name)
    _settings.get_settings().set(_THEME_SETTINGS_KEY, theme)
    native_settings = QSettings()
    native_settings.setValue(_THEME_SETTINGS_KEY, theme)
    native_settings.sync()
    return theme


def os_prefers_dark() -> bool:
    """True when the OS is in dark mode (falls back to the startup palette)."""
    app = QGuiApplication.instance()
    if app is None:
        return True
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    palette = _SYSTEM_PALETTE or QGuiApplication.palette()
    return palette.color(QPalette.Window).lightness() < 128


def resolve_theme(theme_name: str | None) -> str:
    """Map a saved theme name to a concrete palette name (System → OS)."""
    theme = normalize_theme_name(theme_name)
    if theme == THEME_SYSTEM:
        return THEME_DARK if os_prefers_dark() else THEME_LIGHT
    return theme


def tokens_for(theme_name: str | None) -> Tokens:
    return _PALETTES[resolve_theme(theme_name)]


def tokens() -> Tokens:
    """Tokens of the applied theme (or the saved one before the first apply)."""
    if _CURRENT is not None:
        return _CURRENT
    try:
        return tokens_for(load_saved_theme())
    except Exception:
        return _PALETTES[THEME_DARK]


def apply_theme(app: QApplication, theme_name: str | None) -> str:
    global _CURRENT, _SYSTEM_PALETTE, _OS_SCHEME_HOOKED

    theme = normalize_theme_name(theme_name)
    if _SYSTEM_PALETTE is None:
        _SYSTEM_PALETTE = QPalette(app.palette())

    t = tokens_for(theme)
    _CURRENT = t
    app.setStyle("Fusion")
    app.setPalette(_build_palette(t))
    app.setStyleSheet(build_stylesheet(t))

    if not _OS_SCHEME_HOOKED:
        _OS_SCHEME_HOOKED = True
        QGuiApplication.styleHints().colorSchemeChanged.connect(_on_os_scheme_changed)

    theme_bus().changed.emit()
    return theme


def _on_os_scheme_changed(*_args) -> None:
    app = QApplication.instance()
    if app is None:
        return
    if load_saved_theme() == THEME_SYSTEM:
        apply_theme(app, THEME_SYSTEM)


# ---------------------------------------------------------------------------
# Helpers for widgets and rich text
# ---------------------------------------------------------------------------


def mix(color_a: str, color_b: str, amount: float) -> str:
    """Blend ``amount`` of ``color_a`` into ``color_b`` (0 → b, 1 → a)."""
    a = QColor(color_a)
    b = QColor(color_b)
    r = round(a.red() * amount + b.red() * (1 - amount))
    g = round(a.green() * amount + b.green() * (1 - amount))
    bl = round(a.blue() * amount + b.blue() * (1 - amount))
    return f"#{r:02x}{g:02x}{bl:02x}"


def color(tone: str) -> str:
    return getattr(tokens(), tone)


def qcolor(tone: str) -> QColor:
    return QColor(color(tone))


def px(size: str) -> int:
    return TYPE_SCALE[size]


def mono_font(size: str = "caption") -> QFont:
    """The platform's fixed-width face (SF Mono / Cascadia / DejaVu ...)."""
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPixelSize(TYPE_SCALE[size])
    return font


def _style(tone: str | None, size: str | None, weight: int | None, italic: bool) -> str:
    parts = []
    if tone:
        parts.append(f"color:{color(tone)};")
    if size:
        parts.append(f"font-size:{TYPE_SCALE[size]}px;")
    if weight:
        parts.append(f"font-weight:{weight};")
    if italic:
        parts.append("font-style:italic;")
    return "".join(parts)


def span(
    text: object,
    tone: str | None = None,
    *,
    size: str | None = None,
    weight: int | None = None,
    italic: bool = False,
    escape: bool = True,
) -> str:
    """Inline rich-text run styled from the active tokens (text is escaped)."""
    body = _html.escape(str(text)).replace("\n", "<br>") if escape else str(text)
    style = _style(tone, size, weight, italic)
    return f"<span style='{style}'>{body}</span>" if style else body


def block(
    inner: str,
    tone: str | None = None,
    *,
    size: str | None = None,
    weight: int | None = None,
    italic: bool = False,
    gap: int = 0,
) -> str:
    """Block-level rich-text wrapper; ``inner`` is trusted HTML."""
    style = _style(tone, size, weight, italic)
    if gap:
        style += f"margin-bottom:{gap}px;"
    return f"<div style='{style}'>{inner}</div>"


def repolish(widget: QWidget) -> None:
    """Re-evaluate stylesheet rules after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


# ---------------------------------------------------------------------------
# Palette + generated stylesheet
# ---------------------------------------------------------------------------


def _build_palette(t: Tokens) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.Window: t.bg,
        QPalette.WindowText: t.text,
        QPalette.Base: t.surface,
        QPalette.AlternateBase: t.surface_2,
        QPalette.Button: t.surface_2,
        QPalette.ButtonText: t.text,
        QPalette.Text: t.text,
        QPalette.ToolTipBase: t.surface,
        QPalette.ToolTipText: t.text,
        QPalette.Highlight: t.accent,
        QPalette.HighlightedText: t.on_accent,
        QPalette.BrightText: t.bad,
        QPalette.Link: t.accent,
        QPalette.PlaceholderText: t.muted,
        QPalette.Mid: t.border,
        QPalette.Dark: t.border,
        QPalette.Light: t.surface_2,
        QPalette.Midlight: t.surface_2,
        QPalette.Shadow: t.bg,
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(QPalette.Disabled, role, QColor(t.muted))
    return palette


def build_stylesheet(t: Tokens) -> str:
    """The one stylesheet template every theme is generated from."""
    hover = mix(t.text, t.surface_2, 0.08)
    pressed = t.tint("accent", 0.22)
    selected = t.tint("accent", 0.18)
    primary_hover = mix(t.bg, t.accent, 0.15)
    b = f"{t.line}px solid"
    fs = TYPE_SCALE
    return f"""
QWidget {{ font-size: {fs["body"]}px; }}
QToolTip {{ color: {t.text}; background: {t.surface}; border: {b} {t.border}; padding: 4px 6px; }}
QMenuBar {{ background: {t.bg}; color: {t.text}; }}
QMenuBar::item:selected {{ background: {selected}; }}
QMenu {{ background: {t.surface}; color: {t.text}; border: {b} {t.border}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 18px; border-radius: {t.radius}px; }}
QMenu::item:selected {{ background: {selected}; }}
QMenu::item:disabled {{ color: {t.muted}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

QPushButton {{
    background: {t.surface_2}; color: {t.text};
    border: {b} {t.border}; border-radius: {t.radius}px; padding: 5px 10px;
}}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.muted}; }}
QPushButton:checked, QPushButton[state="on"] {{ background: {selected}; border-color: {t.accent}; font-weight: 600; }}
QPushButton[state="paused"] {{ background: {t.tint("warn", 0.18)}; border-color: {t.warn}; color: {t.warn}; font-weight: 600; }}
QPushButton[variant="primary"] {{ background: {t.accent}; color: {t.on_accent}; border-color: {t.accent}; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {primary_hover}; }}
QPushButton[variant="primary"]:disabled {{ background: {t.surface_2}; color: {t.muted}; border-color: {t.border}; }}
QPushButton[variant="flat"] {{ background: transparent; border-color: transparent; color: {t.accent}; padding: 3px 2px; }}
QPushButton[variant="flat"]:hover {{ color: {t.text}; }}
QPushButton[variant="menuitem"] {{ background: transparent; border-color: transparent; text-align: left; padding: 6px 10px; }}
QPushButton[variant="menuitem"]:hover {{ background: {selected}; }}
QPushButton[variant="menuitem"][state="on"] {{ background: {selected}; border-color: transparent; }}
QPushButton[seg="left"] {{ border-top-right-radius: 0px; border-bottom-right-radius: 0px; }}
QPushButton[seg="right"] {{ border-top-left-radius: 0px; border-bottom-left-radius: 0px; border-left: none; }}

QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {t.surface}; color: {t.text};
    border: {b} {t.border}; border-radius: {t.radius}px; padding: 5px 7px;
    selection-background-color: {t.accent}; selection-color: {t.on_accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {t.accent}; }}
QTextEdit[bare="true"], QPlainTextEdit[bare="true"] {{ background: transparent; border: none; padding: 0px; }}

QFrame[card="true"] {{ background: {t.surface}; border: {b} {t.border}; border-radius: {t.radius_card}px; }}
QFrame[card="true"] QLabel {{ background: transparent; }}
QFrame[divider="true"] {{ background: {t.border}; border: none; min-height: 1px; max-height: 1px; }}
QFrame#voiceStylePopover {{ background: {t.surface}; border: {b} {t.border}; }}

QLabel[role="caption"] {{ color: {t.muted}; font-size: {fs["caption"]}px; font-weight: 700; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="small"] {{ color: {t.muted}; font-size: {fs["caption"]}px; }}
QLabel[role="emphasis"] {{ font-size: {fs["emphasis"]}px; font-weight: 600; }}
QLabel[role="title"] {{ font-size: {fs["title"]}px; font-weight: 700; }}
QLabel[role="display"] {{ font-size: {fs["display"]}px; font-weight: 700; }}
QLabel[role="emphasis"][empty="true"] {{ font-size: {fs["body"]}px; font-weight: 400; color: {t.muted}; }}
QLabel[chip="true"] {{
    color: {t.muted}; border: {b} {t.border}; border-radius: 9px;
    padding: 1px 8px; font-size: {fs["caption"]}px;
}}
QLabel[chip="true"][tone="convo"] {{ color: {t.convo}; border-color: {t.convo}; background: {t.tint("convo", 0.12)}; font-weight: 600; }}
QLabel#turnStrip {{
    background: {t.surface_2}; color: {t.text};
    border: {b} {t.border}; border-radius: {t.radius_card}px;
    padding: 6px 10px; font-weight: 700;
}}
QLabel#turnStrip[who="you"] {{ background: {t.tint("you")}; color: {t.you}; border-color: {t.you}; }}
QLabel#turnStrip[who="opp"] {{ background: {t.tint("opp")}; color: {t.opp}; border-color: {t.opp}; }}

QHeaderView::section {{
    background: {t.surface_2}; color: {t.muted}; border: none;
    border-bottom: {b} {t.border}; padding: 5px 6px; font-weight: 600;
}}
QTableView {{
    background: {t.surface}; alternate-background-color: {t.surface_2};
    gridline-color: {t.border}; border: {b} {t.border}; border-radius: {t.radius_card}px;
    selection-background-color: {selected}; selection-color: {t.text};
}}
QProgressBar {{
    background: {t.surface_2}; color: {t.text}; border: {b} {t.border};
    border-radius: {t.radius}px; text-align: center; min-height: 16px;
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: {max(t.radius - 1, 0)}px; }}
QGroupBox {{ border: {b} {t.border}; border-radius: {t.radius_card}px; margin-top: 12px; padding-top: 10px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {t.border}; border-radius: {min(t.radius, 4)}px; min-height: 24px; min-width: 24px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {t.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QStatusBar {{ border-top: {b} {t.border}; }}
"""


def token_names() -> list[str]:
    return [f.name for f in fields(Tokens) if f.type == "str" and f.name != "name"]
