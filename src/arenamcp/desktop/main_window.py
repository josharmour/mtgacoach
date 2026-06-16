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

    Includes a probe button that fetches available models from the endpoint's
    ``GET /v1/models`` route and populates a drop-down for selection.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Model Endpoint")
        self.setMinimumWidth(520)
        self._settings = get_settings()
        self._build_ui()
        self._populate()

    # -- probe helper (runs in a background thread) ---------------------------

    def _probe_endpoint(self) -> list[str]:
        """Hit ``GET {self._url_edit}/models`` and return model IDs."""
        raw = self._url_edit.text().strip().rstrip("/")
        if not raw:
            return []
        # The OpenAI-compat /v1/models endpoint is well-known. If the user
        # typed a full URL with /v1, strip it so we build the correct path;
        # if they only have a hostname add the standard prefix.
        if raw.endswith("/v1"):
            base = raw[:-3]
        elif not raw.endswith("/models"):
            base = raw
        else:
            base = raw.rsplit("/models", 1)[0]

        if "://" not in base and "://" not in raw:
            base = "http://" + base
        models_url = f"{base}/models"

        key = self._key_edit.text().strip() or "vllm"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = UrlRequest(models_url, headers=headers, method="GET")
            with urlopen(req, timeout=10, context=ctx) as resp:
                data = json.loads(resp.read().decode())
            # OpenAI-compatible response: {"object":"list","data":[{"id":"...",...}]}
            models = data.get("data") or data if isinstance(data, list) else data.get("data", [])
            ids = [m["id"] for m in models if isinstance(m, dict) and m.get("id")]
            # Ollama returns {"models": [...]}
            if not ids and "models" in data:
                ids = [m["name"] if "name" in m else m.get("model", "")
                       for m in data["models"] if isinstance(m, dict)]
            # vLLM also accepts /v1/models route; filter out duplicates
            return sorted(set(ids))
        except (URLError, HTTPError, OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Endpoint probe failed for %s: %s", models_url, exc)
            return []

    # -- UI construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(8)

        # API Base URL row with inline probe button
        url_row = QHBoxLayout()
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("http://localhost:8001/v1")
        self._url_edit.setToolTip(
            "OpenAI-compatible API base URL (e.g. your vLLM or Ollama server)"
        )
        url_row.addWidget(self._url_edit, stretch=1)

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
            "Ollama expects 'ollama')"
        )
        form.addRow("API Key:", self._key_edit)

        # Model ID — editable combo so the user can probe, then select from
        # the results, or type a model name directly.
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.NoInsert)  # don't grow on every keystroke
        self._model_combo.lineEdit().setPlaceholderText("gemma-4-12b-it")
        self._model_combo.setToolTip(
            "Model ID to use (e.g. 'gemma-4-12b-it', 'deepseek-v4-flash', etc.)\n"
            "Click Probe to auto-populate from the endpoint."
        )
        form.addRow("Model ID:", self._model_combo)

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

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Re-enable button when URL changes.
        self._url_edit.textChanged.connect(self._on_url_changed)

    def _populate(self) -> None:
        s = self._settings
        mode = s.get("mode", "online")
        self._model_combo.clear()
        self._model_combo.addItem("")  # blank default
        if mode == "local":
            self._url_edit.setText(s.get("local_url", "http://localhost:8001/v1"))
            self._key_edit.setText(s.get("local_api_key", "vllm"))
            model = s.get("local_model") or ""
            if model:
                self._model_combo.setCurrentText(model)
            self._status_label.setText("Local endpoint")
        else:
            self._url_edit.setText("https://api.mtgacoach.com/v1")
            self._key_edit.setText("")
            model = s.get("model") or ""
            if model:
                self._model_combo.setCurrentText(model)
            self._status_label.setText("Online (mtgacoach.com)")

    # -- probe logic ----------------------------------------------------------

    def _on_probe(self) -> None:
        """Fire the HTTP probe in a background thread so the UI stays live."""
        self._probe_btn.setEnabled(False)
        self._probe_btn.setText("Probing\u2026")
        self._status_label.setText("Probing endpoint\u2026")
        self._status_label.setStyleSheet("color: #f0c000;")

        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self) -> None:
        """Called in a background thread. Returns via postEvent."""
        models = self._probe_endpoint()
        QApplication.instance().postEvent(
            self, _ProbeResultEvent(models)
        )


    def _on_probe_result(self, models: list[str]) -> None:
        if not models:
            self._status_label.setText("No models found, or endpoint unreachable.")
            self._status_label.setStyleSheet("color: #f44336;")
            return

        # Remember the current text so we can re-select it if still valid.
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

    # -- helpers --------------------------------------------------------------

    def _on_url_changed(self) -> None:
        self._probe_btn.setEnabled(bool(self._url_edit.text().strip()))
        self._status_label.setText("")
        self._status_label.setStyleSheet("color: #8a8a8a;")

    def _reset_to_online(self) -> None:
        self._url_edit.setText("https://api.mtgacoach.com/v1")
        self._key_edit.setText("")
        self._model_combo.clear()
        self._model_combo.addItem("")
        self._status_label.setText("Online (mtgacoach.com)")
        self._status_label.setStyleSheet("color: #8a8a8a;")

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

        self._status_bar.showMessage(f"Coach exited ({exit_code}). Restarting...")
        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.deleteLater()
            self._process = None
        QTimer.singleShot(250, lambda: self._start_coach(*self._launch_flags))

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
