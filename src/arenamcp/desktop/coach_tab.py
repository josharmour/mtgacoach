from __future__ import annotations

import html
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from arenamcp.rules_engine import RulesEngine
from arenamcp.settings import get_settings
from arenamcp.tts import VoiceOutput

from .audio import AudioPlayback
from .coach_process import CoachProcess
from .theme import THEME_DARK, THEME_HIGH_CONTRAST, THEME_LIGHT, THEME_SYSTEM
from .tts_manager import TtsManager
from .card_overlay import CardOverlayWindow
from .hud import DraftHudWindow
from .match_overlay import MatchOverlayWindow


class CoachTab(QWidget):
    # Emitted when the Restart button is clicked — main_window handles the
    # actual restart (stopping the old process + spawning a new one). Sending
    # a pipe command to a dying process doesn't actually relaunch it.
    restart_requested = Signal()

    _SUMMARY_FIELDS = [
        ("seat", "Seat"),
        ("coach", "Coach"),
        ("bridge", "Bridge"),
    ]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._process: Optional[CoachProcess] = None
        self._tts = TtsManager(self)
        self._all_log_lines: list[tuple[str, str]] = []  # (role, text)
        self._last_game_state = "Turn 0 waiting for MTGA..."
        self._last_game_state_payload: dict[str, Any] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._status_values: dict[str, str] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._show_debug_logging = bool(get_settings().get("desktop_debug_logging", False))
        self._build_ui()
        self._draft_hud = DraftHudWindow()
        self._draft_hud.command_requested.connect(self._handle_hud_command)
        self._draft_hud.debug_report_requested.connect(self._submit_debug_report)
        self._card_overlay = CardOverlayWindow()
        # Match overlay gets a bridge accessor so it can poll Unity for
        # ground-truth card screen rects. The bridge lives in the coach
        # child process, so we route via an IPC query; for now this is
        # a no-op callable until we wire up that query path.
        # No bridge_getter — the UI process does NOT own a bridge instance.
        # Two GREBridge servers fighting over the same single-instance pipe
        # was causing rapid disconnect/reconnect cycles that forced autopilot
        # to fall back to mouse. Card positions now come via `card_positions`
        # pipe events from the coach process (which owns the bridge).
        self._match_overlay = MatchOverlayWindow(bridge_getter=None)
        self._draft_hud.card_overlay_toggled.connect(self._card_overlay.set_enabled)
        self._draft_hud.calibration_toggled.connect(self._card_overlay.set_calibration)
        self._tts.log_line.connect(self._handle_tts_log)
        self._tts.status_line.connect(self._handle_tts_status)
        self._tts.error_line.connect(lambda text: self.append_log(f"TTS: {text}", role="error"))
        try:
            self._tts.start()
        except Exception as exc:
            self.append_log(f"TTS worker failed to start: {exc}", role="error")

    def attach_process(self, process: CoachProcess) -> None:
        if self._process is process:
            return

        self.detach_process()
        self._process = process
        process.event_received.connect(self._handle_event)
        process.stderr_line.connect(self._handle_stderr)
        process.exited.connect(self._handle_process_exit)
        self.append_log("Coach process started.", role="status")

    def detach_process(self) -> None:
        self._draft_hud.hide_overlay()
        self._card_overlay.hide_overlay()
        try:
            self._match_overlay.clear_actions()
            self._match_overlay.on_match_active(False)
        except Exception:
            pass
        if self._process is None:
            return

        try:
            self._process.event_received.disconnect(self._handle_event)
            self._process.stderr_line.disconnect(self._handle_stderr)
            self._process.exited.disconnect(self._handle_process_exit)
        except (RuntimeError, TypeError):
            pass
        self._process = None

    def shutdown(self) -> None:
        self._tts.shutdown()
        self._draft_hud.close()
        self._card_overlay.close()
        self._match_overlay.close()

    def set_debug_logging(self, enabled: bool) -> None:
        self._show_debug_logging = bool(enabled)
        # Re-render the log view so previously hidden (or previously shown)
        # lines match the new filter setting.
        self._rerender_log()

    # Roles always shown to the user, even when debug logging is off. These
    # are the pertinent ones: coaching advice, coach headers, and errors.
    # All other roles (status/dim/debug/default) are hidden unless the
    # View → Show Debug Logging toggle is on.
    _PERTINENT_LOG_ROLES = frozenset({"advice", "header", "error"})

    _LOG_COLORS = {
        "advice": "#69d46c",
        "header": "#b48cff",
        "error": "#ff6666",
        "status": "#64c8dc",
        "dim": "#8a8a8a",
        "debug": "#6b7280",
        "default": "#d7d7d7",
    }

    def append_log(self, text: str, role: str = "default") -> None:
        # Keep the full history (role + text) so we can re-render when the
        # Show Debug Logging toggle flips.
        self._all_log_lines.append((role, text))
        if len(self._all_log_lines) > 500:
            self._all_log_lines = self._all_log_lines[-500:]

        if not self._is_role_visible(role):
            return

        self._render_log_line(role, text)

    def _is_role_visible(self, role: str) -> bool:
        if self._show_debug_logging:
            return True
        return role in self._PERTINENT_LOG_ROLES

    def _render_log_line(self, role: str, text: str) -> None:
        color = self._LOG_COLORS.get(role, self._LOG_COLORS["default"])
        escaped = html.escape(text).replace("\n", "<br>")
        self.log_view.append(
            f"<span style='color:{color}; font-family:Consolas;'>{escaped}</span>"
        )
        scroll_bar = self.log_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _rerender_log(self) -> None:
        """Rebuild the log view from history so the current filter takes effect
        for historical lines as well as new ones.
        """
        self.log_view.clear()
        for role, text in self._all_log_lines:
            if not self._is_role_visible(role):
                continue
            self._render_log_line(role, text)

    def _handle_tts_log(self, text: str) -> None:
        self.append_log(text, role="debug")

    def _handle_tts_status(self, text: str) -> None:
        if self._show_debug_logging:
            self.append_log(text, role="debug")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top_panel = QWidget()
        # Maximum vertical size policy — the top panel only takes the space
        # it needs. When Status is collapsed, this reclaims the empty
        # whitespace for the Game State / Coach Log panels below.
        from PySide6.QtWidgets import QSizePolicy
        top_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        top_layout = QVBoxLayout(top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        # Status section: single collapse/expand toggle, no checkbox.
        status_container = QWidget()
        status_outer = QVBoxLayout(status_container)
        status_outer.setContentsMargins(0, 0, 0, 0)
        status_outer.setSpacing(4)

        status_header = QHBoxLayout()
        status_header.setContentsMargins(0, 0, 0, 0)
        status_header.setSpacing(6)
        self._status_toggle_btn = QPushButton("▶ Status")
        self._status_toggle_btn.setFlat(True)
        self._status_toggle_btn.setCursor(Qt.PointingHandCursor)
        self._status_toggle_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 8px; "
            "font-weight: 600; border: none; } "
            "QPushButton:hover { background: rgba(255,255,255,0.05); }"
        )
        self._status_toggle_btn.clicked.connect(self._toggle_status_section)
        status_header.addWidget(self._status_toggle_btn)
        status_header.addStretch()
        status_outer.addLayout(status_header)

        summary_row = QHBoxLayout()
        summary_row.setSpacing(18)
        for key, label in self._SUMMARY_FIELDS:
            block = QFrame()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(2)
            title = QLabel(label)
            title.setStyleSheet("font-weight: 600; color: #8a8a8a;")
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            block_layout.addWidget(title)
            block_layout.addWidget(value)
            summary_row.addWidget(block, stretch=1)
            self._status_labels[key] = value
        self._status_content = QWidget()
        self._status_content.setLayout(summary_row)
        self._status_content.setVisible(False)
        status_outer.addWidget(self._status_content)
        top_layout.addWidget(status_container)

        button_row = QHBoxLayout()
        commands = [
            ("Mode", "cycle_mode"),
            ("Model", "cycle_model"),
            ("Quick", "toggle_style"),
            ("Voice", "cycle_voice"),
            ("Speed", "cycle_speed"),
            ("Mute", "toggle_mute"),
            ("AP", "toggle_autopilot"),
            ("Analyze Match", "analyze_match"),
            ("Screen", "analyze_screen"),
            ("Win Plan", "read_win_plan"),
            ("Restart", "restart"),
        ]
        for label, command in commands:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, cmd=command: self._send_command(cmd))
            button_row.addWidget(button)
            self._buttons[command] = button
        debug_button = QPushButton("Debug Report")
        debug_button.clicked.connect(self._submit_debug_report)
        button_row.addWidget(debug_button)

        # Match-overlay calibration: draws a thin outline around every card
        # the plugin detects, so we can verify ground-truth positions align.
        match_calib_button = QPushButton("Match Calib")
        match_calib_button.setCheckable(True)
        match_calib_button.setToolTip("Show outline around every card the BepInEx plugin reports")
        match_calib_button.clicked.connect(
            lambda checked: self._match_overlay.set_calibration(checked)
        )
        button_row.addWidget(match_calib_button)

        # Overlay visibility toggle — hide the whole in-game overlay when
        # it's covering MTGA content.
        self._overlay_toggle_btn = QPushButton("Overlay")
        self._overlay_toggle_btn.setCheckable(True)
        self._overlay_toggle_btn.setChecked(True)  # default: on
        self._overlay_toggle_btn.setToolTip("Show/hide the in-game overlay (pill + advice panel)")
        self._overlay_toggle_btn.clicked.connect(
            lambda checked: self._match_overlay.set_enabled(checked)
        )
        button_row.addWidget(self._overlay_toggle_btn)

        # Advice panel corner cycle — click to move the panel between the
        # four corners of MTGA.
        self._advice_anchor_btn = QPushButton("Advice Corner")
        self._advice_anchor_btn.setToolTip("Click to move the advice panel to a different corner")
        self._advice_anchor_btn.clicked.connect(self._cycle_advice_anchor)
        button_row.addWidget(self._advice_anchor_btn)

        # Fallback mode — when the GRE bridge can't submit an action, either
        # ask the user to act (advice) or let autopilot click the cards via
        # mouse (mouse). "advice" is safer; "mouse" is what older versions
        # did and can steal the cursor.
        self._fallback_btn = QPushButton("Fallback: Advice")
        self._fallback_btn.setToolTip(
            "Toggle how autopilot handles actions the bridge can't submit:\n"
            " Advice: warn the user to act manually (default, no mouse)\n"
            " Mouse: fall back to clicking the cards (can grab your cursor)"
        )
        self._fallback_btn.clicked.connect(
            lambda _checked=False: self._send_command("toggle_fallback_mode")
        )
        self._buttons["toggle_fallback_mode"] = self._fallback_btn
        button_row.addWidget(self._fallback_btn)

        top_layout.addLayout(button_row)

        game_box = QGroupBox("Game State")
        game_layout = QVBoxLayout(game_box)
        self.game_state_view = QTextEdit()
        self.game_state_view.setReadOnly(True)
        self.game_state_view.setAcceptRichText(True)
        self.game_state_view.setHtml(self._build_waiting_game_state_html())
        game_layout.addWidget(self.game_state_view)

        log_box = QGroupBox("Coach Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)

        content_splitter = QSplitter(Qt.Horizontal)
        content_splitter.setChildrenCollapsible(True)
        content_splitter.addWidget(game_box)
        content_splitter.addWidget(log_box)
        content_splitter.setStretchFactor(0, 3)
        content_splitter.setStretchFactor(1, 2)
        content_splitter.setSizes([900, 700])

        # Put the top panel directly above the content splitter so the top
        # panel only takes its natural (minimum) height — no wasted space
        # when Status is collapsed. The content splitter gets all remaining
        # vertical room.
        root.addWidget(top_panel)
        root.addWidget(content_splitter, stretch=1)

        chat_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Ask the coach or use slash commands like /deck or /analyze")
        self.chat_input.returnPressed.connect(self.send_chat)
        chat_row.addWidget(self.chat_input, stretch=1)
        send_button = QPushButton("Send")
        send_button.clicked.connect(self.send_chat)
        chat_row.addWidget(send_button)
        root.addLayout(chat_row)

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        self.append_log(f"> {text}", role="status")
        if self._process is not None:
            self._process.send_command("chat", text)

    def _send_command(self, command: str) -> None:
        # Restart is handled by the main window (stop + relaunch process).
        # Sending a pipe command to a dying process doesn't relaunch it.
        if command == "restart":
            self.append_log("Restarting coach...", role="status")
            self.restart_requested.emit()
            return

        if self._process is None:
            return

        if command == "cycle_voice":
            self._persist_voice_cycle()
        elif command == "cycle_speed":
            self._persist_speed_cycle()
        elif command == "toggle_mute":
            self._persist_mute_toggle()

        self._process.send_command(command)

    def _handle_hud_command(self, command: str) -> None:
        """Route commands from the overlay HUD buttons."""
        self._send_command(command)

    def _cycle_advice_anchor(self) -> None:
        """Move the match overlay's advice panel to the next corner."""
        try:
            new_anchor = self._match_overlay.cycle_advice_anchor()
            self.append_log(f"Advice panel: {new_anchor}", role="status")
        except Exception as exc:
            self.append_log(f"Failed to move advice panel: {exc}", role="error")

    def _toggle_status_section(self) -> None:
        """Collapse/expand the Status section."""
        expanded = not self._status_content.isVisible()
        self._status_content.setVisible(expanded)
        self._status_toggle_btn.setText("▼ Status" if expanded else "▶ Status")

    def _get_match_bridge(self):
        """Return a GREBridge instance for the match overlay to query card
        positions directly from the BepInEx plugin.

        The overlay runs in the UI process, which doesn't own the bridge
        the coach process uses — but the bridge is a named-pipe server in
        Python and the plugin is the client, so multiple Python readers
        are fine. We lazily create our own bridge instance here.
        """
        bridge = getattr(self, "_overlay_bridge", None)
        if bridge is not None:
            return bridge
        try:
            from arenamcp.gre_bridge import get_bridge
            bridge = get_bridge()
            self._overlay_bridge = bridge
            return bridge
        except Exception as e:
            self.append_log(f"Match overlay bridge unavailable: {e}", role="debug")
            return None

    def _submit_debug_report(self) -> None:
        """Debug Report flow:
          0. Capture screenshots of both the coach window and MTGA window.
          1. Save the local JSON report (referencing those screenshots).
          2. Copy the JSON path to the clipboard.
          3. Offer to upload to GitHub with an optional description.
        """
        if self._process is None:
            self.append_log("Coach process is not running.", role="error")
            return
        self.append_log("Saving local debug report...", role="status")

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            self._last_debug_screenshot_paths = self._capture_debug_screenshots(ts)
            if self._last_debug_screenshot_paths:
                self.append_log(
                    f"Screenshots captured: {list(self._last_debug_screenshot_paths.keys())}",
                    role="status",
                )
        except Exception as e:
            self.append_log(f"Screenshot capture failed: {e}", role="error")
            self._last_debug_screenshot_paths = {}

        self._pending_bug_report_save = True
        # Send screenshot paths so the child can embed them in the JSON report
        self._process.send_payload({
            "cmd": "debug_report",
            "screenshots": self._last_debug_screenshot_paths,
            "timestamp_hint": ts,
        })

    def _capture_debug_screenshots(self, timestamp: str) -> dict[str, str]:
        """Grab the coach window + MTGA window as PNGs into the bug_reports
        directory. Returns {'coach': path, 'mtga': path} with any that
        succeeded. Missing entries mean the capture failed for that target.
        """
        from pathlib import Path
        try:
            from arenamcp.logging_config import LOG_DIR
            bug_dir = Path(LOG_DIR) / "bug_reports"
        except Exception:
            bug_dir = Path.home() / ".arenamcp" / "logs" / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        out: dict[str, str] = {}

        # 1. Coach PySide window — Qt native grab (DPI-aware, no focus needed)
        try:
            win = self.window()
            if win is not None:
                pix = win.grab()
                if not pix.isNull():
                    coach_path = bug_dir / f"bug_{timestamp}_coach.png"
                    if pix.save(str(coach_path), "PNG"):
                        out["coach"] = str(coach_path)
        except Exception as e:
            self.append_log(f"Coach screenshot failed: {e}", role="debug")

        # 2. MTGA window — bounds from pygetwindow, grab via PIL ImageGrab
        try:
            import pygetwindow as gw  # type: ignore
            from PIL import ImageGrab
            wins = [w for w in gw.getWindowsWithTitle("MTGA") if w.title == "MTGA"]
            if wins:
                w = wins[0]
                if not w.isMinimized:
                    bbox = (w.left, w.top, w.left + w.width, w.top + w.height)
                    img = ImageGrab.grab(bbox=bbox, all_screens=True)
                    mtga_path = bug_dir / f"bug_{timestamp}_mtga.png"
                    img.save(str(mtga_path), "PNG")
                    out["mtga"] = str(mtga_path)
        except Exception as e:
            self.append_log(f"MTGA screenshot failed: {e}", role="debug")

        return out

    def _on_bug_report_saved(self, path: str, error: str) -> None:
        """Handler for `bug_report_saved` pipe event: copy path + offer upload."""
        if not getattr(self, "_pending_bug_report_save", False):
            return
        self._pending_bug_report_save = False

        if error or not path:
            self.append_log(f"Failed to save debug report: {error or 'no path'}", role="error")
            return

        # Step 2: copy path to clipboard
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(path)
            self.append_log(f"Debug report saved and copied to clipboard: {path}", role="status")
        except Exception as exc:
            self.append_log(f"Saved to {path} (clipboard copy failed: {exc})", role="status")

        # Step 3: offer to upload to GitHub
        note, ok = QInputDialog.getText(
            self,
            "Upload Debug Report",
            f"Report saved to:\n{path}\n\n"
            "To upload to GitHub, enter a description (or cancel to skip):",
        )
        if not ok:
            self.append_log("Debug report kept locally (upload skipped).", role="dim")
            return

        self.append_log("Uploading debug report to GitHub...", role="status")
        self._process.send_command("bugreport", note.strip())

    def _handle_event(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return

        event_type = payload.get("type")
        if event_type == "log":
            self.append_log(str(payload.get("message", "")), role="dim")
        elif event_type == "advice":
            seat_info = str(payload.get("seat_info", ""))
            text = str(payload.get("text", ""))
            is_autopilot = seat_info.strip().upper() == "AUTOPILOT"
            t = text.strip()
            is_strategic = (
                t.startswith("PLAN:")
                or t.startswith("MANUAL REQUIRED")
                or "MANUAL REQUIRED" in t[:80]
            )
            if is_autopilot and not is_strategic:
                role_header = "debug"
                role_body = "debug"
            else:
                role_header = "header"
                role_body = "advice"
            self.append_log(f"COACH ({seat_info})", role=role_header)
            self.append_log(text, role=role_body)
            if not is_autopilot:
                try:
                    self._draft_hud.add_advice(text)
                except Exception:
                    pass
            # Also show on the in-match overlay so the user can keep eyes
            # on MTGA. Skip operational autopilot noise (already demoted
            # to debug above); show only strategic content and real coach
            # advice. Strip common plan-summary prefix for readability.
            if is_strategic or not is_autopilot:
                overlay_text = text
                if overlay_text.startswith("PLAN:"):
                    overlay_text = overlay_text[5:].strip()
                try:
                    self._match_overlay.set_advice(overlay_text, seat_info)
                except Exception:
                    pass
        elif event_type == "status":
            key = str(payload.get("key", ""))
            value = str(payload.get("value", ""))
            self._update_status(key, value)
            if key == "AUTOPILOT":
                try:
                    self._draft_hud.update_autopilot("ON" in value)
                except Exception:
                    pass
        elif event_type == "error":
            self.append_log(f"ERROR: {payload.get('message', '')}", role="error")
        elif event_type == "subtask":
            status = str(payload.get("status", "")).strip()
            if status:
                self.append_log(f"  > {status}", role="status")
        elif event_type == "game_state":
            data = payload.get("data")
            if isinstance(data, dict):
                self._update_game_state(data)
                # Tell the match overlay when we're in an active game so
                # it can start polling for card positions.
                try:
                    turn = data.get("turn") or {}
                    # Multiple possible signals: explicit match flag, turn
                    # number, or the presence of any player/battlefield data.
                    has_turn = bool(
                        turn.get("turn_number")
                        or turn.get("number")
                        or data.get("turn_number")
                    )
                    has_players = bool(
                        data.get("players") or data.get("battlefield")
                        or data.get("hand")
                    )
                    in_match = (
                        bool(data.get("match_in_progress"))
                        or bool(data.get("match_id"))
                        or has_turn
                        or has_players
                    )
                    self._match_overlay.on_match_active(in_match)
                except Exception:
                    pass
        elif event_type == "suggested_actions":
            actions = payload.get("actions") or []
            try:
                self._match_overlay.set_suggested_actions(actions)
            except Exception as exc:
                self.append_log(f"Match overlay update failed: {exc}", role="error")
        elif event_type == "card_positions":
            data = payload.get("data") or {}
            try:
                self._match_overlay.update_card_positions(data)
            except Exception as exc:
                self.append_log(f"Card positions update failed: {exc}", role="debug")
        elif event_type == "bug_report_saved":
            self._on_bug_report_saved(str(payload.get("path", "")), str(payload.get("error", "")))
        elif event_type == "draft_state":
            data = payload.get("data")
            if isinstance(data, dict):
                try:
                    self._draft_hud.update_draft_state(data)
                except Exception as exc:
                    self.append_log(f"Draft HUD update failed: {exc}", role="error")
                try:
                    self._card_overlay.update_draft_state(data)
                except Exception as exc:
                    self.append_log(f"Card overlay update failed: {exc}", role="error")
        elif event_type == "speak_request":
            self._tts.request_speech(
                text=str(payload.get("text", "")),
                voice_id=str(payload.get("voice_id", "")),
                voice_name=str(payload.get("voice_name", "")),
                speed=float(payload.get("speed", 1.0) or 1.0),
            )
        elif event_type == "speak_audio":
            path = str(payload.get("path", ""))
            AudioPlayback.play_file(path)
        elif event_type == "speak_stop":
            self._tts.stop_speech()
        elif event_type == "post_match_feedback_request":
            analysis = str(payload.get("analysis", "")).strip()
            match_result = str(payload.get("match_result", "")).strip()
            if analysis:
                self._prompt_post_match_feedback(analysis, match_result)

    def _handle_stderr(self, line: str) -> None:
        self.append_log(f"[stderr] {line}", role="error")

    def _handle_process_exit(self, exit_code: int) -> None:
        self.append_log(f"Coach process exited ({exit_code}).", role="error")

    def _update_status(self, key: str, value: str) -> None:
        normalized = key.upper()
        text = value or "-"
        self._status_values[normalized] = text

        if normalized in {"SEAT_INFO", "SEAT"}:
            self._set_status_label("seat", text)
        elif normalized in {"BACKEND", "PROVIDER"}:
            self._refresh_coach_summary()
            mode_button = self._buttons.get("cycle_mode")
            if mode_button is not None:
                mode_button.setText(f"Mode: {self._compact_backend_label(value)}")
            # Hide the Model cycling button in online mode — the proxy
            # controls which model runs, so exposing its identity (or
            # letting the user cycle) would lock us into a specific
            # model name in the UI.
            self._apply_model_button_visibility()
        elif normalized == "MODEL":
            self._refresh_coach_summary()
            if not self._is_online_backend():
                self._set_button_text("cycle_model", "Model", self._compact_model_label(value))
        elif normalized in {"BRIDGE", "GRE"}:
            self._set_status_label("bridge", text)
        elif normalized == "STYLE":
            # Button label IS the current mode (no "Style:" prefix).
            button = self._buttons.get("toggle_style")
            if button is not None:
                button.setText(self._compact_style_label(value))
        elif normalized in {"VOICE", "VOICE_ID"}:
            self._set_button_text("cycle_voice", "Voice", self._compact_voice_label(value))
        elif normalized == "SPEED":
            self._set_button_text("cycle_speed", "Speed", value)
        elif normalized == "MUTE":
            self._set_button_text("toggle_mute", "Mute", self._compact_mute_label(value))
        elif normalized == "AUTOPILOT":
            autopilot_text = self._normalize_autopilot(value)
            self._set_button_text("toggle_autopilot", "AP", autopilot_text)
        # AFK and Land Only buttons were removed from the UI — ignore the
        # status emits so we don't try to update nonexistent buttons.
        elif normalized in ("AFK", "LAND_ONLY"):
            pass
        elif normalized == "FALLBACK_MODE":
            btn = self._buttons.get("toggle_fallback_mode")
            if btn is not None:
                mode = value.strip().lower() or "advice"
                btn.setText(f"Fallback: {mode.capitalize()}")

    def _set_status_label(self, key: str, value: str) -> None:
        label = self._status_labels.get(key)
        if label is not None:
            label.setText(value or "-")

    def _set_button_text(self, command: str, prefix: str, value: str) -> None:
        button = self._buttons.get(command)
        if button is None:
            return

        clean = value.strip()
        button.setText(prefix if not clean else f"{prefix}: {clean}")

    def _compact_backend_label(self, value: str) -> str:
        clean = value.strip()
        if not clean:
            return "?"

        lower = clean.lower()
        if "online" in lower:
            return "Online"
        if "local" in lower:
            return "Local"
        return clean

    def _compact_model_label(self, value: str) -> str:
        clean = value.strip()
        if not clean:
            return "Default"
        if "/" in clean:
            return clean.split("/", 1)[1]
        return clean

    def _compact_style_label(self, value: str) -> str:
        """Map backend style value to the button label the user sees."""
        clean = value.strip().lower()
        if clean in ("quick", "concise"):
            return "Quick"
        if clean in ("chatty", "verbose"):
            return "Chatty"
        return value.strip().capitalize() or "?"

    def _compact_voice_label(self, value: str) -> str:
        clean = value.strip()
        if not clean:
            return "?"
        for prefix in ("Changed to:", "TTS Voice:"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):].strip()
        clean = clean.replace("(saved)", "").strip()
        return clean or "?"

    def _compact_mute_label(self, value: str) -> str:
        clean = value.strip().lower()
        if "muted" in clean and "unmuted" not in clean:
            return "On"
        if "unmuted" in clean:
            return "Off"
        return value.strip() or "?"

    def _normalize_autopilot(self, value: str) -> str:
        clean = value.strip()
        return clean[3:] if clean.upper().startswith("AP:") else clean

    def _persist_voice_cycle(self) -> None:
        settings = get_settings()
        current_voice = str(settings.get("voice", "am_adam") or "am_adam")
        voices = [voice_id for voice_id, _ in VoiceOutput.VOICES]
        if current_voice not in voices:
            current_index = 0
        else:
            current_index = voices.index(current_voice)
        next_index = (current_index + 2) % len(voices)
        settings.set("voice", voices[next_index])

    def _persist_speed_cycle(self) -> None:
        settings = get_settings()
        try:
            current_speed = float(settings.get("voice_speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            current_speed = 1.0
        presets = list(VoiceOutput.SPEED_PRESETS)
        try:
            current_index = presets.index(current_speed)
        except ValueError:
            current_index = -1
        settings.set("voice_speed", presets[(current_index + 1) % len(presets)])

    def _persist_mute_toggle(self) -> None:
        settings = get_settings()
        settings.set("muted", not bool(settings.get("muted", False)))

    def _prompt_post_match_feedback(self, analysis: str, match_result: str) -> None:
        if self._process is None:
            return

        dialog = QDialog(self)
        result_label = match_result.upper() if match_result else "MATCH"
        dialog.setWindowTitle(f"Submit Coaching Feedback ({result_label})")
        dialog.resize(820, 640)

        layout = QVBoxLayout(dialog)

        intro = QLabel(
            "Post-match analysis is ready. Add any coaching mistakes, missing advice, or UI confusion you want included in a bug report."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        analysis_label = QLabel("Analysis to include")
        analysis_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(analysis_label)

        analysis_view = QTextEdit()
        analysis_view.setReadOnly(True)
        analysis_view.setPlainText(analysis)
        layout.addWidget(analysis_view, stretch=3)

        feedback_label = QLabel("Your coaching feedback")
        feedback_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(feedback_label)

        feedback_input = QTextEdit()
        feedback_input.setPlaceholderText(
            "Example: It missed lethal on turn 5, overvalued the aura line, or repeated generic target prompts."
        )
        layout.addWidget(feedback_input, stretch=2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal,
            dialog,
        )
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText("Submit Report")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setText("Skip")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.Accepted:
            self.append_log("Skipped post-match coaching feedback report.", role="dim")
            return

        feedback = feedback_input.toPlainText().strip()
        self.append_log("Submitting coaching feedback report...", role="status")
        self._process.send_payload(
            {
                "cmd": "bugreport",
                "text": feedback,
                "source": "post_match_analysis",
                "analysis": analysis,
                "match_result": match_result,
            }
        )

    def _refresh_coach_summary(self) -> None:
        backend = self._status_values.get("BACKEND") or self._status_values.get("PROVIDER") or "-"
        model = self._status_values.get("MODEL", "").strip()
        backend_compact = self._compact_backend_label(backend)

        # In online mode the proxy decides which model runs. Don't expose
        # that to users — otherwise swapping a model upstream becomes a
        # visible user-facing change.
        if self._is_online_backend():
            self._set_status_label("coach", backend_compact)
            return

        model_compact = self._compact_model_label(model) if model else ""
        if model_compact and model_compact.lower() not in {"default", backend_compact.lower()}:
            coach_text = f"{backend_compact} / {model_compact}"
        else:
            coach_text = backend_compact
        self._set_status_label("coach", coach_text)

    def _is_online_backend(self) -> bool:
        backend = (self._status_values.get("BACKEND") or self._status_values.get("PROVIDER") or "").lower().strip()
        # Exact match on "online" or "online (...)". Anything containing
        # "local" (including fallback strings like "local (temp) — online
        # failed") is considered local.
        if "local" in backend:
            return False
        return backend == "online" or backend.startswith("online ") or backend.startswith("online(") or "online" == self._compact_backend_label(backend).lower()

    def _apply_model_button_visibility(self) -> None:
        button = self._buttons.get("cycle_model")
        if button is None:
            return
        button.setVisible(not self._is_online_backend())

    def refresh_game_state_view(self) -> None:
        if self._last_game_state_payload:
            self._update_game_state(self._last_game_state_payload)
        else:
            self.game_state_view.setHtml(self._build_waiting_game_state_html())

    def _effective_theme_name(self) -> str:
        configured = str(get_settings().get("desktop_theme", THEME_SYSTEM) or THEME_SYSTEM).strip().lower()
        if configured != THEME_SYSTEM:
            return configured

        window = self.palette().color(self.backgroundRole())
        return THEME_LIGHT if window.lightness() >= 128 else THEME_DARK

    def _theme_tokens(self) -> dict[str, str]:
        theme_name = self._effective_theme_name()
        if theme_name == THEME_LIGHT:
            return {
                "bg": "#ffffff",
                "panel": "#f7f9fc",
                "panel2": "#eef3f8",
                "border": "#cfd6de",
                "text": "#17191c",
                "muted": "#5f6b7a",
                "header": "#1f2937",
                "opponent": "#7c2d12",
                "stack": "#6b21a8",
                "player": "#14532d",
                "land": "#9a6700",
                "creature": "#166534",
                "artifact": "#475569",
                "enchantment": "#7c3aed",
                "planeswalker": "#be123c",
                "battle": "#0f766e",
                "spell": "#1d4ed8",
                "other": "#374151",
                "warning_bg": "#fff7ed",
                "warning_fg": "#9a3412",
                "castable_bg": "#dcfce7",
                "castable_fg": "#166534",
                "uncastable_bg": "#ffedd5",
                "uncastable_fg": "#9a3412",
                "hand_neutral_bg": "#eef3f8",
                "hand_neutral_fg": "#1f2937",
            }
        if theme_name == THEME_HIGH_CONTRAST:
            return {
                "bg": "#000000",
                "panel": "#000000",
                "panel2": "#0f0f0f",
                "border": "#ffffff",
                "text": "#ffffff",
                "muted": "#e5e7eb",
                "header": "#ffffff",
                "opponent": "#ff8a00",
                "stack": "#ffff00",
                "player": "#00ff66",
                "land": "#ffd400",
                "creature": "#00ff66",
                "artifact": "#00e5ff",
                "enchantment": "#ff66ff",
                "planeswalker": "#ff4d4d",
                "battle": "#00ffff",
                "spell": "#80bfff",
                "other": "#ffffff",
                "warning_bg": "#111111",
                "warning_fg": "#ffff00",
                "castable_bg": "#003300",
                "castable_fg": "#00ff66",
                "uncastable_bg": "#331100",
                "uncastable_fg": "#ffff00",
                "hand_neutral_bg": "#0f0f0f",
                "hand_neutral_fg": "#ffffff",
            }
        return {
            "bg": "#0f1317",
            "panel": "#171c21",
            "panel2": "#1f2630",
            "border": "#39424d",
            "text": "#e6edf3",
            "muted": "#9aa4af",
            "header": "#f8fafc",
            "opponent": "#fb923c",
            "stack": "#c084fc",
            "player": "#4ade80",
            "land": "#facc15",
            "creature": "#4ade80",
            "artifact": "#94a3b8",
            "enchantment": "#a78bfa",
            "planeswalker": "#fb7185",
            "battle": "#2dd4bf",
            "spell": "#60a5fa",
            "other": "#cbd5e1",
            "warning_bg": "#3a1f15",
            "warning_fg": "#fdba74",
            "castable_bg": "#163524",
            "castable_fg": "#86efac",
            "uncastable_bg": "#4a2a18",
            "uncastable_fg": "#fdba74",
            "hand_neutral_bg": "#1f2630",
            "hand_neutral_fg": "#e6edf3",
        }

    def _build_waiting_game_state_html(self) -> str:
        tokens = self._theme_tokens()
        return (
            f"<div style='font-family:Consolas,\"Courier New\",monospace;"
            f"color:{tokens['text']}; background:{tokens['bg']}; padding:10px;'>"
            f"<div style='font-size:16px; font-weight:700; margin-bottom:6px;'>Waiting for MTGA...</div>"
            f"<div style='color:{tokens['muted']};'>The board view will appear here once a match is detected.</div>"
            f"</div>"
        )

    def _build_game_state_html(self, data: dict[str, Any]) -> str:
        """Spatial layout mirroring MTGA: opponent on top, stack in the
        middle (prominent), you on the bottom, legal actions collapsed.
        """
        tokens = self._theme_tokens()
        zones = data.get("zones")
        if not isinstance(zones, dict):
            zones = data

        players = data.get("players", [])
        local_seat = _int_value(data.get("local_seat_id"))
        local_player = next((p for p in players if isinstance(p, dict) and _bool_value(p.get("is_local"))), None)
        opponent_player = next((p for p in players if isinstance(p, dict) and not _bool_value(p.get("is_local"))), None)
        opponent_seat = _int_value(opponent_player.get("seat_id")) if isinstance(opponent_player, dict) else 0

        turn = data.get("turn", {})
        header = self._render_game_header(turn, local_seat)
        pending = self._render_pending_decision(
            data.get("pending_decision"),
            data.get("decision_context"),
            data.get("legal_actions"),
            tokens,
        )

        battlefield = zones.get("battlefield", [])
        battlefield = battlefield if isinstance(battlefield, list) else []

        opponent_board = self._render_battlefield_section(
            "Opponent Board",
            [card for card in battlefield if isinstance(card, dict) and self._card_controller_seat(card) == opponent_seat],
            tokens,
            seat_color=tokens["opponent"],
        )
        stack_html = self._render_stack_section(zones.get("stack"), tokens)
        player_board = self._render_battlefield_section(
            "Your Board",
            [card for card in battlefield if isinstance(card, dict) and self._card_controller_seat(card) == local_seat],
            tokens,
            seat_color=tokens["player"],
        )

        top_resources = self._render_resource_row(
            "Opponent",
            opponent_player,
            data,
            zones,
            opponent_seat,
            tokens,
            include_hand_count=True,
        )
        bottom_resources = self._render_resource_row(
            "You",
            local_player,
            data,
            zones,
            local_seat,
            tokens,
            include_hand_cards=True,
        )

        legal_actions = self._render_legal_actions(data.get("legal_actions"), tokens)

        # Spatial composition:
        #   header → pending decision → OPP (resources + board)
        #   → STACK (framed centerpiece)
        #   → YOU (resources + board + hand)
        #   → legal actions (collapsed)
        opp_zone = (
            f"<div style='margin:0 0 6px 0; padding:6px 8px;"
            f" border:1px solid {tokens['opponent']}40; border-radius:6px;"
            f" background:{tokens['panel']};'>"
            f"{top_resources}{opponent_board}"
            f"</div>"
        )
        you_zone = (
            f"<div style='margin:6px 0 0 0; padding:6px 8px;"
            f" border:1px solid {tokens['player']}40; border-radius:6px;"
            f" background:{tokens['panel']};'>"
            f"{bottom_resources}{player_board}"
            f"</div>"
        )

        return (
            f"<div style='font-family:Consolas,\"Courier New\",monospace; color:{tokens['text']};"
            f" background:{tokens['bg']}; padding:8px 10px;'>"
            f"{header}{pending}"
            f"{opp_zone}"
            f"{stack_html}"
            f"{you_zone}"
            f"{legal_actions}"
            f"</div>"
        )

    # -- Life bar helper ----------------------------------------------------
    def _render_life_bar(self, life: Any, max_life: int = 20) -> str:
        """Unicode bar: 20/40 ██████████░░░░░░░░░░"""
        try:
            cur = max(0, int(life))
        except (TypeError, ValueError):
            return "?"
        total_blocks = 10
        # Scale relative to max_life (default 20 — standard MTG). Life above
        # max stays at full. Life below 0 renders as empty.
        ratio = min(1.0, cur / max(1, max_life))
        filled = int(round(ratio * total_blocks))
        bar = "█" * filled + "░" * (total_blocks - filled)
        return f"{cur}/{max_life}&nbsp;<span style='letter-spacing:-1px;'>{bar}</span>"

    def _render_game_header(self, turn: Any, local_seat: int) -> str:
        tokens = self._theme_tokens()
        if not isinstance(turn, dict):
            return ""
        turn_num = _int_value(turn.get("turn_number"))
        phase = _str_value(turn.get("phase")) or "?"
        # Strip noisy "Phase_" prefix
        phase_display = phase.replace("Phase_", "")
        step = _str_value(turn.get("step")).replace("Step_", "")
        active_player = _int_value(turn.get("active_player"))
        active_label = ""
        if active_player and local_seat:
            active_label = "YOURS" if active_player == local_seat else "OPP"
        bits = [f"T{turn_num}", phase_display]
        if step and step != phase_display:
            bits.append(step)
        if active_label:
            bits.append(active_label)
        line = "  ·  ".join(bits)
        accent = tokens["player"] if active_label == "YOURS" else tokens["opponent"] if active_label == "OPP" else tokens["header"]
        return (
            f"<div style='margin:0 0 6px 0; padding:4px 8px; border-left:3px solid {accent};"
            f" background:{tokens['panel2']}; color:{tokens['header']}; font-size:12px; font-weight:700;'>"
            f"{html.escape(line)}</div>"
        )

    def _render_pending_decision(
        self,
        pending: Any,
        decision_context: Any,
        legal_actions: Any,
        tokens: dict[str, str],
    ) -> str:
        text = _str_value(pending).strip()
        if not text:
            return ""
        context = decision_context if isinstance(decision_context, dict) else {}
        source_card = _str_value(context.get("source_card"))
        detail_line = ""
        if source_card:
            detail_line = f"Source: {source_card}"
        elif _str_value(context.get("type")):
            detail_line = f"Type: {_str_value(context.get('type'))}"

        option_chips = self._render_pending_decision_options(context, legal_actions, tokens)
        detail_html = ""
        if detail_line:
            detail_html = (
                f"<div style='color:{tokens['text']}; font-size:11px; margin-top:4px;'>"
                f"{html.escape(detail_line)}</div>"
            )
        return (
            f"<div style='margin:0 0 4px 0; padding:4px 8px; border-left:3px solid {tokens['warning_fg']};"
            f" background:{tokens['warning_bg']}; font-size:11px;'>"
            f"<span style='color:{tokens['warning_fg']}; font-weight:700;'>⚠ {html.escape(text)}</span>"
            f"{detail_html}"
            f"{option_chips}"
            f"</div>"
        )

    def _render_pending_decision_options(
        self,
        decision_context: dict[str, Any],
        legal_actions: Any,
        tokens: dict[str, str],
    ) -> str:
        options: list[str] = []
        decision_type = _str_value(decision_context.get("type")).lower()

        if isinstance(legal_actions, list):
            for action in legal_actions:
                text = _str_value(action).strip()
                if not text:
                    continue
                if text.startswith("Select target: "):
                    options.append(text.removeprefix("Select target: ").strip())
                elif decision_type in {"scry", "surveil", "select_n", "choose", "choose_creature", "choose_land", "choose_enchantment", "choose_artifact", "choose_permanent"}:
                    if text not in {"Pass", "Done"} and not text.startswith("Action: "):
                        options.append(text)

        if not options:
            option_cards = decision_context.get("option_cards")
            if isinstance(option_cards, list):
                options.extend(_str_value(card).strip() for card in option_cards if _str_value(card).strip())

        deduped: list[str] = []
        seen: set[str] = set()
        for option in options:
            if option and option not in seen:
                seen.add(option)
                deduped.append(option)

        if not deduped:
            return ""

        chips = "".join(
            f"<span style='display:inline-block; margin:6px 6px 0 0; padding:4px 7px; border:1px solid {tokens['border']};"
            f"border-radius:999px; background:{tokens['panel']}; color:{tokens['warning_fg']}; font-size:11px;'>"
            f"{html.escape(option)}</span>"
            for option in deduped[:6]
        )
        return (
            f"<div style='margin-top:4px;'>"
            f"<div style='color:{tokens['muted']}; font-size:11px; text-transform:uppercase; letter-spacing:0.04em;'>Current options</div>"
            f"{chips}</div>"
        )

    def _render_resource_row(
        self,
        seat_label: str,
        player: Optional[dict[str, Any]],
        game_state: dict[str, Any],
        zones: dict[str, Any],
        seat_id: int,
        tokens: dict[str, str],
        *,
        include_hand_count: bool = False,
        include_hand_cards: bool = False,
    ) -> str:
        """Compact single-line resource row with a unicode life bar:
            OPP   ♥ 20/40 ██████████░░░░░░░░░░   📚 33   🪦 2   ⬜ 0   ✋ 3
        """
        accent = tokens["opponent"] if seat_label == "Opponent" else tokens["player"]
        life_value = _int_value(player.get("life_total")) if isinstance(player, dict) else 0
        # Starting life defaults to 20 (or 30 if anywhere in game_state hints otherwise)
        starting_life = 20
        try:
            starting_life = int(game_state.get("starting_life") or 20) or 20
        except (TypeError, ValueError):
            starting_life = 20
        life = self._render_life_bar(life_value, starting_life)
        lib = self._library_value(zones, seat_label == "Opponent")
        grave_cards = self._cards_for_zone_and_seat(zones.get("graveyard"), seat_id)
        exile_cards = self._cards_for_zone_and_seat(zones.get("exile"), seat_id)
        grave_count = str(len(grave_cards)) if grave_cards is not None else "?"
        exile_count = str(len(exile_cards)) if exile_cards is not None else "?"

        # Compact pills — Qt's QTextEdit ignores inline-block margins, so
        # use explicit non-breaking spaces + a dot separator between pills.
        sep = "&nbsp;&nbsp;·&nbsp;&nbsp;"

        def pill(icon: str, value: str, color: str, title: str = "", raw_value: bool = False) -> str:
            title_attr = f" title='{html.escape(title)}'" if title else ""
            rendered = value if raw_value else html.escape(value)
            return (
                f"<span{title_attr}>"
                f"<span style='color:{tokens['muted']}; font-size:11px;'>{icon}</span>"
                f"&nbsp;"
                f"<span style='color:{color}; font-weight:600;'>{rendered}</span>"
                f"</span>"
            )

        pills = [
            pill("♥", life, accent, "Life total", raw_value=True),
            pill("📚", lib, tokens["spell"], "Library count"),
            pill("🪦", grave_count, tokens["other"], f"Graveyard: {self._zone_summary(grave_cards, 8) if grave_cards else 'empty'}"),
            pill("⬜", exile_count, tokens["other"], f"Exile: {self._zone_summary(exile_cards, 8) if exile_cards else 'empty'}"),
        ]
        if include_hand_count:
            hand_count = _int_value(zones.get("opponent_hand_count"))
            pills.append(pill("✋", str(hand_count), tokens["spell"], "Opponent hand size"))

        hand_html = ""
        if include_hand_cards:
            hand_cards = self._cards_for_zone_and_seat(zones.get("my_hand") or zones.get("hand"), seat_id, allow_unknown_owner=True)
            if hand_cards:
                pills.append(pill("✋", str(len(hand_cards)), tokens["spell"], "Your hand size"))
                hand_html = (
                    f"<div style='margin:2px 0 6px 0; padding:3px 6px;"
                    f" border-left:2px solid {tokens['spell']}; font-size:11px; line-height:1.45;'>"
                    f"{self._render_hand_summary(hand_cards, game_state, tokens)}"
                    f"</div>"
                )

        row = sep.join(pills)
        seat_tag = "YOU" if seat_label == "You" else "OPP"
        tag_style = (
            f"color:{accent}; font-size:10px; font-weight:700; letter-spacing:0.06em;"
        )
        # Three non-breaking spaces after the tag so it doesn't touch the first pill
        return (
            f"<div style='margin:0 0 4px 0; font-size:11px;'>"
            f"<span style='{tag_style}'>{seat_tag}</span>&nbsp;&nbsp;&nbsp;"
            f"{row}</div>{hand_html}"
        )

    def _resource_chip(
        self,
        label: str,
        value: str,
        accent: str,
        tokens: dict[str, str],
        *,
        allow_html: bool = False,
        wide: bool = False,
    ) -> str:
        # Kept for backward-compat in case other callers exist; new code uses
        # the inline pill in _render_resource_row. This emits a compact span.
        rendered_value = value or "-"
        if not allow_html:
            rendered_value = html.escape(rendered_value)
        return (
            f"<span style='margin-right:8px; color:{accent};'>"
            f"<span style='color:{tokens['muted']}; font-size:10px;'>{html.escape(label)}</span> "
            f"<span style='font-weight:600;'>{rendered_value}</span></span>"
        )

    def _render_battlefield_section(
        self,
        title: str,
        cards: list[dict[str, Any]],
        tokens: dict[str, str],
        *,
        seat_color: str,
    ) -> str:
        grouped = self._group_battlefield(cards)
        parts = [
            self._render_card_lane(label, grouped.get(key, []), key, tokens)
            for key, label in (
                ("land", "Lands"),
                ("creature", "Creatures"),
                ("planeswalker", "PW"),
                ("enchantment", "Ench."),
                ("artifact", "Artifact"),
                ("battle", "Battle"),
                ("other", "Other"),
            )
        ]
        body = "".join(part for part in parts if part)
        if not body:
            # Skip empty battlefield entirely — don't waste space on "no permanents visible"
            return ""
        tag = "YOU" if "Your" in title else "OPP"
        # Small header line with the seat tag; the lanes underneath have
        # their own labels. Qt QTextEdit doesn't honor min-width/display so
        # we keep layout block-based with a clear visual separator.
        return (
            f"<div style='margin:0 0 4px 0;'>"
            f"<div style='color:{seat_color}; font-size:10px; font-weight:700;"
            f" letter-spacing:0.06em; margin-bottom:2px;'>{tag} · BOARD</div>"
            f"<div style='margin-left:8px;'>{body}</div>"
            f"</div>"
        )

    def _render_card_lane(self, label: str, cards: list[dict[str, Any]], type_key: str, tokens: dict[str, str]) -> str:
        if not cards:
            return ""
        accent = tokens.get(type_key, tokens["other"])
        summaries = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(
            html.escape(self._compact_card_summary(card))
            for card in cards
        )
        # Inline label + summaries on a single row per lane.
        # Three nbsp after label so Qt's QTextEdit (which ignores margin)
        # still shows a visible gap between label and cards.
        return (
            f"<div style='font-size:11px; line-height:1.45;'>"
            f"<span style='color:{tokens['muted']}; text-transform:uppercase;"
            f" letter-spacing:0.04em;'>{html.escape(label)}:</span>&nbsp;&nbsp;"
            f"<span style='color:{accent};'>{summaries}</span>"
            f"</div>"
        )

    def _compact_card_summary(self, card: dict[str, Any]) -> str:
        """Compact one-line card summary with emoji state indicators.

        Examples:
            Forest                            — plain land
            Goblin 1/1 💤                      — summoning sick
            Wurm 5/5 ⟲→                        — tapped + attacking
            Planeswalker [L 4]                 — loyalty
            Llanowar Elves 1/1 [+1+1]          — with counter
        """
        name = _str_value(card.get("name"), "?")
        detail_parts: list[str] = []
        if "creature" in _str_value(card.get("type_line")).lower():
            power = card.get("power")
            toughness = card.get("toughness")
            if power not in (None, "") and toughness not in (None, ""):
                detail_parts.append(f"{power}/{toughness}")
        loyalty = card.get("counters", {}).get("Loyalty") if isinstance(card.get("counters"), dict) else None
        if loyalty not in (None, ""):
            detail_parts.append(f"[L {loyalty}]")
        # State icons (compact, glanceable)
        icons: list[str] = []
        if _bool_value(card.get("is_tapped")):
            icons.append("⟲")
        if _bool_value(card.get("is_attacking")):
            icons.append("→")
        if _bool_value(card.get("is_blocking")):
            icons.append("⚔")
        if _bool_value(card.get("summoning_sick")):
            icons.append("💤")
        if icons:
            detail_parts.append("".join(icons))
        counters = self._format_non_loyalty_counters(card.get("counters"))
        if counters:
            detail_parts.append(f"[{counters}]")

        detail_text = ", ".join(part for part in detail_parts if part)
        if detail_text:
            return f"{name} ({detail_text})"
        return name

    def _render_stack_section(self, stack_zone: Any, tokens: dict[str, str]) -> str:
        # Hide the section entirely when empty — reduces clutter. Only
        # show a framed centerpiece when there are actual stack items.
        if not isinstance(stack_zone, list) or not stack_zone:
            return (
                f"<div style='margin:6px 0; text-align:center;"
                f" color:{tokens['muted']}; font-size:10px; letter-spacing:0.08em;'>"
                f"── STACK · empty ──</div>"
            )

        rows = []
        for idx, item in enumerate(reversed([card for card in stack_zone if isinstance(card, dict)]), start=1):
            name = _str_value(item.get("name"), "?")
            detail = _str_value(item.get("type_line"))
            owner = "You" if _bool_value(item.get("is_local")) else ""
            source_card = item.get("source_card")
            source_name = ""
            if isinstance(source_card, dict):
                source_name = _str_value(source_card.get("name"))

            if detail.lower() == "ability" and source_name:
                title = f"{source_name} ability"
            else:
                title = name

            meta_parts: list[str] = []
            if source_name and title != source_name:
                meta_parts.append(f"From {source_name}")
            if detail and detail.lower() != "ability":
                meta_parts.append(detail)
            if owner:
                meta_parts.append(owner)

            oracle_text = _str_value(item.get("oracle_text"))
            oracle_summary = self._truncate_stack_text(oracle_text)

            meta_html = ""
            if meta_parts:
                meta_html = (
                    f"<div style='color:{tokens['muted']}; font-size:11px; margin-top:2px;'>"
                    f"{html.escape(' | '.join(meta_parts))}</div>"
                )
            oracle_html = ""
            if oracle_summary:
                oracle_html = (
                    f"<div style='color:{tokens['text']}; font-size:11px; margin-top:4px; line-height:1.35;'>"
                    f"{html.escape(oracle_summary)}</div>"
                )
            fallback_html = ""
            if not oracle_summary and detail:
                fallback_html = (
                    f"<div style='color:{tokens['muted']}; font-size:11px; margin-top:4px;'>"
                    f"{html.escape(detail)}</div>"
                )
            # Circled-digit resolve order (①②③...) — top of stack = idx 1
            circled = "①②③④⑤⑥⑦⑧⑨⑩"
            marker = circled[idx - 1] if 0 <= idx - 1 < len(circled) else f"#{idx}"
            owner_tag = ""
            if owner == "You":
                owner_tag = f" <span style='color:{tokens['player']}; font-size:10px;'>YOURS</span>"
            else:
                owner_tag = f" <span style='color:{tokens['opponent']}; font-size:10px;'>OPP</span>"

            rows.append(
                f"<div style='margin:0 0 4px 0; padding:5px 8px;"
                f" border-left:3px solid {tokens['stack']}; background:{tokens['panel2']};'>"
                f"<span style='color:{tokens['stack']}; font-weight:700; font-size:14px;'>{marker}</span>"
                f"&nbsp;&nbsp;"
                f"<span style='color:{tokens['header']}; font-weight:700;'>{html.escape(title)}</span>"
                f"{owner_tag}"
                f"{meta_html}"
                f"{oracle_html}"
                f"{fallback_html}"
                f"</div>"
            )
        # Framed centerpiece between opp and you zones
        return (
            f"<div style='margin:8px 0; padding:8px 10px;"
            f" border:2px solid {tokens['stack']}; border-radius:8px;"
            f" background:{tokens['panel']};'>"
            f"<div style='font-size:11px; color:{tokens['stack']};"
            f" font-weight:700; letter-spacing:0.08em; margin-bottom:6px;'>"
            f"⏳ STACK · resolve order ↓"
            f"</div>"
            f"{''.join(rows)}</div>"
        )

    def _truncate_stack_text(self, text: str, limit: int = 180) -> str:
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3].rstrip() + "..."

    def _render_legal_actions(self, actions_zone: Any, tokens: dict[str, str]) -> str:
        if not isinstance(actions_zone, list):
            return ""
        actions: list[str] = []
        for action in actions_zone:
            action_text = _str_value(action).strip()
            if (
                action_text
                and action_text != "Pass"
                and not action_text.startswith("Action: Activate_Mana")
                and not action_text.startswith("Action: FloatMana")
            ):
                actions.append(action_text)
        if not actions:
            return ""
        rendered_parts: list[str] = []
        for action in actions[:12]:
            lower = action.lower()
            border = tokens["spell"]
            fg = tokens["spell"]
            bg = tokens["panel"]
            label = action
            if lower.startswith("cast "):
                if "[ok]" in lower:
                    border = tokens["castable_fg"]
                    fg = tokens["castable_fg"]
                    bg = tokens["castable_bg"]
                else:
                    border = tokens["uncastable_fg"]
                    fg = tokens["uncastable_fg"]
                    bg = tokens["uncastable_bg"]
                    label = f"{action} [MANUAL PAY / NOT CONFIRMED]"
            # Compact inline chip instead of full-width padded row
            rendered_parts.append(
                f"<span style='display:inline-block; margin:0 4px 3px 0; padding:2px 6px;"
                f" border:1px solid {border}; border-left:3px solid {border}; border-radius:4px;"
                f" background:{bg}; color:{fg}; font-size:10px;'>{html.escape(label)}</span>"
            )
        rendered = "".join(rendered_parts)
        # Collapsible — hidden by default since this is usually the biggest
        # chunk of the game state view and MTGA already shows these on screen.
        return (
            f"<details style='margin-top:4px; font-size:11px;'>"
            f"<summary style='cursor:pointer; color:{tokens['muted']};"
            f" text-transform:uppercase; letter-spacing:0.04em;'>"
            f"Legal actions ({len(actions)})</summary>"
            f"<div style='margin-top:4px;'>{rendered}</div>"
            f"</details>"
        )

    def _group_battlefield(self, cards: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {
            "land": [],
            "creature": [],
            "planeswalker": [],
            "enchantment": [],
            "artifact": [],
            "battle": [],
            "other": [],
        }
        for card in cards:
            type_line = _str_value(card.get("type_line")).lower()
            if "land" in type_line:
                grouped["land"].append(card)
            elif "creature" in type_line:
                grouped["creature"].append(card)
            elif "planeswalker" in type_line:
                grouped["planeswalker"].append(card)
            elif "enchantment" in type_line:
                grouped["enchantment"].append(card)
            elif "artifact" in type_line:
                grouped["artifact"].append(card)
            elif "battle" in type_line:
                grouped["battle"].append(card)
            else:
                grouped["other"].append(card)
        return grouped

    def _card_controller_seat(self, card: dict[str, Any]) -> int:
        return _int_value(card.get("controller_seat_id")) or _int_value(card.get("owner_seat_id"))

    def _cards_for_zone_and_seat(
        self,
        zone: Any,
        seat_id: int,
        *,
        allow_unknown_owner: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(zone, list):
            return []
        items = []
        for card in zone:
            if not isinstance(card, dict):
                continue
            owner = _int_value(card.get("owner_seat_id")) or _int_value(card.get("controller_seat_id"))
            if owner == seat_id or (allow_unknown_owner and owner == 0):
                items.append(card)
        return items

    def _zone_summary(self, cards: list[dict[str, Any]], limit: int) -> str:
        if not cards:
            return "0"
        names = [_str_value(card.get("name"), "?") for card in cards[:limit]]
        extra = f" +{len(cards) - limit}" if len(cards) > limit else ""
        return f"{len(cards)} ({', '.join(names)}{extra})"

    def _hand_summary(self, cards: list[dict[str, Any]]) -> str:
        if not cards:
            return "0"
        names = []
        for card in cards[:8]:
            name = _str_value(card.get("name"), "?")
            mana = _str_value(card.get("mana_cost"))
            names.append(f"{name} {mana}".strip())
        extra = f" +{len(cards) - 8}" if len(cards) > 8 else ""
        return f"{len(cards)} ({', '.join(names)}{extra})"

    def _render_hand_summary(
        self,
        cards: list[dict[str, Any]],
        game_state: dict[str, Any],
        tokens: dict[str, str],
    ) -> str:
        if not cards:
            return "0"

        castable_names = self._castable_hand_names(game_state)
        rows = "".join(
            self._render_hand_badge(card, castable_names, game_state, tokens)
            for card in cards
            if isinstance(card, dict)
        )
        return (
            f"<div style='color:{tokens['muted']}; font-size:11px; margin-bottom:4px;'>{len(cards)} card(s)</div>"
            f"<table cellspacing='0' cellpadding='0' style='border-collapse:separate; border-spacing:0 4px; width:100%;'>{rows}</table>"
        )

    def _castable_hand_names(self, game_state: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        legal_actions = game_state.get("legal_actions")
        if not isinstance(legal_actions, list):
            return names
        for action in legal_actions:
            text = _str_value(action).strip()
            if not text.lower().startswith("cast "):
                continue
            if "[ok]" not in text.lower():
                continue
            card_name = text[5:].split("[", 1)[0].strip().lower()
            if card_name:
                names.add(card_name)
        return names

    def _render_hand_badge(
        self,
        card: dict[str, Any],
        castable_names: set[str],
        game_state: dict[str, Any],
        tokens: dict[str, str],
    ) -> str:
        name = _str_value(card.get("name"), "?")
        mana_cost = _str_value(card.get("mana_cost"))
        type_line = _str_value(card.get("type_line")).lower()
        status = self._hand_card_status(card, castable_names, game_state)

        if status == "castable":
            background = tokens["castable_bg"]
            foreground = tokens["castable_fg"]
            border = tokens["castable_fg"]
        elif status == "uncastable":
            background = tokens["uncastable_bg"]
            foreground = tokens["uncastable_fg"]
            border = tokens["uncastable_fg"]
        elif "land" in type_line:
            background = tokens["panel2"]
            foreground = tokens["land"]
            border = tokens["land"]
        else:
            background = tokens["hand_neutral_bg"]
            foreground = tokens["hand_neutral_fg"]
            border = tokens["border"]

        detail = html.escape(mana_cost) if mana_cost else ""
        type_label = "LAND" if status == "land" else "CAST" if status == "castable" else "NO MANA" if status == "uncastable" else ""
        detail_cell = (
            f"<td style='padding:4px 8px; color:{foreground}; opacity:0.9; white-space:nowrap;'>{detail}</td>"
            if detail
            else "<td></td>"
        )
        type_cell = (
            f"<td style='padding:4px 8px; color:{foreground}; opacity:0.9; white-space:nowrap; text-align:right;'>{html.escape(type_label)}</td>"
            if type_label
            else "<td></td>"
        )
        return (
            f"<tr>"
            f"<td colspan='3' style='padding:0;'>"
            f"<table cellspacing='0' cellpadding='0' style='border-collapse:collapse; width:100%; "
            f"border:1px solid {border}; border-left:4px solid {border}; border-radius:7px; background:{background};'>"
            f"<tr>"
            f"<td style='padding:4px 8px; color:{foreground}; font-weight:700;'>{html.escape(name)}</td>"
            f"{detail_cell}"
            f"{type_cell}"
            f"</tr>"
            f"</table>"
            f"</td>"
            f"</tr>"
        )

    def _hand_card_status(
        self,
        card: dict[str, Any],
        castable_names: set[str],
        game_state: dict[str, Any],
    ) -> str:
        name = _str_value(card.get("name")).strip().lower()
        mana_cost = _str_value(card.get("mana_cost"))
        type_line = _str_value(card.get("type_line")).lower()

        if "land" in type_line:
            return "land"
        if name and name in castable_names:
            return "castable"
        if not mana_cost:
            return "neutral"

        local_seat = next(
            (
                _int_value(player.get("seat_id"))
                for player in game_state.get("players", [])
                if isinstance(player, dict) and _bool_value(player.get("is_local"))
            ),
            0,
        )
        if not local_seat:
            return "neutral"

        try:
            mana_pool = RulesEngine._get_mana_pool(game_state, local_seat)
        except Exception:
            return "neutral"
        return "castable" if RulesEngine._can_afford(mana_cost, mana_pool) else "uncastable"

    def _library_value(self, zones: dict[str, Any], opponent: bool) -> str:
        if opponent:
            hand_count = zones.get("opponent_hand_count")
            return "?" if hand_count is None else "Unknown"
        library_count = zones.get("library_count")
        if library_count is None:
            return "?"
        return f"{_int_value(library_count)}"

    def _format_non_loyalty_counters(self, counters: Any) -> str:
        if not isinstance(counters, dict):
            return ""
        parts = []
        for name, value in counters.items():
            if name == "Loyalty":
                continue
            parts.append(f"{name} {_int_value(value)}")
        return ", ".join(parts)

    def _update_game_state(self, data: dict[str, Any]) -> None:
        self._last_game_state_payload = data
        html_view = self._build_game_state_html(data)
        self._last_game_state = html_view
        self.game_state_view.setHtml(html_view)


def _str_value(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _int_value(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict):
        nested = value.get("value")
        return _int_value(nested, fallback)
    return fallback


def _bool_value(value: Any, fallback: bool = False) -> bool:
    return value if isinstance(value, bool) else fallback
