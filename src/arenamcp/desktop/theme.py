from __future__ import annotations

from typing import Final

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from arenamcp.settings import get_settings

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

_KNOWN_THEMES = frozenset(THEME_LABELS)
_SYSTEM_PALETTE: QPalette | None = None
_SYSTEM_STYLE: str | None = None
_THEME_SETTINGS_KEY: Final = "desktop_theme"


def available_themes() -> list[tuple[str, str]]:
    return list(THEME_LABELS.items())


def normalize_theme_name(theme_name: str | None) -> str:
    if not theme_name:
        return THEME_SYSTEM
    normalized = theme_name.strip().lower()
    return normalized if normalized in _KNOWN_THEMES else THEME_SYSTEM


def load_saved_theme() -> str:
    settings = get_settings()
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
    get_settings().set(_THEME_SETTINGS_KEY, theme)
    native_settings = QSettings()
    native_settings.setValue(_THEME_SETTINGS_KEY, theme)
    native_settings.sync()
    return theme


def apply_theme(app: QApplication, theme_name: str | None) -> str:
    global _SYSTEM_PALETTE, _SYSTEM_STYLE

    theme = normalize_theme_name(theme_name)
    if _SYSTEM_PALETTE is None:
        _SYSTEM_PALETTE = QPalette(app.palette())
    if _SYSTEM_STYLE is None:
        _SYSTEM_STYLE = app.style().objectName()

    if theme == THEME_SYSTEM:
        if _SYSTEM_STYLE:
            app.setStyle(_SYSTEM_STYLE)
        app.setPalette(QPalette(_SYSTEM_PALETTE))
        app.setStyleSheet("")
        return theme

    app.setStyle("Fusion")
    palette = _build_palette(theme)
    app.setPalette(palette)
    app.setStyleSheet(_build_stylesheet(theme))
    return theme


def _build_palette(theme_name: str) -> QPalette:
    if theme_name == THEME_LIGHT:
        return _make_palette(
            window="#f5f6f8",
            window_text="#17191c",
            base="#ffffff",
            alternate_base="#eef1f5",
            button="#e9edf2",
            button_text="#17191c",
            text="#17191c",
            tooltip_base="#ffffff",
            tooltip_text="#17191c",
            highlight="#1f6feb",
            highlighted_text="#ffffff",
            bright_text="#b42318",
            link="#1f6feb",
            placeholder="#6b7280",
            mid="#c1c7d0",
        )

    if theme_name == THEME_HIGH_CONTRAST:
        return _make_palette(
            window="#000000",
            window_text="#ffffff",
            base="#000000",
            alternate_base="#0f0f0f",
            button="#000000",
            button_text="#ffffff",
            text="#ffffff",
            tooltip_base="#000000",
            tooltip_text="#ffffff",
            highlight="#ffff00",
            highlighted_text="#000000",
            bright_text="#ff4d4d",
            link="#00ffff",
            placeholder="#cfcfcf",
            mid="#ffffff",
        )

    return _make_palette(
        window="#181c20",
        window_text="#e6edf3",
        base="#0f1317",
        alternate_base="#20262c",
        button="#262d35",
        button_text="#e6edf3",
        text="#e6edf3",
        tooltip_base="#11161b",
        tooltip_text="#f8fafc",
        highlight="#58a6ff",
        highlighted_text="#0b1220",
        bright_text="#ff7b72",
        link="#79c0ff",
        placeholder="#8b949e",
        mid="#39424d",
    )


def _make_palette(
    *,
    window: str,
    window_text: str,
    base: str,
    alternate_base: str,
    button: str,
    button_text: str,
    text: str,
    tooltip_base: str,
    tooltip_text: str,
    highlight: str,
    highlighted_text: str,
    bright_text: str,
    link: str,
    placeholder: str,
    mid: str,
) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(window))
    palette.setColor(QPalette.WindowText, QColor(window_text))
    palette.setColor(QPalette.Base, QColor(base))
    palette.setColor(QPalette.AlternateBase, QColor(alternate_base))
    palette.setColor(QPalette.Button, QColor(button))
    palette.setColor(QPalette.ButtonText, QColor(button_text))
    palette.setColor(QPalette.Text, QColor(text))
    palette.setColor(QPalette.ToolTipBase, QColor(tooltip_base))
    palette.setColor(QPalette.ToolTipText, QColor(tooltip_text))
    palette.setColor(QPalette.Highlight, QColor(highlight))
    palette.setColor(QPalette.HighlightedText, QColor(highlighted_text))
    palette.setColor(QPalette.BrightText, QColor(bright_text))
    palette.setColor(QPalette.Link, QColor(link))
    palette.setColor(QPalette.PlaceholderText, QColor(placeholder))
    palette.setColor(QPalette.Mid, QColor(mid))
    return palette


def _build_stylesheet(theme_name: str) -> str:
    if theme_name == THEME_LIGHT:
        return """
QToolTip {
    color: #17191c;
    background-color: #ffffff;
    border: 1px solid #c1c7d0;
    padding: 4px 6px;
}
QGroupBox {
    border: 1px solid #cfd6de;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QTabBar::tab {
    background: #e9edf2;
    border: 1px solid #cfd6de;
    border-bottom: none;
    padding: 8px 12px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background: #ffffff;
}
QPushButton, QLineEdit, QPlainTextEdit, QTextEdit {
    border: 1px solid #cfd6de;
    border-radius: 6px;
    padding: 6px 8px;
}
QPushButton:hover {
    background-color: #dfe6ef;
}
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 2px solid #1f6feb;
}
QStatusBar {
    border-top: 1px solid #cfd6de;
}
"""

    if theme_name == THEME_HIGH_CONTRAST:
        return """
QWidget {
    font-size: 13px;
}
QToolTip {
    color: #000000;
    background-color: #ffff00;
    border: 2px solid #ffffff;
    padding: 4px 6px;
}
QGroupBox {
    border: 2px solid #ffffff;
    border-radius: 0px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}
QTabBar::tab {
    background: #000000;
    color: #ffffff;
    border: 2px solid #ffffff;
    padding: 8px 12px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: #ffff00;
    color: #000000;
}
QPushButton, QLineEdit, QPlainTextEdit, QTextEdit {
    border: 2px solid #ffffff;
    border-radius: 0px;
    padding: 6px 8px;
    background: #000000;
    color: #ffffff;
}
QPushButton:hover {
    background-color: #1a1a1a;
}
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 3px solid #ffff00;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background: #000000;
    border: 1px solid #ffffff;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #ffff00;
    min-height: 20px;
    min-width: 20px;
}
QStatusBar {
    border-top: 2px solid #ffffff;
}
"""

    return """
QToolTip {
    color: #f8fafc;
    background-color: #11161b;
    border: 1px solid #39424d;
    padding: 4px 6px;
}
QGroupBox {
    border: 1px solid #39424d;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QTabBar::tab {
    background: #20262c;
    border: 1px solid #39424d;
    border-bottom: none;
    padding: 8px 12px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background: #181c20;
}
QPushButton, QLineEdit, QPlainTextEdit, QTextEdit {
    border: 1px solid #39424d;
    border-radius: 6px;
    padding: 6px 8px;
}
QPushButton:hover {
    background-color: #313944;
}
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 2px solid #58a6ff;
}
QStatusBar {
    border-top: 1px solid #39424d;
}
"""
