"""Support stays local until the user chooses a destination and sends."""

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QPushButton

from arenamcp import support


def test_support_review_does_not_send_and_preserves_draft(monkeypatch):
    app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr(
        support.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True
    )
    assert support.SUPPORT_EMAIL == "support@mtgacoach.com"

    def inspect(dialog):
        assert not opened
        editor = dialog.findChild(QPlainTextEdit)
        editor.setPlainText("My question & details + more")
        buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
        buttons["Copy message"].click()
        assert app.clipboard().text() == editor.toPlainText()
        assert not opened
        buttons["Open email draft"].click()
        from urllib.parse import parse_qs, urlsplit

        url = urlsplit(opened[0])
        assert url.scheme == "mailto" and url.path == "support@mtgacoach.com"
        assert parse_qs(url.query)["body"] == [editor.toPlainText()]
        assert parse_qs(url.query)["subject"] == ["MTGA Coach support"]
        return 0

    monkeypatch.setattr(QDialog, "exec", inspect)
    support.contact_josh()


@pytest.mark.parametrize("browser_ok", [True, False])
def test_disabled_email_and_patreon_share_no_draft(monkeypatch, browser_ok):
    app = QApplication.instance() or QApplication([])
    app.clipboard().setText("clipboard-sentinel")
    opened = []
    monkeypatch.setattr(support, "SUPPORT_EMAIL", "")
    monkeypatch.setenv("API_KEY", "secret-sentinel")
    monkeypatch.setattr(support.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or browser_ok)
    def inspect(dialog):
        dialog.show()
        app.processEvents()
        editor = dialog.findChild(QPlainTextEdit)
        initial = editor.toPlainText()
        assert "secret-sentinel" not in initial
        assert "file://" not in initial and "@" not in initial
        assert "Version:" in initial and "OS:" in initial
        assert app.clipboard().text() == "clipboard-sentinel"
        assert not opened
        buttons = {b.text(): b for b in dialog.findChildren(QPushButton)}
        assert not buttons["Open email draft"].isEnabled()
        buttons["Open email draft"].click()
        assert not opened
        editor.setPlainText("private-draft-sentinel")
        buttons["Open Patreon (members)"].click()
        assert opened == ["https://www.patreon.com/c/mtgacoach"]
        assert app.clipboard().text() == "clipboard-sentinel"
        from PySide6.QtWidgets import QLabel
        status = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "sent successfully" not in status.lower()
        assert "paste and send" in status if browser_ok else "in your browser" in status
        dialog.close()
        return 0
    monkeypatch.setattr(QDialog, "exec", inspect)
    support.contact_josh()
