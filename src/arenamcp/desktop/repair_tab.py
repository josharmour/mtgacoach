"""Simplified self-repair tab: one button, a checklist, and nothing else.

2026-07-06 redesign (see the repair audit): the old tab exposed a wall of
per-component buttons around file-existence checks that false-passed most
real broken installs. This tab exposes exactly what a user needs:

  • Check & Repair — runs every diagnostic in dependency order via
    arenamcp.repair_engine, fixing silently where safe, and shows a plain
    checklist (ok / fixed / needs-you + one sentence of what to do).
    Auto-runs the first time the tab is shown.
  • A license-key box that appears only when the license check demands it
    (the audit found NO UI anywhere could set the key).
  • Restart Coach — shown after repairs so the fixes load.
  • A collapsed details log for bug reports.

All platform specifics live in arenamcp.platform_integration; all check
logic lives in arenamcp.repair_engine (GUI-free, unit-tested).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arenamcp.desktop.trial_dialog import TrialSignupDialog

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from arenamcp.desktop import theme
from arenamcp.desktop.runtime import RuntimeState, detect_runtime_state
from arenamcp.repair_engine import RepairEngine, RepairReport, set_license_key
from arenamcp.subscription import open_subscribe_page

logger = logging.getLogger(__name__)

# status → (glyph, theme tone)
_STATUS_GLYPH = {
    "ok": ("✓", "good"),
    "fixed": ("✦", "accent"),
    "action_needed": ("⚠", "warn"),
    "error": ("✗", "bad"),
}


class RepairTab(QWidget):
    """One-button check-and-repair surface."""

    restart_requested = Signal(bool, bool, bool)
    provisioning_changed = Signal(bool)
    guided_setup_finished = Signal(bool, str)
    license_changed = Signal()  # a key was applied or (re)provisioned; the access badge re-checks

    _report_ready = Signal(object)  # RepairReport, marshaled to the UI thread
    _progress = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False
        self._guided = False
        self._auto_ran = False
        self._last_report: RepairReport | None = None
        self._trial_dialog: TrialSignupDialog | None = None
        self._build_ui()
        self._report_ready.connect(self._render_report)
        self._progress.connect(self._on_progress)
        theme.on_theme_changed(self._restyle_rows)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignTop)
        root.setContentsMargins(2, 0, 2, 8)
        root.setSpacing(8)

        self._summary = QLabel(
            "Checks your whole setup — runtime, license, MTGA, and the bridge — and fixes what it safely can."
        )
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        buttons = QHBoxLayout()
        self._run_btn = QPushButton("Check && Repair")
        self._run_btn.setProperty("variant", "primary")
        self._run_btn.clicked.connect(self.run_checks)
        buttons.addWidget(self._run_btn)

        self._restart_btn = QPushButton("Restart Coach")
        self._restart_btn.clicked.connect(lambda: self.restart_requested.emit(False, False, False))
        self._restart_btn.hide()
        buttons.addWidget(self._restart_btn)
        from arenamcp.support import contact_josh

        support = QPushButton("Contact Josh…")
        support.clicked.connect(lambda: contact_josh(self))
        buttons.addWidget(support)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self._rows = QVBoxLayout()
        self._rows.setSpacing(6)
        root.addLayout(self._rows)

        # License entry — shown when the license check demands it, or on request (a trial user
        # who just subscribed needs somewhere to paste the new key).
        self._license_box = QWidget()
        lv = QVBoxLayout(self._license_box)
        lv.setContentsMargins(0, 6, 0, 0)
        lv.setSpacing(6)
        help_text = QLabel(
            "Subscribe on Patreon to keep coaching after the 7-day free trial. Your subscriber key is "
            "emailed to you; patrons can also sign in with Patreon on the subscribe page to see it. "
            "Paste it below."
        )
        help_text.setWordWrap(True)
        help_text.setProperty("role", "muted")
        lv.addWidget(help_text)
        links = QHBoxLayout()
        subscribe_btn = QPushButton("Subscribe on Patreon…")
        subscribe_btn.setProperty("variant", "primary")
        subscribe_btn.clicked.connect(open_subscribe_page)
        links.addWidget(subscribe_btn)
        get_key_btn = QPushButton("Already a patron? Get my key…")
        get_key_btn.clicked.connect(open_subscribe_page)
        links.addWidget(get_key_btn)
        links.addStretch(1)
        lv.addLayout(links)
        lb = QHBoxLayout()
        lb.addWidget(QLabel("Subscriber key:"))
        self._license_edit = QLineEdit()
        self._license_edit.setEchoMode(QLineEdit.Password)
        self._license_edit.setPlaceholderText("sk-…")
        self._license_edit.returnPressed.connect(self._apply_license)
        lb.addWidget(self._license_edit, 1)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_license)
        lb.addWidget(apply_btn)
        lv.addLayout(lb)
        self._license_box.hide()
        root.addWidget(self._license_box)

        self._enter_key_btn = QPushButton("Enter a subscriber key…")
        self._enter_key_btn.setProperty("variant", "flat")
        self._enter_key_btn.clicked.connect(self._show_license_box)
        root.addWidget(self._enter_key_btn, alignment=Qt.AlignLeft)

        self._details_btn = QPushButton("Show details")
        self._details_btn.setProperty("variant", "flat")
        self._details_btn.clicked.connect(self._toggle_details)
        root.addWidget(self._details_btn, alignment=Qt.AlignLeft)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        self._details.setFont(theme.mono_font())
        self._details.setMaximumHeight(180)
        self._details.hide()
        root.addWidget(self._details)

        root.addStretch(1)

    # ------------------------------------------------------------------
    # Public API kept for MainWindow compatibility
    # ------------------------------------------------------------------
    def refresh_state(self) -> RuntimeState:
        return detect_runtime_state()

    def start_guided_setup(self) -> None:
        """First-run flow == the same one-pass repair, with a completion signal."""
        self._guided = True
        self.run_checks()

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if not self._auto_ran:
            self._auto_ran = True
            self.run_checks()

    def run_checks(self) -> None:
        if self._running:
            return
        self._running = True
        self._run_btn.setEnabled(False)
        self._run_btn.setText("Checking…")
        self._clear_rows()
        self._details.clear()

        def _work() -> None:
            try:
                report = RepairEngine().run(progress=self._progress.emit)
            except Exception as e:  # never strand the button
                logger.exception("repair run crashed")
                from arenamcp.repair_engine import CheckResult

                report = RepairReport()
                report.results.append(CheckResult("engine", "Repair engine", "error", str(e)))
            self._report_ready.emit(report)

        threading.Thread(target=_work, daemon=True, name="repair-run").start()

    # ------------------------------------------------------------------
    def _on_progress(self, name: str) -> None:
        self._summary.setText(f"Checking {name.replace('_', ' ')}…")

    def _render_report(self, report: RepairReport) -> None:
        self._running = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("Check && Repair")
        self._last_report = report
        self._render_rows(report)

        needs_license = False
        fixed_anything = False
        for r in report.results:
            self._details.appendPlainText(f"[{r.status}] {r.label}: {r.detail} {r.action_hint}".strip())
            if r.key == "license" and r.status == "action_needed":
                needs_license = True
            if r.status == "fixed":
                fixed_anything = True

        self._summary.setText(report.summary())
        self._license_box.setVisible(needs_license)
        self._enter_key_btn.setVisible(not needs_license)
        self._restart_btn.setVisible(fixed_anything)
        self.license_changed.emit()

        ready = report.healthy
        self.provisioning_changed.emit(ready)
        if self._guided:
            self._guided = False
            self.guided_setup_finished.emit(ready, report.summary())

        # Two-step trial flow: silent auto-provisioning is gone. When the
        # license check demands attention and no key is configured at all,
        # offer the email-verification trial signup instead of leaving the
        # user stranded at a key prompt. Deferred so the modal opens after
        # this slot (and any guided-setup signals) finish.
        if needs_license and not self._has_license_key():
            QTimer.singleShot(0, self._offer_trial_signup)

    # ------------------------------------------------------------------
    # Free-trial signup (two-step email verification)
    # ------------------------------------------------------------------
    @staticmethod
    def _has_license_key() -> bool:
        from arenamcp.settings import get_settings

        return bool((get_settings().get("license_key") or "").strip())

    def _offer_trial_signup(self) -> None:
        """Open the 'Start your free trial' wizard (email -> code -> key)."""
        if self._trial_dialog is not None and self._trial_dialog.isVisible():
            return
        from arenamcp.desktop.trial_dialog import TrialSignupDialog

        dlg = TrialSignupDialog(self)
        self._trial_dialog = dlg
        dlg.trial_started.connect(self._on_trial_started)
        dlg.license_key_requested.connect(self._show_license_box)
        dlg.exec()
        self._trial_dialog = None

    def _on_trial_started(self, key: str, expires_at: str) -> None:
        # Key + expiry were persisted by the dialog via save_trial_key();
        # refresh the badge and re-validate against the gateway.
        logger.info("Trial started (expires %s)", expires_at or "?")
        self.license_changed.emit()
        self.run_checks()

    def _apply_license(self) -> None:
        key = self._license_edit.text().strip()
        if not key:
            return
        result = set_license_key(key)
        self._license_edit.clear()
        self._summary.setText(f"{result.label}: {result.detail}")
        self.license_changed.emit()
        # Re-run so the checklist and provisioning signal reflect reality.
        self.run_checks()

    def _show_license_box(self) -> None:
        self._license_box.show()
        self._enter_key_btn.hide()
        self._license_edit.setFocus()

    def _render_rows(self, report: RepairReport) -> None:
        self._clear_rows()
        for r in report.results:
            glyph, tone = _STATUS_GLYPH.get(r.status, ("•", "muted"))
            text = (
                theme.span(glyph, tone, weight=700)
                + "&nbsp;"
                + theme.span(r.label, weight=700)
                + " — "
                + theme.span(r.detail)
            )
            if r.action_hint:
                text += "<br>" + theme.span(f"→ {r.action_hint}", "muted")
            row = QLabel(text)
            row.setWordWrap(True)
            row.setTextFormat(Qt.RichText)
            self._rows.addWidget(row)

    def _restyle_rows(self) -> None:
        if self._last_report is not None and not self._running:
            self._render_rows(self._last_report)

    # ------------------------------------------------------------------
    def _toggle_details(self) -> None:
        show = not self._details.isVisible()
        self._details.setVisible(show)
        self._details_btn.setText("Hide details" if show else "Show details")

    def _clear_rows(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
