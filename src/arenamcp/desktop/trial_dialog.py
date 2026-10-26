"""Two-step email-verified free-trial signup dialog.

Silent trial auto-provisioning was replaced by an emailed verification code
(server contract: POST /api/trial/request-code, then POST /api/trial with the
code). This dialog walks the user through both steps, persists the minted key
through the existing settings path (subscription.save_trial_key), and lets the
owner of the dialog refresh the access badge via the trial_started signal.

Network calls run on daemon threads (same pattern as RepairTab.run_checks);
results are marshaled back to the UI thread through the _step_done signal so
the dialog never blocks the coach's event loop.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from arenamcp.subscription import (
    SUBSCRIBE_URL,
    request_trial_code,
    request_trial_key,
    save_trial_key,
)

logger = logging.getLogger(__name__)


class TrialSignupDialog(QDialog):
    """Modal 'Start your free trial' wizard: email -> verification code -> key."""

    trial_started = Signal(str, str)  # key, expires_at — persisted BEFORE this fires
    license_key_requested = Signal()  # 'I have a license key' escape hatch

    _step_done = Signal(dict)  # network result, marshaled back to the UI thread

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start your free trial")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._email = ""
        self._stage = "email"  # "email" | "code"
        self._step_done.connect(self._on_step_result)
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        intro = QLabel(
            "Try mtgacoach free for 7 days. We'll email you a verification code "
            "to tie the trial to your address — no payment, no spam."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Step 1 — email
        self._email_label = QLabel("Step 1 — Your email:")
        root.addWidget(self._email_label)
        row1 = QHBoxLayout()
        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("you@example.com")
        self._email_edit.returnPressed.connect(self._send_code)
        row1.addWidget(self._email_edit, 1)
        self._send_btn = QPushButton("Send verification code")
        self._send_btn.setProperty("variant", "primary")
        self._send_btn.clicked.connect(self._send_code)
        row1.addWidget(self._send_btn)
        root.addLayout(row1)

        # Step 2 — verification code (revealed after a code is sent)
        self._code_label = QLabel("Step 2 — Enter the 6-digit code we emailed you:")
        root.addWidget(self._code_label)
        row2 = QHBoxLayout()
        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText("123456")
        self._code_edit.returnPressed.connect(self._verify)
        row2.addWidget(self._code_edit, 1)
        self._verify_btn = QPushButton("Verify && start trial")
        self._verify_btn.setProperty("variant", "primary")
        self._verify_btn.clicked.connect(self._verify)
        row2.addWidget(self._verify_btn)
        root.addLayout(row2)

        self._change_email_btn = QPushButton("Use a different email…")
        self._change_email_btn.setProperty("variant", "flat")
        self._change_email_btn.clicked.connect(self._back_to_email)
        root.addWidget(self._change_email_btn)

        # Shared inline status/error line for both steps.
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

        root.addStretch(1)

        escape = QPushButton("I have a license key")
        escape.setProperty("variant", "flat")
        escape.clicked.connect(self._use_license_key)
        root.addWidget(escape, alignment=Qt.AlignmentFlag.AlignLeft)

        self._show_stage("email")

    # ------------------------------------------------------------------
    def _show_stage(self, stage: str) -> None:
        self._stage = stage
        in_email = stage == "email"
        for w in (self._email_label, self._email_edit, self._send_btn):
            w.setVisible(in_email)
        for w in (self._code_label, self._code_edit, self._verify_btn, self._change_email_btn):
            w.setVisible(not in_email)
        if in_email:
            self._email_edit.setFocus()
        else:
            self._code_edit.setFocus()

    def _back_to_email(self) -> None:
        self._status_label.setText("")
        self._show_stage("email")

    # ------------------------------------------------------------------
    # Step 1: request the emailed code
    # ------------------------------------------------------------------
    def _send_code(self) -> None:
        email = self._email_edit.text().strip()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            self._status_label.setText("Please enter a valid email address.")
            return
        self._set_busy(True, "Sending…")
        self._status_label.setText("")

        def _work() -> None:
            result = request_trial_code(email)
            self._step_done.emit({"stage": "email", **result})

        threading.Thread(target=_work, daemon=True, name="trial-request-code").start()

    # ------------------------------------------------------------------
    # Step 2: exchange the code for a trial key
    # ------------------------------------------------------------------
    def _verify(self) -> None:
        code = self._code_edit.text().strip()
        if not code:
            self._status_label.setText("Enter the 6-digit code from your email.")
            return
        self._set_busy(True, "Verifying…")
        self._status_label.setText("")

        def _work() -> None:
            result = request_trial_key(self._email, code)
            self._step_done.emit({"stage": "code", **result})

        threading.Thread(target=_work, daemon=True, name="trial-verify").start()

    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, verb: str = "") -> None:
        for btn in (self._send_btn, self._verify_btn):
            btn.setEnabled(not busy)
            if busy:
                btn.setText(verb)

    def _restore_buttons(self) -> None:
        self._send_btn.setText("Send verification code")
        self._verify_btn.setText("Verify && start trial")
        self._set_busy(False)

    # ------------------------------------------------------------------
    def _on_step_result(self, result: dict) -> None:
        """Runs on the UI thread (via _step_done)."""
        self._restore_buttons()
        status = result.get("status", "")

        if result.get("stage") == "email":
            if status == "code_sent":
                minutes = max(1, int(result.get("expires_in", 900)) // 60)
                self._email = self._email_edit.text().strip()
                self._status_label.setText(
                    f"Code sent to {self._email} — it expires in about {minutes} minutes. "
                    "Check your spam folder if it doesn't arrive."
                )
                self._show_stage("code")
                return
            if status == "rate_limited":
                self._status_label.setText(result.get("message") or "Too many requests — try again soon.")
                return
            if status == "offline":
                self._status_label.setText(
                    result.get("message") or "Can't reach mtgacoach.com — check your connection."
                )
                return
            # generic error (invalid input / 503 delivery trouble / other)
            self._status_label.setText(result.get("message") or "Couldn't send a verification code.")
            return

        # stage == "code"
        if result.get("key"):
            save_trial_key(result["key"], result.get("expires_at", ""))
            logger.info(
                "Trial key provisioned (%s), expires %s",
                result.get("status"),
                result.get("expires_at"),
            )
            self.trial_started.emit(result["key"], result.get("expires_at", ""))
            self.accept()
            return
        if status in ("invalid_code", "email_verification_required"):
            # Inline error; let them retry (or request a fresh code via 'Use a different email…').
            self._status_label.setText(result.get("message") or "That code didn't work — try again.")
            return
        if status == "trial_expired":
            self._status_label.setText(
                result.get("message") or f"Your free trial has ended — subscribe at {SUBSCRIBE_URL}."
            )
            return
        if status == "rate_limited":
            self._status_label.setText(result.get("message") or "Too many attempts — try again soon.")
            return
        if status == "offline":
            self._status_label.setText(
                result.get("message") or "Can't reach mtgacoach.com — check your connection."
            )
            return
        self._status_label.setText(result.get("message") or "Couldn't start the trial — try again.")

    # ------------------------------------------------------------------
    def _use_license_key(self) -> None:
        """Escape hatch: route to the existing subscriber-key entry flow."""
        self.license_key_requested.emit()
        self.reject()
