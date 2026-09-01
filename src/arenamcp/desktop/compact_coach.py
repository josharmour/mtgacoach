"""Compact, svelte sidebar HUD for MTGA Coach."""

from __future__ import annotations

import contextlib
import html
import logging
from typing import Any

from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from arenamcp.settings import get_settings

from .brain_stream_window import BrainStreamWindow
from .coach_session import CoachSession

logger = logging.getLogger(__name__)


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
        self._settings = get_settings()
        self._session = session or CoachSession(self)
        self._dot_values: dict[str, str] = {}
        self._buttons: dict[str, Any] = {}
        self._game_plan: dict[str, Any] = {}
        self._latest_advice: tuple[str, str] | None = None
        self._debug_logging = bool(self._settings.get("desktop_debug_logging", False))
        self._activity_history: list[tuple[str, str]] = []

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
        root.addWidget(self.turn_strip)

        # 2. Status dots: Model / Bridge / Seat indicators
        self.status_dots = QLabel()
        self.status_dots.setObjectName("statusDots")
        self.status_dots.setTextFormat(Qt.RichText)
        self.status_dots.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_dots)

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

        # Turn Plan Header & Label
        self.turn_plan_label = QLabel()
        self.turn_plan_label.setObjectName("turnPlanLabel")
        self.turn_plan_label.setWordWrap(True)
        self.turn_plan_label.setTextFormat(Qt.RichText)
        self.turn_plan_label.hide()
        act_layout.addWidget(self.turn_plan_label)

        # Speech & Advice Subtitle Log
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        self.log_view.document().setMaximumBlockCount(500)
        act_layout.addWidget(self.log_view)

        self.main_splitter.addWidget(activity_container)
        self.main_splitter.setSizes([260, 240])
        root.addWidget(self.main_splitter, stretch=1)

        # 4. Controls Bar (AP toggle, Brain Stream, Bug Report / Voice, Style, Mute)
        ctrl_row1 = QHBoxLayout()
        ctrl_row1.setSpacing(5)

        self.ap_btn = QPushButton("AP: OFF")
        self.ap_btn.setObjectName("apButton")
        self.ap_btn.setProperty("apOn", "false")
        self.ap_btn.setToolTip("Toggle autopilot — plays actions via GRE named pipe")
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

        root.addLayout(ctrl_row1)

        ctrl_row2 = QHBoxLayout()
        ctrl_row2.setSpacing(5)

        self.voice_btn = QPushButton("Voice: Auto")
        self.voice_btn.setObjectName("voiceButton")
        self.voice_btn.setToolTip("Cycle TTS voice (Sky / Alloy / Echo / Nova / etc.)")
        self.voice_btn.clicked.connect(self._session.cycle_voice)
        ctrl_row2.addWidget(self.voice_btn)
        self._buttons["cycle_voice"] = self.voice_btn

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

    def _on_game_state_changed(self, state: dict[str, Any]) -> None:
        self.update_turn_strip(state)
        html_content = self._format_game_state_html(state)
        self.game_state_view.setHtml(html_content)
        if self._brain_stream_window and self._brain_stream_window.isVisible():
            self._brain_stream_window.update_game_state(state)

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
            self.turn_plan_label.setText("<div style='font-size:11px; margin-bottom:4px;'>" + "<br>".join(items) + "</div>")
            self.turn_plan_label.show()
        elif isinstance(plan, str) and plan.strip():
            self.turn_plan_label.setText(f"<div style='font-size:11px;'><b>Plan:</b> {html.escape(plan)}</div>")
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
            ap_on = "ON" in val
            self.ap_btn.setText("AP: ON" if ap_on else "AP: OFF")
            self.ap_btn.setProperty("apOn", "true" if ap_on else "false")
            self._repolish(self.ap_btn)
        elif key == "STYLE":
            self.style_btn.setText(val)
        elif key == "MUTE":
            self.mute_btn.setText(f"Mute: {val}")
        elif key == "VOICE":
            self.voice_btn.setText(f"Voice: {val}")

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
        """Append a colored line to the subtitle & activity feed."""
        self._activity_history.append((text, role))
        t = self._theme_tokens()
        colors = self._LOG_COLORS_DARK if t["is_dark"] else self._LOG_COLORS_LIGHT
        color = colors.get(role, colors["status"])

        font_size = "13px" if role == "spoken" else "11px"
        font_weight = "600" if role in ("spoken", "error") else "400"
        escaped = html.escape(text).replace("\n", "<br>")

        line_html = f"<div style='color:{color}; font-size:{font_size}; font-weight:{font_weight}; margin-bottom:3px;'>{escaped}</div>"
        self.log_view.append(line_html)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def update_turn_strip(self, game_state: dict[str, Any]) -> None:
        turn = game_state.get("turn") or {}
        turn_num = turn.get("turn_number") or game_state.get("turn_number", 0)
        phase = turn.get("phase") or game_state.get("phase", "") or ""
        phase_short = phase.replace("Phase_", "").replace("Step_", " ").strip()

        local_seat = game_state.get("local_seat_id")
        active_player = turn.get("active_player")

        if not turn_num and not phase:
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

            self.turn_strip.setText(f"{who_label} (T{turn_num}) · {phase_short}")
            self.turn_strip.setProperty("who", who)

        self._repolish(self.turn_strip)

    def _refresh_status_dots(self) -> None:
        model = self._dot_values.get("MODEL") or "Local"
        bridge_on = self._dot_values.get("BRIDGE") != "OFF"
        seat = self._dot_values.get("SEAT") or "?"

        t = self._theme_tokens()
        green = t["castable_fg"]
        red = t["uncastable_fg"]
        bridge_color = green if bridge_on else red

        html_dots = (
            f"<span style='color:{t['muted']};'>Backend: </span>"
            f"<span style='color:{t['text']}; font-weight:600;'>{html.escape(model)}</span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<span style='color:{bridge_color}; font-size:13px;'>●</span>"
            f"<span style='color:{t['text']};'> Bridge</span>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<span style='color:{t['muted']};'>Seat: {html.escape(str(seat))}</span>"
        )
        self.status_dots.setText(html_dots)

    def send_chat(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        if text.lower() in ("/report", "/bug", "/debug", "/bugreport"):
            self._session.trigger_debug_report()
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

        your_bf = [c for c in battlefield if c.get("controller_seat_id") == local_seat or c.get("owner_seat_id") == local_seat]
        opp_bf = [c for c in battlefield if c.get("controller_seat_id") != local_seat and c.get("owner_seat_id") != local_seat]

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
                hand_items.append(f"<span style='color:{tokens['castable_fg']};'>{name}</span> <span style='color:{tokens['muted']};'>{cost}</span>")
        hand_joined = " · ".join(hand_items) if hand_items else "<i>Empty</i>"
        hand_html = f"<div style='font-size:11px; margin-bottom:6px;'><b style='color:{tokens['muted']};'>HAND:</b> {hand_joined}</div>"

        # Render Battlefield Creatures & Lands
        creatures = [c for c in your_bf if "creature" in str(c.get("type_line", "")).lower()]
        lands = [c for c in your_bf if "land" in str(c.get("type_line", "")).lower()]

        c_strs = [f"{c.get('name', '?')} ({c.get('power', 0)}/{c.get('toughness', 0)})" for c in creatures[:6]]
        l_strs = [str(c.get('name', '?')) for c in lands[:8]]

        board_html = (
            f"<div style='font-size:11px;'>"
            f"<b style='color:{tokens['muted']};'>CREATURES ({len(creatures)}):</b> {html.escape(' · '.join(c_strs)) if c_strs else 'None'}<br>"
            f"<b style='color:{tokens['muted']};'>LANDS ({len(lands)}):</b> {html.escape(' · '.join(l_strs)) if l_strs else 'None'}"
            f"</div>"
        )

        return f"<div style='font-family:sans-serif;'>{opp_html}{you_html}{hand_html}{board_html}</div>"
