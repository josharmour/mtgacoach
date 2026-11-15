"""Read per-print study notes from the selected deck in a local, nonmodal window."""

import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class DeckCardStudyDialog(QDialog):
    def __init__(self, session, deck, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"{deck.get('name', 'Deck')} · Card study")
        self.resize(650, 550)
        self.session = session
        self.deck_id, self.identity = deck["deck_id"], deck["identity"]
        self.token = ""
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Card"))
        self.cards = QComboBox()
        self.cards.setAccessibleName("Card to study")
        layout.addWidget(self.cards)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 1)
        models = QHBoxLayout()
        models.addWidget(QLabel("On-device model"))
        self.models = QComboBox()
        self.models.setAccessibleName("Installed local model")
        models.addWidget(self.models, 1)
        self.build = QPushButton("Study this card")
        self.build.setToolTip("Prepare play, hold and avoid guidance on this device between matches.")
        self.build.setEnabled(False)
        models.addWidget(self.build)
        layout.addLayout(models)
        self.cards.currentIndexChanged.connect(self.read)
        self.build.clicked.connect(self.generate)
        session.deckCardStudyChanged.connect(self.received)
        self.request("list")

    def request(self, operation):
        self.token = uuid.uuid4().hex
        self.session.request_deck_card_study(
            {
                "operation": operation,
                "deck_id": self.deck_id,
                "identity": self.identity,
                "card": self.cards.currentData(),
                "model": self.models.currentText(),
                "token": self.token,
            }
        )

    def read(self):
        if self.cards.currentData() is not None:
            self.detail.setPlainText("Reading saved card study…")
            self.request("read")

    def generate(self):
        self.build.setEnabled(False)
        self.detail.setPlainText("Studying this card on your device…")
        self.request("build")

    def received(self, result):
        if (
            result.get("token") != self.token
            or result.get("identity") != self.identity
            or result.get("deck_id") != self.deck_id
            or result.get("card") != self.cards.currentData()
        ):
            return
        if "cards" in result:
            self.cards.blockSignals(True)
            self.cards.clear()
            for entry in result["cards"]:
                self.cards.addItem(entry["name"], entry["card"])
            self.cards.blockSignals(False)
            self.models.addItems(result["models"])
            if not self.models.count():
                self.models.setPlaceholderText("No installed local models found")
            if self.cards.count():
                self.read()
            else:
                self.detail.setPlainText("This deck has no resolved nonland prints.")
        else:
            self.detail.setPlainText(result.get("detail") or result.get("error") or "Card study unavailable.")
        self.build.setEnabled(bool(self.models.count() and self.cards.count()))
