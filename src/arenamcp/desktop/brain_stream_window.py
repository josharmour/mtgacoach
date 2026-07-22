from __future__ import annotations

import json
import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class BrainStreamWindow(QMainWindow):
    """Live Streaming Inspector Window.

    Provides a real-time view into the coaching engine's internal workings:
    1. Live Prompt Context: raw GRE state, hand, battlefield, draw odds, turn history.
    2. Live Reasoning Stream: token streaming of LLM reasoning traces.
    3. Engine Telemetry: latency badge (e.g. "129ms vLLM"), trigger event log, and bridge connection state.
    """

    window_closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Brain Stream Inspector — mtgacoach")
        self.resize(1350, 880)
        self.setMinimumSize(900, 600)

        self._trigger_count = 0
        self._build_ui()
        self._apply_dark_theme()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # -- Telemetry Header Bar ----------------------------------------------
        header_bar = QFrame()
        header_bar.setFrameShape(QFrame.StyledPanel)
        header_bar.setStyleSheet(
            "QFrame { background: rgba(30, 30, 46, 0.85); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 8px; padding: 6px 12px; }"
        )
        header_layout = QHBoxLayout(header_bar)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(14)

        title_lbl = QLabel("🧠 BRAIN STREAM INSPECTOR")
        title_lbl.setStyleSheet("font-weight: 700; font-size: 14px; color: #cdd6f4;")
        header_layout.addWidget(title_lbl)

        header_layout.addStretch()

        # Telemetry Badges
        self.latency_badge = QLabel("⚡ 129ms vLLM")
        self.latency_badge.setStyleSheet(
            "QLabel { background: #313244; color: #a6e3a1; font-weight: 600; font-size: 12px; border-radius: 12px; padding: 4px 10px; border: 1px solid #a6e3a1; }"
        )
        header_layout.addWidget(self.latency_badge)

        self.bridge_badge = QLabel("● Bridge: Disconnected")
        self.bridge_badge.setStyleSheet(
            "QLabel { background: #313244; color: #f38ba8; font-weight: 600; font-size: 12px; border-radius: 12px; padding: 4px 10px; border: 1px solid #f38ba8; }"
        )
        header_layout.addWidget(self.bridge_badge)

        self.trigger_badge = QLabel("🎯 Triggers: 0")
        self.trigger_badge.setStyleSheet(
            "QLabel { background: #313244; color: #89b4fa; font-weight: 600; font-size: 12px; border-radius: 12px; padding: 4px 10px; border: 1px solid #89b4fa; }"
        )
        header_layout.addWidget(self.trigger_badge)

        clear_btn = QPushButton("Clear Stream")
        clear_btn.setStyleSheet(
            "QPushButton { background: #45475a; color: #cdd6f4; border: none; border-radius: 4px; padding: 4px 10px; font-weight: 600; }"
            "QPushButton:hover { background: #585b70; }"
        )
        clear_btn.clicked.connect(self.clear_all)
        header_layout.addWidget(clear_btn)

        root.addWidget(header_bar)

        # -- Main Content Splitter ---------------------------------------------
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)

        # Left Column: Multi-Pane Live Context & Data Dashboard
        context_container = QFrame()
        context_container.setFrameShape(QFrame.StyledPanel)
        context_layout = QVBoxLayout(context_container)
        context_layout.setContentsMargins(6, 6, 6, 6)

        self.context_tabs = QTabWidget()
        
        # --- TAB 1: Live Game Dashboard (2x2 Multi-Pane Grid) ---
        dashboard_widget = QWidget()
        dash_layout = QVBoxLayout(dashboard_widget)
        dash_layout.setContentsMargins(2, 2, 2, 2)

        dash_splitter_v = QSplitter(Qt.Vertical)
        dash_splitter_v.setChildrenCollapsible(False)

        top_row = QSplitter(Qt.Horizontal)
        top_row.setChildrenCollapsible(False)

        # 1. Your Hand Pane
        hand_box = QGroupBox("🎴 YOUR HAND")
        hand_lay = QVBoxLayout(hand_box)
        hand_lay.setContentsMargins(4, 6, 4, 4)
        self.hand_view = QTextEdit()
        self.hand_view.setReadOnly(True)
        self.hand_view.setFont(QFont("Consolas", 10))
        hand_lay.addWidget(self.hand_view)
        top_row.addWidget(hand_box)

        # 2. Battlefield State Pane
        bf_box = QGroupBox("⚔️ BATTLEFIELD STATE")
        bf_lay = QVBoxLayout(bf_box)
        bf_lay.setContentsMargins(4, 6, 4, 4)
        self.battlefield_view = QTextEdit()
        self.battlefield_view.setReadOnly(True)
        self.battlefield_view.setFont(QFont("Consolas", 10))
        bf_lay.addWidget(self.battlefield_view)
        top_row.addWidget(bf_box)

        bottom_row = QSplitter(Qt.Horizontal)
        bottom_row.setChildrenCollapsible(False)

        # 3. Draw Odds & Strategy Pane
        odds_box = QGroupBox("📈 DRAW ODDS & STRATEGY")
        odds_lay = QVBoxLayout(odds_box)
        odds_lay.setContentsMargins(4, 6, 4, 4)
        self.draw_odds_view = QTextEdit()
        self.draw_odds_view.setReadOnly(True)
        self.draw_odds_view.setFont(QFont("Consolas", 10))
        odds_lay.addWidget(self.draw_odds_view)
        bottom_row.addWidget(odds_box)

        # 4. Match Turn Timeline Pane
        hist_box = QGroupBox("📜 MATCH TURN TIMELINE")
        hist_lay = QVBoxLayout(hist_box)
        hist_lay.setContentsMargins(4, 6, 4, 4)
        self.turn_history_view = QTextEdit()
        self.turn_history_view.setReadOnly(True)
        self.turn_history_view.setFont(QFont("Consolas", 10))
        hist_lay.addWidget(self.turn_history_view)
        bottom_row.addWidget(hist_box)

        dash_splitter_v.addWidget(top_row)
        dash_splitter_v.addWidget(bottom_row)
        dash_splitter_v.setSizes([350, 350])
        dash_layout.addWidget(dash_splitter_v)

        self.context_tabs.addTab(dashboard_widget, "📊 Game Dashboard")

        # --- TAB 2: Full Advice Stream (Concatenated across turns) ---
        self.advice_history_view = QTextEdit()
        self.advice_history_view.setReadOnly(True)
        self.advice_history_view.setFont(QFont("Consolas", 10))
        self.advice_history_view.setPlaceholderText("Concatenated advice history across turns will appear here...")
        self.context_tabs.addTab(self.advice_history_view, "💬 Advice Stream")

        # --- TAB 3: Raw GRE State ---
        self.raw_gre_view = QTextEdit()
        self.raw_gre_view.setReadOnly(True)
        self.raw_gre_view.setFont(QFont("Consolas", 9))
        self.context_tabs.addTab(self.raw_gre_view, "🔍 Raw GRE State")

        context_layout.addWidget(self.context_tabs)
        main_splitter.addWidget(context_container)

        # Right Splitter: Reasoning Stream & Telemetry Event Log
        right_splitter = QSplitter(Qt.Vertical)

        # Top Right: Live Reasoning Stream
        reasoning_container = QFrame()
        reasoning_layout = QVBoxLayout(reasoning_container)
        reasoning_layout.setContentsMargins(6, 6, 6, 6)

        reasoning_hdr = QLabel("LIVE REASONING STREAM (LLM Traces)")
        reasoning_hdr.setStyleSheet("font-weight: 700; color: #a6e3a1; margin-bottom: 4px;")
        reasoning_layout.addWidget(reasoning_hdr)

        self.reasoning_view = QTextEdit()
        self.reasoning_view.setReadOnly(True)
        self.reasoning_view.setFont(QFont("Consolas", 10))
        self.reasoning_view.setStyleSheet(
            "QTextEdit { background: #11111b; color: #a6e3a1; border: 1px solid #313244; border-radius: 6px; padding: 6px; }"
        )
        self.reasoning_view.setPlaceholderText("Waiting for live reasoning traces from LLM backend...")
        reasoning_layout.addWidget(self.reasoning_view)

        right_splitter.addWidget(reasoning_container)

        # Bottom Right: Trigger Event Log
        telemetry_container = QFrame()
        telemetry_layout = QVBoxLayout(telemetry_container)
        telemetry_layout.setContentsMargins(6, 6, 6, 6)

        telemetry_hdr = QLabel("TRIGGER EVENT LOG")
        telemetry_hdr.setStyleSheet("font-weight: 700; color: #fab387; margin-bottom: 4px;")
        telemetry_layout.addWidget(telemetry_hdr)

        self.trigger_log_view = QTextEdit()
        self.trigger_log_view.setReadOnly(True)
        self.trigger_log_view.setFont(QFont("Consolas", 9))
        self.trigger_log_view.setStyleSheet(
            "QTextEdit { background: #11111b; color: #cdd6f4; border: 1px solid #313244; border-radius: 6px; padding: 6px; }"
        )
        telemetry_layout.addWidget(self.trigger_log_view)

        right_splitter.addWidget(telemetry_container)
        right_splitter.setSizes([450, 250])

        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([500, 600])

        root.addWidget(main_splitter, stretch=1)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e2e; }
            QTabWidget::pane { border: 1px solid #313244; background: #181825; border-radius: 6px; }
            QTabBar::tab { background: #313244; color: #a6adc8; padding: 6px 12px; margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #45475a; color: #cdd6f4; font-weight: bold; }
            QTextEdit { background: #181825; color: #cdd6f4; border: 1px solid #313244; border-radius: 6px; }
        """)

    # -- Updates & Event Handling API -----------------------------------------

    def update_telemetry(
        self,
        latency: str | float | int = "",
        backend: str = "vLLM",
        bridge_connected: bool = False,
    ) -> None:
        """Update telemetry badges for latency, backend model name, and bridge connection."""
        if latency != "":
            if isinstance(latency, (int, float)):
                lat_str = f"⚡ {int(latency)}ms {backend}"
            else:
                lat_str = f"⚡ {latency} {backend}"
            self.latency_badge.setText(lat_str)

        if bridge_connected:
            self.bridge_badge.setText("● Bridge: Connected")
            self.bridge_badge.setStyleSheet(
                "QLabel { background: #313244; color: #a6e3a1; font-weight: 600; font-size: 12px; border-radius: 12px; padding: 4px 10px; border: 1px solid #a6e3a1; }"
            )
        else:
            self.bridge_badge.setText("● Bridge: Disconnected")
            self.bridge_badge.setStyleSheet(
                "QLabel { background: #313244; color: #f38ba8; font-weight: 600; font-size: 12px; border-radius: 12px; padding: 4px 10px; border: 1px solid #f38ba8; }"
            )

    def log_trigger_event(self, event_name: str, details: str = "") -> None:
        """Append a trigger event entry to the telemetry event log."""
        self._trigger_count += 1
        self.trigger_badge.setText(f"🎯 Triggers: {self._trigger_count}")
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {event_name}"
        if details:
            line += f": {details}"
        self.trigger_log_view.append(line)
        sb = self.trigger_log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def update_game_state(self, state_data: dict[str, Any]) -> None:
        """Update Live Prompt Context panels with raw GRE state, hand, battlefield, odds, history."""
        if not isinstance(state_data, dict):
            return

        # 1. Raw GRE State
        try:
            raw_json = json.dumps(state_data, indent=2)
            self.raw_gre_view.setPlainText(raw_json)
        except Exception:
            self.raw_gre_view.setPlainText(str(state_data))

        # 2. Hand
        hand = state_data.get("hand") or []
        if isinstance(hand, list):
            hand_lines = []
            for item in hand:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("card_name") or f"GrpId {item.get('grp_id')}"
                    cost = item.get("mana_cost") or item.get("cost") or ""
                    hand_lines.append(f"• {name}  [{cost}]" if cost else f"• {name}")
                else:
                    hand_lines.append(f"• {item}")
            self.hand_view.setPlainText("\n".join(hand_lines) if hand_lines else "(Hand empty)")
        else:
            self.hand_view.setPlainText(str(hand))

        # 3. Battlefield
        bf = state_data.get("battlefield") or []
        players = state_data.get("players") or {}
        bf_lines = []
        if isinstance(players, dict):
            me = players.get("hero") or players.get("me") or {}
            opp = players.get("opponent") or players.get("opp") or {}
            if me:
                bf_lines.append(f"YOU: Life={me.get('life', '?')} Mana={me.get('mana_pool', '{}')}")
            if opp:
                bf_lines.append(f"OPP: Life={opp.get('life', '?')}")
            if bf_lines:
                bf_lines.append("-" * 35)

        if isinstance(bf, list):
            for obj in bf:
                if isinstance(obj, dict):
                    owner = "You" if obj.get("controller") == 1 or obj.get("is_mine") else "Opp"
                    name = obj.get("name") or obj.get("card_name") or f"ID {obj.get('id')}"
                    pt = ""
                    if obj.get("power") is not None and obj.get("toughness") is not None:
                        pt = f" [{obj.get('power')}/{obj.get('toughness')}]"
                    tapped = " (Tapped)" if obj.get("tapped") else ""
                    bf_lines.append(f"• [{owner}] {name}{pt}{tapped}")
                else:
                    bf_lines.append(f"• {obj}")
        self.battlefield_view.setPlainText("\n".join(bf_lines) if bf_lines else "(Battlefield empty)")

        # 4. Draw Odds
        odds = state_data.get("draw_odds") or state_data.get("odds") or {}
        if isinstance(odds, dict) and odds:
            odds_lines = [f"{k}: {v}" for k, v in odds.items()]
            self.draw_odds_view.setPlainText("\n".join(odds_lines))
        else:
            self.draw_odds_view.setPlainText("Draw Odds: Calculating active deck probabilities...")

        # 5. Turn History (Concatenated & Persisted Across Turns)
        if not hasattr(self, "_turn_history_set"):
            self._turn_history_set = set()
            self._last_turn_key = None

        turn = state_data.get("turn") or {}
        turn_num = turn.get("turn_number") or state_data.get("turn_number", 0)
        phase = turn.get("phase") or state_data.get("phase", "") or "Main"
        active_p = state_data.get("active_player") or turn.get("active_player") or ""

        # Reset turn history accumulator when Turn 1 starts in a new match
        if turn_num == 1 and phase in ("Beginning", "Main 1", "Main") and getattr(self, "_last_turn_num", 0) > 1:
            self._turn_history_set.clear()
            self.turn_history_view.clear()

        self._last_turn_num = turn_num

        turn_key = f"T{turn_num}_{phase}_{active_p}"
        if turn_num > 0 and turn_key != self._last_turn_key:
            self._last_turn_key = turn_key
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            who = "HERO" if str(active_p) in ("1", "hero", "me") else ("OPPONENT" if active_p else "ACTIVE")
            entry_line = f"[{ts}] Turn {turn_num} ({phase}) — {who}'s Turn"
            if entry_line not in self._turn_history_set:
                self._turn_history_set.add(entry_line)
                self.turn_history_view.append(entry_line)

        # Append any structural history items passed in payload
        history = state_data.get("turn_history") or state_data.get("history") or []
        if isinstance(history, list):
            for item in history:
                item_str = f"  • {item}"
                if item_str not in self._turn_history_set:
                    self._turn_history_set.add(item_str)
                    self.turn_history_view.append(item_str)

    def append_advice_history(self, seat_info: str, text: str) -> None:
        """Append an advice entry to the concatenated multi-turn advice stream."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        prefix = f"[{ts}] COACH ({seat_info})" if seat_info else f"[{ts}] COACH"
        entry = f"{prefix}\n{text}\n" + ("-" * 40)
        self.advice_history_view.append(entry)
        sb = self.advice_history_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def append_reasoning_token(self, token: str) -> None:
        """Stream a single reasoning token or trace chunk from the LLM backend."""
        self.reasoning_view.moveCursor(self.reasoning_view.textCursor().End)
        self.reasoning_view.insertPlainText(token)
        sb = self.reasoning_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_reasoning_text(self, text: str) -> None:
        """Replace the reasoning stream text entirely."""
        self.reasoning_view.setPlainText(text)
        sb = self.reasoning_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_all(self) -> None:
        self.reasoning_view.clear()
        self.trigger_log_view.clear()
        self.advice_history_view.clear()

    def closeEvent(self, event) -> None:
        self.window_closed.emit()
        super().closeEvent(event)
