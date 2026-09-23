"""Compact, svelte sidebar HUD for MTGA Coach."""

from __future__ import annotations

import contextlib
import html
import logging
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from arenamcp import settings as settings

from .brain_stream_window import BrainStreamWindow
from .coach_session import CoachSession
from .conversation_transcript import ConversationTranscript
from .flow_layout import FlowLayout

logger = logging.getLogger(__name__)

CONVERSATION_MODES = ("turn_advice", "conversation")
VERBOSITY_LEVELS = ("quiet", "balanced", "detailed")
_CONVO_STATES = ("idle", "listening", "thinking", "speaking")


def _str_value(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CompactCoachPanel(QWidget):
    # Panel-level dynamic property for the conversation-mode visual identity:
    # set on every mode ack, repolished with the rest of the panel styles.
    OBJECT_NAME = "CompactCoachPanel"
    """Svelte, single-column sidebar layout of the MTGA Coach HUD (~260-440px wide)."""

    repair_requested = Signal()
    performance_requested = Signal()
    restart_requested = Signal()

    _PERTINENT_LOG_ROLES = frozenset({"spoken", "error", "status", "advice"})

    _LOG_COLORS_DARK = {
        "spoken": "#69d46c",
        "advice": "#89b4fa",
        "error": "#f38ba8",
        "status": "#cdd6f4",
        "info": "#a6adc8",
        "debug": "#6c7086",
    }

    _LOG_COLORS_LIGHT = {
        "spoken": "#1b7e2c",
        "advice": "#1e66f5",
        "error": "#d20f39",
        "status": "#4c4f69",
        "info": "#6c6f85",
        "debug": "#9ca0b0",
    }

    def __init__(self, session: CoachSession | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings.get_settings()
        self._session = session or CoachSession(self)
        self._dot_values: dict[str, str] = {}
        self._buttons: dict[str, Any] = {}
        self._game_plan: dict[str, Any] = {}
        self._latest_advice: tuple[str, str] | None = None
        self._debug_logging = bool(self._settings.get("desktop_debug_logging", False))
        self._activity_history: list[tuple[str, str]] = []
        self._conversation_mode = str(
            self._settings.get("conversation_mode", "turn_advice") or "turn_advice"
        )
        self._conversation_verbosity = str(
            self._settings.get("conversation_verbosity", "balanced") or "balanced"
        )

        self._brain_stream_window: BrainStreamWindow | None = None

        self._build_ui()
        self._wire_session()
        self._apply_compact_style()

    @property
    def session(self) -> CoachSession:
        return self._session

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 1. Turn strip: turn number + active player + phase
        self.turn_strip = QLabel("Waiting for MTGA…")
        self.turn_strip.setObjectName("turnStrip")
        self.turn_strip.setProperty("who", "none")
        self.turn_strip.setAlignment(Qt.AlignCenter)
        self.turn_strip.setWordWrap(True)
        root.addWidget(self.turn_strip)

        # 2. Status dots: Model / Bridge / Seat indicators
        self.status_dots = QLabel()
        self.status_dots.setObjectName("statusDots")
        self.status_dots.setTextFormat(Qt.RichText)
        self.status_dots.setAlignment(Qt.AlignCenter)
        self.status_dots.setWordWrap(True)
        root.addWidget(self.status_dots)

        # Conversation status line (idle / listening / thinking / speaking)
        self.conversation_status_label = QLabel()
        self.conversation_status_label.setObjectName("conversationStatusLabel")
        self.conversation_status_label.setAlignment(Qt.AlignCenter)
        self.conversation_status_label.hide()
        root.addWidget(self.conversation_status_label)

        # 3. Main content splitter: Game State + Advice & Speech Feed
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.setObjectName("compactSplitter")
        self.main_splitter.setChildrenCollapsible(False)

        # Game State View (HTML summary of Hero, Hand, and Battlefield lanes)
        self.game_state_view = QTextEdit()
        self.game_state_view.setObjectName("gameStateView")
        self.game_state_view.setReadOnly(True)
        self.game_state_view.setMinimumHeight(140)
        self.main_splitter.addWidget(self.game_state_view)

        # Turn Plan & Activity Container
        activity_container = QWidget()
        act_layout = QVBoxLayout(activity_container)
        act_layout.setContentsMargins(0, 0, 0, 0)
        act_layout.setSpacing(4)

        # MCTS Best Line Pill (Click to open Brain Stream)
        self.mcts_pill_label = QLabel()
        self.mcts_pill_label.setObjectName("mctsPillLabel")
        self.mcts_pill_label.setWordWrap(True)
        self.mcts_pill_label.setTextFormat(Qt.RichText)
        self.mcts_pill_label.setCursor(Qt.PointingHandCursor)
        self.mcts_pill_label.setToolTip("Click to open full MCTS Decision Tree & Brain Stream Inspector (Ctrl+B)")
        self.mcts_pill_label.mousePressEvent = lambda _e: self.toggle_brain_stream()
        self.mcts_pill_label.hide()
        act_layout.addWidget(self.mcts_pill_label)

        # Turn Plan Header & Label
        self.turn_plan_label = QLabel()
        self.turn_plan_label.setObjectName("turnPlanLabel")
        self.turn_plan_label.setWordWrap(True)
        self.turn_plan_label.setTextFormat(Qt.RichText)
        self.turn_plan_label.hide()
        act_layout.addWidget(self.turn_plan_label)

        # Conversation transcript (visible only in conversation mode)
        self.conversation_transcript = ConversationTranscript()
        self.conversation_transcript.setObjectName("conversationTranscript")
        self.conversation_transcript.setMinimumHeight(120)
        self.conversation_transcript.hide()
        act_layout.addWidget(self.conversation_transcript)

        # Speech & Advice Subtitle Log
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        act_layout.addWidget(self.log_view)

        self.main_splitter.addWidget(activity_container)
        self.main_splitter.setSizes([260, 240])
        root.addWidget(self.main_splitter, stretch=1)

        # 4. Controls Bar (AP toggle, Brain Stream, Bug Report / Voice, Style, Mute)
        ctrl_row1 = FlowLayout()
        ctrl_row1.setSpacing(5)

        self.ap_btn = QPushButton("AP: OFF")
        self.ap_btn.setObjectName("apButton")
        self.ap_btn.setProperty("apOn", "false")
        self.ap_btn.setToolTip("Toggle autoplay — automatically plays the current match")
        self.ap_btn.clicked.connect(self._session.toggle_autopilot)
        ctrl_row1.addWidget(self.ap_btn)
        self._buttons["toggle_autopilot"] = self.ap_btn

        self.brain_stream_btn = QPushButton("🧠 Brain Stream")
        self.brain_stream_btn.setObjectName("brainStreamButton")
        self.brain_stream_btn.setToolTip("Open/Toggle the live Brain Stream Inspector window (Ctrl+B)")
        self.brain_stream_btn.clicked.connect(self.toggle_brain_stream)
        ctrl_row1.addWidget(self.brain_stream_btn)

        self.bug_report_btn = QPushButton("🐞 Report")
        self.bug_report_btn.setObjectName("bugReportButton")
        self.bug_report_btn.setToolTip("Capture and submit bug report snapshot package (Ctrl+Shift+D)")
        self.bug_report_btn.clicked.connect(self._session.trigger_debug_report)
        ctrl_row1.addWidget(self.bug_report_btn)

        # Conversation Mode switch: toggles turn_advice <-> conversation.
        # Label shows the CURRENT mode (unambiguous), highlight shows on-state.
        self.mode_btn = QPushButton("Mode: Turn Advice")
        self.mode_btn.setObjectName("modeButton")
        self.mode_btn.setProperty("convoOn", "false")
        self.mode_btn.setToolTip("Switch between Conversation mode and Turn Advice mode")
        self.mode_btn.clicked.connect(self._toggle_mode)
        ctrl_row1.addWidget(self.mode_btn)

        # Verbosity cycler (quiet -> balanced -> detailed)
        saved_verbosity = str(self._settings.get("conversation_verbosity", "balanced") or "balanced")
        self.verbosity_btn = QPushButton(f"Detail: {saved_verbosity.capitalize()}")
        self.verbosity_btn.setObjectName("verbosityButton")
        self.verbosity_btn.setToolTip("Cycle conversation verbosity (Quiet / Balanced / Detailed)")
        self.verbosity_btn.clicked.connect(self._cycle_verbosity)
        ctrl_row1.addWidget(self.verbosity_btn)

        # Stop-speaking (works in both modes)
        self.stop_speech_btn = QPushButton("⏹ Stop")
        self.stop_speech_btn.setObjectName("stopSpeechButton")
        self.stop_speech_btn.setToolTip("Stop current speech playback")
        self.stop_speech_btn.clicked.connect(self._session.stop_speaking)
        ctrl_row1.addWidget(self.stop_speech_btn)

        root.addLayout(ctrl_row1)

        ctrl_row2 = FlowLayout()
        ctrl_row2.setSpacing(5)

        self.voice_btn = QPushButton("Voice: Auto")
        self.voice_btn.setObjectName("voiceButton")
        self.voice_btn.setToolTip("Cycle TTS voice (Sky / Alloy / Echo / Nova / etc.)")
        self.voice_btn.clicked.connect(self._session.cycle_voice)
        ctrl_row2.addWidget(self.voice_btn)
        self._buttons["cycle_voice"] = self.voice_btn

        saved_speed = settings.get_settings().get("voice_speed", 1.0)
        self.speed_btn = QPushButton(f"Speed: {saved_speed}x")
        self.speed_btn.setObjectName("speedButton")
        self.speed_btn.setToolTip("Cycle TTS voice speed (0.8x, 1.0x, 1.2x, 1.5x)")
        self.speed_btn.clicked.connect(self._session.cycle_speed)
        ctrl_row2.addWidget(self.speed_btn)
        self._buttons["cycle_speed"] = self.speed_btn

        self.style_btn = QPushButton("Quick")
        self.style_btn.setToolTip("Cycle advice style (Quick / Concise / Chatty)")
        self.style_btn.clicked.connect(self._session.toggle_style)
        ctrl_row2.addWidget(self.style_btn)
        self._buttons["toggle_style"] = self.style_btn

        self.mute_btn = QPushButton("Mute: Off")
        self.mute_btn.setToolTip("Mute / unmute spoken advice")
        self.mute_btn.clicked.connect(self._session.toggle_mute)
        ctrl_row2.addWidget(self.mute_btn)
        self._buttons["toggle_mute"] = self.mute_btn

        # Push-to-talk (hold to record, release to transcribe + send)
        self.ptt_btn = QPushButton("🎙 Hold to talk")
        self.ptt_btn.setObjectName("pttButton")
        from .ptt import PttController

        self._ptt_controller = PttController(
            self.ptt_btn, self._session, on_send=self._send_ptt_text
        )
        ctrl_row2.addWidget(self.ptt_btn)

        root.addLayout(ctrl_row2)

        # 5. Chat Input Bar
        chat_layout = QHBoxLayout()
        chat_layout.setSpacing(4)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Ask coach or /report…")
        self.chat_input.returnPressed.connect(self.send_chat)
        chat_layout.addWidget(self.chat_input, stretch=1)

        send_btn = QPushButton("Send")
        send_btn.setObjectName("sendButton")
        send_btn.clicked.connect(self.send_chat)
        chat_layout.addWidget(send_btn)

        root.addLayout(chat_layout)

    def _wire_session(self) -> None:
        self._session.gameStateChanged.connect(self._on_game_state_changed)
        self._session.turnPlanChanged.connect(self._on_turn_plan_changed)
        self._session.gamePlanChanged.connect(self._on_game_plan_changed)
        self._session.statusChanged.connect(self._on_status_changed)
        self._session.spokenLine.connect(self._on_spoken_line)
        self._session.adviceReceived.connect(self._on_advice_received)
        self._session.logEmitted.connect(self._on_log_emitted)
        self._session.errorOccurred.connect(self._on_error_occurred)
        self._session.telemetryUpdated.connect(self._on_telemetry_updated)
        self._session.reasoningChunk.connect(self._on_reasoning_chunk)
        self._session.mctsUpdated.connect(self._on_mcts_updated)
        self._session.bugReportSaved.connect(self._on_bug_report_saved)
        self._session.modeChanged.connect(self._on_mode_changed)
        self._session.conversationReply.connect(self._on_conversation_reply)
        self._session.conversationStatus.connect(self._on_conversation_status)
        fallback_notice = getattr(self._session, "localFallbackNotice", None)
        if fallback_notice is not None and hasattr(fallback_notice, "connect"):
            fallback_notice.connect(self._on_local_fallback_notice)
        self._session.started.connect(self._sync_conversation_prefs)

    def _on_game_state_changed(self, state: dict[str, Any]) -> None:
        self.update_turn_strip(state)
        html_content = self._format_game_state_html(state)
        self.game_state_view.setHtml(html_content)
        if self._brain_stream_window and self._brain_stream_window.isVisible():
            self._brain_stream_window.update_game_state(state)

        turn = state.get("turn") if isinstance(state, dict) else None
        if not turn:
            self.mcts_pill_label.hide()

    def _on_mcts_updated(self, payload: Any) -> None:
        if self._brain_stream_window and self._brain_stream_window.isVisible():
            self._brain_stream_window.update_mcts_tree(payload)

        if not payload:
            self.mcts_pill_label.hide()
            return

        if isinstance(payload, dict):
            data = payload
        elif hasattr(payload, "to_dict"):
            data = payload.to_dict()
        else:
            self.mcts_pill_label.hide()
            return

        branches = data.get("branches") or []
        blunders = data.get("blunder_traps") or []
        best_action = data.get("best_action") or ""

        if not branches and not best_action:
            self.mcts_pill_label.hide()
            return

        best_branch = branches[0] if branches else None
        src_tag = "Lookahead"
        src_color = "#a6adc8"
        line_title = "🌳 Tactical Line"

        if best_branch:
            win_p = float(best_branch.get("normalized_score", best_branch.get("win_probability", 0.5)))
            win_pct = int(round(win_p * 100))
            delta = float(best_branch.get("value_delta", 0.0))
            delta_str = (
                f"+{delta * 100:.1f}%" if delta > 0 else (f"{delta * 100:.1f}%" if delta != 0 else "0%")
            )
            steps = best_branch.get("sequence_steps") or []
            if steps:
                action_text = " → ".join(str(s) for s in steps[:3])
            else:
                action_text = str(best_branch.get("action") or best_action)
        else:
            win_pct = int(round(float(data.get("root_win_probability", 0.5)) * 100))
            delta_str = "0%"
            action_text = str(best_action)

        score_text = f"{win_pct}%"
        win_color = "#a6e3a1" if win_pct >= 55 else ("#f9e2af" if win_pct >= 45 else "#f38ba8")

        html_lines = [
            f"<div style='margin-bottom:2px;'>"
            f"  <span style='color:{src_color}; font-weight:700;'>{line_title}</span> "
            f"  <span style='color:{win_color}; font-weight:700;'>{score_text}</span> "
            f"  <span style='color:#a6adc8; font-size:10px;'>({delta_str})</span> "
            f"  <span style='color:#6c7086; font-size:9px; float:right;'>[{html.escape(src_tag)}]</span>"
            f"</div>",
            f"<div style='color:#cdd6f4; font-size:11px; line-height:1.2;'><b>⭐</b> {html.escape(action_text)}</div>",
        ]

        if blunders:
            trap_txt = str(blunders[0].get("action") or "")
            if trap_txt:
                html_lines.append(
                    f"<div style='color:#f38ba8; font-size:10px; margin-top:2px;'>⚠️ <b>Trap:</b> Avoid {html.escape(trap_txt[:45])}</div>"
                )

        self.mcts_pill_label.setText("".join(html_lines))
        self.mcts_pill_label.show()

    def _on_turn_plan_changed(self, plan: Any) -> None:
        if not plan:
            self.turn_plan_label.hide()
            return
        if isinstance(plan, dict):
            steps = plan.get("steps") or plan.get("actions") or []
            goal = plan.get("goal") or plan.get("overall_strategy") or ""
            items = []
            if goal:
                items.append(f"<b>Plan:</b> {html.escape(str(goal))}")
            if steps:
                step_strs = [html.escape(str(s)) for s in steps[:4]]
                items.append(" → ".join(step_strs))
            self.turn_plan_label.setText(
                "<div style='font-size:11px; margin-bottom:4px;'>" + "<br>".join(items) + "</div>"
            )
            self.turn_plan_label.show()
        elif isinstance(plan, str) and plan.strip():
            self.turn_plan_label.setText(
                f"<div style='font-size:11px;'><b>Plan:</b> {html.escape(plan)}</div>"
            )
            self.turn_plan_label.show()
        else:
            self.turn_plan_label.hide()

    def _on_game_plan_changed(self, plan: Any) -> None:
        if isinstance(plan, dict):
            self._game_plan = plan

    def _on_status_changed(self, key: str, val: str) -> None:
        self._dot_values[key] = val
        self._refresh_status_dots()

        if key == "AUTOPILOT":
            paused = "PAUSED" in val
            ap_on = "ON" in val or paused
            self.ap_btn.setText("AP: PAUSED" if paused else "AP: ON" if ap_on else "AP: OFF")
            self.ap_btn.setToolTip(
                "Paused: see the latest autoplay notice. Toggle off/on to retry."
                if paused else "Toggle autoplay"
            )
            self.ap_btn.setProperty("apOn", "true" if ap_on else "false")
            self._repolish(self.ap_btn)
        elif key == "MODE":
            self._on_mode_changed(val)
        elif key == "VERBOSITY":
            verbosity = val.strip().lower()
            if verbosity in VERBOSITY_LEVELS:
                self._conversation_verbosity = verbosity
                self.verbosity_btn.setText(f"Detail: {verbosity.capitalize()}")
        elif key == "STYLE":
            self.style_btn.setText(val)
        elif key == "MUTE":
            self.mute_btn.setText(f"Mute: {val}")
        elif key in ("VOICE", "VOICE_ID"):
            voice_name = val
            if voice_name.lower().startswith("changed to:"):
                voice_name = voice_name.split(":", 1)[1].strip()
            if "(saved)" in voice_name.lower():
                voice_name = voice_name.replace("(saved)", "").strip()
            if voice_name.lower().startswith("tts voice:"):
                voice_name = voice_name.split(":", 1)[1].strip()
            self.voice_btn.setText(f"Voice: {voice_name}")
        elif key in ("SPEED", "VOICE_SPEED"):
            speed_val = val
            if speed_val.lower().startswith("changed to:"):
                speed_val = speed_val.split(":", 1)[1].strip()
            if "(saved)" in speed_val.lower():
                speed_val = speed_val.replace("(saved)", "").strip()
            if not speed_val.endswith("x") and not speed_val.endswith("X"):
                speed_val = f"{speed_val}x"
            self.speed_btn.setText(f"Speed: {speed_val}")

    @property
    def conversation_mode(self) -> str:
        return self._conversation_mode

    def _toggle_mode(self) -> None:
        next_mode = "turn_advice" if self._conversation_mode == "conversation" else "conversation"
        self._session.set_mode(next_mode)
        self._settings.set("conversation_mode", next_mode)

    def _cycle_verbosity(self) -> None:
        try:
            idx = VERBOSITY_LEVELS.index(self._conversation_verbosity)
        except ValueError:
            idx = VERBOSITY_LEVELS.index("balanced")
        next_verbosity = VERBOSITY_LEVELS[(idx + 1) % len(VERBOSITY_LEVELS)]
        self._conversation_verbosity = next_verbosity
        self.verbosity_btn.setText(f"Detail: {next_verbosity.capitalize()}")
        self._session.set_verbosity(next_verbosity)
        self._settings.set("conversation_verbosity", next_verbosity)

    def _on_mode_changed(self, mode: str) -> None:
        mode = str(mode).strip()
        if mode not in CONVERSATION_MODES:
            return
        self._conversation_mode = mode
        in_convo = mode == "conversation"
        # Panel-wide conversation identity: tint border/background so the
        # whole HUD visibly changes theme in conversation mode, not just
        # the mode button (user request 2026-09-16).
        self.setProperty("convoActive", "true" if in_convo else "false")
        self.setObjectName(self.OBJECT_NAME)
        self.mode_btn.setText("Mode: Conversation" if in_convo else "Mode: Turn Advice")
        self.mode_btn.setProperty("convoOn", "true" if in_convo else "false")
        self._repolish(self.mode_btn)
        self._repolish(self)
        self.conversation_transcript.setVisible(in_convo)
        self.log_view.setVisible(not in_convo)
        self.conversation_status_label.setVisible(in_convo)

    def _on_conversation_reply(self, text: str) -> None:
        self.conversation_transcript.set_pending(False)
        self.conversation_transcript.add_entry("coach", text)

    def _on_local_fallback_notice(self, message: str) -> None:
        """[LOCAL FALLBACK] notices ride the conversation transcript."""
        self.conversation_transcript.set_pending(False)
        self.conversation_transcript.add_entry("system", str(message))
        self.append_log(str(message), role="error")

    def _on_conversation_status(self, state: str) -> None:
        state = str(state).strip().lower()
        if state not in _CONVO_STATES:
            return
        if state == "thinking":
            self.conversation_transcript.set_pending(True)
        elif state in ("idle", "speaking"):
            self.conversation_transcript.set_pending(False)
        self.conversation_status_label.setText(f"⏺ {state.capitalize()}")
        self.conversation_status_label.setProperty("convoState", state)
        self._repolish(self.conversation_status_label)

    def _send_ptt_text(self, text: str) -> None:
        self.conversation_transcript.add_entry("user", text)
        self.append_log(f"> {text}", role="status")
        self._session.send_chat(text)

    def _sync_conversation_prefs(self) -> None:
        """Sync saved mode/verbosity to the engine at session start."""
        saved_mode = str(self._settings.get("conversation_mode", "turn_advice") or "turn_advice")
        saved_verbosity = str(
            self._settings.get("conversation_verbosity", "balanced") or "balanced"
        )
        self._session.set_verbosity(saved_verbosity)
        if saved_mode != self._conversation_mode:
            self._session.set_mode(saved_mode)

    def _on_bug_report_saved(self, path: str, error: str) -> None:
        if error or not path:
            self.append_log(f"Failed to save bug report: {error or 'no path'}", role="error")
            return

        from pathlib import Path
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        p = Path(path).resolve()
        file_url = p.as_uri()

        try:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(file_url)
            self.append_log(
                f"🐞 Bug report saved! Local link copied to clipboard:\n{file_url}",
                role="status",
            )
        except Exception as exc:
            self.append_log(
                f"🐞 Bug report saved to: {path} (clipboard copy failed: {exc})",
                role="status",
            )

        original_text = "🐞 Report"
        self.bug_report_btn.setText("🐞 Copied!")
        QTimer.singleShot(3000, lambda: self.bug_report_btn.setText(original_text))

    def _on_spoken_line(self, text: str) -> None:
        self.append_log(text, role="spoken")

    def _on_advice_received(self, text: str, label: str) -> None:
        self._latest_advice = (text, label)
        if self._debug_logging:
            self.append_log(f"[{label}] {text}", role="advice")

    def _on_log_emitted(self, msg: str, role: str) -> None:
        if role in self._PERTINENT_LOG_ROLES or self._debug_logging:
            self.append_log(msg, role=role)

    def _on_error_occurred(self, err: str) -> None:
        self.append_log(err, role="error")

    def _on_telemetry_updated(self, data: dict[str, Any]) -> None:
        if self._brain_stream_window and self._brain_stream_window.isVisible():
            self._brain_stream_window.update_telemetry(
                latency=data.get("latency_ms", ""),
                backend=data.get("model", ""),
                bridge_connected=data.get("bridge_connected", False),
            )

    def _on_reasoning_chunk(self, chunk: str) -> None:
        if self._brain_stream_window and self._brain_stream_window.isVisible():
            self._brain_stream_window.append_reasoning_chunk(chunk)

    def append_log(self, text: str, role: str = "status") -> None:
        """Prepend a colored line to the subtitle & activity feed (newest on top)."""
        self._activity_history.insert(0, (text, role))
        if len(self._activity_history) > 500:
            self._activity_history = self._activity_history[:500]

        t = self._theme_tokens()
        colors = self._LOG_COLORS_DARK if t["is_dark"] else self._LOG_COLORS_LIGHT
        color = colors.get(role, colors["status"])

        font_size = "13px" if role in ("spoken", "advice") else "11px"
        font_weight = "600" if role in ("spoken", "error", "advice") else "400"
        escaped = html.escape(text).replace("\n", "<br>")

        line_html = f"<div style='color:{color}; font-size:{font_size}; font-weight:{font_weight}; margin-bottom:4px;'>{escaped}</div>"

        doc = self.log_view.document()
        sb = self.log_view.verticalScrollBar()
        was_at_top = sb.value() <= 10

        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        if not doc.isEmpty():
            cursor.insertBlock()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.insertHtml(line_html)

        # Trim oldest entries from the bottom if exceeding 500 blocks
        while doc.blockCount() > 500:
            last_block = doc.lastBlock()
            del_cursor = QTextCursor(last_block)
            del_cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
            del_cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            del_cursor.removeSelectedText()

        # If the user was viewing the top, keep it pinned at the top so new advice is immediately visible
        if was_at_top:
            sb.setValue(sb.minimum())

    def update_turn_strip(self, game_state: dict[str, Any]) -> None:
        turn = game_state.get("turn") or {}
        turn_num = turn.get("turn_number") or game_state.get("turn_number", 0)
        phase = turn.get("phase") or game_state.get("phase", "") or ""
        phase_short = phase.replace("Phase_", "").replace("Step_", " ").strip()

        local_seat = game_state.get("local_seat_id")
        active_player = turn.get("active_player")
        match_id = game_state.get("match_id")
        pending = str(game_state.get("pending_decision") or "")

        if "mulligan" in pending.lower() or (match_id and not turn_num and not phase):
            self.turn_strip.setText("Game Starting · Mulligan")
            self.turn_strip.setProperty("who", "you")
        elif not turn_num and not phase:
            self.turn_strip.setText("Waiting for MTGA…")
            self.turn_strip.setProperty("who", "none")
        else:
            if active_player is not None and local_seat is not None:
                is_your_turn = int(active_player) == int(local_seat)
                who = "you" if is_your_turn else "opp"
                who_label = "Your Turn" if is_your_turn else "Opponent's Turn"
            else:
                who = "none"
                who_label = "Turn"

            phase_display = f" · {phase_short}" if phase_short else ""
            self.turn_strip.setText(f"{who_label} (T{turn_num}){phase_display}")
            self.turn_strip.setProperty("who", who)

        self._repolish(self.turn_strip)

    def _refresh_status_dots(self) -> None:
        import sys

        model = self._dot_values.get("MODEL") or "Local"
        bridge_val = self._dot_values.get("BRIDGE")
        bridge_on = bridge_val != "OFF" and bridge_val is not None
        seat = self._dot_values.get("SEAT") or "?"

        t = self._theme_tokens()
        green = t["castable_fg"]
        red = t["uncastable_fg"]

        # On macOS, MTGA runs without BepInEx bridge, tracking game state via Player.log watcher
        is_mac = sys.platform == "darwin"
        if is_mac and not bridge_on:
            source_label = "Log Watcher"
            has_game = seat != "?" or self._dot_values.get("GAME") == "IN_MATCH"
            source_color = green if has_game else t["muted"]
        else:
            source_label = "Bridge"
            source_color = green if bridge_on else red

        html_dots = (
            f"<span style='color:{t['muted']};'>Backend: </span>"
            f"<span style='color:{t['text']}; font-weight:600;'>{html.escape(model)}</span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<span style='color:{source_color}; font-size:13px;'>●</span>"
            f"<span style='color:{t['text']};'> {source_label}</span>"
        )
        self.status_dots.setText(html_dots)

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        if text.lower() in ("/report", "/bug", "/debug", "/bugreport", "/debugreport"):
            self._session.trigger_debug_report()
            return
        if self._conversation_mode == "conversation":
            self.conversation_transcript.add_entry("user", text)
            self._session.send_chat(text)
            return
        self.append_log(f"> {text}", role="status")
        self._session.send_chat(text)

    def toggle_brain_stream(self) -> None:
        if self._brain_stream_window is None:
            self._brain_stream_window = BrainStreamWindow(self)
        if self._brain_stream_window.isVisible():
            self._brain_stream_window.hide()
        else:
            self._brain_stream_window.show()
            self._brain_stream_window.raise_()
            self._brain_stream_window.activateWindow()

    def set_debug_logging(self, enabled: bool) -> None:
        self._debug_logging = bool(enabled)
        self._settings.set("desktop_debug_logging", self._debug_logging)

    def _theme_tokens(self) -> dict[str, Any]:
        from .theme import get_theme_tokens

        return get_theme_tokens(self)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def _apply_compact_style(self) -> None:
        t = self._theme_tokens()
        accent = t["spell"]
        self.setStyleSheet(
            f"""
#CompactCoachPanel[convoActive="true"] {{
    border: 2px solid {t["castable_fg"]};
    border-radius: 10px;
    background: {t["castable_bg"]};
}}
#CompactCoachPanel[convoActive="false"] {{
    border: none;
}}
#turnStrip {{
    background: {t["panel2"]};
    color: {t["header"]};
    border: 1px solid {t["border"]};
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 13px;
    font-weight: 700;
}}
#turnStrip[who="you"] {{
    background: {t["castable_bg"]};
    color: {t["castable_fg"]};
    border-color: {t["castable_fg"]};
}}
#turnStrip[who="opp"] {{
    background: {t["uncastable_bg"]};
    color: {t["uncastable_fg"]};
    border-color: {t["uncastable_fg"]};
}}
#statusDots {{
    font-size: 11px;
    padding: 0 2px;
}}
QTextEdit#gameStateView, QTextEdit#logView {{
    border: 1px solid {t["border"]};
    border-radius: 8px;
    background: {t["bg"]};
    padding: 4px;
}}
QSplitter#compactSplitter::handle {{
    background: {t["border"]};
    height: 4px;
    margin: 2px 0px;
    border-radius: 2px;
}}
QSplitter#compactSplitter::handle:hover {{
    background: {accent};
}}
QPushButton#apButton {{
    font-weight: 700;
}}
QPushButton#apButton[apOn="true"] {{
    background: {t["castable_bg"]};
    color: {t["castable_fg"]};
    border: 1px solid {t["castable_fg"]};
}}
QPushButton#brainStreamButton {{
    font-weight: 600;
    color: {accent};
    border: 1px solid {accent};
    border-radius: 6px;
    padding: 4px 6px;
}}
QPushButton#modeButton {{
    font-weight: 700;
}}
QPushButton#modeButton[convoOn="true"] {{
    background: {t["castable_bg"]};
    color: {t["castable_fg"]};
    border: 1px solid {t["castable_fg"]};
}}
QPushButton#stopSpeechButton {{
    font-weight: 600;
}}
QTextEdit#conversationTranscript {{
    border: 1px solid {t["border"]};
    border-radius: 8px;
    background: {t["bg"]};
    padding: 4px;
}}
QLabel#conversationStatusLabel {{
    color: {t["muted"]};
    font-size: 11px;
}}
QPushButton#bugReportButton {{
    color: {t["uncastable_fg"]};
    border: 1px solid {t["uncastable_fg"]};
    border-radius: 6px;
    font-weight: 600;
    padding: 4px 6px;
}}
QPushButton#sendButton {{
    font-weight: 600;
}}
#mctsPillLabel {{
    background: {t["panel2"]};
    border: 1px solid {t["border"]};
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 11px;
}}
#mctsPillLabel:hover {{
    border-color: {accent};
    background: {t["panel2"]};
}}
"""
        )

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
            with contextlib.suppress(Exception):
                self._apply_compact_style()
                self._refresh_status_dots()

    # -- HTML Game State Formatter --------------------------------------------

    def _format_game_state_html(self, state: dict[str, Any]) -> str:
        tokens = self._theme_tokens()
        local_seat = state.get("local_seat_id")
        players = state.get("players", [])

        you = next((p for p in players if p.get("is_local") or p.get("seat_id") == local_seat), {})
        opp = next((p for p in players if p.get("seat_id") != you.get("seat_id")), {})

        you_life = you.get("life_total", 20)
        opp_life = opp.get("life_total", 20)

        hand = state.get("hand", [])
        battlefield = state.get("battlefield", [])

        your_bf = [
            c
            for c in battlefield
            if c.get("controller_seat_id") == local_seat or c.get("owner_seat_id") == local_seat
        ]
        opp_bf = [
            c
            for c in battlefield
            if c.get("controller_seat_id") != local_seat and c.get("owner_seat_id") != local_seat
        ]

        # Render Opponent Summary
        opp_html = (
            f"<div style='margin-bottom:6px;'>"
            f"<span style='color:{tokens['uncastable_fg']}; font-weight:700;'>OPPONENT</span>"
            f"&nbsp;&nbsp;<span style='font-size:14px; font-weight:700; color:{tokens['uncastable_fg']};'>♥ {opp_life}</span>"
            f"&nbsp;&nbsp;<span style='color:{tokens['muted']}; font-size:11px;'>Board: {len(opp_bf)} cards</span>"
            f"</div>"
        )

        # Render You Summary
        you_html = (
            f"<div style='margin-bottom:6px;'>"
            f"<span style='color:{tokens['castable_fg']}; font-weight:700;'>YOU</span>"
            f"&nbsp;&nbsp;<span style='font-size:14px; font-weight:700; color:{tokens['castable_fg']};'>♥ {you_life}</span>"
            f"&nbsp;&nbsp;<span style='color:{tokens['muted']}; font-size:11px;'>Hand: {len(hand)} · Board: {len(your_bf)}</span>"
            f"</div>"
        )

        # Render Hand
        hand_items = []
        for c in hand:
            if isinstance(c, dict):
                name = html.escape(str(c.get("name") or "?"))
                cost = html.escape(str(c.get("mana_cost") or "").replace("{", "").replace("}", ""))
                hand_items.append(
                    f"<span style='color:{tokens['castable_fg']};'>{name}</span> <span style='color:{tokens['muted']};'>{cost}</span>"
                )
        hand_joined = " · ".join(hand_items) if hand_items else "<i>Empty</i>"
        hand_html = f"<div style='font-size:11px; margin-bottom:6px;'><b style='color:{tokens['muted']};'>HAND:</b> {hand_joined}</div>"

        # Render Battlefield Creatures & Lands
        creatures = [c for c in your_bf if "creature" in str(c.get("type_line", "")).lower()]
        lands = [c for c in your_bf if "land" in str(c.get("type_line", "")).lower()]

        c_strs = [
            f"{c.get('name', '?')} ({c.get('power', 0)}/{c.get('toughness', 0)})" for c in creatures[:6]
        ]
        l_strs = [str(c.get("name", "?")) for c in lands[:8]]

        board_html = (
            f"<div style='font-size:11px;'>"
            f"<b style='color:{tokens['muted']};'>CREATURES ({len(creatures)}):</b> {html.escape(' · '.join(c_strs)) if c_strs else 'None'}<br>"
            f"<b style='color:{tokens['muted']};'>LANDS ({len(lands)}):</b> {html.escape(' · '.join(l_strs)) if l_strs else 'None'}"
            f"</div>"
        )

        return f"<div style='font-family:sans-serif;'>{opp_html}{you_html}{hand_html}{board_html}</div>"
