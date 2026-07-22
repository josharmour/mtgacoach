from __future__ import annotations

import html
import sys
from typing import Any

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QAction
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

from .coach_tab import CoachTab, _int_value, _str_value


class CompactCoachPanel(CoachTab):
    """Single-column sidebar layout of the coach UI (~440px wide).

    All process/event/TTS handling is inherited from CoachTab — only the
    widget layout and game-state composition differ. Information is ordered
    by glance priority for use during a match:

        turn strip → status dots → latest advice (hero card) → turn plan
        → board state / activity log (splitter) → controls → chat

    Secondary tools (self-play, debug report, overlay calibration, repair)
    live behind the ⋯ overflow menu so the always-visible surface stays
    calm enough to parse at a glance.
    """

    repair_requested = Signal()
    classic_requested = Signal()

    # The activity feed is a TTS subtitle track, not an operations log: by
    # default it shows only the literal text handed to the speech engine
    # (role "spoken", captured from speak_request/speak_audio events) plus
    # errors. Everything else — advice events with their "PLAN:"/"COACH (...)"
    # framing, autopilot chatter, status lines — is gated behind
    # View → Show Debug Logging.
    _PERTINENT_LOG_ROLES = frozenset({"spoken", "error"})

    # Spoken subtitles get their own color so they stand out from the
    # (debug-only) advice/autopilot lines around them.
    _LOG_COLORS_DARK = {**CoachTab._LOG_COLORS_DARK, "spoken": "#69d46c"}
    _LOG_COLORS_LIGHT = {**CoachTab._LOG_COLORS_LIGHT, "spoken": "#1b7e2c"}

    # -- layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        self._dot_values: dict[str, str] = {}
        self._saved_split_sizes: list[int] | None = None
        self._activity_expanded = True
        self._game_plan: dict[str, Any] = {}
        self._latest_advice: tuple[str, str] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Turn strip: whose turn + phase, tinted green/orange by turn owner.
        self.turn_strip = QLabel("Waiting for MTGA…")
        self.turn_strip.setObjectName("turnStrip")
        self.turn_strip.setProperty("who", "none")
        self.turn_strip.setAlignment(Qt.AlignCenter)
        root.addWidget(self.turn_strip)

        # Status dots: coach backend / bridge / seat as colored ● indicators.
        self.status_dots = QLabel()
        self.status_dots.setObjectName("statusDots")
        self.status_dots.setTextFormat(Qt.RichText)
        self.status_dots.setAlignment(Qt.AlignCenter)
        root.addWidget(self.status_dots)

        # Hero advice card: the latest coach advice, always visible on top.
        card = QFrame()
        card.setObjectName("adviceCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 9)
        card_layout.setSpacing(4)
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("COACH")
        title.setObjectName("adviceTitle")
        self._card_title = title
        self.advice_meta = QLabel("")
        self.advice_meta.setObjectName("adviceMeta")
        meta_row.addWidget(title)
        meta_row.addStretch()
        meta_row.addWidget(self.advice_meta)
        card_layout.addLayout(meta_row)
        self.advice_label = QLabel("Advice will appear here once a match starts.")
        self.advice_label.setObjectName("adviceText")
        self.advice_label.setTextFormat(Qt.RichText)
        self.advice_label.setWordWrap(True)
        self.advice_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(self.advice_label)
        root.addWidget(card)
        self._advice_card = card

        # Sticky autopilot turn-plan panel (inherited update/style logic).
        self.turn_plan_label = QLabel()
        self.turn_plan_label.setObjectName("turnPlanPanel")
        self.turn_plan_label.setWordWrap(True)
        self.turn_plan_label.setTextFormat(Qt.PlainText)
        self.turn_plan_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._apply_turn_plan_style()
        self.turn_plan_label.setVisible(False)
        root.addWidget(self.turn_plan_label)

        # Board view and activity log share a vertical splitter.
        self.game_state_view = QTextEdit()
        self.game_state_view.setObjectName("gameStateView")
        self.game_state_view.setReadOnly(True)
        self.game_state_view.setAcceptRichText(True)
        self.game_state_view.setHtml(self._build_waiting_game_state_html())

        activity = QWidget()
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        activity_layout.setSpacing(2)
        self._log_toggle_btn = QPushButton()
        self._log_toggle_btn.setObjectName("activityToggle")
        self._log_toggle_btn.setFlat(True)
        self._log_toggle_btn.setCursor(Qt.PointingHandCursor)
        self._log_toggle_btn.setToolTip(
            "Show/hide the spoken-advice history.\n"
            "View → Show Debug Logging adds autopilot/status chatter."
        )
        self._log_toggle_btn.clicked.connect(self._toggle_activity_log)
        activity_layout.addWidget(self._log_toggle_btn)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        activity_layout.addWidget(self.log_view)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.game_state_view)
        splitter.addWidget(activity)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 260])
        root.addWidget(splitter, stretch=1)
        self._content_splitter = splitter

        # Controls: two rows of the most-used actions; everything else in ⋯.
        def _btn(label, tooltip, *, command=None, on_click=None, object_name=None):
            b = QPushButton(label)
            b.setToolTip(tooltip)
            b.setCursor(Qt.PointingHandCursor)
            if object_name:
                b.setObjectName(object_name)
            if command is not None:
                b.clicked.connect(
                    lambda _checked=False, cmd=command: self._send_command(cmd)
                )
                self._buttons[command] = b
            elif on_click is not None:
                b.clicked.connect(on_click)
            return b

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        ap_btn = _btn(
            "AP",
            "Toggle autopilot — plays the game for you via the GRE bridge",
            command="toggle_autopilot",
            object_name="apButton",
        )
        ap_btn.setProperty("apOn", "false")
        row1.addWidget(ap_btn, stretch=2)
        stop_btn = _btn(
            "STOP",
            "Force-stop autopilot: halts it AND clears the in-flight plan/turn "
            "intent so re-enabling doesn't resume the same loop",
            command="force_stop",
            object_name="forceStopButton",
        )
        stop_btn.setStyleSheet(
            "QPushButton#forceStopButton { color: #ff5252; font-weight: bold; }"
        )
        row1.addWidget(stop_btn, stretch=1)
        row1.addWidget(
            _btn("Quick", "Cycle the advice style (quick / concise / verbose ...)",
                 command="toggle_style"),
            stretch=1,
        )
        row1.addWidget(
            _btn("Suggest Deck", "Request deck recommendations for current format",
                 on_click=self._suggest_deck),
            stretch=1,
        )
        root.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(_btn("Voice", "Cycle the TTS voice", command="cycle_voice"), stretch=1)
        if self._developer_mode:
            # Dev machines only: cycle through the online gateway's served
            # models (same pipe command the classic tab's Model button sends).
            row2.addWidget(
                _btn("Model", "Cycle through the online gateway's models",
                     command="cycle_model"),
                stretch=1,
            )
        row2.addWidget(
            _btn("Debug Report", "Capture logs + game state and file a bug report",
                 on_click=self._submit_debug_report),
            stretch=1,
        )
        row2.addWidget(_btn("Mute", "Mute / unmute spoken advice", command="toggle_mute"), stretch=1)
        row2.addWidget(self._build_overflow_button())
        root.addLayout(row2)

        chat_row = QHBoxLayout()
        chat_row.setSpacing(6)
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setPlaceholderText("Ask the coach…  (/deck, /analyze)")
        self.chat_input.returnPressed.connect(self.send_chat)
        chat_row.addWidget(self.chat_input, stretch=1)
        send_button = QPushButton("Send")
        send_button.setObjectName("sendButton")
        send_button.setCursor(Qt.PointingHandCursor)
        send_button.setToolTip("Send to the coach")
        send_button.clicked.connect(self.send_chat)
        chat_row.addWidget(send_button)
        root.addLayout(chat_row)

        self._apply_compact_style()
        self._refresh_status_dots()
        self._apply_activity_expanded(bool(get_settings().get("compact_log_expanded", True)))

    def _build_overflow_button(self) -> QToolButton:
        """The ⋯ menu holding secondary/system tools from both classic screens."""
        more = QToolButton()
        more.setObjectName("overflowButton")
        more.setText("⋯")
        more.setToolTip("More tools")
        more.setCursor(Qt.PointingHandCursor)
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        menu.setToolTipsVisible(True)

        bs_action = menu.addAction("Brain Stream Inspector")
        bs_action.setToolTip("Open live streaming inspector for Prompt Context, Reasoning, and Telemetry")
        bs_action.triggered.connect(lambda _checked=False: self.toggle_brain_stream())

        deck_action = menu.addAction("Suggest Deck")
        deck_action.setToolTip("Request deck recommendations & suggestions")
        deck_action.triggered.connect(lambda _checked=False: self._suggest_deck())

        if sys.platform == "win32":
            screen_action = menu.addAction("Analyze Screen")
            screen_action.setToolTip("Analyze a screenshot of the game with the vision model")
            screen_action.triggered.connect(
                lambda _checked=False: self._send_command("analyze_screen")
            )

        # QAction supports the same setText/setEnabled calls the inherited
        # self-play lifecycle code makes against the classic QPushButton.
        self._self_play_btn = QAction("Self-Play", self)
        self._self_play_btn.setToolTip(
            "Stop live coaching and run a headless bot-vs-bot self-play session.\n"
            "This frees the GRE bridge (port 44222) for the self-play process.\n"
            "Output streams into the activity log."
        )
        self._self_play_btn.triggered.connect(
            lambda _checked=False: self._toggle_self_play()
        )
        menu.addAction(self._self_play_btn)

        speed_action = menu.addAction("Speed")
        speed_action.setToolTip("Cycle the speaking speed")
        speed_action.triggered.connect(lambda _checked=False: self._send_command("cycle_speed"))
        # Registered under its command so status updates retitle it
        # ("Speed: 1.4x") just like a toolbar button — QAction has the same
        # setText interface.
        self._buttons["cycle_speed"] = speed_action

        restart_action = menu.addAction("Restart Coach")
        restart_action.setToolTip("Restart the coaching engine")
        restart_action.triggered.connect(
            lambda _checked=False: self._send_command("restart")
        )

        # Overlay tools are available on every platform (Qt-native overlays).
        menu.addSeparator()
        calib_action = menu.addAction("Calibrate Cards")
        calib_action.setCheckable(True)
        calib_action.setToolTip(
            "Draw border around MTGA cards reported by bridge plugin to verify alignment"
        )
        calib_action.toggled.connect(
            lambda checked: self._match_overlay.set_calibration(checked)
        )
        overlay_action = menu.addAction("In-Game Overlay")
        overlay_action.setCheckable(True)
        overlay_action.setChecked(True)
        overlay_tooltip = "Show/hide the in-game overlay (pill + advice panel)"
        if sys.platform.startswith("linux"):
            overlay_tooltip += (
                "\nNote: under pure Wayland the overlay may not track the "
                "MTGA window (XWayland works)."
            )
        overlay_action.setToolTip(overlay_tooltip)
        overlay_action.toggled.connect(
            lambda checked: self._match_overlay.set_enabled(checked)
        )
        reset_action = menu.addAction("Reset Advice Panel")
        reset_action.setToolTip(
            "Snap the advice panel back to its default position and size"
        )
        reset_action.triggered.connect(lambda _checked=False: self._reset_advice_panel())

        menu.addSeparator()
        repair_action = menu.addAction("Setup && Repair…")
        repair_action.setToolTip("Open the setup / repair tools")
        repair_action.triggered.connect(lambda _checked=False: self.repair_requested.emit())
        classic_action = menu.addAction("Switch to Classic Layout")
        classic_action.triggered.connect(lambda _checked=False: self.classic_requested.emit())

        more.setMenu(menu)
        return more

    # -- chat & command handlers ----------------------------------------------

    def _suggest_deck(self) -> None:
        """Trigger deck suggestions for active format."""
        self.append_log("Evaluating MTGA inventory & wildcards for deck suggestions...", role="status")
        if hasattr(self, "chat_input"):
            self.chat_input.setText("/deck")
        self.send_chat()

    def send_chat(self) -> None:
        """Process chat input or command from compact UI."""
        text = ""
        if hasattr(self, "chat_input"):
            text = self.chat_input.text().strip()
            self.chat_input.clear()
        if not text:
            return
        self.append_log(f"> {text}", role="status")
        self._send_command("chat", text)

    def attach_process(self, process: Any) -> None:
        """Attach process to receive IPC signals and send commands."""
        self._process = process
        try:
            process.log_emitted.connect(self._on_ipc_log)
            process.message_emitted.connect(self._on_ipc_message)
            process.status_emitted.connect(self._on_ipc_status)
        except Exception as e:
            logger.warning("Failed to connect process signals: %s", e)

    def detach_process(self) -> None:
        """Detach process handlers."""
        proc = getattr(self, "_process", None)
        if proc is not None:
            try:
                proc.log_emitted.disconnect(self._on_ipc_log)
                proc.message_emitted.disconnect(self._on_ipc_message)
                proc.status_emitted.disconnect(self._on_ipc_status)
            except Exception:
                pass
            self._process = None

    def _send_command(self, command: str, text: str = "") -> None:
        """Send command over IPC pipe to coach process."""
        proc = getattr(self, "_process", None)
        if proc is None and hasattr(self, "parent"):
            parent = self.parent()
            if hasattr(parent, "_process"):
                proc = parent._process

        if proc is not None:
            proc.send_command(command, text)
        else:
            logger.warning("Coach process unavailable for command: %s", command)

    # -- activity log collapse -------------------------------------------------

    def _toggle_activity_log(self) -> None:
        expanded = not self._activity_expanded
        self._apply_activity_expanded(expanded)
        get_settings().set("compact_log_expanded", expanded)

    def _apply_activity_expanded(self, expanded: bool) -> None:
        # Tracked explicitly — isVisible() is unreliable while the panel
        # itself is hidden (during startup, or on the Repair page).
        self._activity_expanded = expanded
        splitter = self._content_splitter
        if expanded:
            self.log_view.setVisible(True)
            sizes = self._saved_split_sizes
            if sizes and len(sizes) == 2 and min(sizes) > 0:
                splitter.setSizes(sizes)
        else:
            self._saved_split_sizes = splitter.sizes()
            self.log_view.setVisible(False)
            header_h = max(24, self._log_toggle_btn.sizeHint().height())
            total = sum(self._saved_split_sizes) or 800
            splitter.setSizes([max(1, total - header_h), header_h])
        self._refresh_activity_toggle_text()

    def _render_log_line(self, role: str, text: str) -> None:
        if not self._show_debug_logging:
            # Subtitle mode renders the whole feed newest-first instead of
            # appending chronologically.
            self._render_subtitle_feed()
            return
        if role == "spoken":
            # Debug mode: keep chronology, but make spoken lines stand out.
            color = self._LOG_COLORS.get("spoken", self._LOG_COLORS["default"])
            muted = self._theme_tokens()["muted"]
            escaped = html.escape(text).replace("\n", "<br>")
            self.log_view.append(
                f"<div style='margin-top:6px;'>"
                f"<span style='color:{muted}; font-family:Consolas;'>»&nbsp;</span>"
                f"<span style='color:{color}; font-family:Consolas;'>{escaped}</span>"
                f"</div>"
            )
            scroll_bar = self.log_view.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.maximum())
            return
        super()._render_log_line(role, text)

    def _rerender_log(self) -> None:
        if self._show_debug_logging:
            super()._rerender_log()
            return
        self._render_subtitle_feed()

    def _render_subtitle_feed(self) -> None:
        """Rebuild the subtitle view newest-first: the latest spoken line sits
        at the top and older ones flow down and off the bottom.
        """
        colors = self._LOG_COLORS
        muted = self._theme_tokens()["muted"]
        blocks: list[str] = []
        for role, text in reversed(self._all_log_lines):
            if not self._is_role_visible(role):
                continue
            escaped = html.escape(text).replace("\n", "<br>")
            if role == "spoken":
                blocks.append(
                    f"<div style='margin-bottom:8px;'>"
                    f"<span style='color:{muted};'>»&nbsp;</span>"
                    f"<span style='color:{colors['spoken']};'>{escaped}</span>"
                    f"</div>"
                )
            else:
                color = colors.get(role, colors["default"])
                blocks.append(
                    f"<div style='margin-bottom:8px; color:{color};'>{escaped}</div>"
                )
        self.log_view.setHtml(
            "<div style='font-family:Consolas,\"Courier New\",monospace;'>"
            + "".join(blocks)
            + "</div>"
        )
        self.log_view.verticalScrollBar().setValue(0)

    def _refresh_activity_toggle_text(self) -> None:
        arrow = "▾" if self._activity_expanded else "▸"
        label = "ACTIVITY (DEBUG)" if self._show_debug_logging else "SPOKEN ADVICE"
        self._log_toggle_btn.setText(f"{arrow}  {label}")

    def set_debug_logging(self, enabled: bool) -> None:
        super().set_debug_logging(enabled)
        self._refresh_activity_toggle_text()

    # -- status indicators -------------------------------------------------------

    def _set_status_label(self, key: str, value: str) -> None:
        self._dot_values[key] = (value or "").strip() or "-"
        self._refresh_status_dots()

    def _refresh_status_dots(self) -> None:
        tokens = self._theme_tokens()
        ok = tokens["player"]
        bad = tokens["planeswalker"]
        muted = tokens["muted"]

        def dot(color: str, label: str) -> str:
            return (
                f"<span style='color:{color};'>●</span>&nbsp;"
                f"<span style='color:{tokens['text']};'>{html.escape(label)}</span>"
            )

        coach = self._dot_values.get("coach", "-")
        bridge = self._dot_values.get("bridge", "-")
        seat = self._dot_values.get("seat", "-")

        parts = [dot(ok if coach != "-" else muted, coach if coach != "-" else "Coach")]
        bridge_lower = bridge.lower()
        if "connected" in bridge_lower and "disconnected" not in bridge_lower:
            bridge_color = ok
        elif "log mode" in bridge_lower:
            # Native Mac client: log-only coaching is the designed state,
            # not a failure — show it healthy, not red.
            bridge_color = ok
        elif bridge == "-":
            bridge_color = muted
        else:
            bridge_color = bad
        bridge_label = "Bridge" if bridge == "-" else f"Bridge {bridge}"
        parts.append(dot(bridge_color, bridge_label))
        if seat != "-":
            parts.append(dot(tokens["spell"], seat))

        self.status_dots.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def _update_status(self, key: str, value: str) -> None:
        super()._update_status(key, value)
        if key.upper() == "AUTOPILOT":
            button = self._buttons.get("toggle_autopilot")
            if button is not None:
                button.setProperty("apOn", "true" if "ON" in value.upper() else "false")
                self._repolish(button)

    # -- hero advice ----------------------------------------------------------

    def _handle_event(self, payload: Any) -> None:
        super()._handle_event(payload)
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type in ("speak_request", "speak_audio"):
            # Subtitle track: log exactly what the speech engine was given.
            spoken = str(payload.get("text", "")).strip()
            if spoken:
                self.append_log(spoken, role="spoken")
            return
        if event_type == "game_plan":
            data = payload.get("data")
            plan = data if isinstance(data, dict) else {}
            if not any(plan.get(k) for k in ("win_conditions", "path", "threat", "develop_next")):
                plan = {}
            self._game_plan = plan
            self._refresh_hero_card()
            return
        if event_type != "advice":
            return
        seat_info = str(payload.get("seat_info", ""))
        text = str(payload.get("text", "")).strip()
        if not text:
            return
        is_autopilot = seat_info.strip().upper() == "AUTOPILOT"
        t_upper = text.upper()
        is_strategic = t_upper.startswith("PLAN:") or "MANUAL REQUIRED" in t_upper[:80]
        # Mirror the classic filter: the hero card shows coach advice and
        # strategic plans, not per-decision autopilot chatter (which stays
        # in the activity log).
        if is_autopilot and not is_strategic:
            return
        if text.startswith("PLAN:"):
            text = text[5:].strip()
        self._set_hero_advice(text, seat_info)

    def _set_hero_advice(self, text: str, seat_info: str) -> None:
        display = text.strip()
        if len(display) > 600:
            display = display[:597].rstrip() + "…"
        self._latest_advice = (display, seat_info.strip().upper())
        self._refresh_hero_card()

    def _refresh_hero_card(self) -> None:
        """Top card = current strategy. While a structured game plan exists it
        renders the WIN/PATH/THREAT/NEXT hierarchy (autopilot's plan when AP
        drives, the coach's otherwise); without one it falls back to the
        latest strategic/coach advice line.
        """
        if self._game_plan:
            self._card_title.setText("STRATEGY")
            meta_bits = []
            turn_formed = _int_value(self._game_plan.get("turn_formed"))
            if turn_formed:
                meta_bits.append(f"T{turn_formed}")
            source = str(self._game_plan.get("source") or "").strip().upper()
            if source:
                meta_bits.append(source)
            self.advice_meta.setText(" · ".join(meta_bits))
            self.advice_label.setText(self._build_strategy_html(self._game_plan))
            return
        self._card_title.setText("COACH")
        if self._latest_advice is not None:
            text, seat = self._latest_advice
            self.advice_meta.setText(seat)
            self.advice_label.setText(html.escape(text).replace("\n", "<br>"))

    def _build_strategy_html(self, plan: dict[str, Any]) -> str:
        t = self._theme_tokens()

        def row(label: str, value: str, color: str) -> str:
            return (
                f"<tr>"
                f"<td style='color:{t['muted']}; font-size:10px; font-weight:700;"
                f" padding:1px 8px 1px 0; vertical-align:top;'>{label}</td>"
                f"<td style='color:{color}; padding:1px 0;'>{html.escape(value)}</td>"
                f"</tr>"
            )

        rows: list[str] = []
        wins = [str(w).strip() for w in (plan.get("win_conditions") or []) if str(w).strip()]
        if wins:
            rows.append(row("WIN", " / ".join(wins), t["castable_fg"]))
        path = str(plan.get("path") or "").strip()
        if path:
            rows.append(row("PATH", path, t["text"]))
        threat = str(plan.get("threat") or "").strip()
        if threat:
            rows.append(row("THREAT", threat, t["uncastable_fg"]))
        develop = str(plan.get("develop_next") or "").strip()
        if develop:
            rows.append(row("NEXT", develop, t["spell"]))
        return f"<table cellspacing='0' cellpadding='0'>{''.join(rows)}</table>"

    # -- game state -----------------------------------------------------------

    def _update_game_state(self, data: dict[str, Any]) -> None:
        self._refresh_turn_strip(data)
        super()._update_game_state(data)

    def refresh_game_state_view(self) -> None:
        super().refresh_game_state_view()
        if not self._last_game_state_payload:
            self.turn_strip.setText("Waiting for MTGA…")
            self.turn_strip.setProperty("who", "none")
            self._repolish(self.turn_strip)

    def _refresh_turn_strip(self, data: dict[str, Any]) -> None:
        turn = data.get("turn")
        if not isinstance(turn, dict):
            turn = {}
        local_seat = _int_value(data.get("local_seat_id"))
        turn_num = _int_value(turn.get("turn_number"))
        phase = _str_value(turn.get("phase")).replace("Phase_", "") or "?"
        step = _str_value(turn.get("step")).replace("Step_", "")
        if step in ("None", "-", "?"):
            step = ""
        active_player = _int_value(turn.get("active_player"))

        who = "none"
        if not turn_num and not active_player:
            text = "Waiting for MTGA…"
        else:
            bits = [f"T{turn_num}", phase]
            if step and step != phase:
                bits.append(step)
            if active_player and local_seat:
                yours = active_player == local_seat
                who = "you" if yours else "opp"
                bits.append("YOUR TURN" if yours else "OPP TURN")
            text = "  ·  ".join(bits)
            if _str_value(data.get("pending_decision")).strip():
                text = f"⚠ {text}"

        self.turn_strip.setText(text)
        if self.turn_strip.property("who") != who:
            self.turn_strip.setProperty("who", who)
            self._repolish(self.turn_strip)

    def _build_game_state_html(self, data: dict[str, Any]) -> str:
        """Narrow-column board view: pending decision → OPP → stack → YOU
        (hand inline). The turn header lives in the strip widget above, not
        in this document.
        """
        tokens = self._theme_tokens()
        zones = data.get("zones")
        if not isinstance(zones, dict):
            zones = data

        players = data.get("players", [])
        local_seat = _int_value(data.get("local_seat_id"))
        local_player = next(
            (p for p in players if isinstance(p, dict) and p.get("is_local") is True), None
        )
        opponent_player = next(
            (p for p in players if isinstance(p, dict) and p.get("is_local") is not True), None
        )
        opponent_seat = (
            _int_value(opponent_player.get("seat_id")) if isinstance(opponent_player, dict) else 0
        )

        pending = self._render_pending_decision(
            data.get("pending_decision"),
            data.get("decision_context"),
            data.get("legal_actions"),
            tokens,
        )

        battlefield = zones.get("battlefield", [])
        battlefield = battlefield if isinstance(battlefield, list) else []
        opp_cards = [
            card for card in battlefield
            if isinstance(card, dict) and self._card_controller_seat(card) == opponent_seat
        ]
        you_cards = [
            card for card in battlefield
            if isinstance(card, dict) and self._card_controller_seat(card) == local_seat
        ]

        opp_zone = (
            f"<div style='margin:0 0 4px 0; padding:5px 7px;"
            f" border:1px solid {tokens['opponent']}40; border-radius:8px;"
            f" background:{tokens['panel']};'>"
            f"{self._render_seat_strip('OPP', opponent_player, data, zones, opponent_seat, tokens)}"
            f"{self._render_board_lanes(opp_cards, tokens)}"
            f"</div>"
        )

        stack_html = self._render_stack_section(zones.get("stack"), tokens)

        hand_cards = self._cards_for_zone_and_seat(
            zones.get("my_hand") or zones.get("hand"), local_seat, allow_unknown_owner=True
        )
        hand_html = self._render_hand_lane(hand_cards, data, tokens)
        you_zone = (
            f"<div style='margin:4px 0 0 0; padding:5px 7px;"
            f" border:1px solid {tokens['player']}40; border-radius:8px;"
            f" background:{tokens['panel']};'>"
            f"{self._render_seat_strip('YOU', local_player, data, zones, local_seat, tokens)}"
            f"{self._render_board_lanes(you_cards, tokens)}"
            f"{hand_html}"
            f"</div>"
        )

        # No legal-actions section here: that list is engine plumbing, not
        # user-facing. It still drives the hand castability colors and the
        # pending-decision options above.
        return (
            f"<div style='font-family:Consolas,\"Courier New\",monospace; color:{tokens['text']};"
            f" background:{tokens['bg']}; padding:6px;'>"
            f"{pending}{opp_zone}{stack_html}{you_zone}"
            f"</div>"
        )

    def _render_seat_strip(
        self,
        tag: str,
        player: Any,
        game_state: dict[str, Any],
        zones: dict[str, Any],
        seat_id: int,
        tokens: dict[str, str],
    ) -> str:
        """One-line seat summary: tag, big life + bar, then count chips.

            OPP  ♥ 17 ▰▰▰▰▰▰▰▰▱▱  ✋ 6 · 🪦 2
        """
        accent = tokens["opponent"] if tag == "OPP" else tokens["player"]
        life_value = _int_value(player.get("life_total")) if isinstance(player, dict) else 0
        try:
            starting_life = int(game_state.get("starting_life") or 20) or 20
        except (TypeError, ValueError):
            starting_life = 20
        ratio = min(1.0, max(0, life_value) / max(1, starting_life))
        filled = int(round(ratio * 10))
        bar = "▰" * filled + "▱" * (10 - filled)

        grave_cards = self._cards_for_zone_and_seat(zones.get("graveyard"), seat_id)
        exile_cards = self._cards_for_zone_and_seat(zones.get("exile"), seat_id)

        chips: list[tuple[str, str, str]] = []
        if tag == "OPP":
            chips.append(("✋", str(_int_value(zones.get("opponent_hand_count"))), "Opponent hand size"))
        else:
            library_count = zones.get("library_count")
            chips.append((
                "📚",
                "?" if library_count is None else str(_int_value(library_count)),
                "Your library count",
            ))
        chips.append((
            "🪦",
            str(len(grave_cards)),
            f"Graveyard: {self._zone_summary(grave_cards, 8) if grave_cards else 'empty'}",
        ))
        # Exile is usually 0 — only spend width on it when non-empty.
        if exile_cards:
            chips.append(("⬜", str(len(exile_cards)), f"Exile: {self._zone_summary(exile_cards, 8)}"))

        chip_html = "&nbsp;·&nbsp;".join(
            f"<span title='{html.escape(tip)}'>"
            f"<span style='color:{tokens['muted']};'>{icon}</span>"
            f"<span style='color:{tokens['text']}; font-weight:600;'>&nbsp;{html.escape(value)}</span>"
            f"</span>"
            for icon, value, tip in chips
        )
        return (
            f"<div style='font-size:11px; margin:0 0 3px 0;'>"
            f"<span style='color:{accent}; font-weight:700;'>{tag}</span>"
            f"&nbsp;&nbsp;"
            f"<span style='color:{accent}; font-size:14px; font-weight:700;' title='Life total'>"
            f"♥&nbsp;{life_value}</span>"
            f"&nbsp;<span style='color:{accent}; letter-spacing:-1px;'>{bar}</span>"
            f"&nbsp;&nbsp;{chip_html}"
            f"</div>"
        )

    def _render_hand_lane(
        self,
        cards: list[dict[str, Any]],
        game_state: dict[str, Any],
        tokens: dict[str, str],
    ) -> str:
        """Your hand as a lane row matching the battlefield lanes, with
        castability carried by text color instead of badge boxes:

            HAND (2):  Earthbender Ascension 2G · Icetill Explorer 2GG
        """
        if not cards:
            return ""
        castable_names = self._castable_hand_names(game_state)
        rendered: list[str] = []
        for card in cards:
            if not isinstance(card, dict):
                continue
            status = self._hand_card_status(card, castable_names, game_state)
            if status == "castable":
                color, tip = tokens["castable_fg"], "Castable now"
            elif status == "uncastable":
                color, tip = tokens["uncastable_fg"], "Not enough mana"
            elif status == "land":
                color, tip = tokens["land"], "Land"
            else:
                color, tip = tokens["hand_neutral_fg"], ""
            name = _str_value(card.get("name"), "?")
            cost = _str_value(card.get("mana_cost")).replace("{", "").replace("}", "")
            label = f"{name} {cost}".strip()
            title_attr = f" title='{html.escape(tip)}'" if tip else ""
            rendered.append(
                f"<span style='color:{color};'{title_attr}>{html.escape(label)}</span>"
            )
        if not rendered:
            return ""
        joined = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(rendered)
        return (
            f"<div style='font-size:11px; line-height:1.45;'>"
            f"<span style='color:{tokens['muted']}; text-transform:uppercase;"
            f" letter-spacing:0.04em;'>Hand ({len(rendered)}):</span>&nbsp;&nbsp;"
            f"{joined}"
            f"</div>"
        )

    def _render_board_lanes(self, cards: list[dict[str, Any]], tokens: dict[str, str]) -> str:
        """Board lanes ordered for glanceability: threats first, lands last."""
        grouped = self._group_battlefield(cards)
        parts = [
            self._render_card_lane(label, grouped.get(key, []), key, tokens)
            for key, label in (
                ("creature", "Creatures"),
                ("planeswalker", "PW"),
                ("battle", "Battles"),
                ("enchantment", "Ench."),
                ("artifact", "Artifacts"),
                ("other", "Other"),
                ("land", "Lands"),
            )
        ]
        body = "".join(part for part in parts if part)
        if not body:
            return ""
        return f"<div style='margin-left:2px;'>{body}</div>"

    # -- styling ----------------------------------------------------------------

    @staticmethod
    def _repolish(widget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
            try:
                self._apply_compact_style()
                self._refresh_status_dots()
                self._refresh_hero_card()
            except Exception:
                pass

    def _apply_compact_style(self) -> None:
        t = self._theme_tokens()
        accent = t["spell"]
        self.setStyleSheet(
            f"""
#turnStrip {{
    background: {t['panel2']};
    color: {t['header']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    padding: 7px 10px;
    font-size: 13px;
    font-weight: 700;
}}
#turnStrip[who="you"] {{
    background: {t['castable_bg']};
    color: {t['castable_fg']};
    border-color: {t['castable_fg']};
}}
#turnStrip[who="opp"] {{
    background: {t['uncastable_bg']};
    color: {t['uncastable_fg']};
    border-color: {t['uncastable_fg']};
}}
#statusDots {{
    font-size: 11px;
    padding: 0 2px;
}}
#adviceCard {{
    background: {t['panel']};
    border: 1px solid {t['border']};
    border-left: 4px solid {accent};
    border-radius: 10px;
}}
#adviceTitle {{
    color: {accent};
    font-size: 10px;
    font-weight: 700;
}}
#adviceMeta {{
    color: {t['muted']};
    font-size: 10px;
    font-weight: 600;
}}
#adviceText {{
    color: {t['text']};
    font-size: 13px;
}}
#activityToggle {{
    border: none;
    background: transparent;
    color: {t['muted']};
    text-align: left;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 4px;
}}
#activityToggle:hover {{
    color: {t['text']};
}}
QTextEdit#gameStateView, QTextEdit#logView {{
    border: 1px solid {t['border']};
    border-radius: 8px;
    background: {t['bg']};
}}
QPushButton#apButton {{
    font-weight: 700;
}}
QPushButton#apButton[apOn="true"] {{
    background: {t['castable_bg']};
    color: {t['castable_fg']};
    border: 1px solid {t['castable_fg']};
}}
QToolButton#overflowButton {{
    border: 1px solid {t['border']};
    border-radius: 6px;
    padding: 4px 10px;
    font-weight: 700;
}}
QToolButton#overflowButton::menu-indicator {{
    width: 0px;
}}
QPushButton#sendButton {{
    font-weight: 600;
}}
"""
        )
