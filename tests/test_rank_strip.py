"""Account identity remains visible alongside the Arena rank cards."""

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from arenamcp.desktop.rank_strip import RankStrip


def test_account_header_unknown_named_and_narrow(monkeypatch):
    app = QApplication.instance() or QApplication([])
    strip = RankStrip()
    strip.resize(540, 200)
    strip._render()
    assert "Not detected yet" in strip.account_label.text()
    strip._reader.account_name = "<Player>#12345"
    strip._render()
    assert strip.account_label.text() == "Arena account · <Player>#12345"
    assert strip.account_label.textFormat() == Qt.PlainText
    assert strip.buttons["Constructed"].text().startswith("Constructed")
    strip._reader.account_name = "Very long account name " * 10
    strip.resize(240, 200)
    strip._render()
    assert strip.account_label.text().endswith("…")
    assert strip._reader.account_name.strip() in strip.account_label.toolTip()
    strip.close()
    app.processEvents()
