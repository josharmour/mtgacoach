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
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from arenamcp.desktop.runtime import RuntimeState, detect_runtime_state
from arenamcp.repair_engine import RepairEngine, RepairReport, set_license_key

logger = logging.getLogger(__name__)

_STATUS_GLYPH = {
    "ok": ("✓", "#3fb950"),
    "fixed": ("✦", "#58a6ff"),
    "action_needed": ("⚠", "#d29922"),
    "error": ("✗", "#f85149"),
}


class RepairTab(QWidget):
    """One-button check-and-repair surface."""

    restart_requested = Signal(bool, bool, bool)
    provisioning_changed = Signal(bool)
    guided_setup_finished = Signal(bool, str)

    _report_ready = Signal(object)  # RepairReport, marshaled to the UI thread
    _progress = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._running = False
        self._guided = False
        self._auto_ran = False
        self._build_ui()
        self._report_ready.connect(self._render_report)
        self._progress.connect(self._on_progress)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignTop)

        title = QLabel("Repair")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(title)

        self._summary = QLabel(
            "Checks your whole setup — runtime, license, MTGA, and the "
            "bridge — and fixes what it safely can."
        )
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        buttons = QHBoxLayout()
        self._run_btn = QPushButton("Check & Repair")
        self._run_btn.setStyleSheet("font-weight: bold; padding: 8px 18px;")
        self._run_btn.clicked.connect(self.run_checks)
        buttons.addWidget(self._run_btn)

        self._restart_btn = QPushButton("Restart Coach")
        self._restart_btn.clicked.connect(
            lambda: self.restart_requested.emit(False, False, False)
        )
        self._restart_btn.hide()
        buttons.addWidget(self._restart_btn)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self._rows = QVBoxLayout()
        self._rows.setSpacing(4)
        root.addLayout(self._rows)

        # License entry — hidden until the license check demands it.
        self._license_box = QWidget()
        lb = QHBoxLayout(self._license_box)
        lb.setContentsMargins(0, 6, 0, 0)
        lb.addWidget(QLabel("License key:"))
        self._license_edit = QLineEdit()
        self._license_edit.setEchoMode(QLineEdit.Password)
        self._license_edit.setPlaceholderText("sk-…")
        lb.addWidget(self._license_edit, 1)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_license)
        lb.addWidget(apply_btn)
        self._license_box.hide()
        root.addWidget(self._license_box)

        self._details_btn = QPushButton("Show details")
        self._details_btn.setFlat(True)
        self._details_btn.clicked.connect(self._toggle_details)
        root.addWidget(self._details_btn, alignment=Qt.AlignLeft)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
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
                report.results.append(
                    CheckResult("engine", "Repair engine", "error", str(e))
                )
            self._report_ready.emit(report)

        threading.Thread(target=_work, daemon=True, name="repair-run").start()

    # ------------------------------------------------------------------
    def _on_progress(self, name: str) -> None:
        self._summary.setText(f"Checking {name.replace('_', ' ')}…")

    def _render_report(self, report: RepairReport) -> None:
        self._running = False
        self._run_btn.setEnabled(True)
        self._run_btn.setText("Check & Repair")
        self._clear_rows()

        needs_license = False
        fixed_anything = False
        for r in report.results:
            glyph, color = _STATUS_GLYPH.get(r.status, ("•", "#8b949e"))
            row = QLabel(
                f"<span style='color:{color}; font-weight:bold;'>{glyph}</span> "
                f"<b>{r.label}</b> — {r.detail}"
                + (
                    f"<br/><span style='color:#8b949e;'>&nbsp;&nbsp;→ {r.action_hint}</span>"
                    if r.action_hint
                    else ""
                )
            )
            row.setWordWrap(True)
            row.setTextFormat(Qt.RichText)
            self._rows.addWidget(row)
            self._details.appendPlainText(
                f"[{r.status}] {r.label}: {r.detail} {r.action_hint}".strip()
            )
            if r.key == "license" and r.status == "action_needed":
                needs_license = True
            if r.status == "fixed":
                fixed_anything = True

        self._summary.setText(report.summary())
        self._license_box.setVisible(needs_license)
        self._restart_btn.setVisible(fixed_anything)

        ready = report.healthy
        self.provisioning_changed.emit(ready)
        if self._guided:
            self._guided = False
            self.guided_setup_finished.emit(ready, report.summary())

    def _apply_license(self) -> None:
        key = self._license_edit.text().strip()
        if not key:
            return
        result = set_license_key(key)
        self._license_edit.clear()
        self._summary.setText(f"{result.label}: {result.detail}")
        # Re-run so the checklist and provisioning signal reflect reality.
        self.run_checks()

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
