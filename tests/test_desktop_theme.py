"""Desktop theme: one token source, readable in every theme, System follows the OS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop import theme

DESKTOP = Path(theme.__file__).parent

# Colour and size literals are only allowed in theme.py; everything else goes
# through tokens (span/block/qcolor) or stylesheet roles.
_FORBIDDEN = [
    (re.compile(r"#[0-9a-fA-F]{6}\b"), "hex colour"),
    (re.compile(r"\brgba?\("), "rgb()/rgba() colour"),
    (re.compile(r"\bQColor\("), "QColor literal"),
    (re.compile(r"font-size"), "font-size"),
    (re.compile(r"setPointSize|setPixelSize"), "font size"),
    (re.compile(r"QFont\(\""), "named font family"),
    (re.compile(r"palette\("), "stylesheet palette() role"),
]


def test_no_colour_or_size_literals_outside_theme():
    offenders = []
    for path in sorted(DESKTOP.glob("*.py")):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern, what in _FORBIDDEN:
                if pattern.search(line):
                    offenders.append(f"{path.name}:{lineno}: {what}: {line.strip()}")
    assert not offenders, "use arenamcp.desktop.theme instead:\n" + "\n".join(offenders)


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("name", [theme.THEME_DARK, theme.THEME_LIGHT, theme.THEME_HIGH_CONTRAST])
def test_every_text_tone_is_readable(name):
    t = theme._PALETTES[name]
    for ground in (t.bg, t.surface):
        assert _contrast(t.text, ground) >= 7
        for tone in ("muted", "accent", "good", "warn", "bad", "you", "opp", "convo"):
            ratio = _contrast(getattr(t, tone), ground)
            assert ratio >= 4.5, f"{name}: {tone} on {ground} is {ratio:.2f}:1"
    assert _contrast(t.on_accent, t.accent) >= 4.5


def test_high_contrast_has_its_own_tokens():
    hc = theme._PALETTES[theme.THEME_HIGH_CONTRAST]
    dark = theme._PALETTES[theme.THEME_DARK]
    assert hc.surface == "#000000"
    assert hc.text == "#ffffff"
    assert hc.line == 2
    assert hc.good != dark.good


@pytest.mark.parametrize("os_dark, expected", [(True, theme.THEME_DARK), (False, theme.THEME_LIGHT)])
def test_system_theme_follows_os(monkeypatch, os_dark, expected):
    monkeypatch.setattr(theme, "os_prefers_dark", lambda: os_dark)
    assert theme.resolve_theme(theme.THEME_SYSTEM) == expected
    assert theme.resolve_theme(theme.THEME_HIGH_CONTRAST) == theme.THEME_HIGH_CONTRAST


def test_apply_theme_emits_change_and_swaps_tokens(qapp):
    seen = []
    theme.theme_bus().changed.connect(lambda: seen.append(theme.tokens().name))
    theme.apply_theme(qapp, theme.THEME_LIGHT)
    theme.apply_theme(qapp, theme.THEME_HIGH_CONTRAST)
    theme.apply_theme(qapp, theme.THEME_DARK)
    assert seen[-3:] == [theme.THEME_LIGHT, theme.THEME_HIGH_CONTRAST, theme.THEME_DARK]
    assert "#000000" not in qapp.styleSheet()  # dark sheet, not HC


@pytest.mark.parametrize("name", [theme.THEME_DARK, theme.THEME_LIGHT, theme.THEME_HIGH_CONTRAST])
def test_generated_stylesheet_is_fully_resolved(name):
    sheet = theme.build_stylesheet(theme._PALETTES[name])
    assert "{t." not in sheet and "None" not in sheet
    assert sheet.count("{") == sheet.count("}")


def test_span_escapes_and_uses_tokens(qapp):
    theme.apply_theme(qapp, theme.THEME_DARK)
    html = theme.span("<b>Bolt</b>", "bad", size="caption")
    assert "&lt;b&gt;Bolt&lt;/b&gt;" in html
    assert theme.tokens().bad in html
    assert f"font-size:{theme.TYPE_SCALE['caption']}px" in html


def test_repair_button_label_is_not_a_mnemonic(qapp):
    from arenamcp.desktop.repair_tab import RepairTab

    tab = RepairTab()
    assert tab._run_btn.text() == "Check && Repair"
