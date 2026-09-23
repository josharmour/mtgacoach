"""Compact, svelte sidebar HUD for MTGA Coach."""

from __future__ import annotations

import datetime
import logging
import re
import sys
from collections import Counter
from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from arenamcp import settings as settings

from . import theme
from .coach_session import CoachSession
from .conversation_transcript import ConversationTranscript
from .flow_layout import FlowLayout
from .theme import block, span

logger = logging.getLogger(__name__)

CONVERSATION_MODES = ("turn_advice", "conversation")
VERBOSITY_LEVELS = ("quiet", "balanced", "detailed")
_CONVO_STATES = ("idle", "listening", "thinking", "speaking")
MAX_FEED_LINES = 500

# Feed role → (tone, size, weight).
_FEED_STYLE = {
    "spoken": ("text", "body", 600),
    "advice": ("accent", "body", 400),
    "error": ("bad", "body", 600),
    "status": ("text", "caption", 400),
    "info": ("muted", "caption", 400),
    "debug": ("muted", "caption", 400),
}

_NOW_EMPTY = "Advice shows up here when you have a decision to make."
_BUG_REPORT_LABEL = "Report a bug  ·  F12"


def _str_value(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pretty_phase(phase: str) -> str:
    text = phase.replace("Phase_", "").replace("Step_", " ").strip()
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", text)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setProperty("role", "caption")
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setProperty("card", True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(4)
    return frame, layout


def _divider() -> QFrame:
    line = QFrame()
    line.setProperty("divider", True)
    return line


class VoiceStylePopover(QFrame):
    """Pop-up panel behind the ⋯ button: voice, speed, style, detail, mute.

    Clicking a setting cycles it in place; the panel stays open until the
    user clicks elsewhere.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("voiceStylePopover")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 8, 6, 6)
        self._layout.setSpacing(1)
        heading = _caption("Voice & style")
        heading.setContentsMargins(10, 0, 0, 4)
        self._layout.addWidget(heading)
        self.setMinimumWidth(220)

    def add_button(self, button: QPushButton) -> None:
        button.setProperty("variant", "menuitem")
        self._layout.addWidget(button)

    def add_divider(self) -> None:
        self._layout.addSpacing(3)
        self._layout.addWidget(_divider())
        self._layout.addSpacing(3)

    def popup_from(self, anchor: QWidget) -> None:
        self.adjustSize()
        top_right = anchor.mapToGlobal(QPoint(anchor.width(), 0))
        x = top_right.x() - self.width()
        y = top_right.y() - self.height() - 4
        screen = anchor.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            if y < avail.top():
                y = anchor.mapToGlobal(QPoint(0, anchor.height())).y() + 4
            x = max(avail.left(), min(x, avail.right() - self.width()))
        self.move(x, y)
        self.show()


class CompactCoachPanel(QWidget):
    """Svelte, single-column sidebar layout of the MTGA Coach HUD (~260-440px wide)."""

    repair_requested = Signal()
    performance_requested = Signal()
    restart_requested = Signal()

    _PERTINENT_LOG_ROLES = frozenset({"spoken", "error", "status", "advice"})

    def __init__(self, session: CoachSession | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings.get_settings()
        self._session = session or CoachSession(self)
        self._dot_values: dict[str, str] = {}
        self._game_plan: dict[str, Any] = {}
        self._latest_advice: tuple[str, str] | None = None
        self._debug_logging = bool(self._settings.get("desktop_debug_logging", False))
        # Chronological (oldest first): (HH:MM, text, role).
        self._activity_history: list[tuple[str, str, str]] = []
        self._last_state: dict[str, Any] = {}
        self._last_mcts: Any = None
        self._last_plan: Any = None
        self._now: tuple[str, str] | None = None
        self._conversation_mode = str(self._settings.get("conversation_mode", "turn_advice") or "turn_advice")
        self._conversation_verbosity = str(
            self._settings.get("conversation_verbosity", "balanced") or "balanced"
        )

        self._build_ui()
        self._wire_session()
        theme.on_theme_changed(self._restyle)
        self._apply_mode_ui(self._conversation_mode)

    @property
    def session(self) -> CoachSession:
        return self._session

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Turn strip: whose turn, turn number, phase.
        self.turn_strip = QLabel("Waiting for MTGA…")
        self.turn_strip.setObjectName("turnStrip")
        self.turn_strip.setProperty("who", "none")
        self.turn_strip.setAlignment(Qt.AlignCenter)
        self.turn_strip.setWordWrap(True)
        root.addWidget(self.turn_strip)

        # Status chips: model, game source, conversation mode.
        self.status_row = QWidget()
        chips = FlowLayout(self.status_row)
        chips.setSpacing(6)
        self.model_chip = self._make_chip()
        self.source_chip = self._make_chip()
        self.mode_chip = self._make_chip()
        self.mode_chip.setText("Chat mode")
        self.mode_chip.setProperty("tone", "convo")
        self.mode_chip.setToolTip("Conversation mode: ask by voice or text, replies land in the transcript")
        for chip in (self.model_chip, self.source_chip, self.mode_chip):
            chips.addWidget(chip)
        root.addWidget(self.status_row)

        # Now card: the latest spoken advice, then the tactical line and plan.
        self.now_card, now_layout = _card()
        now_head = QHBoxLayout()
        now_head.addWidget(_caption("Now"))
        now_head.addStretch()
        self.now_time = QLabel("")
        self.now_time.setProperty("role", "small")
        now_head.addWidget(self.now_time)
        now_layout.addLayout(now_head)

        self.now_text = QLabel(_NOW_EMPTY)
        self.now_text.setObjectName("nowText")
        self.now_text.setProperty("role", "emphasis")
        self.now_text.setProperty("empty", True)
        self.now_text.setWordWrap(True)
        self.now_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        now_layout.addWidget(self.now_text)

        self.now_divider = _divider()
        self.now_divider.hide()
        now_layout.addWidget(self.now_divider)

        self.mcts_pill_label = QLabel()
        self.mcts_pill_label.setObjectName("mctsPillLabel")
        self.mcts_pill_label.setWordWrap(True)
        self.mcts_pill_label.setTextFormat(Qt.RichText)
        self.mcts_pill_label.setToolTip("Best line from the heuristic tactical search, with its score")
        self.mcts_pill_label.hide()
        now_layout.addWidget(self.mcts_pill_label)

        self.turn_plan_label = QLabel()
        self.turn_plan_label.setObjectName("turnPlanLabel")
        self.turn_plan_label.setWordWrap(True)
        self.turn_plan_label.setTextFormat(Qt.RichText)
        self.turn_plan_label.hide()
        now_layout.addWidget(self.turn_plan_label)
        root.addWidget(self.now_card)

        # Board card: sized to its content.
        board_card, board_layout = _card()
        self.game_state_view = QLabel()
        self.game_state_view.setObjectName("gameStateView")
        self.game_state_view.setTextFormat(Qt.RichText)
        self.game_state_view.setWordWrap(True)
        self.game_state_view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        board_layout.addWidget(self.game_state_view)
        root.addWidget(board_card)

        # Feed card: chronological activity log, or the chat transcript.
        feed_card, feed_layout = _card()
        feed_head = QHBoxLayout()
        self.feed_caption = _caption("Feed")
        feed_head.addWidget(self.feed_caption)
        feed_head.addStretch()
        self.conversation_status_label = QLabel()
        self.conversation_status_label.setObjectName("conversationStatusLabel")
        self.conversation_status_label.setProperty("role", "small")
        self.conversation_status_label.hide()
        feed_head.addWidget(self.conversation_status_label)
        feed_layout.addLayout(feed_head)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setProperty("bare", True)
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(90)
        feed_layout.addWidget(self.log_view, 1)

        self.conversation_transcript = ConversationTranscript()
        self.conversation_transcript.setObjectName("conversationTranscript")
        self.conversation_transcript.setProperty("bare", True)
        self.conversation_transcript.setMinimumHeight(90)
        self.conversation_transcript.hide()
        feed_layout.addWidget(self.conversation_transcript, 1)
        root.addWidget(feed_card, stretch=1)

        # Mode switch (segmented) + Voice & style menu button.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self.advice_mode_btn = QPushButton("Advice")
        self.advice_mode_btn.setProperty("seg", "left")
        self.advice_mode_btn.setToolTip("Turn advice: the coach speaks up when you have a decision")
        self.chat_mode_btn = QPushButton("Chat")
        self.chat_mode_btn.setProperty("seg", "right")
        self.chat_mode_btn.setToolTip("Conversation: ask by voice or text; replies land in the transcript")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for button in (self.advice_mode_btn, self.chat_mode_btn):
            button.setCheckable(True)
            # Let the segments share whatever width the sidebar has.
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(40)
            self._mode_group.addButton(button)
            mode_row.addWidget(button, 1)
        self.advice_mode_btn.clicked.connect(lambda: self._select_mode("turn_advice"))
        self.chat_mode_btn.clicked.connect(lambda: self._select_mode("conversation"))
        mode_row.addSpacing(6)
        self.more_btn = QPushButton("⋯")
        self.more_btn.setObjectName("moreButton")
        self.more_btn.setToolTip("Voice & style: voice, speed, advice length, chat detail, mute, bug report")
        self.more_btn.clicked.connect(self._show_voice_style)
        mode_row.addWidget(self.more_btn)
        root.addLayout(mode_row)

        # Primary controls.
        controls = FlowLayout()
        controls.setSpacing(6)
        self.ap_btn = QPushButton("Autoplay: Off")
        self.ap_btn.setObjectName("apButton")
        self.ap_btn.setProperty("state", "off")
        self.ap_btn.setToolTip("Toggle autoplay — automatically plays the current match")
        self.ap_btn.clicked.connect(self._session.toggle_autopilot)
        controls.addWidget(self.ap_btn)

        self.ptt_btn = QPushButton("🎙 Hold to talk")
        self.ptt_btn.setObjectName("pttButton")
        self.ptt_btn.setProperty("variant", "primary")
        from .ptt import PttController

        self._ptt_controller = PttController(self.ptt_btn, self._session, on_send=self._send_ptt_text)
        controls.addWidget(self.ptt_btn)

        self.stop_speech_btn = QPushButton("■")
        self.stop_speech_btn.setObjectName("stopSpeechButton")
        self.stop_speech_btn.setToolTip("Stop speaking")
        self.stop_speech_btn.setAccessibleName("Stop speaking")
        self.stop_speech_btn.clicked.connect(self._session.stop_speaking)
        controls.addWidget(self.stop_speech_btn)
        root.addLayout(controls)

        # Chat input.
        chat_layout = QHBoxLayout()
        chat_layout.setSpacing(6)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Ask the coach, or /report…")
        self.chat_input.returnPressed.connect(self.send_chat)
        chat_layout.addWidget(self.chat_input, stretch=1)
        send_btn = QPushButton("Send")
        send_btn.setObjectName("sendButton")
        send_btn.clicked.connect(self.send_chat)
        chat_layout.addWidget(send_btn)
        root.addLayout(chat_layout)

        self._build_voice_style_popover()
        self._refresh_status_dots()
        self._render_board({})

    def _build_voice_style_popover(self) -> None:
        self.voice_style_popover = VoiceStylePopover(self)
        pop = self.voice_style_popover

        self.voice_btn = QPushButton("Voice: Auto")
        self.voice_btn.setObjectName("voiceButton")
        self.voice_btn.setToolTip("Cycle the text-to-speech voice")
        self.voice_btn.clicked.connect(self._session.cycle_voice)
        pop.add_button(self.voice_btn)

        saved_speed = settings.get_settings().get("voice_speed", 1.0)
        self.speed_btn = QPushButton(f"Speed: {saved_speed}x")
        self.speed_btn.setObjectName("speedButton")
        self.speed_btn.setToolTip("Cycle speech speed (0.8x, 1.0x, 1.2x, 1.5x)")
        self.speed_btn.clicked.connect(self._session.cycle_speed)
        pop.add_button(self.speed_btn)

        self.style_btn = QPushButton("Advice: Quick")
        self.style_btn.setObjectName("styleButton")
        self.style_btn.setToolTip("Turn-advice length: Quick (short, speakable) or Chatty (explains why)")
        self.style_btn.clicked.connect(self._session.toggle_style)
        pop.add_button(self.style_btn)

        saved_verbosity = str(self._settings.get("conversation_verbosity", "balanced") or "balanced")
        self.verbosity_btn = QPushButton(f"Chat detail: {saved_verbosity.capitalize()}")
        self.verbosity_btn.setObjectName("verbosityButton")
        self.verbosity_btn.setToolTip("Length of conversation replies (Quiet / Balanced / Detailed)")
        self.verbosity_btn.clicked.connect(self._cycle_verbosity)
        pop.add_button(self.verbosity_btn)

        self.mute_btn = QPushButton("Mute: Off")
        self.mute_btn.setObjectName("muteButton")
        self.mute_btn.setProperty("state", "off")
        self.mute_btn.setToolTip("Mute / unmute spoken advice")
        self.mute_btn.clicked.connect(self._session.toggle_mute)
        pop.add_button(self.mute_btn)

        pop.add_divider()
        self.bug_report_btn = QPushButton(_BUG_REPORT_LABEL)
        self.bug_report_btn.setObjectName("bugReportButton")
        self.bug_report_btn.setToolTip("Save a bug-report snapshot and copy its link (F12 or Ctrl+Shift+D)")
        self.bug_report_btn.clicked.connect(self._on_bug_report_clicked)
        pop.add_button(self.bug_report_btn)

    @staticmethod
    def _make_chip() -> QLabel:
        chip = QLabel()
        chip.setProperty("chip", True)
        chip.setTextFormat(Qt.RichText)
        return chip

    def _show_voice_style(self) -> None:
        self.voice_style_popover.popup_from(self.more_btn)

    def _on_bug_report_clicked(self) -> None:
        self.voice_style_popover.hide()
        self._session.trigger_debug_report()

    # ------------------------------------------------------------------
    # Session wiring
    # ------------------------------------------------------------------
    def _wire_session(self) -> None:
        self._session.gameStateChanged.connect(self._on_game_state_changed)
        self._session.turnPlanChanged.connect(self._on_turn_plan_changed)
        self._session.gamePlanChanged.connect(self._on_game_plan_changed)
        self._session.statusChanged.connect(self._on_status_changed)
        self._session.spokenLine.connect(self._on_spoken_line)
        self._session.adviceReceived.connect(self._on_advice_received)
        self._session.logEmitted.connect(self._on_log_emitted)
        self._session.errorOccurred.connect(self._on_error_occurred)
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
        self._last_state = state if isinstance(state, dict) else {}
        self.update_turn_strip(self._last_state)
        self._render_board(self._last_state)
        if not self._last_state.get("turn"):
            self._last_mcts = None
            self.mcts_pill_label.hide()
            self._sync_now_divider()

    def _render_board(self, state: dict[str, Any]) -> None:
        self.game_state_view.setText(self._format_game_state_html(state))

    def _on_mcts_updated(self, payload: Any) -> None:
        self._last_mcts = payload
        self._render_tactical_line()

    def _render_tactical_line(self) -> None:
        payload = self._last_mcts
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        if not payload or not isinstance(payload, dict):
            self.mcts_pill_label.hide()
            self._sync_now_divider()
            return

        branches = payload.get("branches") or []
        blunders = payload.get("blunder_traps") or []
        best_action = payload.get("best_action") or ""
        if not branches and not best_action:
            self.mcts_pill_label.hide()
            self._sync_now_divider()
            return

        best_branch = branches[0] if branches else None
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
            win_pct = int(round(float(payload.get("root_win_probability", 0.5)) * 100))
            delta_str = "0%"
            action_text = str(best_action)

        score_tone = "good" if win_pct >= 55 else ("warn" if win_pct >= 45 else "bad")
        lines = [
            block(
                span("Tactical line", "muted", weight=700)
                + "&nbsp;&nbsp;"
                + span(f"{win_pct}%", score_tone, weight=700)
                + "&nbsp;"
                + span(f"({delta_str})", "muted"),
                size="caption",
                gap=2,
            ),
            block(span(action_text, "text"), size="caption"),
        ]
        if blunders:
            trap_txt = str(blunders[0].get("action") or "")
            if trap_txt:
                lines.append(block(span(f"Avoid: {trap_txt[:60]}", "warn"), size="caption"))

        self.mcts_pill_label.setText("".join(lines))
        self.mcts_pill_label.show()
        self._sync_now_divider()

    def _on_turn_plan_changed(self, plan: Any) -> None:
        self._last_plan = plan
        self._render_turn_plan()

    def _render_turn_plan(self) -> None:
        plan = self._last_plan
        label = span("Plan", "muted", weight=700) + "&nbsp;&nbsp;"
        text = ""
        if isinstance(plan, dict):
            steps = plan.get("steps") or plan.get("actions") or []
            goal = plan.get("goal") or plan.get("overall_strategy") or ""
            items = []
            if goal:
                items.append(label + span(goal))
            if steps:
                items.append(span(" → ".join(str(s) for s in steps[:4]), "muted"))
            text = block("<br>".join(items), size="caption") if items else ""
        elif isinstance(plan, str) and plan.strip():
            text = block(label + span(plan), size="caption")

        if text:
            self.turn_plan_label.setText(text)
            self.turn_plan_label.show()
        else:
            self.turn_plan_label.hide()
        self._sync_now_divider()

    def _sync_now_divider(self) -> None:
        self.now_divider.setVisible(
            not self.mcts_pill_label.isHidden() or not self.turn_plan_label.isHidden()
        )

    def _on_game_plan_changed(self, plan: Any) -> None:
        if isinstance(plan, dict):
            self._game_plan = plan

    def _on_status_changed(self, key: str, val: str) -> None:
        self._dot_values[key] = val
        self._refresh_status_dots()

        if key == "AUTOPILOT":
            paused = "PAUSED" in val
            ap_on = "ON" in val or paused
            state = "paused" if paused else "on" if ap_on else "off"
            self.ap_btn.setText(f"Autoplay: {state.capitalize()}")
            self.ap_btn.setToolTip(
                "Paused: see the latest autoplay notice. Toggle off/on to retry."
                if paused
                else "Toggle autoplay — automatically plays the current match"
            )
            self.ap_btn.setProperty("state", state)
            theme.repolish(self.ap_btn)
        elif key == "MODE":
            self._on_mode_changed(val)
        elif key == "VERBOSITY":
            verbosity = val.strip().lower()
            if verbosity in VERBOSITY_LEVELS:
                self._conversation_verbosity = verbosity
                self.verbosity_btn.setText(f"Chat detail: {verbosity.capitalize()}")
        elif key == "STYLE":
            self.style_btn.setText(f"Advice: {val}")
        elif key == "MUTE":
            self.mute_btn.setText(f"Mute: {val}")
            self.mute_btn.setProperty("state", "on" if val.strip().lower() == "on" else "off")
            theme.repolish(self.mute_btn)
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

    # ------------------------------------------------------------------
    # Conversation mode
    # ------------------------------------------------------------------
    @property
    def conversation_mode(self) -> str:
        return self._conversation_mode

    def _select_mode(self, mode: str) -> None:
        if mode == self._conversation_mode:
            return
        self._session.set_mode(mode)
        self._settings.set("conversation_mode", mode)

    def _cycle_verbosity(self) -> None:
        try:
            idx = VERBOSITY_LEVELS.index(self._conversation_verbosity)
        except ValueError:
            idx = VERBOSITY_LEVELS.index("balanced")
        next_verbosity = VERBOSITY_LEVELS[(idx + 1) % len(VERBOSITY_LEVELS)]
        self._conversation_verbosity = next_verbosity
        self.verbosity_btn.setText(f"Chat detail: {next_verbosity.capitalize()}")
        self._session.set_verbosity(next_verbosity)
        self._settings.set("conversation_verbosity", next_verbosity)

    def _on_mode_changed(self, mode: str) -> None:
        mode = str(mode).strip()
        if mode not in CONVERSATION_MODES:
            return
        self._conversation_mode = mode
        self._apply_mode_ui(mode)

    def _apply_mode_ui(self, mode: str) -> None:
        in_convo = mode == "conversation"
        (self.chat_mode_btn if in_convo else self.advice_mode_btn).setChecked(True)
        self.feed_caption.setText("CONVERSATION" if in_convo else "FEED")
        self.mode_chip.setVisible(in_convo)
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
        self.conversation_status_label.setText(f"● {state.capitalize()}")
        self.conversation_status_label.setProperty("convoState", state)

    def _send_ptt_text(self, text: str) -> None:
        self.conversation_transcript.add_entry("user", text)
        self.append_log(f"> {text}", role="status")
        self._session.send_chat(text)

    def _sync_conversation_prefs(self) -> None:
        """Sync saved mode/verbosity to the engine at session start."""
        saved_mode = str(self._settings.get("conversation_mode", "turn_advice") or "turn_advice")
        saved_verbosity = str(self._settings.get("conversation_verbosity", "balanced") or "balanced")
        self._session.set_verbosity(saved_verbosity)
        if saved_mode != self._conversation_mode:
            self._session.set_mode(saved_mode)

    # ------------------------------------------------------------------
    # Feed, Now card, bug reports
    # ------------------------------------------------------------------
    def _on_bug_report_saved(self, path: str, error: str) -> None:
        if error or not path:
            self.append_log(f"Failed to save bug report: {error or 'no path'}", role="error")
            return

        from pathlib import Path

        from PySide6.QtWidgets import QApplication

        p = Path(path).resolve()
        file_url = p.as_uri()

        try:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(file_url)
            self.append_log(f"Bug report saved — link copied to clipboard:\n{file_url}", role="status")
        except Exception as exc:
            self.append_log(f"Bug report saved to: {path} (clipboard copy failed: {exc})", role="status")

        self.bug_report_btn.setText("Report saved — link copied")
        QTimer.singleShot(3000, lambda: self.bug_report_btn.setText(_BUG_REPORT_LABEL))

    def _on_spoken_line(self, text: str) -> None:
        self._now = (datetime.datetime.now().strftime("%H:%M"), text)
        self._render_now()
        self.append_log(text, role="spoken")

    def _render_now(self) -> None:
        if self._now is None:
            self.now_text.setText(_NOW_EMPTY)
            self.now_text.setProperty("empty", True)
            self.now_time.setText("")
        else:
            stamp, text = self._now
            self.now_text.setText(text)
            self.now_text.setProperty("empty", False)
            self.now_time.setText(stamp)
        theme.repolish(self.now_text)

    def _on_advice_received(self, text: str, label: str) -> None:
        self._latest_advice = (text, label)
        if self._debug_logging:
            self.append_log(f"[{label}] {text}", role="advice")

    def _on_log_emitted(self, msg: str, role: str) -> None:
        if role in self._PERTINENT_LOG_ROLES or self._debug_logging:
            self.append_log(msg, role=role)

    def _on_error_occurred(self, err: str) -> None:
        self.append_log(err, role="error")

    @staticmethod
    def _feed_line_html(stamp: str, text: str, role: str) -> str:
        tone, size, weight = _FEED_STYLE.get(role, _FEED_STYLE["status"])
        return block(
            span(stamp, "muted", size="caption")
            + "&nbsp;&nbsp;"
            + span(text, tone, size=size, weight=weight),
            gap=4,
        )

    def append_log(self, text: str, role: str = "status") -> None:
        """Append a line to the activity feed (oldest first, newest at the bottom)."""
        stamp = datetime.datetime.now().strftime("%H:%M")
        self._activity_history.append((stamp, text, role))
        if len(self._activity_history) > MAX_FEED_LINES:
            del self._activity_history[: len(self._activity_history) - MAX_FEED_LINES]

        doc = self.log_view.document()
        sb = self.log_view.verticalScrollBar()
        stick_to_bottom = sb.value() >= sb.maximum() - 10

        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(self._feed_line_html(stamp, text, role))

        while doc.blockCount() > MAX_FEED_LINES:
            first = doc.firstBlock()
            del_cursor = QTextCursor(first)
            del_cursor.setPosition(first.next().position(), QTextCursor.MoveMode.KeepAnchor)
            del_cursor.removeSelectedText()

        if stick_to_bottom:
            sb.setValue(sb.maximum())

    def _render_feed(self) -> None:
        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10
        self.log_view.setHtml(
            "".join(self._feed_line_html(stamp, text, role) for stamp, text, role in self._activity_history)
        )
        if at_bottom:
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Turn strip, status chips, chat
    # ------------------------------------------------------------------
    def update_turn_strip(self, game_state: dict[str, Any]) -> None:
        turn = game_state.get("turn") or {}
        turn_num = turn.get("turn_number") or game_state.get("turn_number", 0)
        phase = turn.get("phase") or game_state.get("phase", "") or ""
        phase_short = _pretty_phase(phase)

        local_seat = game_state.get("local_seat_id")
        active_player = turn.get("active_player")
        match_id = game_state.get("match_id")
        pending = str(game_state.get("pending_decision") or "")

        if "mulligan" in pending.lower() or (match_id and not turn_num and not phase):
            self.turn_strip.setText("Game starting · Mulligan")
            self.turn_strip.setProperty("who", "you")
        elif not turn_num and not phase:
            self.turn_strip.setText("Waiting for MTGA…")
            self.turn_strip.setProperty("who", "none")
        else:
            if active_player is not None and local_seat is not None:
                is_your_turn = int(active_player) == int(local_seat)
                who = "you" if is_your_turn else "opp"
                who_label = "Your turn" if is_your_turn else "Opponent's turn"
            else:
                who = "none"
                who_label = "Turn"
            phase_display = f" · {phase_short}" if phase_short else ""
            self.turn_strip.setText(f"{who_label} · T{turn_num}{phase_display}")
            self.turn_strip.setProperty("who", who)

        theme.repolish(self.turn_strip)

    def _refresh_status_dots(self) -> None:
        model = self._dot_values.get("MODEL") or "Model pending"
        bridge_val = self._dot_values.get("BRIDGE")
        bridge_on = bridge_val is not None and bridge_val != "OFF"
        seat = self._dot_values.get("SEAT") or "?"
        in_game = seat != "?" or self._dot_values.get("GAME") == "IN_MATCH"

        model_tone = "good" if self._dot_values.get("MODEL") else "muted"
        self.model_chip.setText(span("●", model_tone) + "&nbsp;" + span(model, "text", weight=600))
        self.model_chip.setToolTip("Model serving your advice")

        if bridge_on:
            self.source_chip.setText(span("●", "good") + "&nbsp;Bridge")
            self.source_chip.setToolTip("Game state from Player.log, enriched by the GRE bridge")
        else:
            self.source_chip.setText(span("●", "good" if in_game else "muted") + "&nbsp;Log watcher")
            tip = "Game state from Player.log"
            if sys.platform != "darwin":
                tip += " — the GRE bridge isn't connected, so autoplay can't submit actions"
            self.source_chip.setToolTip(tip)

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

    def set_debug_logging(self, enabled: bool) -> None:
        self._debug_logging = bool(enabled)
        self._settings.set("desktop_debug_logging", self._debug_logging)

    def _restyle(self) -> None:
        """Re-render every rich-text view with the new theme's colours."""
        self._refresh_status_dots()
        self._render_board(self._last_state)
        self._render_tactical_line()
        self._render_turn_plan()
        self._render_now()
        self._render_feed()

    # -- HTML Game State Formatter --------------------------------------------

    def _format_game_state_html(self, state: dict[str, Any]) -> str:
        players = state.get("players", []) or []
        hand = state.get("hand", []) or []
        battlefield = state.get("battlefield", []) or []
        if not players and not hand and not battlefield:
            return span("The board shows up here once a match starts.", "muted")

        local_seat = state.get("local_seat_id")
        you = next((p for p in players if p.get("is_local") or p.get("seat_id") == local_seat), {})
        opp = next((p for p in players if p.get("seat_id") != you.get("seat_id")), {})
        you_life = you.get("life_total", 20)
        opp_life = opp.get("life_total", 20)

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

        def is_type(card: dict[str, Any], word: str) -> bool:
            return word in str(card.get("type_line", "")).lower()

        def creature_list(cards: list[dict[str, Any]]) -> str:
            creatures = [c for c in cards if is_type(c, "creature")]
            if not creatures:
                return span("none", "muted")
            parts = []
            for c in creatures[:6]:
                part = span(f"{c.get('name', '?')} {c.get('power', 0)}/{c.get('toughness', 0)}")
                if c.get("is_tapped"):
                    part += span(" tapped", "muted")
                parts.append(part)
            if len(creatures) > 6:
                parts.append(span(f"+{len(creatures) - 6} more", "muted"))
            return " · ".join(parts)

        def label(text: str) -> str:
            return span(text, "muted", weight=700) + "&nbsp;&nbsp;"

        life_rows = block(
            span("Opponent", "opp", weight=700)
            + "&nbsp;&nbsp;"
            + span(f"♥ {opp_life}", "opp", size="emphasis", weight=700)
            + "&nbsp;&nbsp; "
            + span(f"board {len(opp_bf)}", "muted", size="caption")
        ) + block(
            span("You", "you", weight=700)
            + "&nbsp;&nbsp;"
            + span(f"♥ {you_life}", "you", size="emphasis", weight=700)
            + "&nbsp;&nbsp; "
            + span(f"{_plural(len(hand), 'card')} in hand · board {len(your_bf)}", "muted", size="caption"),
            gap=3,
        )

        hand_items = []
        for c in hand:
            if isinstance(c, dict):
                cost = str(c.get("mana_cost") or "").replace("{", "").replace("}", "")
                item = span(c.get("name") or "?")
                if cost:
                    item += " " + span(cost, "muted")
                hand_items.append(item)
        hand_line = " · ".join(hand_items) if hand_items else span("empty", "muted")

        land_counts = Counter(str(c.get("name", "?")) for c in your_bf if is_type(c, "land"))
        lands = ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in land_counts.items())
        lands_line = span(lands) if lands else span("none", "muted")

        details = "<br>".join(
            [
                label("Hand") + hand_line,
                label("Your creatures") + creature_list(your_bf),
                label("Lands") + lands_line,
                label("Their creatures") + creature_list(opp_bf),
            ]
        )
        return life_rows + block(details, size="caption")
