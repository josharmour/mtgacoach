from __future__ import annotations

import json
import logging
import threading
import ssl
from typing import Optional
from urllib.request import urlopen, Request as UrlRequest
from urllib.error import URLError, HTTPError

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from arenamcp.settings import get_settings

from .coach_process import CoachProcess
from .coach_tab import CoachTab
from .compact_coach import CompactCoachPanel
from .repair_tab import RepairTab
from .runtime import RuntimeState, open_url, read_version
from .theme import THEME_LABELS, apply_theme, available_themes, load_saved_theme, save_theme

logger = logging.getLogger(__name__)

UI_MODE_CLASSIC = "classic"
UI_MODE_COMPACT = "compact"
_UI_MODE_KEY = "desktop_ui_mode"



# -- custom event for cross-thread probe result -------------------------

class _ProbeResultEvent(QEvent):
    """Delivered from the probe worker thread to the UI thread."""
    _EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

    def __init__(self, models: list[str]) -> None:
        super().__init__(self._EVENT_TYPE)
        self.models = models

    @staticmethod
    def is_probe_result(event: QEvent) -> bool:
        return isinstance(event, _ProbeResultEvent)


class ModelEndpointDialog(QDialog):
    """Dialog to configure the LLM endpoint URL, API key, and model name.

    Features:
    - URL autocomplete from known endpoints history
    - Probe button to discover available models from the endpoint
    - Saves/restores endpoint configs including discovered model lists
    """

    _KNOWN_KEY = "known_endpoints"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Model Endpoint")
        self.setMinimumWidth(520)
        self._populating = True
        self._settings = get_settings()
        self._build_ui()
        self._populate()

    # -- known endpoints persistence ---------------------------------------

    def _get_known(self) -> dict[str, list[str]]:
        """Return {url: [model_id, ...]} dict from settings."""
        data = self._settings.get(self._KNOWN_KEY, {})
        return data if isinstance(data, dict) else {}

    def _set_known(self, known: dict[str, dict[str, str | list[str]]]) -> None:
        self._settings._data[self._KNOWN_KEY] = known
        self._settings.save()

    # -- probe helper (runs in a background thread) -------------------------

    def _probe_endpoint(self) -> list[str]:
        """Hit GET /v1/models and return model IDs."""
        raw = self._url_edit.text().strip().rstrip("/")
        if not raw:
            return []
        base_url = raw.rstrip("/")
        models_url = f"{base_url}/models"

        key = self._key_edit.text().strip() or "vllm"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

        def _do_probe(url: str) -> list[str]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = UrlRequest(url, headers=headers, method="GET")
            with urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode())
            models = data.get("data") or data if isinstance(data, list) else data.get("data", [])
            ids = [m["id"] for m in models if isinstance(m, dict) and m.get("id")]
            if not ids and "models" in data:
                ids = [m["name"] if "name" in m else m.get("model", "")
                       for m in data["models"] if isinstance(m, dict)]
            return sorted(set(ids))

        try:
            return _do_probe(models_url)
        except (URLError, HTTPError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Endpoint probe failed for %s: %s", models_url, exc)
            http_url = models_url.replace("https://", "http://")
            if http_url != models_url:
                logger.warning("Falling back to HTTP probe: %s", http_url)
                try:
                    return _do_probe(http_url)
                except (URLError, HTTPError, OSError, json.JSONDecodeError, ValueError) as exc2:
                    logger.warning("HTTP fallback also failed for %s: %s", http_url, exc2)
            return []

    # -- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(8)

        # API Base URL row with inline probe button
        url_row = QHBoxLayout()
        self._url_combo = QComboBox()
        self._url_combo.setEditable(True)
        self._url_combo.setInsertPolicy(QComboBox.NoInsert)
        self._url_combo.lineEdit().setPlaceholderText("http://localhost:8001/v1")
        self._url_combo.setToolTip(
            "OpenAI-compatible API base URL —\n"
            "previous endpoints are auto-completed.\n"
            "Known endpoints: " + ", ".join(
                self._get_known().keys() or ["(none yet)"]
            )
        )
        self._url_edit = self._url_combo.lineEdit()
        self._url_edit.textChanged.connect(self._on_url_changed)
        self._url_combo.currentIndexChanged.connect(self._on_url_selected)
        url_row.addWidget(self._url_combo, stretch=1)

        self._probe_btn = QPushButton("Probe")
        self._probe_btn.setToolTip(
            "Query the endpoint for available model IDs (GET /v1/models)"
        )
        self._probe_btn.clicked.connect(self._on_probe)
        url_row.addWidget(self._probe_btn)

        form.addRow("API Base URL:", url_row)

        # API key
        self._key_edit = QLineEdit()
        self._key_edit.setPlaceholderText("vllm")
        self._key_edit.setToolTip(
            "API key for the endpoint (vLLM accepts any value; "
            "Ollama expects 'ollama'")
        form.addRow("API Key:", self._key_edit)

        # Model ID — editable combo so the user can probe, then select from
        # the results, or type a model name directly.
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.NoInsert)
        self._model_combo.lineEdit().setPlaceholderText("nemotron-3-super")
        self._model_combo.setToolTip(
            "Model ID to use (e.g. 'nemotron-3-super', 'gpt-5.4', etc.)\n"
            "Click Probe to auto-populate from the endpoint."
        )
        form.addRow("Model ID:", self._model_combo)

        self._populating = False
        self._use_online = QPushButton("Reset to Online (mtgacoach.com)")
        self._use_online.setToolTip("Restore the default online endpoint")
        layout.addLayout(form)

        layout.addSpacing(8)

        # Status / probe result
        status_row = QHBoxLayout()
        self._status_label = QLabel()
        self._status_label.setStyleSheet("color: #8a8a8a;")
        self._status_label.setWordWrap(True)
        status_row.addWidget(self._status_label, stretch=1)
        layout.addLayout(status_row)

        layout.addWidget(self._use_online)
        self._use_online.clicked.connect(self._reset_to_online)

        # Forget endpoint button — small, secondary hover
        bottom_row = QHBoxLayout()
        self._forget_btn = QPushButton("Forget Endpoint")
        self._forget_btn.setToolTip("Remove this endpoint from known list")
        self._forget_btn.setStyleSheet(
            "QPushButton { color: #888; border: none; text-decoration: underline; padding: 4px 8px; }"
            "QPushButton:hover { color: #f44336; }"
        )
        self._forget_btn.clicked.connect(self._on_forget)
        bottom_row.addWidget(self._forget_btn)
        bottom_row.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        bottom_row.addWidget(buttons)
        layout.addLayout(bottom_row)

    def _populate(self) -> None:
        s = self._settings
        mode = s.get("mode", "online")
        self._model_combo.clear()
        self._model_combo.addItem("")  # blank default

        # Populate URL combo from known endpoints
        known = self._get_known()
        self._url_combo.clear()
        for url in known:
            self._url_combo.addItem(url)

        if mode == "local":
            url = s.get("local_url", "http://localhost:8001/v1")
            key = s.get("local_api_key", "vllm")
            model = s.get("local_model") or ""
        else:
            url = "https://api.mtgacoach.com/v1"
            key = ""
            model = s.get("model") or ""

        self._url_combo.setCurrentText(url)
        self._key_edit.setText(key)
        if model:
            self._model_combo.setCurrentText(model)

        # If this is a known endpoint, restore models too
        self._restore_models_for_url(url)
        self._status_label.setText(
            "Local endpoint" if mode == "local" else "Online (mtgacoach.com)"
        )

    def _restore_models_for_url(self, url: str) -> None:
        """If *url* is in the known list, restore its models + separator label."""
        known = self._get_known()
        rec = known.get(url.rstrip("/"))
        if not isinstance(rec, dict):
            return
        models = rec.get("models", [])
        if isinstance(models, list) and models:
            self._model_combo.clear()
            self._model_combo.addItem("")  # blank default
            self._model_combo.addItems(models)
            self._model_combo.setCurrentText("")
            self._status_label.setText(
                f"\u2714 {len(models)} known models \u2014 probe again to refresh"
            )
            self._status_label.setStyleSheet("color: #888;")

    # -- probe logic ----------------------------------------------------------

    def _on_probe(self) -> None:
        """Fire the HTTP probe in a background thread so the UI stays live."""
        self._probe_btn.setEnabled(False)
        self._probe_btn.setText("Probing\u2026")
        self._status_label.setText("Probing endpoint\u2026")
        self._status_label.setStyleSheet("color: #f0c000;")

        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self) -> None:
        """Background thread helper. Returns results via postEvent."""
        models = self._probe_endpoint()
        QApplication.instance().postEvent(
            self, _ProbeResultEvent(models)
        )

    def _on_probe_result(self, models: list[str]) -> None:
        if not models:
            self._status_label.setText("No models found for this endpoint.")
            self._status_label.setStyleSheet("color: #f44336;")
            return

        current = self._model_combo.currentText().strip()
        self._model_combo.clear()
        self._model_combo.addItem("")  # blank default
        self._model_combo.addItems(models)
        if current and current in models:
            self._model_combo.setCurrentText(current)

        self._status_label.setText(
            f"Probed \u2014 {len(models)} model{'s' if len(models) != 1 else ''} found"
        )
        self._status_label.setStyleSheet("color: #4caf50;")

        # Save/update this endpoint in history
        url = self._url_edit.text().strip().rstrip("/")
        key = self._key_edit.text().strip()
        known = self._get_known()
        rec = known.get(url)
        if not isinstance(rec, dict):
            rec = {}
        rec["models"] = models
        if key:
            rec["key"] = key
        known[url] = rec
        self._set_known(known)

        # Ensure URL is in the combo list
        idx = self._url_combo.findText(url)
        if idx < 0:
            self._url_combo.insertItem(0, url)

    # -- helpers --------------------------------------------------------------

    def _on_url_changed(self) -> None:
        if self._populating:
            return
        self._probe_btn.setEnabled(bool(self._url_edit.text().strip()))
        self._status_label.setText("")
        self._status_label.setStyleSheet("color: #8a8a8a;")

    def _on_url_selected(self, index: int) -> None:
        if self._populating:
            return
        """When the user picks a known endpoint from the dropdown, restore its details."""
        if index < 0:
            return
        url = self._url_combo.itemText(index)
        if not url:
            return
        known = self._get_known()
        rec = known.get(url)
        if not isinstance(rec, dict):
            return
        models = rec.get("models", [])
        key = rec.get("key", "")
        if key:
            self._key_edit.setText(key)
        self._restore_models_for_url(url)

    def _reset_to_online(self) -> None:
        self._url_combo.setCurrentText("https://api.mtgacoach.com/v1")
        self._url_edit.setText("https://api.mtgacoach.com/v1")
        self._key_edit.setText("")
        self._model_combo.clear()
        self._model_combo.addItem("")
        self._status_label.setText("Online (mtgacoach.com)")
        self._status_label.setStyleSheet("color: #8a8a8a;")

    def _on_forget(self) -> None:
        """Remove the current URL from known endpoints."""
        url = self._url_edit.text().strip().rstrip("/")
        if not url:
            return
        known = self._get_known()
        if url not in known:
            return
        del known[url]
        self._set_known(known)
        # Remove from combo
        idx = self._url_combo.findText(url)
        if idx >= 0:
            self._url_combo.removeItem(idx)
        self._status_label.setText(f"Forgot: {url}")
        self._status_label.setStyleSheet("color: #f44336;")

    def _on_accept(self) -> None:
        s = self._settings
        url = self._url_edit.text().strip()
        key = self._key_edit.text().strip()
        model = self._model_combo.currentText().strip()

        is_online = "mtgacoach.com" in url or "api.mtgacoach.com" in url
        if is_online:
            s.set("mode", "online")
            s.set("model", model if model else None)
        else:
            s.set("mode", "local")
            s.set("local_url", url)
            s.set("local_api_key", key)
            s.set("local_model", model if model else None)

        self.accept()

    # -- event handling (for custom cross-thread events) ---------------

    def event(self, event: QEvent) -> bool:
        """Handle custom events delivered from worker threads."""
        if _ProbeResultEvent.is_probe_result(event):
            pr = event  # type: ignore[assignment]
            self._probe_btn.setEnabled(True)
            self._probe_btn.setText("Probe")
            self._on_probe_result(pr.models)
            return True
        return super().event(event)

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._closing = False
        self._launch_flags = (False, False, False)
        self._process: Optional[CoachProcess] = None
        self._startup_prompt_shown = False
        self._settings = get_settings()
        self._theme_actions: dict[str, QAction] = {}
        self._debug_logging_action: Optional[QAction] = None
        self._compact_action: Optional[QAction] = None
        self._current_theme = load_saved_theme()

        mode = str(self._settings.get(_UI_MODE_KEY, UI_MODE_CLASSIC) or "").strip().lower()
        self._ui_mode = mode if mode in (UI_MODE_CLASSIC, UI_MODE_COMPACT) else UI_MODE_CLASSIC

        self.setWindowTitle(f"mtgacoach v{read_version()}")

        self.repair_tab = RepairTab()
        self.repair_tab.restart_requested.connect(self.restart_coach)
        self.repair_tab.provisioning_changed.connect(self._handle_provisioning_changed)
        self.repair_tab.guided_setup_finished.connect(self._handle_guided_setup_finished)

        self.coach_tab: CoachTab | None = None
        self.tabs: Optional[QTabWidget] = None
        self._stack: Optional[QStackedWidget] = None
        self._repair_scroll: Optional[QScrollArea] = None
        self._repair_back_btn: Optional[QPushButton] = None
        self._build_central_widget()

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._status_bar = status_bar

        refresh_action = QAction("Refresh Status", self)
        refresh_action.triggered.connect(self.refresh_state)
        self.menuBar().addAction(refresh_action)
        self._build_theme_menu()
        self._build_view_menu()
        self._build_model_menu()

        self._apply_window_geometry()
        self.refresh_state()
        self._auto_start()

    def _build_central_widget(self) -> None:
        """Build the central widget for the current UI mode.

        Classic: the original Coach/Repair tab pair.
        Compact: a narrow sidebar panel with Repair on a slide-over page so
        the window can sit beside the MTGA window.
        """
        if self._ui_mode == UI_MODE_COMPACT:
            panel = CompactCoachPanel()
            panel.repair_requested.connect(self._show_repair_view)
            panel.classic_requested.connect(lambda: self._set_ui_mode(UI_MODE_CLASSIC))
            self.coach_tab = panel

            repair_page = QWidget()
            page_layout = QVBoxLayout(repair_page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            page_layout.setSpacing(6)
            header = QHBoxLayout()
            back_btn = QPushButton("\u2190 Back to Coach")
            back_btn.clicked.connect(self._show_coach_view)
            header.addWidget(back_btn)
            header.addStretch()
            page_layout.addLayout(header)
            # The repair tab is laid out for a wide window; scroll it rather
            # than letting its minimum size force the sidebar wide open.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(self.repair_tab)
            page_layout.addWidget(scroll)
            self._repair_scroll = scroll
            self._repair_back_btn = back_btn

            stack = QStackedWidget()
            stack.addWidget(panel)
            stack.addWidget(repair_page)
            self._stack = stack
            self.tabs = None
            self.setCentralWidget(stack)
        else:
            tabs = QTabWidget()
            self.coach_tab = CoachTab()
            tabs.addTab(self.coach_tab, "Coach")
            tabs.addTab(self.repair_tab, "Repair")
            self.tabs = tabs
            self._stack = None
            self._repair_scroll = None
            self._repair_back_btn = None
            self.setCentralWidget(tabs)

        # Restart button on the coach panel — reuses the repair tab's restart
        # logic so state like autopilot/dry-run flags is preserved.
        self.coach_tab.restart_requested.connect(self._restart_coach_keep_flags)

    _WINDOW_GEOMETRY_KEY = "desktop_window_geometry"

    def _apply_window_geometry(self) -> None:
        # Try saved geometry first (captured on last closeEvent).
        saved = self._settings.get(self._WINDOW_GEOMETRY_KEY)
        if isinstance(saved, dict):
            pos_x = saved.get("x")
            pos_y = saved.get("y")
            w = saved.get("width")
            h = saved.get("height")
            saved_mode = saved.get("ui_mode")
            if saved_mode == self._ui_mode and w and h:
                if pos_x is not None and pos_y is not None:
                    self.setGeometry(pos_x, pos_y, w, h)
                else:
                    self.resize(w, h)
                return

        if self._ui_mode == UI_MODE_COMPACT:
            self.setMinimumWidth(380)
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                width = 440
                height = max(700, avail.height() - 60)
                self.resize(width, height)
                self.move(avail.right() - width - 16, avail.top() + 24)
            else:
                self.resize(440, 1000)
        else:
            self.setMinimumWidth(0)
            self.resize(1400, 980)

    # -- view routing (works for both classic tabs and the compact stack) ----

    def _show_coach_view(self) -> None:
        if self.tabs is not None:
            self.tabs.setCurrentIndex(0)
        elif self._stack is not None:
            self._stack.setCurrentIndex(0)

    def _show_repair_view(self) -> None:
        if self.tabs is not None:
            self.tabs.setCurrentIndex(1)
        elif self._stack is not None:
            self._stack.setCurrentIndex(1)

    def _set_coach_view_enabled(self, ready: bool) -> None:
        if self.tabs is not None:
            self.tabs.setTabEnabled(0, ready)
            if not ready and self.tabs.currentIndex() == 0:
                self.tabs.setCurrentIndex(1)
        elif self._stack is not None:
            if self._repair_back_btn is not None:
                self._repair_back_btn.setEnabled(ready)
            if not ready:
                self._stack.setCurrentIndex(1)

    def _set_ui_mode(self, mode: str) -> None:
        if mode not in (UI_MODE_CLASSIC, UI_MODE_COMPACT) or mode == self._ui_mode:
            return

        process = self._process
        old_coach = self.coach_tab
        if old_coach is not None:
            if process is not None:
                old_coach.detach_process()
            try:
                old_coach.restart_requested.disconnect(self._restart_coach_keep_flags)
            except (RuntimeError, TypeError):
                pass
            old_coach.shutdown()

        # Keep the repair tab alive across layouts — it owns provisioning
        # state and its signals were connected once in __init__.
        if self._repair_scroll is not None:
            self._repair_scroll.takeWidget()
        self.repair_tab.setParent(None)

        old_central = self.takeCentralWidget()
        if old_central is not None:
            old_central.deleteLater()

        self._ui_mode = mode
        self._settings.set(_UI_MODE_KEY, mode)
        self._build_central_widget()

        if self._compact_action is not None:
            self._compact_action.blockSignals(True)
            self._compact_action.setChecked(mode == UI_MODE_COMPACT)
            self._compact_action.blockSignals(False)

        if self._debug_logging_action is not None:
            self.coach_tab.set_debug_logging(self._debug_logging_action.isChecked())

        if process is not None:
            self.coach_tab.attach_process(process)
            # Re-emits voice/speed/mute status so the fresh panel's button
            # labels reflect current state instead of defaults.
            self._sync_runtime_preferences(process)

        self._apply_window_geometry()
        self._status_bar.showMessage(
            "Compact sidebar layout" if mode == UI_MODE_COMPACT else "Classic layout", 4000
        )
        self._bump_theme_status()

    def refresh_state(self) -> RuntimeState:
        return self.repair_tab.refresh_state()

    def restart_coach(self, autopilot: bool, dry_run: bool, afk: bool) -> None:
        self._launch_flags = (autopilot, dry_run, afk)
        old_process = self._process
        if old_process is not None:
            self.coach_tab.detach_process()
            try:
                old_process.exited.disconnect(self._on_process_exited)
            except (RuntimeError, TypeError):
                pass
            # Async stop — doesn't block the Qt main thread while the old
            # process exits (old code did waitForFinished(3000) then a
            # waitForFinished(2000) on kill, freezing the UI for up to 5s).
            old_process.stop_async()
            self._process = None
        # Defer start slightly so the old process has a moment to release
        # its stdio handles; this is non-blocking.
        QTimer.singleShot(150, lambda: self._start_coach(*self._launch_flags))

    def _restart_coach_keep_flags(self) -> None:
        """Restart the coach with the most recent launch flags (autopilot, dry_run, afk).

        Triggered by the coach tab's Restart button — avoids sending a pipe
        command to a dying process (which wouldn't actually relaunch).
        """
        flags = getattr(self, "_launch_flags", (False, False, False))
        self.restart_coach(*flags)

    def _bump_theme_status(self) -> None:
        """Re-emit the current theme name on the status bar."""
        try:
            label = THEME_LABELS.get(self._current_theme, self._current_theme)
            self._status_bar.showMessage(f"Theme: {label}", 0)
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._closing = True
        self._current_theme = save_theme(self._current_theme)

        # Save window geometry so it reopens in the same spot.
        geom = self.frameGeometry()
        self._settings.set(self._WINDOW_GEOMETRY_KEY, {
            "x": geom.x(),
            "y": geom.y(),
            "width": geom.width(),
            "height": geom.height(),
            "ui_mode": self._ui_mode,
        })

        self._settings.set(
            "desktop_debug_logging",
            bool(self._debug_logging_action.isChecked()) if self._debug_logging_action else False,
        )
        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.stop()
            self._process.deleteLater()
            self._process = None
        self.coach_tab.shutdown()
        super().closeEvent(event)

    def _auto_start(self) -> None:
        state = self.refresh_state()
        if not state.is_fully_provisioned:
            if state.python_exe is None:
                self._status_bar.showMessage("Python 3.10+ is required. Open Repair to finish setup.")
            else:
                self._status_bar.showMessage("Setup is incomplete. Open Repair to finish setup.")
            self._show_repair_view()
            self._show_startup_prompt(state)
            return
        self._start_coach(False, False, False)

    def _start_coach(self, autopilot: bool, dry_run: bool, afk: bool) -> None:
        if self._process is not None and self._process.is_running:
            self._status_bar.showMessage("Coach is already running.")
            self._show_coach_view()
            return

        state = self.refresh_state()
        if not state.is_fully_provisioned:
            self._status_bar.showMessage("Setup is incomplete. Finish Repair before starting the coach.")
            self._show_repair_view()
            return

        process = CoachProcess(self)
        process.exited.connect(self._on_process_exited)
        self._process = process
        self.coach_tab.attach_process(process)

        try:
            process.start(autopilot=autopilot, dry_run=dry_run, afk=afk)
            self._sync_runtime_preferences(process)
            self._status_bar.showMessage("Coach is running.")
            self._show_coach_view()
        except Exception as exc:
            self.coach_tab.detach_process()
            self._process = None
            self._status_bar.showMessage(f"Coach failed to start: {exc}")
            self._show_repair_view()
            QMessageBox.critical(
                self,
                "Coach Launch Failed",
                f"{exc}\n\n{process.last_error}",
            )

    def _on_process_exited(self, exit_code: int) -> None:
        if self._closing:
            return
        sender = self.sender()
        if sender is not None and sender is not self._process:
            return

        # Audit #18: an unconditional 250ms restart turned a crashing coach
        # into a GUI-freezing loop that hid the cause and yanked the user
        # off the Repair tab. Back off, and stop after 3 rapid crashes.
        import time as _time

        now = _time.monotonic()
        if now - getattr(self, "_last_coach_exit_ts", 0.0) > 60.0:
            self._coach_crash_count = 0
        self._last_coach_exit_ts = now
        self._coach_crash_count = getattr(self, "_coach_crash_count", 0) + 1

        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.deleteLater()
            self._process = None

        if self._coach_crash_count >= 3:
            self._status_bar.showMessage(
                f"Coach crashed {self._coach_crash_count} times (exit "
                f"{exit_code}) — not restarting. Open the Repair tab."
            )
            self._show_repair_view()
            return

        delay_ms = 250 * (4 ** (self._coach_crash_count - 1))  # 250ms, 1s
        self._status_bar.showMessage(
            f"Coach exited ({exit_code}). Restarting in {delay_ms / 1000:.1f}s..."
        )
        QTimer.singleShot(delay_ms, lambda: self._start_coach(*self._launch_flags))

    def _build_theme_menu(self) -> None:
        theme_menu = self.menuBar().addMenu("Theme")
        action_group = QActionGroup(self)
        action_group.setExclusive(True)

        current_theme = self._current_theme
        for theme_name, label in available_themes():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(theme_name == current_theme)
            action.setData(theme_name)
            action_group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[theme_name] = action
        action_group.triggered.connect(self._handle_theme_action)

    def _handle_theme_action(self, action: QAction) -> None:
        theme_name = str(action.data() or "")
        if theme_name:
            self._apply_theme_choice(theme_name)

    def _build_view_menu(self) -> None:
        view_menu = self.menuBar().addMenu("View")
        compact_action = QAction("Compact Sidebar Layout", self)
        compact_action.setCheckable(True)
        compact_action.setChecked(self._ui_mode == UI_MODE_COMPACT)
        compact_action.setToolTip(
            "Narrow single-column layout sized to sit beside the MTGA window"
        )
        compact_action.toggled.connect(
            lambda checked: self._set_ui_mode(UI_MODE_COMPACT if checked else UI_MODE_CLASSIC)
        )
        view_menu.addAction(compact_action)
        self._compact_action = compact_action

        debug_action = QAction("Show Debug Logging", self)
        debug_action.setCheckable(True)
        debug_action.setChecked(bool(self._settings.get("desktop_debug_logging", False)))
        debug_action.toggled.connect(self._set_debug_logging)
        view_menu.addAction(debug_action)
        self._debug_logging_action = debug_action
        self._set_debug_logging(debug_action.isChecked())

    def _set_debug_logging(self, enabled: bool) -> None:
        self.coach_tab.set_debug_logging(enabled)
        self._settings.set("desktop_debug_logging", bool(enabled))

    def _apply_theme_choice(self, theme_name: str) -> None:
        app = QApplication.instance()
        if app is None:
            return

        applied = apply_theme(app, theme_name)
        self._current_theme = save_theme(applied)
        for name, action in self._theme_actions.items():
            action.setChecked(name == self._current_theme)
        self.coach_tab.refresh_game_state_view()
        self._status_bar.showMessage(f"Theme: {THEME_LABELS[self._current_theme]}", 3000)

    def _sync_runtime_preferences(self, process: CoachProcess) -> None:
        process.send_payload(
            {
                "cmd": "sync_voice_preferences",
                "voice": self._settings.get("voice"),
                "voice_speed": self._settings.get("voice_speed", 1.0),
                "muted": bool(self._settings.get("muted", False)),
            }
        )

    def _handle_provisioning_changed(self, ready: bool) -> None:
        self._set_coach_view_enabled(ready)

    def _handle_guided_setup_finished(self, success: bool, message: str) -> None:
        self._status_bar.showMessage(message, 8000)
        if not success:
            QMessageBox.warning(self, "Setup Incomplete", message)
            self._show_repair_view()
            return

        state = self.refresh_state()
        if state.is_fully_provisioned and self._process is None:
            self._start_coach(*self._launch_flags)

    def _show_startup_prompt(self, state: RuntimeState) -> None:
        if self._startup_prompt_shown:
            return
        self._startup_prompt_shown = True

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Information)
        dialog.setWindowTitle("Finish mtgacoach Setup")

        if state.python_exe is None:
            dialog.setText("Python 3.10+ is required before mtgacoach can finish setup.")
            dialog.setInformativeText(
                "Install Python, then return here and retry detection."
            )
            open_python = dialog.addButton("Open Python Downloads", QMessageBox.AcceptRole)
            retry = dialog.addButton("Retry Detection", QMessageBox.ActionRole)
            dialog.addButton("Later", QMessageBox.RejectRole)
            dialog.exec()
            clicked = dialog.clickedButton()
            if clicked == open_python:
                open_url("https://www.python.org/downloads/windows/")
            elif clicked == retry:
                self._startup_prompt_shown = False
                self._auto_start()
            return

        dialog.setText("mtgacoach still needs first-run setup.")
        dialog.setInformativeText(
            "Run guided setup now. It usually takes 3-5 minutes and streams progress in the Repair tab."
        )
        run_setup = dialog.addButton("Set Everything Up", QMessageBox.AcceptRole)
        dialog.addButton("Later", QMessageBox.RejectRole)
        dialog.exec()
        if dialog.clickedButton() == run_setup:
            self._show_repair_view()
            self.repair_tab.start_guided_setup()

    # -- Model menu (endpoint configuration) ---------------------------------

    def _build_model_menu(self) -> None:
        model_menu = self.menuBar().addMenu("Model")
        config_action = QAction("Configure Endpoint\u2026", self)
        config_action.setToolTip(
            "Configure the LLM endpoint URL, API key, and model name"
        )
        config_action.triggered.connect(self._show_model_dialog)
        model_menu.addAction(config_action)

    def _show_model_dialog(self) -> None:
        dialog = ModelEndpointDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return

        # Settings are already saved by the dialog's _on_accept.
        # If the backend in the child coach process supports hot-swapping,
        # notify it — but most backends just pick up the new settings on restart.
        mode_label = "local" if self._settings.get("mode") == "local" else "online"
        self._status_bar.showMessage(
            f"Endpoint updated ({mode_label}). Restart the coach to apply.",
            8000,
        )
