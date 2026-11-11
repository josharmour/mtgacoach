"""November 11 NAS reconciliation: one controller and real loss-review producer."""

from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_session_recaps import record

from arenamcp.loss_streak import LossStreakStore
from arenamcp.standalone_loss_streak import LossStreakMixin


def test_main_window_constructs_exactly_one_recap_controller(qapp, monkeypatch):
    from arenamcp.desktop import main_window, session_recap
    from arenamcp.desktop.repair_tab import RepairTab

    constructor = Mock(return_value=SimpleNamespace(closing=lambda: True))
    monkeypatch.setattr(session_recap, "SessionRecapController", constructor)
    monkeypatch.setattr(main_window.MainWindow, "_start_session", lambda self: None)
    monkeypatch.setattr(main_window, "WatchdogPingBridge", None)
    monkeypatch.setattr(RepairTab, "run_checks", Mock())
    window = main_window.MainWindow()
    try:
        constructor.assert_called_once_with(window)
    finally:
        window.close()
        window.deleteLater()


def test_real_loss_reports_feed_r1_dismissal_restart_and_correction(qapp, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QWidget

    from arenamcp import logging_config
    from arenamcp.desktop.session_recap import SessionRecapController

    log_dir = tmp_path / ".arenamcp"
    monkeypatch.setattr(logging_config, "LOG_DIR", log_dir)
    monkeypatch.setattr("arenamcp.desktop.session_recap.Path.home", lambda: tmp_path)
    rows = []
    monkeypatch.setattr(SessionRecapController, "records", staticmethod(lambda: list(rows)))
    store = LossStreakStore(log_dir / "loss_streak_reviews.json")

    class Engine(LossStreakMixin):
        pass

    engine = Engine()
    engine._loss_streak_store = store
    engine._coach = None  # Offline producer: no model/network calls.
    engine.ui = SimpleNamespace(advice=lambda *args, **kwargs: None)
    windows = []

    def controller():
        window = QWidget()
        window._closed = False
        windows.append(window)
        return SessionRecapController(window)

    def finish(mid):
        store.observe(mid, "same-deck", True)
        # Two game rows still count as one authoritative BO3 match.
        for game in (1, 2):
            rows.append(record(f"{mid}-g{game}"))
        store.finish(mid, "loss")

    try:
        first = controller()
        finish("a")
        finish("b")
        assert first.closing() is True
        assert first.reviews() == []
        finish("c")
        assert first.reviews() == []  # Pending is not a completed review.
        engine._run_loss_reviews()
        reviews = first.reviews()
        assert len(reviews) == 1 and reviews[0]["matches"] == ["a", "b", "c"]
        assert "incomplete" in reviews[0]["recommendation"]
        # A relaunch, not an injected report fixture, discovers the real producer output.
        second = controller()
        second.offer_previous()
        assert second.dialog is not None
        next(b for b in second.dialog.buttons() if b.text() == "Don't show this recap again").click()
        third = controller()
        third.offer_previous()
        assert third.dialog is None
        for mid in ("d", "e", "f"):
            finish(mid)
        engine._run_loss_reviews()
        assert third.closing() is False
        assert third.store.pending(third.reviews())["id"] == "review:d,e,f"
        store.finish("e", "win")
        fourth = controller()
        fourth.offer_previous()
        assert fourth.dialog is None  # Superseded producer job cannot re-offer.
        assert fourth.store.pending(fourth.reviews()) is None
    finally:
        for window in windows:
            window._closed = True
            window.close()
            window.deleteLater()
