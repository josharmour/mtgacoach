"""Compact, svelte sidebar HUD for MTGA Coach."""

from __future__ import annotations

import datetime
import html
import logging
import math
import re
import sys
import time
from collections import Counter
from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from arenamcp import settings as settings

from . import theme
from .coach_session import CoachSession
from .flow_layout import FlowLayout
from .theme import block, span

logger = logging.getLogger(__name__)

# Where MTGA runs; "android" = a phone tethered over adb (arenamcp.android_link).
GAME_DEVICES = ("desktop", "android")


def _device_label(device: str) -> str:
    if device == "android":
        return "Android phone"
    return "This Mac" if sys.platform == "darwin" else "This PC"


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

# Strategic role (board_assessment) → tone on the plan card.
_ROLE_TONES = {
    "aggressor": "good",
    "race": "accent",
    "defender": "warn",
    "control/stabilize": "bad",
}


def _plan_step_text(step: dict[str, Any]) -> str:
    """'Island + Undulating Witness' for one validated game-plan turn."""
    parts = [str(step.get("land") or "")] + [str(c) for c in step.get("cast") or []]
    text = " + ".join(p for p in parts if p) or "—"
    attack = str(step.get("attack") or "").strip()
    if attack and attack.lower() not in ("none", "no", "-"):
        text += f", attack: {attack}"
    return text


