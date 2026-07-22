import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from arenamcp.desktop.card_overlay import CardBadge, CardOverlayWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_card_badge_refresh_synergy_and_lock(qapp):
    """Test CardBadge set_data formats synergy badges and locked color pair indicators."""
    badge = CardBadge()

    badge.set_data(
        score=85.0,
        tier="GOLD",
        pair="UR",
        reason="fits locked UR",
        synergy_badge="⚡ Spells (4)",
        is_locked=True,
    )

    text = badge.text()
    assert "85" in text
    assert "🔒 UR" in text
    assert "⚡ Spells (4)" in text
    assert "2px solid #3b82f6" in badge.styleSheet()


def test_card_overlay_window_set_locked_color_pair(qapp):
    """Test CardOverlayWindow set_locked_color_pair updates locked state."""
    overlay = CardOverlayWindow()
    overlay.set_locked_color_pair("WB")
    assert overlay._locked_color_pair == "WB"

    overlay.set_locked_color_pair(None)
    assert overlay._locked_color_pair is None
