from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.dev_source import source_revision
from arenamcp.desktop.main_window import MainWindow


def test_copy_commit_keeps_running_revision_when_checkout_moves(qapp, monkeypatch):
    original = qapp.clipboard().text()
    running = "a" * 40
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/shared/dev")
    monkeypatch.setenv("MTGACOACH_DEV_COMMIT", running)
    monkeypatch.setattr(MainWindow, "_start_session", Mock())
    window = MainWindow()
    try:
        monkeypatch.setenv("MTGACOACH_DEV_COMMIT", "b" * 40)
        window.dev_commit_button.click()
        assert qapp.clipboard().text() == running
        assert "Copied commit aaaaaaa" in window.statusBar().currentMessage()
    finally:
        window.close()
        qapp.clipboard().setText(original)


def test_unknown_dev_revision_does_not_copy_stale_text(qapp, monkeypatch):
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/missing/share")
    monkeypatch.setenv("MTGACOACH_DEV_COMMIT", "unknown commit")
    monkeypatch.setattr(MainWindow, "_start_session", Mock())
    window = MainWindow()
    try:
        assert not window.dev_commit_button.isEnabled()
    finally:
        window.close()


def test_managed_window_has_no_dev_commit_button(qapp, monkeypatch):
    monkeypatch.delenv("MTGACOACH_DEV_CHECKOUT", raising=False)
    monkeypatch.setattr(MainWindow, "_start_session", Mock())
    window = MainWindow()
    try:
        assert window.dev_commit_button is None
    finally:
        window.close()


def test_source_revision_returns_full_hash_for_copy(monkeypatch, tmp_path):
    monkeypatch.delenv("MTGACOACH_DEV_GIT_DIR", raising=False)
    metadata = tmp_path / ".git"
    metadata.mkdir()
    full = "123456789abcdef" * 2 + "0123456789"
    (metadata / "HEAD").write_text(full)
    assert source_revision(str(tmp_path), short=False) == full
    assert source_revision(str(tmp_path)) == full[:7]
