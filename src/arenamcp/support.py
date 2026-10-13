"""User-reviewed support drafts. No background uploads or raw log collection."""

import platform
from urllib.parse import quote, urlencode

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

SUPPORT_EMAIL = "support@mtgacoach.com"
PATREON_URL = "https://www.patreon.com/c/mtgacoach"


def diagnostic_summary():
    # Construct from trusted application/runtime values, never logs/config/report contents.
    from arenamcp import __version__

    return (
        f"MTGA Coach support details\nVersion: {__version__}\n"
        f"OS: {platform.system()}\nPython: {platform.python_version()}\n"
        "No keys, account details, game data, screenshots or logs included."
    )


def contact_josh(parent=None, summary=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Contact Josh")
    dialog.resize(560, 400)
    layout = QVBoxLayout(dialog)
    intro = QLabel(
        "Tell Josh what happened and what you expected. Review the message below. "
        "Nothing is sent automatically; send it yourself in email or Patreon."
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)
    editor = QPlainTextEdit(
        "Hi Josh, I need help with MTGA Coach.\n\nWhat happened:\n\n"
        "What I expected:\n\n" + (summary or diagnostic_summary())
    )
    editor.setAccessibleName("Support message — review before sharing")
    layout.addWidget(editor)
    status = QLabel("")
    status.setWordWrap(True)
    layout.addWidget(status)
    row = QHBoxLayout()
    copy = QPushButton("Copy message")

    def copy_message():
        QApplication.clipboard().setText(editor.toPlainText())
        status.setText("Message copied. Paste it into your email or Patreon conversation and send.")

    copy.clicked.connect(copy_message)
    row.addWidget(copy)
    email = QPushButton("Open email draft")
    email.setEnabled(bool(SUPPORT_EMAIL))
    email.setToolTip(SUPPORT_EMAIL or "Public support email is awaiting configuration.")

    def open_email():
        url = (
            "mailto:"
            + SUPPORT_EMAIL
            + "?"
            + urlencode({"subject": "MTGA Coach support", "body": editor.toPlainText()}, quote_via=quote)
        )
        if QDesktopServices.openUrl(QUrl(url)):
            status.setText("Email draft opened. Review it and press Send in your email app.")
        else:
            status.setText("Could not open an email app. Copy the message and email " + SUPPORT_EMAIL)

    email.clicked.connect(open_email)
    row.addWidget(email)
    patreon = QPushButton("Open Patreon (members)")

    def open_patreon():
        if QDesktopServices.openUrl(QUrl(PATREON_URL)):
            status.setText(
                "On MTGA Coach’s Patreon page, choose More → Message. "
                "Copy your message here, then paste and send there. Patreon requires membership or a prior payment."
            )
        else:
            status.setText("Open " + PATREON_URL + " in your browser to message MTGA Coach.")

    patreon.clicked.connect(open_patreon)
    row.addWidget(patreon)
    layout.addLayout(row)
    close = QPushButton("Close")
    close.clicked.connect(dialog.reject)
    layout.addWidget(close)
    dialog.exec()