_NOW_EMPTY = "Advice shows up here when you have a decision to make."
_BUG_REPORT_LABEL = "Debug report  ·  F12"


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
    reload_engine_requested = Signal()
    autopilot_bug_requested = Signal()

    _PERTINENT_LOG_ROLES = frozenset({"spoken", "error", "status", "advice"})

    def __init__(self, session: CoachSession | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings.get_settings()
        self._session = session or CoachSession(self)
        self._dot_values: dict[str, str] = {}
        self._auto_queue_enabled = bool(self._settings.get("auto_queue_enabled", False))
        self._auto_queue_detail = "Waiting for coaching to start." if self._auto_queue_enabled else ""
        self._startup_status: dict[str, Any] = {}
        self._startup_elapsed_s = 0.0
        self._startup_updated_at = time.monotonic()
        self._startup_timer = QTimer(self)
        self._startup_timer.setInterval(1000)
        self._startup_timer.timeout.connect(self._render_startup_status)
        # Auto-concede countdown (engine "concede_countdown" events).
        self._concede: dict[str, Any] = {}
        self._concede_deadline = 0.0
        self._concede_since = 0.0
        self._concede_timer = QTimer(self)
        self._concede_timer.setInterval(250)
        self._concede_timer.timeout.connect(self._render_concede)
        # Takes a finished countdown's banner down after a few seconds.
        self._concede_clear_timer = QTimer(self)
        self._concede_clear_timer.setSingleShot(True)
        self._concede_clear_timer.timeout.connect(self._clear_concede)
        self._concede_style = ""
        self._game_plan: dict[str, Any] = {}
        self._latest_advice: tuple[str, str] | None = None
        self._debug_logging = bool(self._settings.get("desktop_debug_logging", False))
        # Chronological (oldest first): (HH:MM, text, role).
        self._activity_history: list[tuple[str, str, str]] = []
        self._last_state: dict[str, Any] = {}
        self._last_mcts: Any = None
        self._last_plan: Any = None
        self._now: tuple[str, str] | None = None

        self._build_ui()
        self._wire_session()
        theme.on_theme_changed(self._restyle)

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

        # Status chips: model and game source.
        self.status_row = QWidget()
        chips = FlowLayout(self.status_row)
        chips.setSpacing(6)
        self.model_chip = self._make_chip()
        self.source_chip = self._make_chip()
        for chip in (self.model_chip, self.source_chip):
            chips.addWidget(chip)
        root.addWidget(self.status_row)

        # Keep startup visible even when a model name or game state has arrived.
        # Neither of those means the coaching request path is ready yet.
        self.startup_banner, startup_layout = _card()
        self.startup_banner.setObjectName("startupBanner")
        self.startup_label = QLabel()
        self.startup_label.setObjectName("startupStatusLabel")
        self.startup_label.setTextFormat(Qt.RichText)
        self.startup_label.setWordWrap(True)
        self.startup_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        startup_layout.addWidget(self.startup_label)
        self.startup_banner.hide()
        root.addWidget(self.startup_banner)

        # Auto-concede countdown: the reason, seconds left and a Cancel button.
        self.concede_banner, concede_layout = _card()
        self.concede_banner.setObjectName("concedeBanner")
        self.concede_label = QLabel()
        self.concede_label.setObjectName("concedeLabel")
        self.concede_label.setTextFormat(Qt.RichText)
        self.concede_label.setWordWrap(True)
        concede_layout.addWidget(self.concede_label)
        self.concede_cancel_btn = QPushButton("Cancel — keep playing")
        self.concede_cancel_btn.setObjectName("concedeCancelButton")
        self.concede_cancel_btn.setProperty("variant", "primary")
        self.concede_cancel_btn.setToolTip("Stop the auto-concede and keep playing this game")
        self.concede_cancel_btn.clicked.connect(self._on_concede_cancel_clicked)
        concede_layout.addWidget(self.concede_cancel_btn)
        self.concede_banner.hide()
        root.addWidget(self.concede_banner)

        self.arena_status_label = QLabel()
        self.arena_status_label.setObjectName("arenaConnectionStatus")
        self.arena_status_label.setTextFormat(Qt.RichText)
        self.arena_status_label.setWordWrap(True)
        self.arena_status_label.hide()
        root.addWidget(self.arena_status_label)

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
        self.mcts_pill_label.setToolTip(
            "Rule-of-thumb suggestion from the heuristic hints (not a simulation)"
        )
        self.mcts_pill_label.hide()
        now_layout.addWidget(self.mcts_pill_label)

        self.turn_plan_label = QLabel()
        self.turn_plan_label.setObjectName("turnPlanLabel")
        self.turn_plan_label.setWordWrap(True)
        self.turn_plan_label.setTextFormat(Qt.RichText)
        self.turn_plan_label.hide()
        now_layout.addWidget(self.turn_plan_label)

        # Strategy: role (who's the beatdown), clocks, and the 3-turn plan.
        self.game_plan_label = QLabel()
        self.game_plan_label.setObjectName("gamePlanLabel")
        self.game_plan_label.setWordWrap(True)
        self.game_plan_label.setTextFormat(Qt.RichText)
        self.game_plan_label.hide()
        now_layout.addWidget(self.game_plan_label)
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

        # Feed card: proactive advice and chronological activity log.
        feed_card, feed_layout = _card()
        feed_head = QHBoxLayout()
        self.feed_caption = _caption("Feed")
        feed_head.addWidget(self.feed_caption)
        feed_head.addStretch()
        self.latest_feed_btn = QPushButton("Latest ↓")
        self.latest_feed_btn.setToolTip("Show the newest entry and keep following the feed")
        self.latest_feed_btn.hide()
        self.latest_feed_btn.clicked.connect(self._follow_feed_latest)
        feed_head.addWidget(self.latest_feed_btn)
        feed_layout.addLayout(feed_head)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setProperty("bare", True)
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(90)
        self._feed_follow_latest = True
        self._feed_updating = False
        self.log_view.verticalScrollBar().valueChanged.connect(self._on_feed_scrolled)
        self.log_view.verticalScrollBar().rangeChanged.connect(self._on_feed_range_changed)
        feed_layout.addWidget(self.log_view, 1)

        root.addWidget(feed_card, stretch=1)

        # Primary controls.
        controls = FlowLayout()
        controls.setSpacing(6)
        self.ap_btn = QPushButton("Autoplay: Off")
        self.ap_btn.setObjectName("apButton")
        self.ap_btn.setProperty("state", "off")
        self.ap_btn.setToolTip("Toggle autoplay — automatically plays the current match")
        self.ap_btn.clicked.connect(self._session.toggle_autopilot)
        controls.addWidget(self.ap_btn)

        self.auto_queue_btn = QPushButton()
        self.auto_queue_btn.setObjectName("autoQueueButton")
        self.auto_queue_btn.setToolTip(
            "After each match, queue again with the most recently played queue and deck. "
            "Continues until turned off; requires Autoplay to be on."
        )
        self.auto_queue_btn.clicked.connect(self._toggle_auto_queue)
        controls.addWidget(self.auto_queue_btn)

        self.stop_speech_btn = QPushButton("■")
        self.stop_speech_btn.setObjectName("stopSpeechButton")
        self.stop_speech_btn.setToolTip("Stop speaking")
        self.stop_speech_btn.setAccessibleName("Stop speaking")
        self.stop_speech_btn.clicked.connect(self._session.stop_speaking)
        controls.addWidget(self.stop_speech_btn)

        self.more_btn = QPushButton("Voice && style")
        self.more_btn.setObjectName("moreButton")
        self.more_btn.setToolTip("Voice, speed, advice length, and mute")
        self.more_btn.clicked.connect(self._show_voice_style)
        controls.addWidget(self.more_btn)
        root.addLayout(controls)

        self.auto_queue_status_label = QLabel()
        self.auto_queue_status_label.setObjectName("autoQueueStatus")
        self.auto_queue_status_label.setTextFormat(Qt.RichText)
        self.auto_queue_status_label.setWordWrap(True)
        root.addWidget(self.auto_queue_status_label)
        self._render_auto_queue_status()

        self.autopilot_bug_btn = QPushButton("Autopilot bug")
        self.autopilot_bug_btn.setObjectName("autopilotBugButton")
        self.autopilot_bug_btn.setProperty("variant", "primary")
        self.autopilot_bug_btn.setToolTip(
            "Report an incorrect autoplay decision with the current game context"
        )
        self.autopilot_bug_btn.clicked.connect(self._on_autopilot_bug_clicked)
        root.addWidget(self.autopilot_bug_btn)
        self.autopilot_report_link = QLabel()
        self.autopilot_report_link.setWordWrap(True)
        self.autopilot_report_link.setTextFormat(Qt.RichText)
        self.autopilot_report_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.autopilot_report_link.setOpenExternalLinks(True)
        self.autopilot_report_link.hide()
        root.addWidget(self.autopilot_report_link)

        # Keep reporting and recovery controls directly accessible.
        recovery_controls = FlowLayout()
        recovery_controls.setSpacing(6)
        self.bug_report_btn = QPushButton(_BUG_REPORT_LABEL)
        self.bug_report_btn.setObjectName("bugReportButton")
        self.bug_report_btn.setToolTip("Save a debug-report snapshot and copy its link (F12 or Ctrl+Shift+D)")
        self.bug_report_btn.clicked.connect(self._on_bug_report_clicked)
        recovery_controls.addWidget(self.bug_report_btn)

        self.reload_engine_btn = QPushButton("Reload engine")
        self.reload_engine_btn.setObjectName("reloadEngineButton")
        self.reload_engine_btn.setStyleSheet("padding: 5px 6px;")
        self.reload_engine_btn.setToolTip("Apply coaching-code fixes; keep Arena and this window open.")
        self.reload_engine_btn.clicked.connect(self.reload_engine_requested.emit)
        recovery_controls.addWidget(self.reload_engine_btn)

        self.restart_btn = QPushButton("Restart Coach")
        self.restart_btn.setObjectName("restartCoachButton")
        self.restart_btn.setStyleSheet("padding: 5px 6px;")
        self.restart_btn.setToolTip("Reload the Coach app and coaching engine. MTGA stays open.")
        self.restart_btn.clicked.connect(self.restart_requested.emit)
        recovery_controls.addWidget(self.restart_btn)
        root.addLayout(recovery_controls)

        self._build_voice_style_popover()
        self._refresh_status_dots()
        self._render_board({})

    def _build_voice_style_popover(self) -> None:
        self.voice_style_popover = VoiceStylePopover(self)
        pop = self.voice_style_popover

        self.voice_btn = QPushButton("Voice: Auto")
        self.voice_btn.setObjectName("voiceButton")
        self.voice_btn.setToolTip("Choose the text-to-speech voice (F6 cycles)")
        self.voice_btn.setMenu(self._voice_menu())
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

        self.mute_btn = QPushButton("Mute: Off")
        self.mute_btn.setObjectName("muteButton")
        self.mute_btn.setProperty("state", "off")
        self.mute_btn.setToolTip("Mute / unmute spoken advice")
        self.mute_btn.clicked.connect(self._session.toggle_mute)
        pop.add_button(self.mute_btn)

        pop.add_divider()
        self.device_btn = QPushButton(f"Play on: {_device_label(self._game_device())}")
        self.device_btn.setObjectName("deviceButton")
        self.device_btn.setToolTip(
            "Where MTGA runs: this computer, or an Android phone tethered over adb. Restarts the coach."
        )
        self.device_btn.clicked.connect(self._cycle_game_device)
        pop.add_button(self.device_btn)

    def _voice_menu(self) -> QMenu:
        """Every Kokoro voice, grouped by accent and gender."""
        from arenamcp.kokoro_voices import KOKORO_VOICES, VOICE_GROUPS

        menu = QMenu(self)
        submenus: dict[str, QMenu] = {}
        for voice_id, label in KOKORO_VOICES:
            group = VOICE_GROUPS.get(voice_id[:2], "Other")
            if group not in submenus:
                submenus[group] = menu.addMenu(group)
            action = submenus[group].addAction(label.split(" (")[0])
            action.triggered.connect(lambda _checked=False, v=voice_id: self._session.set_voice(v))
        return menu

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

    def _toggle_auto_queue(self) -> None:
        self._auto_queue_enabled = not self._auto_queue_enabled
        self._settings.set("auto_queue_enabled", self._auto_queue_enabled)
        self._auto_queue_detail = "Waiting for Autoplay and match status." if self._auto_queue_enabled else ""
        self._render_auto_queue_status()
        self._session.set_auto_queue(self._auto_queue_enabled)

    def _render_auto_queue_status(self) -> None:
        enabled = self._auto_queue_enabled
        self.auto_queue_btn.setText(f"Auto-queue: {'On' if enabled else 'Off'}")
        self.auto_queue_btn.setProperty("state", "on" if enabled else "off")
        theme.repolish(self.auto_queue_btn)
        detail = self._auto_queue_detail.strip()
        if detail and not detail.lower().startswith("auto-queue"):
            detail = f"Auto-queue: {detail}"
        self.auto_queue_status_label.setText(block(span(detail, "muted"), size="caption"))
        self.auto_queue_status_label.setVisible(bool(detail))

    def _on_autopilot_bug_clicked(self) -> None:
        self.autopilot_bug_btn.setText("Capturing autopilot bug…")
        self.autopilot_bug_btn.setEnabled(False)
        self.autopilot_bug_requested.emit()

    def _on_autopilot_bug_status(self, status: dict[str, Any]) -> None:
        path = str(status.get("path") or "")
        if path:
            url = html.escape(QUrl.fromLocalFile(path).toString(), quote=True)
            self.autopilot_report_link.setText(
                f'Saved locally · <a href="{url}">Open autopilot bug report</a>'
            )
            self.autopilot_report_link.setToolTip(path)
            self.autopilot_report_link.show()
        state = str(status.get("phase") or status.get("state") or status.get("status") or "")
        if state in ("completed", "error"):
            self.autopilot_bug_btn.setText("Autopilot bug")
            self.autopilot_bug_btn.setEnabled(True)
        elif state == "recording":
            self.autopilot_bug_btn.setText("Recording bug · paused")
            self.autopilot_bug_btn.setEnabled(False)
        else:
            self.autopilot_bug_btn.setText("Capturing autopilot bug…")
            self.autopilot_bug_btn.setEnabled(False)
        message = str(status.get("message") or "")
        if message:
            self.autopilot_bug_btn.setToolTip(message)
            self.append_log(message, role="error" if state == "error" else "status")

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
        self._session.started.connect(self._ensure_proactive_advice)
        startup_signal = getattr(self._session, "startupStatusChanged", None)
        if startup_signal is not None:
            startup_signal.connect(self._on_startup_status)
        startup_status = getattr(self._session, "last_startup_status", None)
        if isinstance(startup_status, dict) and startup_status:
            self._on_startup_status(startup_status)
        concede_signal = getattr(self._session, "concedeCountdownChanged", None)
        if concede_signal is not None:
            concede_signal.connect(self._on_concede_countdown)
        bug_signal = getattr(self._session, "autopilotBugStatusChanged", None)
        if bug_signal is not None:
            bug_signal.connect(self._on_autopilot_bug_status)
        bug_status = getattr(self._session, "last_autopilot_bug_status", None)
        if isinstance(bug_status, dict) and bug_status:
            self._on_autopilot_bug_status(bug_status)

    def _on_startup_status(self, status: dict[str, Any]) -> None:
        now = time.monotonic()
        phase = str(status.get("phase", "starting"))
        if (
            not self._startup_timer.isActive()
            or (phase == "starting" and "elapsed_s" not in status)
            or (phase == "reloading" and self._startup_status.get("phase") != "reloading")
        ):
            elapsed_s = 0.0
        else:
            elapsed_s = self._startup_elapsed_s + now - self._startup_updated_at
        try:
            # Child phases time their own startup work. Do not move the visible
            # clock backwards when those counters arrive after process launch.
            elapsed_s = max(elapsed_s, float(status.get("elapsed_s", elapsed_s)))
        except (TypeError, ValueError):
            pass
        self._startup_status = dict(status)
        self._startup_elapsed_s = elapsed_s
        self._startup_updated_at = now
        if status.get("ready") or phase in ("error", "stopped"):
            self._startup_timer.stop()
        else:
            self._startup_timer.start()
        self._render_startup_status()
        self._refresh_status_dots()

    def _render_startup_status(self) -> None:
        status = self._startup_status
        if not status or (status.get("ready") and status.get("phase") != "connection_warning"):
            self.startup_banner.hide()
            return
        phase = str(status.get("phase", "starting"))
        failed = phase in ("error", "stopped")
        tone = "bad" if failed else "warn"
        if phase == "connection_warning":
            heading = "Connection check incomplete"
            explanation = "Coaching will try the next game decision; no reload required."
        elif failed:
            heading = "Coach stopped" if phase == "stopped" else "Startup needs attention"
            explanation = (
                "Reload engine to resume coaching."
                if phase == "stopped"
                else "Check the connection details above or use Reload engine to retry."
            )
        else:
            elapsed_s = self._startup_elapsed_s + time.monotonic() - self._startup_updated_at
            heading = f"Preparing coach · {int(elapsed_s)}s"
            explanation = "Advice and autoplay are waiting for startup to finish."
        message = str(status.get("message") or "Starting coaching engine…")
        self.startup_label.setText(
            block(span(heading, tone, weight=700), gap=3)
            + block(span(message), size="caption", gap=3)
            + block(span(explanation, "muted"), size="caption")
        )
        tokens = theme.tokens()
        self.startup_banner.setStyleSheet(
            f"QFrame#startupBanner {{ background: {tokens.tint(tone, 0.10)}; "
            f"border: 1px solid {theme.color(tone)}; border-radius: {tokens.radius_card}px; }}"
        )
        self.startup_banner.show()

    # -- auto-concede countdown -------------------------------------------------

    # A countdown's events: offering -> armed -> conceding -> sent -> conceded,
    # or it ends cancelled / aborted / failed / unconfirmed.
    _CONCEDE_ACTIVE = ("offering", "armed", "conceding", "sent")
    _CONCEDE_ENDED = ("cancelled", "aborted", "conceded", "failed", "unconfirmed")
    # No follow-up this long after an armed countdown's deadline (or after an
    # offer was shown): the engine went quiet, take the banner down.
    _CONCEDE_STALE_S = 15.0
    _CONCEDE_OFFER_STALE_S = 120.0

    def _on_concede_countdown(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        state = str(payload.get("state") or "")
        current = self._concede
        if (
            state != "offering"
            and current.get("state") in self._CONCEDE_ACTIVE
            and payload.get("id") is not None
            and payload.get("id") != current.get("id")
        ):
            return  # an event of an older countdown
        self._concede = dict(payload)
        self._concede_since = time.monotonic()
        if state in ("offering", "armed"):
            try:
                seconds = float(payload.get("seconds") or 10)
            except (TypeError, ValueError):
                seconds = 10.0
            self._concede_deadline = time.monotonic() + seconds
            if state == "offering" or current.get("id") != payload.get("id"):
                self.concede_cancel_btn.setEnabled(True)
                self.concede_cancel_btn.setText("Cancel — keep playing")
            self._concede_clear_timer.stop()
            self._concede_timer.start()
        else:
            self._concede_timer.stop()
            if state in self._CONCEDE_ENDED:
                self._concede_clear_timer.start(10000 if state in ("failed", "unconfirmed") else 5000)
        self._render_concede()

    def _clear_concede(self) -> None:
        if self._concede.get("state") not in self._CONCEDE_ACTIVE:
            self._concede = {}
            self._render_concede()

    def _concede_stale(self) -> bool:
        """An offer or countdown the engine never followed up (lost or out-of-order events)."""
        state = self._concede.get("state")
        now = time.monotonic()
        if state == "armed":
            return now > self._concede_deadline + self._CONCEDE_STALE_S
        if state == "offering":
            return now > getattr(self, "_concede_since", now) + self._CONCEDE_OFFER_STALE_S
        return False

    def _on_concede_cancel_clicked(self) -> None:
        cancel = getattr(self._session, "cancel_concede", None)
        if callable(cancel):
            cancel()
        self.concede_cancel_btn.setEnabled(False)
        self.concede_cancel_btn.setText("Cancelling…")

    def _render_concede(self) -> None:
        if self._concede_stale():
            logger.info("Auto-concede banner dropped: no word from the engine")
            self._concede = {}
        payload = self._concede
        state = str(payload.get("state") or "")
        if not state:
            self._concede_timer.stop()
            self.concede_banner.hide()
            return
        reason = str(payload.get("reason") or "")
        if state == "offering":
            tone = "bad"
            heading = "Auto-concede offered"
            detail = (
                f"The {payload.get('seconds') or 10}-second countdown starts when the coach finishes "
                "speaking. Cancel to keep playing."
            )
        elif state == "armed":
            left = max(0, math.ceil(self._concede_deadline - time.monotonic()))
            tone = "bad"
            heading = (
                f"Auto-concede in {left}s" if left else "Auto-concede: checking the board one last time…"
            )
            detail = "Autoplay concedes this game when the countdown ends."
        elif state == "conceding":
            tone, heading, detail = "bad", "Conceding this game…", ""
        elif state == "sent":
            tone, heading, detail = "bad", "Concede sent — waiting for Arena to end the game…", ""
        elif state == "conceded":
            tone, heading, detail = "muted", "Conceded this game", ""
        elif state == "unconfirmed":
            tone, heading = "bad", "Auto-concede not confirmed"
            detail = str(payload.get("message") or "Check Arena, and concede from its menu if you want to.")
            reason = ""
        elif state == "cancelled":
            tone, heading, detail = (
                "warn",
                "Auto-concede cancelled",
                "Keep playing — it won't ask again this game.",
            )
            reason = ""
        elif state == "aborted":
            tone, heading, detail = "warn", "Auto-concede stopped", ""
        else:
            tone, heading = "bad", "Auto-concede failed"
            detail = str(payload.get("message") or "Concede from Arena's menu if you want to.")
            reason = str(payload.get("error") or "")
        html = block(span(heading, tone, weight=700), gap=3)
        if reason:
            html += block(span(reason), size="caption", gap=3)
        if detail:
            html += block(span(detail, "muted"), size="caption")
        self.concede_label.setText(html)
        self.concede_cancel_btn.setVisible(state in ("offering", "armed"))
        tokens = theme.tokens()
        style = (
            f"QFrame#concedeBanner {{ background: {tokens.tint(tone, 0.10)}; "
            f"border: 1px solid {theme.color(tone)}; border-radius: {tokens.radius_card}px; }}"
        )
        if style != self._concede_style:  # the 250 ms tick only changes the text
            self._concede_style = style
            self.concede_banner.setStyleSheet(style)
        self.concede_banner.show()

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
            steps = best_branch.get("sequence_steps") or []
            if steps:
                action_text = " → ".join(str(s) for s in steps[:3])
            else:
                action_text = str(best_branch.get("action") or best_action)
        else:
            action_text = str(best_action)

        # The score is a weighted life/power/hand difference, not a win
        # chance — show a word, not a percentage.
        root = float(payload.get("root_win_probability", 0.5))
        position, score_tone = (
            ("favorable", "good")
            if root >= 0.6
            else (("unfavorable", "bad") if root <= 0.4 else ("even", "warn"))
        )
        lines = [
            block(
                span("Heuristic hint", "muted", weight=700)
                + "&nbsp;&nbsp;"
                + span(f"board {position}", score_tone, weight=700),
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
            not self.mcts_pill_label.isHidden()
            or not self.turn_plan_label.isHidden()
            or not self.game_plan_label.isHidden()
        )

    def _on_game_plan_changed(self, plan: Any) -> None:
        if isinstance(plan, dict):
            self._game_plan = plan
            self._render_game_plan()

    def _render_game_plan(self) -> None:
        """Role + clocks + the next three of our turns, from the game_plan event."""
        plan = self._game_plan if isinstance(self._game_plan, dict) else {}
        facts = plan.get("facts") if isinstance(plan.get("facts"), dict) else {}
        role = str(facts.get("role") or plan.get("role") or "")
        steps = [s for s in plan.get("turn_plan") or [] if isinstance(s, dict)]
        if not role and not steps:
            self.game_plan_label.hide()
            self._sync_now_divider()
            return

        def label(text: str) -> str:
            return span(text, "muted", weight=700) + "&nbsp;&nbsp;"

        lines = []
        if role:
            head = label("Role") + span(role.upper(), _ROLE_TONES.get(role, "accent"), weight=700)
            reason = str(facts.get("role_reason") or plan.get("role_reason") or "")
            if reason:
                head += "&nbsp;" + span(f"— {reason}", "muted")
            lines.append(block(head, size="caption"))
        if facts:
            theirs, ours = facts.get("their_clock"), facts.get("our_clock")
            bits = [
                f"they kill you in {theirs}" if theirs else "no clock on you",
                f"you kill them in {ours}" if ours else "no clock on them",
            ]
            if facts.get("race"):
                bits.append(f"race {facts['race']}")
            lines.append(block(label("Clocks") + span(" · ".join(bits)), size="caption"))
            flags = [str(f) for f in facts.get("flags") or [] if f]
            if flags:
                lines.append(block(span(" · ".join(flags[:2]), "bad", weight=600), size="caption"))
        if steps:
            turns = [f"T{s.get('turn')} {_plan_step_text(s)}" for s in steps[:3]]
        else:
            turns = [
                f"T{s.get('turn')} {' + '.join(s.get('casts') or []) or '—'}"
                for s in facts.get("lookahead") or []
                if isinstance(s, dict)
            ][:3]
        if turns:
            lines.append(block(label("Next 3") + span(" → ".join(turns)), size="caption"))
        wins = [str(w) for w in plan.get("win_conditions") or [] if w]
        if wins:
            lines.append(block(label("Win") + span(wins[0], "muted"), size="caption"))
        self.game_plan_label.setText("".join(lines))
        self.game_plan_label.show()
        self._sync_now_divider()

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
        elif key == "AUTO_QUEUE":
            state = val.strip().upper()
            if state in {"ON", "OFF"}:
                self._auto_queue_enabled = state == "ON"
                if not self._auto_queue_enabled:
                    self._auto_queue_detail = ""
                self._render_auto_queue_status()
        elif key == "AUTO_QUEUE_DETAIL":
            self._auto_queue_detail = val
            self._render_auto_queue_status()
        elif key == "ARENA":
            self._render_arena_status()
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

    def _render_arena_status(self) -> None:
        message = self._dot_values.get("ARENA", "").strip()
        self.arena_status_label.setText(block(span(message, "warn"), size="caption"))
        self.arena_status_label.setVisible(bool(message))

    # ------------------------------------------------------------------
    # Proactive coaching and device selection
    # ------------------------------------------------------------------
    def _ensure_proactive_advice(self) -> None:
        """A saved chat preference must not silence the proactive desktop."""
        self._session.set_mode("turn_advice")

    def _game_device(self) -> str:
        device = str(self._settings.get("game_device", "desktop") or "desktop")
        return device if device in GAME_DEVICES else "desktop"

    def _cycle_game_device(self) -> None:
        current = self._game_device()
        next_device = GAME_DEVICES[(GAME_DEVICES.index(current) + 1) % len(GAME_DEVICES)]
        self._settings.set("game_device", next_device)
        self.device_btn.setText(f"Play on: {_device_label(next_device)}")
        # The coach links the phone (or stops mirroring it) only at start.
        self._dot_values.pop("DEVICE", None)
        self._refresh_status_dots()
        self.voice_style_popover.hide()
        self.restart_requested.emit()

    def _on_mode_changed(self, mode: str) -> None:
        if str(mode).strip() == "conversation":
            self._ensure_proactive_advice()

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
        display_text = text
        if self._latest_advice:
            detailed, label = self._latest_advice
            if label in {"DRAFT", "DECK"} and detailed.startswith(text):
                display_text = detailed
        self._now = (datetime.datetime.now().strftime("%H:%M"), display_text)
        self._render_now()
        if not self._activity_history or self._activity_history[-1][1] != text:
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
        if not text.strip():
            return
        self._latest_advice = (text, label)
        # Advice is useful even when narration is muted, suppressed, or has not
        # arrived yet. The passive desktop never requires a chat interaction.
        self._now = (datetime.datetime.now().strftime("%H:%M"), text)
        self._render_now()
        self.append_log(text, role="advice")

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
        self._feed_updating = True

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

        self._feed_updating = False
        self._scroll_feed_to_latest()
        # Qt can finish wrapping/layout after insertHtml returns. Follow the
        # resulting range as well as the immediate document size.
        QTimer.singleShot(0, self._scroll_feed_to_latest)

    def _on_feed_scrolled(self, value: int) -> None:
        if self._feed_updating:
            return
        sb = self.log_view.verticalScrollBar()
        self._feed_follow_latest = value >= sb.maximum() - 10
        self.latest_feed_btn.setVisible(not self._feed_follow_latest)

    def _on_feed_range_changed(self, _minimum: int, _maximum: int) -> None:
        if not self._feed_updating:
            self._scroll_feed_to_latest()

    def _scroll_feed_to_latest(self) -> None:
        if not self._feed_follow_latest:
            return
        self._feed_updating = True
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._feed_updating = False
        self.latest_feed_btn.hide()

    def _follow_feed_latest(self) -> None:
        self._feed_follow_latest = True
        self._scroll_feed_to_latest()
        QTimer.singleShot(0, self._scroll_feed_to_latest)

    def _render_feed(self) -> None:
        sb = self.log_view.verticalScrollBar()
        old_position = sb.value()
        self._feed_updating = True
        self.log_view.setHtml(
            "".join(self._feed_line_html(stamp, text, role) for stamp, text, role in self._activity_history)
        )
        sb.setValue(old_position)
        self._feed_updating = False
        self._scroll_feed_to_latest()
        QTimer.singleShot(0, self._scroll_feed_to_latest)

    # ------------------------------------------------------------------
    # Turn strip and status chips
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
        bridge_val = self._dot_values.get("BRIDGE") or ""
        # "Connected (<runtime>)" | "Disconnected" | "Log mode"; "ON" from older builds.
        bridge_on = bridge_val.startswith("Connected") or bridge_val == "ON"
        bridge_runtime = (
            bridge_val[bridge_val.find("(") + 1 : bridge_val.rfind(")")] if "(" in bridge_val else ""
        )
        seat = self._dot_values.get("SEAT") or "?"
        in_game = seat != "?" or self._dot_values.get("GAME") == "IN_MATCH"

        startup_phase = self._startup_status.get("phase")
        preparing = bool(self._startup_status) and not self._startup_status.get("ready")
        model_tone = (
            "bad"
            if startup_phase in ("error", "stopped")
            else "warn"
            if preparing or startup_phase == "connection_warning"
            else "good"
            if self._dot_values.get("MODEL")
            else "muted"
        )
        self.model_chip.setText(span("●", model_tone) + "&nbsp;" + span(model, "text", weight=600))
        self.model_chip.setToolTip(
            "Coaching is not ready yet; see the startup status below."
            if preparing
            else "Connection check incomplete. Coaching will try the next game decision."
            if startup_phase == "connection_warning"
            else "Coach ready. Model used for advice."
            if self._startup_status.get("ready")
            else "Model configured for advice"
        )

        device = self._dot_values.get("DEVICE") or ""
        if device.startswith("ANDROID"):
            phone = device.split(":", 1)[1] if ":" in device else ""
            if not phone:
                self.source_chip.setText(span("●", "bad") + "&nbsp;Android · no phone")
                self.source_chip.setToolTip(
                    "No phone with MTGA found over adb. Plug it in with USB debugging on, then restart the "
                    "coach; Setup & Repair checks the phone."
                )
            else:
                phone_bridge = bridge_on and bridge_runtime in ("", "il2cpp-android")
                self.source_chip.setText(
                    span("●", "good" if phone_bridge else "warn") + "&nbsp;" + span(f"Android · {phone}")
                )
                if phone_bridge:
                    tip = ", actions through the bridge in MTGA."
                elif bridge_on:
                    tip = (
                        ". The bridge is held by MTGA on this computer, not the phone; close MTGA here so "
                        "autoplay can act on the phone."
                    )
                else:
                    tip = (
                        ". The bridge isn't connected, so autoplay can't act: start MTGA on the phone. After an "
                        "MTGA update, re-run spikes/android-il2cpp/install.sh."
                    )
                self.source_chip.setToolTip(f"MTGA on {phone}: game log mirrored over adb{tip}")
        elif bridge_on:
            self.source_chip.setText(span("●", "good") + "&nbsp;Bridge")
            self.source_chip.setToolTip("Game state from Player.log, enriched by the GRE bridge")
        else:
            self.source_chip.setText(span("●", "good" if in_game else "muted") + "&nbsp;Log watcher")
            tip = "Game state from Player.log"
            if sys.platform != "darwin":
                tip += " — the GRE bridge isn't connected, so autoplay can't submit actions"
            self.source_chip.setToolTip(tip)

    def set_debug_logging(self, enabled: bool) -> None:
        self._debug_logging = bool(enabled)
        self._settings.set("desktop_debug_logging", self._debug_logging)

    def _restyle(self) -> None:
        """Re-render every rich-text view with the new theme's colours."""
        self._refresh_status_dots()
        self._render_startup_status()
        self._render_concede()
        self._render_arena_status()
        self._render_auto_queue_status()
        self._render_board(self._last_state)
        self._render_tactical_line()
        self._render_turn_plan()
        self._render_game_plan()
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
