from __future__ import annotations

import json
import logging
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QFrame,
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
        self.resize(1150, 780)
        self.setMinimumSize(850, 550)

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

        # Left Column: Live Prompt Context (Tabs)
        context_container = QFrame()
        context_container.setFrameShape(QFrame.StyledPanel)
        context_layout = QVBoxLayout(context_container)
        context_layout.setContentsMargins(6, 6, 6, 6)

        context_hdr = QLabel("LIVE PROMPT CONTEXT")
        context_hdr.setStyleSheet("font-weight: 700; color: #89b4fa; margin-bottom: 4px;")
        context_layout.addWidget(context_hdr)

        self.context_tabs = QTabWidget()
        
        # 1. Hand
        self.hand_view = QTextEdit()
        self.hand_view.setReadOnly(True)
        self.hand_view.setFont(QFont("Consolas", 10))
        self.context_tabs.addTab(self.hand_view, "Hand")

        # 2. Battlefield
        self.battlefield_view = QTextEdit()
        self.battlefield_view.setReadOnly(True)
        self.battlefield_view.setFont(QFont("Consolas", 10))
        self.context_tabs.addTab(self.battlefield_view, "Battlefield")

        # 3. Draw Odds
        self.draw_odds_view = QTextEdit()
        self.draw_odds_view.setReadOnly(True)
        self.draw_odds_view.setFont(QFont("Consolas", 10))
        self.context_tabs.addTab(self.draw_odds_view, "Draw Odds")

        # 4. Turn History
        self.turn_history_view = QTextEdit()
        self.turn_history_view.setReadOnly(True)
        self.turn_history_view.setFont(QFont("Consolas", 10))
        self.context_tabs.addTab(self.turn_history_view, "Turn History")

        # 5. Raw GRE State
        self.raw_gre_view = QTextEdit()
        self.raw_gre_view.setReadOnly(True)
        self.raw_gre_view.setFont(QFont("Consolas", 9))
        self.context_tabs.addTab(self.raw_gre_view, "Raw GRE State")

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

        # 5. Turn History
        history = state_data.get("turn_history") or state_data.get("history") or []
        turn = state_data.get("turn") or {}
        turn_num = turn.get("turn_number") or state_data.get("turn_number", 0)
        hist_lines = [f"Current Turn: {turn_num} ({turn.get('phase', 'Main')})"]
        if isinstance(history, list):
            for entry in history:
                hist_lines.append(f"• {entry}")
        self.turn_history_view.setPlainText("\n".join(hist_lines))

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

    def closeEvent(self, event) -> None:
        self.window_closed.emit()
        super().closeEvent(event)
