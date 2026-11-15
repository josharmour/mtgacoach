"""Read per-print study notes from the selected deck in a local, nonmodal window."""

import uuid

from PySide6.QtCore import Qt, QTimer
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
        self.operation = ""
        self.running_pass = False
        self.remaining = []
        self.pass_digest = None
        self.next_job = QTimer(self)
        self.next_job.setSingleShot(True)
        self.next_job.timeout.connect(self.advance_pass)
        layout = QVBoxLayout(self)
        self.view = QComboBox()
        self.view.setAccessibleName("Deck study topic")
        for label, key in (
            ("Individual cards", "cards"),
            ("Sequencing and recovery", "sequencing"),
            ("Opponent threat classes", "switches"),
        ):
            self.view.addItem(label, key)
        layout.addWidget(self.view)
        self.card_label = QLabel("Card")
        layout.addWidget(self.card_label)
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
        self.pass_button = QPushButton("Study remaining cards")
        self.pass_button.setEnabled(False)
        self.pass_button.setToolTip(
            "Study each missing, source-verified nonland card with the selected on-device model."
        )
        self.pass_button.clicked.connect(self.toggle_pass)
        layout.addWidget(self.pass_button)
        self.progress = QLabel()
        self.progress.setWordWrap(True)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.view.currentIndexChanged.connect(self.change_topic)
        self.cards.currentIndexChanged.connect(self.read)
        self.build.clicked.connect(self.generate)
        session.deckCardStudyChanged.connect(self.received)
        self.request("list")

    def request(self, operation):
        self.token = uuid.uuid4().hex
        self.operation = operation
        self.session.request_deck_card_study(
            {
                "operation": operation,
                "deck_id": self.deck_id,
                "identity": self.identity,
                "card": self.cards.currentData(),
                "model": self.models.currentText(),
                "scope": self.view.currentData(),
                "token": self.token,
                "expected_digest": self.pass_digest if self.running_pass else None,
            }
        )

    def change_topic(self):
        cards = self.view.currentData() == "cards"
        self.cards.setVisible(cards)
        self.card_label.setVisible(cards)
        self.pass_button.setVisible(cards)
        self.progress.hide()
        self.build.setText("Study this card" if cards else "Study these topics")
        self.read()

    def read(self):
        if self.view.currentData() != "cards" or self.cards.currentData() is not None:
            self.detail.setPlainText("Reading saved card study…")
            self.request("read")

    def generate(self):
        self.build.setEnabled(False)
        self.pass_button.setEnabled(self.running_pass)
        self.view.setEnabled(False)
        self.detail.setPlainText("Studying on your device…")
        self.request("build")

    def toggle_pass(self):
        if self.running_pass:
            self.stop_pass()
            self.progress.setText("Paused. A card already in progress can finish.")
            return
        self.running_pass = True
        self.remaining = []
        self.models.setEnabled(False)
        self.cards.setEnabled(False)
        self.build.setEnabled(False)
        self.pass_button.setText("Pause card pass")
        self.progress.setText("Checking current prints and saved studies…")
        self.progress.show()
        self.request("prepare")

    def stop_pass(self):
        self.running_pass = False
        self.pass_digest = None
        self.next_job.stop()
        self.remaining.clear()
        self.pass_button.setText("Study remaining cards")
        self.models.setEnabled(True)
        self.cards.setEnabled(True)
        self.controls()

    def closeEvent(self, event):
        self.stop_pass()
        self.token = ""
        super().closeEvent(event)

    def controls(self):
        available = bool(self.models.count() and (self.view.currentData() != "cards" or self.cards.count()))
        self.view.setEnabled(not self.running_pass and self.operation not in ("build", "prepare"))
        self.build.setEnabled(
            available and not self.running_pass and self.operation not in ("build", "prepare")
        )
        self.pass_button.setEnabled(self.running_pass or self.build.isEnabled())

    def advance_pass(self):
        if not self.running_pass:
            return
        if not self.remaining:
            self.request("prepare")
            return
        gid = self.remaining.pop(0)
        i = self.cards.findData(gid)
        if i < 0:
            self.stop_pass()
            self.progress.setText("Deck card list changed. Reopen the study.")
            return
        self.cards.blockSignals(True)
        self.cards.setCurrentIndex(i)
        self.cards.blockSignals(False)
        self.progress.setText(
            f"Studying {self.cards.currentText()} • {len(self.remaining)} remaining after this card."
        )
        self.generate()

    def received(self, result):
        if (
            result.get("token") != self.token
            or result.get("identity") != self.identity
            or result.get("deck_id") != self.deck_id
            or result.get("card") != self.cards.currentData()
        ):
            return
        operation, self.operation = self.operation, ""
        if self.running_pass and "error" in result:
            self.stop_pass()
            self.progress.setText("Pass stopped. Completed card studies are saved on this device.")
        if "card_pass" in result:
            if not self.running_pass:
                self.controls()
                return
            report = result["card_pass"]
            if self.pass_digest is not None:
                # Recheck all current inputs once after the frozen worklist.
                # A changing deck or missing cache must not create a retry loop.
                self.stop_pass()
            else:
                self.pass_digest = report["model_digest"]
                self.remaining = list(report["pending"])
            if self.running_pass and self.remaining:
                self.next_job.start(0)
            else:
                self.stop_pass()
            measures = []
            for value, missing, label in (
                (f"{report['seconds']:.1f}s", report["unreported_seconds"], "build time"),
                (report["generated_tokens"], report["unreported_generated"], "generated tokens"),
                (report["thinking_tokens"], report["unreported_thinking"], "thinking tokens"),
            ):
                measures.append(
                    f"{value} reported {label} ({missing} unreported)" if missing else f"{value} {label}"
                )
            self.progress.setText(
                f"{report['cached']} local roles awaiting review, {report['admitted']} saved study roles "
                f"across {report['nonlands']} known nonland cards. {report['unresolved']} deck prints have unresolved "
                f"types. {report['withheld']} withheld on print checks. {len(report['pending'])} still to study. "
                f"Cached roles used {', '.join(measures)}."
            )
        elif "cards" in result:
            self.cards.blockSignals(True)
            self.cards.clear()
            for entry in result["cards"]:
                self.cards.addItem(entry["name"], entry["card"])
            self.cards.blockSignals(False)
            self.models.addItems(result["models"])
            if result.get("unresolved"):
                self.progress.setText(
                    f"{self.cards.count()} known nonland cards. "
                    f"{result['unresolved']} deck prints have unresolved types."
                )
                self.progress.show()
            if not self.models.count():
                self.models.setPlaceholderText("No installed local models found")
            if self.cards.count():
                self.read()
            else:
                self.detail.setPlainText("This deck has no resolved nonland prints.")
        else:
            self.detail.setPlainText(result.get("detail") or result.get("error") or "Card study unavailable.")
        if self.running_pass and operation == "build" and "error" not in result:
            self.next_job.start(0)
        self.controls()
