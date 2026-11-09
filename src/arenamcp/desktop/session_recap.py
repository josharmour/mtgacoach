"""Optional session feedback, with durable unacknowledged offers after quick exit."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox, QTextEdit, QVBoxLayout

from arenamcp.match_history import MatchHistory
from arenamcp.session_recap import SessionRecaps

logger = logging.getLogger(__name__)


class SessionRecapController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = SessionRecaps(Path.home() / ".arenamcp" / "session_recaps.json")
        self.dialog = None
        self._close_offered = False
        self.store.begin(self.records(), self.reviews())
        QTimer.singleShot(700, self.offer_previous)

    @staticmethod
    def records():
        history = MatchHistory()
        return history.get_recent(history.total_games)

    @staticmethod
    def reviews():
        from arenamcp.logging_config import LOG_DIR

        try:
            data = json.loads((LOG_DIR / "loss_streak_reviews.json").read_text(encoding="utf-8"))
            return [
                job["report"]
                for job in data.get("jobs", {}).values()
                if job.get("status") == "complete" and isinstance(job.get("report"), dict)
            ]
        except (OSError, ValueError, AttributeError):
            return []

    def offer_previous(self):
        if getattr(self.window, "_closed", False):
            return
        recap = self.store.pending()
        if recap:
            self.offer(self.store.refresh(recap, self.records()), "Your last session recap is available")

    def offer(self, recap, title, *, close_after=False):
        box = QMessageBox(self.window)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setWindowTitle("Session feedback")
        box.setText(title)
        review = box.addButton("Review recap", QMessageBox.ButtonRole.AcceptRole)
        dismiss = box.addButton("Don't show this recap again", QMessageBox.ButtonRole.DestructiveRole)
        later = box.addButton(
            "Close and remind me next time" if close_after else "Later", QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(review)

        def selected(button):
            if button is review:
                QTimer.singleShot(0, lambda: self.review(recap))
            elif button is dismiss:
                self.store.acknowledge(recap["id"])
            if close_after and button in (later, dismiss):
                QTimer.singleShot(0, self.window.close)

        box.buttonClicked.connect(selected)
        self.dialog = box
        box.open()

    def review(self, recap):
        if getattr(self.window, "_closed", False):
            return
        box = QDialog(self.window)
        box.setWindowTitle("Your session review")
        box.resize(720, 600)
        layout = QVBoxLayout(box)
        text = QTextEdit(box)
        text.setReadOnly(True)
        text.setPlainText(recap["text"])
        text.setAccessibleName("Session review")
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, box)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Done")

        def done():
            self.store.acknowledge(recap["id"])
            box.accept()

        for action in recap.get("actions", ["history"]):
            label = {
                "history": "Review games",
                "support": "Prepare support note",
                "deck": "Compare deck options",
            }.get(action)
            if label:
                button = buttons.addButton(label, QDialogButtonBox.ButtonRole.ActionRole)
                button.clicked.connect(
                    lambda checked=False, action=action: self.take_action(action, recap, box)
                )

        buttons.accepted.connect(done)
        layout.addWidget(buttons)
        self.dialog = box
        box.open()

    def take_action(self, action, recap, box):
        """User-selected navigation/drafting only. Never send or modify a deck here."""
        if action == "support":
            from arenamcp.support import contact_josh

            contact_josh(box, summary="Session evidence (not a verified bug diagnosis):\n\n" + recap["text"])
        elif action == "history":
            show = getattr(self.window, "_show_performance_view", None)
            if callable(show):
                show()
                box.reject()  # Navigation is not acknowledgement; review remains available.
        elif action == "deck":
            show = getattr(self.window, "_show_decks_view", None)
            if callable(show):
                show()
                self.window.decks_tab.open_builder()
                box.reject()

    def closing(self) -> bool:
        """False keeps the window open for the offer; another close always works."""
        try:
            recap = self.store.capture(self.records(), self.reviews())
            if recap and not self._close_offered:
                self._close_offered = True
                self.offer(recap, "Would you like feedback on this session before closing?", close_after=True)
                return False
        except Exception:
            logger.warning("Could not save session recap", exc_info=True)
        return True
