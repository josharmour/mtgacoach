"""User-reviewed dialog for sending a bug report to MTGA Coach Discord."""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from arenamcp.bug_report_upload import (
    fetch_bug_report_profile,
    load_local_report,
    submit_bug_report,
)
from arenamcp.settings import get_settings


class _BugSubmitWorker(QObject):
    finished = Signal(dict)

    def __init__(self, fn: Callable[[], dict[str, Any]]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        self.finished.emit(self._fn())


_ACTIVE_WORKERS: dict[QThread, QObject] = {}


def _launch_background_worker(
    fn: Callable[[], dict[str, Any]], callback: Callable[[dict[str, Any]], None]
) -> QThread:
    """Run *fn* on a parentless QThread kept alive until completion even if dialog closes."""
    thread = QThread()
    worker = _BugSubmitWorker(fn)
    worker.moveToThread(thread)
    _ACTIVE_WORKERS[thread] = worker

    def _cleanup() -> None:
        _ACTIVE_WORKERS.pop(thread, None)
        worker.deleteLater()
        thread.deleteLater()

    thread.started.connect(worker.run)
    worker.finished.connect(callback)
    worker.finished.connect(thread.quit)
    thread.finished.connect(_cleanup)
    thread.start()
    return thread


class DiscordBugReportDialog(QDialog):
    """Compose, check for duplicates, and send an Option A bug report to Discord."""

    def __init__(
        self,
        report_path: str | Path | None = None,
        parent: QWidget | None = None,
        *,
        submit_fn: Callable[..., dict[str, Any]] | None = None,
        prefill_fn: Callable[..., dict[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Send Bug Report to Discord")
        self.resize(560, 520)

        self._settings = get_settings()
        self._submit_fn = submit_fn or submit_bug_report
        self._prefill_fn = prefill_fn or fetch_bug_report_profile
        self._report_path = str(report_path or "")
        self._report_data, self._screenshot_paths = load_local_report(report_path)
        self._closed = False
        self._invite_url = ""
        self._result_thread_url = ""
        self._dup_matches: list[dict[str, Any]] = []

        self._build_ui()
        self._start_prefill()

    def reject(self) -> None:
        self._closed = True
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closed = True
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        intro = QLabel(
            "Posts a public summary in #bug-reports under your verified Discord username so we can "
            "follow up with you. Full diagnostic logs are stored privately on our server (never posted publicly)."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(8)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Short summary, e.g. Autopilot skipped combat step")
        self.title_edit.setMaxLength(90)
        form.addRow("Title:", self.title_edit)

        self.category_combo = QComboBox()
        self.category_combo.addItem("Desktop", "desktop")
        self.category_combo.addItem("Autopilot", "autopilot")
        self.category_combo.addItem("Coach", "coach")
        form.addRow("Category:", self.category_combo)

        self.discord_edit = QLineEdit()
        self.discord_edit.setText(str(self._settings.get("discord_handle", "") or "").strip())
        self.discord_edit.setPlaceholderText("Your Discord username in the MTGA Coach server")
        self.discord_edit.setMaxLength(40)
        form.addRow("Discord handle:", self.discord_edit)

        root.addLayout(form)

        note_label = QLabel("What happened? (steps, expected vs actual):")
        root.addWidget(note_label)
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText(
            "Describe what happened and how to reproduce it (at least 10 characters)..."
        )
        self.note_edit.setMinimumHeight(110)
        root.addWidget(self.note_edit)

        self.screenshots_check = QCheckBox(
            f"Attach {len(self._screenshot_paths)} screenshot(s) to the public #bug-reports post"
        )
        if not self._screenshot_paths:
            self.screenshots_check.setText("No screenshots captured (diagnostics still stored privately)")
            self.screenshots_check.setEnabled(False)
            self.screenshots_check.setChecked(False)
        else:
            self.screenshots_check.setChecked(False)
        root.addWidget(self.screenshots_check)

        # Inline server-membership / error callout
        self.status_frame = QFrame()
        self.status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QVBoxLayout(self.status_frame)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.status_label)
        self.invite_btn = QPushButton("Join MTGA Coach Discord")
        self.invite_btn.clicked.connect(self._open_invite)
        self.invite_btn.hide()
        status_layout.addWidget(self.invite_btn)
        self.status_frame.hide()
        root.addWidget(self.status_frame)

        # Inline duplicate check panel
        self.dup_frame = QFrame()
        self.dup_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.dup_layout = QVBoxLayout(self.dup_frame)
        dup_header = QLabel(
            "Similar open bug reports already exist in #bug-reports. Add your report to one of them, or create a new post:"
        )
        dup_header.setWordWrap(True)
        self.dup_layout.addWidget(dup_header)
        self.dup_list_layout = QVBoxLayout()
        self.dup_layout.addLayout(self.dup_list_layout)
        self.dup_group = QButtonGroup(self)
        dup_btn_row = QHBoxLayout()
        self.attach_btn = QPushButton("Add my report to selected thread")
        self.attach_btn.clicked.connect(self._on_attach_selected)
        dup_btn_row.addWidget(self.attach_btn)
        self.force_new_btn = QPushButton("Create new post anyway")
        self.force_new_btn.clicked.connect(self._on_force_new)
        dup_btn_row.addWidget(self.force_new_btn)
        self.dup_layout.addLayout(dup_btn_row)
        self.dup_frame.hide()
        root.addWidget(self.dup_frame)

        # Bottom action buttons
        btn_row = QHBoxLayout()
        self.send_btn = QPushButton("Check & Send to Discord")
        self.send_btn.clicked.connect(lambda: self._start_submit())
        btn_row.addWidget(self.send_btn)

        self.open_thread_btn = QPushButton("Open in Discord")
        self.open_thread_btn.clicked.connect(self._open_result_thread)
        self.open_thread_btn.hide()
        btn_row.addWidget(self.open_thread_btn)

        self.copy_link_btn = QPushButton("Copy Link")
        self.copy_link_btn.clicked.connect(self._copy_result_thread)
        self.copy_link_btn.hide()
        btn_row.addWidget(self.copy_link_btn)

        btn_row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

    def _license_key(self) -> str:
        return str(self._settings.get("license_key", "") or "").strip()

    def _start_prefill(self) -> None:
        key = self._license_key()
        if not key or self.discord_edit.text().strip():
            return
        _launch_background_worker(lambda: self._prefill_fn(license_key=key), self._on_prefill_done)

    def _on_prefill_done(self, info: dict[str, Any]) -> None:
        if self._closed:
            return
        handle = str(info.get("discord_handle") or "").strip()
        invite = str(info.get("invite_url") or "").strip()
        if invite:
            self._invite_url = invite
        if handle and not self.discord_edit.text().strip():
            self.discord_edit.setText(handle)
            self._settings.set("discord_handle", handle)
            self._settings.save()

    def _set_busy(self, busy: bool) -> None:
        self.send_btn.setEnabled(not busy)
        self.attach_btn.setEnabled(not busy)
        self.force_new_btn.setEnabled(not busy)
        self.title_edit.setEnabled(not busy)
        self.category_combo.setEnabled(not busy)
        self.discord_edit.setEnabled(not busy)
        self.note_edit.setEnabled(not busy)

    def _show_status(self, text: str, *, invite_url: str = "") -> None:
        self.status_label.setText(text)
        self._invite_url = invite_url or self._invite_url
        self.invite_btn.setVisible(bool(self._invite_url))
        self.status_frame.show()

    def _start_submit(self, *, existing_thread_id: str = "", force_new: bool = False) -> None:
        title = " ".join(self.title_edit.text().split())
        note = self.note_edit.toPlainText().strip()
        handle = self.discord_edit.text().strip()
        category = str(self.category_combo.currentData() or "desktop")

        if len(title) < 4:
            self._show_status("Please enter a short title (at least 4 characters).")
            self.title_edit.setFocus()
            return
        if not handle:
            self._show_status(
                "Please enter your Discord username (you must be in the MTGA Coach Discord server)."
            )
            self.discord_edit.setFocus()
            return
        if len(note) < 10:
            self._show_status("Please describe what happened (at least 10 characters).")
            self.note_edit.setFocus()
            return

        self.status_frame.hide()
        self.dup_frame.hide()
        self._set_busy(True)
        self.send_btn.setText("Submitting…")

        key = self._license_key()
        include_shots = bool(self.screenshots_check.isEnabled() and self.screenshots_check.isChecked())

        _launch_background_worker(
            lambda: self._submit_fn(
                license_key=key,
                title=title,
                note=note,
                category=category,
                discord_handle=handle,
                report_data=self._report_data,
                screenshot_paths=self._screenshot_paths,
                include_screenshots=include_shots,
                existing_thread_id=existing_thread_id,
                force_new=force_new,
            ),
            self._on_submit_finished,
        )

    def _clear_dup_list(self) -> None:
        for btn in list(self.dup_group.buttons()):
            self.dup_group.removeButton(btn)
        while self.dup_list_layout.count():
            item = self.dup_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _populate_duplicates(self, matches: list[dict[str, Any]]) -> None:
        self._clear_dup_list()
        self._dup_matches = matches
        for idx, m in enumerate(matches):
            tid = str(m.get("thread_id") or "")
            title = str(m.get("title") or tid)
            reason = str(m.get("reason") or "")
            count = int(m.get("report_count") or 1)
            url = str(m.get("url") or "")
            rb = QRadioButton(f"{title} ({count} report{'s' if count != 1 else ''}) — {reason}")
            rb.setProperty("thread_id", tid)
            if idx == 0:
                rb.setChecked(True)
            self.dup_group.addButton(rb)
            self.dup_list_layout.addWidget(rb)
            if url.startswith(("https://", "http://")):
                safe_url = html.escape(url, quote=True)
                link_lbl = QLabel(f'<a href="{safe_url}">{safe_url}</a>')
                link_lbl.setOpenExternalLinks(True)
                self.dup_list_layout.addWidget(link_lbl)
        self.dup_frame.show()

    def _on_attach_selected(self) -> None:
        checked = self.dup_group.checkedButton()
        if checked is None:
            return
        tid = str(checked.property("thread_id") or "")
        if tid:
            self._start_submit(existing_thread_id=tid, force_new=False)

    def _on_force_new(self) -> None:
        self._start_submit(existing_thread_id="", force_new=True)

    def _on_submit_finished(self, result: dict[str, Any]) -> None:
        if self._closed:
            return
        self._set_busy(False)
        self.send_btn.setText("Check & Send to Discord")

        if not result.get("ok"):
            self._show_status(
                str(result.get("message") or "Could not send bug report."),
                invite_url=str(result.get("invite_url") or ""),
            )
            return

        username = str(result.get("discord_username") or self.discord_edit.text().strip())
        if username:
            self.discord_edit.setText(username)
            self._settings.set("discord_handle", username)
            self._settings.save()

        status = str(result.get("status") or "")
        if status == "possible_duplicates":
            matches = list(result.get("matches") or [])
            if matches:
                self._populate_duplicates(matches)
                return

        self._result_thread_url = str(result.get("thread_url") or "")
        bug_id = str(result.get("bug_id") or "")
        action_text = (
            "Added your report to the existing thread" if status == "attached" else "Posted new bug report"
        )
        self._show_status(f"{action_text} ({bug_id}). Thread: {self._result_thread_url}")
        self.send_btn.hide()
        self.dup_frame.hide()
        self.open_thread_btn.setVisible(bool(self._result_thread_url))
        self.copy_link_btn.setVisible(bool(self._result_thread_url))
        self.close_btn.setText("Done")

    def _open_invite(self) -> None:
        if self._invite_url:
            QDesktopServices.openUrl(QUrl(self._invite_url))

    def _open_result_thread(self) -> None:
        if self._result_thread_url:
            QDesktopServices.openUrl(QUrl(self._result_thread_url))

    def _copy_result_thread(self) -> None:
        if not self._result_thread_url:
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._result_thread_url)
            self._show_status(f"Discord thread link copied: {self._result_thread_url}")

    @property
    def result_thread_url(self) -> str:
        return self._result_thread_url
