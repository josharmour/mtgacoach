
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Log, RichLog, Input, Button, Static
from textual.selection import Selection

from textual.message import Message
from textual import events

import threading
import time
import os
import sys
import logging

# Import core logic
from arenamcp.standalone import StandaloneCoach, UIAdapter, LOG_FILE, LOG_DIR
from arenamcp.tts import VoiceOutput
from arenamcp.match_validator import start_recording, get_current_recording

logger = logging.getLogger(__name__)


class SelectableRichLog(RichLog):
    """RichLog subclass with proper text selection and scroll-on-drag support.

    Fixes two issues with the base RichLog:
    1. Text selection/copy grabs content from outside the pane because RichLog
       does not apply offset metadata to rendered strips (unlike Log).
    2. The pane does not scroll when highlight-dragging near its edges.
    """

    # Margin in rows from the top/bottom edge that triggers auto-scroll
    _SCROLL_MARGIN = 2
    # How many lines to scroll per tick when dragging near edges
    _SCROLL_SPEED = 2
    # Timer interval in seconds for auto-scroll during drag
    _SCROLL_INTERVAL = 0.05

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drag_scrolling = False
        self._drag_scroll_direction = 0  # -1 up, +1 down, 0 none
        self._scroll_timer = None

    def _render_line(self, y: int, scroll_x: int, width: int) -> "Strip":
        """Override to apply offset metadata for text selection support.

        The base RichLog._render_line omits the apply_offsets() call that is
        present in Log._render_line. Without offset metadata, the compositor
        cannot determine text coordinates for selection.
        """
        if y >= len(self.lines):
            from textual.strip import Strip
            return Strip.blank(width, self.rich_style)

        key = (y + self._start_line, scroll_x, width, self._widest_line_width)
        if key in self._line_cache:
            return self._line_cache[key]

        line = self.lines[y].crop_extend(scroll_x, scroll_x + width, self.rich_style)
        line = line.apply_offsets(scroll_x, y)

        self._line_cache[key] = line
        return line

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract selected text from the log lines.

        The base Widget.get_selection calls _render() which for ScrollView
        returns a debug panel, not the actual log content. This override
        extracts text directly from the stored Strip objects.
        """
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        if not text:
            return None
        return selection.extract(text), "\n"

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Handle mouse move to auto-scroll when dragging near edges."""
        # Check if a selection is in progress at the screen level
        try:
            if not self.screen._selecting:
                self._stop_drag_scroll()
                return
        except (AttributeError, Exception):
            self._stop_drag_scroll()
            return

        # Check if the mouse button is held (dragging)
        if not event.button:
            self._stop_drag_scroll()
            return

        # Determine if the mouse is near the top or bottom edge
        content_height = self.scrollable_content_region.height
        local_y = event.y

        if local_y < self._SCROLL_MARGIN and self.scroll_offset.y > 0:
            self._drag_scroll_direction = -1
            self._start_drag_scroll()
        elif local_y >= content_height - self._SCROLL_MARGIN and self.scroll_offset.y < self.max_scroll_y:
            self._drag_scroll_direction = 1
            self._start_drag_scroll()
        else:
            self._stop_drag_scroll()

    def _start_drag_scroll(self) -> None:
        """Start the auto-scroll timer for drag selection."""
        if self._drag_scrolling:
            return
        self._drag_scrolling = True
        self._scroll_timer = self.set_interval(
            self._SCROLL_INTERVAL, self._do_drag_scroll
        )

    def _stop_drag_scroll(self) -> None:
        """Stop the auto-scroll timer."""
        if not self._drag_scrolling:
            return
        self._drag_scrolling = False
        self._drag_scroll_direction = 0
        if self._scroll_timer is not None:
            self._scroll_timer.stop()
            self._scroll_timer = None

    def _do_drag_scroll(self) -> None:
        """Perform one tick of auto-scrolling during drag selection."""
        if self._drag_scroll_direction < 0:
            self.scroll_up(animate=False)
        elif self._drag_scroll_direction > 0:
            self.scroll_down(animate=False)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Stop drag scrolling when mouse button is released."""
        self._stop_drag_scroll()


class TextualLogHandler(logging.Handler):
    """Custom logging handler that writes to a Textual Log widget."""

    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def emit(self, record):
        try:
            msg = self.format(record)
            # We must schedule the write on the main thread
            # .write() on RichLog is thread-safe in Textual (mostly), but better explicit
            if hasattr(self.widget, 'app') and self.widget.app:
                self.widget.app.call_from_thread(self.widget.write, msg)
        except Exception:
            self.handleError(record)

class TUIAdapter(UIAdapter):
    """Adapter to route coach output to Textual widgets."""
    def __init__(self, app: "ArenaApp"):
        self.app = app

    def _safe_call(self, method, *args, **kwargs):
        """Invoke method on main thread, safely handling if we are already there."""
        # Textual apps primarily run on the main thread (usually)
        # We can check if we are in the same thread as the app loop
        try:
             # accessing private _thread_id is risky but standard in Textual hacking
             # better to just try/except or check threading
             if threading.get_ident() == self.app._thread_id:
                 method(*args, **kwargs)
             else:
                 self.app.call_from_thread(method, *args, **kwargs)
        except Exception:
             # Fallback if _thread_id missing or other error
             self.app.call_from_thread(method, *args, **kwargs)

    def log(self, message: str) -> None:
        self._safe_call(self.app.write_log, message)

    def advice(self, text: str, seat_info: str) -> None:
        self._safe_call(self.app.write_advice, text, seat_info)

    def status(self, key: str, value: str) -> None:
        self._safe_call(self.app.update_status, key, value)

    def error(self, message: str) -> None:
        self._safe_call(self.app.write_log, f"[bold red]ERROR: {message}[/]")

    def speak(self, text: str) -> None:
        """Speak text using the coach's voice output."""
        if self.app.coach and self.app.coach._voice_output:
            try:
                # Use call_from_thread to ensure thread safety if this triggers UI updates
                # but speak() is blocking by default in some contexts, so be careful.
                # Actually, StandaloneCoach calls this. We should just pass it through.
                # However, standalone.py calls self.ui.speak("Voice changed.")
                # We want to use the actual engine.
                self.app.coach._voice_output.speak(text, blocking=False)
            except Exception as e:
                self.error(f"TTS Error: {e}")

    def subtask(self, status: str) -> None:
        """Update the subtask/progress display in the TUI."""
        self._safe_call(self.app.update_subtask, status)


class GameStateDisplay(Static):
    """Live game state visualization widget."""
    
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._last_state = None
    
    def update_state(self, game_state_snapshot: dict):
        """Update the display with current game state."""
        self._last_state = game_state_snapshot
        self.refresh_display()
    
    @staticmethod
    def _format_permanent_name(card: dict) -> str:
        """Format a permanent name with token/counter annotations."""
        name = card.get("name", "?")
        kind = card.get("object_kind", "")
        prefix = ""
        if kind == "TOKEN":
            prefix = "*"  # Asterisk prefix for tokens
        elif kind == "EMBLEM":
            prefix = "E:"
        suffix = ""
        counters = card.get("counters", {})
        if counters:
            counter_parts = []
            for ctype, count in counters.items():
                clean = ctype.replace("CounterType_", "").replace("Counter_", "")
                counter_parts.append(f"{count}{clean[:3]}")
            suffix = f" [{','.join(counter_parts)}]"
        return f"{prefix}{name}{suffix}"

    def refresh_display(self):
        """Render the current game state to markup."""
        if not self._last_state:
            self.update("[dim]Waiting for game state...[/]")
            return

        try:
            lines = []
            turn_info = self._last_state.get("turn_info", {})
            players = self._last_state.get("players", [])
            zones = self._last_state.get("zones", {})
            battlefield = zones.get("battlefield", [])
            hand = zones.get("my_hand", [])
            pending_decision = self._last_state.get("pending_decision")
            decision_context = self._last_state.get("decision_context")
            recent_events = self._last_state.get("recent_events", [])
            damage_taken = self._last_state.get("damage_taken", {})

            # Turn and Phase
            turn_num = turn_info.get("turn_number", "?")
            phase = turn_info.get("phase", "").replace("Phase_", "")
            step = turn_info.get("step", "").replace("Step_", "")
            active_player = turn_info.get("active_player", 0)
            priority_player = turn_info.get("priority_player", 0)

            # Identify local player
            local_player = next((p for p in players if p.get("is_local")), None)
            local_seat = local_player.get("seat_id") if local_player else None

            # FIX: During DeclareBlock, defending player (non-active) has priority
            if step == "DeclareBlock" and active_player != local_seat:
                priority_player = local_seat

            # Header line
            active_label = f"[green]YOUR[/]" if active_player == local_seat else f"[yellow]OPP[/]"
            priority_label = f"[green]YOU[/]" if priority_player == local_seat else f"[yellow]OPP[/]"
            lines.append(f"[bold]T{turn_num}[/] {phase}{f':{step}' if step else ''} | Active:{active_label} Priority:{priority_label}")

            # Life totals with cumulative damage
            if players:
                life_parts = []
                for p in players:
                    seat = p.get("seat_id")
                    life = p.get("life_total", 20)
                    is_you = p.get("is_local", False)
                    label = f"[bold green]YOU[/]" if is_you else f"[yellow]OPP[/]"
                    net_change = life - 20
                    dmg_str = f" ({net_change:+d})" if net_change != 0 else ""
                    life_parts.append(f"{label}:{life}{dmg_str}")
                lines.append(" | ".join(life_parts))

            # Pending decision with context
            if pending_decision:
                decision_line = f"[bold red]⚠ {pending_decision}[/]"
                if decision_context:
                    dtype = decision_context.get("type", "")
                    if dtype == "declare_attackers":
                        attackers = decision_context.get("legal_attackers", [])
                        if attackers:
                            decision_line += f" [dim]({len(attackers)} legal: {', '.join(attackers[:4])}{'...' if len(attackers) > 4 else ''})[/]"
                    elif dtype == "declare_blockers":
                        blockers = decision_context.get("legal_blockers", [])
                        if blockers:
                            decision_line += f" [dim]({len(blockers)} legal: {', '.join(blockers[:4])}{'...' if len(blockers) > 4 else ''})[/]"
                    elif dtype == "target_selection":
                        src = decision_context.get("source_card")
                        if src:
                            decision_line += f" [dim](for {src})[/]"
                    elif dtype == "distribution":
                        src = decision_context.get("source_card")
                        total = decision_context.get("total")
                        if src:
                            decision_line += f" [dim]({total} from {src})[/]"
                    elif dtype == "numeric_input":
                        src = decision_context.get("source_card")
                        min_v = decision_context.get("min", 0)
                        max_v = decision_context.get("max", 0)
                        if src:
                            decision_line += f" [dim]({src}: {min_v}-{max_v})[/]"
                    elif dtype == "search":
                        decision_line += " [dim](searching library)[/]"
                    elif dtype == "pay_costs":
                        src = decision_context.get("source_card")
                        if src:
                            decision_line += f" [dim](for {src})[/]"
                    elif dtype == "choose_starting_player":
                        decision_line += " [dim](play or draw?)[/]"
                    elif dtype == "casting_time_options":
                        decision_line += " [dim](alternative cost?)[/]"
                lines.append(decision_line)

            # Battlefield (compact view) with token distinction
            your_permanents = [c for c in battlefield if c.get("controller_seat_id") == local_seat]
            opp_permanents = [c for c in battlefield if c.get("controller_seat_id") != local_seat]
            your_creatures = [c for c in your_permanents if c.get("power") is not None]
            opp_creatures = [c for c in opp_permanents if c.get("power") is not None]
            your_tokens = [c for c in your_permanents if c.get("object_kind") == "TOKEN"]
            opp_tokens = [c for c in opp_permanents if c.get("object_kind") == "TOKEN"]

            if your_permanents or opp_permanents:
                you_summary = f"{len(your_permanents)} ({len(your_creatures)}⚔"
                if your_tokens:
                    you_summary += f" {len(your_tokens)}T"
                you_summary += ")"
                opp_summary = f"{len(opp_permanents)} ({len(opp_creatures)}⚔"
                if opp_tokens:
                    opp_summary += f" {len(opp_tokens)}T"
                opp_summary += ")"
                lines.append(f"[bold]Board:[/] You:{you_summary} Opp:{opp_summary}")

                # List your permanents with counter annotations
                if your_permanents:
                    your_names = [self._format_permanent_name(c) for c in your_permanents]
                    lines.append(f"  [green]You:[/] {', '.join(your_names[:6])}" + ("..." if len(your_names) > 6 else ""))

                # List opponent permanents
                if opp_permanents:
                    opp_names = [self._format_permanent_name(c) for c in opp_permanents]
                    lines.append(f"  [yellow]Opp:[/] {', '.join(opp_names[:6])}" + ("..." if len(opp_names) > 6 else ""))

                # Show attacking creatures prominently
                attackers = [c for c in battlefield if c.get("is_attacking")]
                blockers = [c for c in battlefield if c.get("is_blocking")]
                if attackers:
                    atk_names = [c.get("name", "?") for c in attackers]
                    lines.append(f"[bold red]⚔ Attacking:[/] {', '.join(atk_names[:3])}" + ("..." if len(atk_names) > 3 else ""))
                if blockers:
                    blk_names = [c.get("name", "?") for c in blockers]
                    lines.append(f"[bold blue]🛡 Blocking:[/] {', '.join(blk_names[:3])}" + ("..." if len(blk_names) > 3 else ""))
            else:
                lines.append("[dim]Board: Empty[/]")

            # Hand - show more cards (up to 7 visible)
            if hand:
                hand_names = [c.get("name", "?") for c in hand]
                lines.append(f"[bold]Hand ({len(hand)}):[/] {', '.join(hand_names[:7])}" + ("..." if len(hand) > 7 else ""))
            else:
                lines.append("[dim]Hand: Empty[/]")

            # Graveyard, Exile, Library counts
            graveyard = zones.get("graveyard", [])
            exile = zones.get("exile", [])
            your_gy = len([c for c in graveyard if c.get("owner_seat_id") == local_seat])
            opp_gy = len([c for c in graveyard if c.get("owner_seat_id") != local_seat])
            your_exile = len([c for c in exile if c.get("owner_seat_id") == local_seat])
            opp_exile = len([c for c in exile if c.get("owner_seat_id") != local_seat])
            library_count = zones.get("library_count", "?")

            lines.append(f"[dim]GY: You={your_gy} Opp={opp_gy} | Exile: You={your_exile} Opp={opp_exile} | Lib: {library_count}[/]")

            # Recent events ticker (last 3 notable events)
            notable_events = [e for e in recent_events if e.get("type") in
                              ("damage_dealt", "zone_transfer", "counter_added", "counter_removed",
                               "token_created", "controller_changed", "card_revealed", "game_end")]
            if notable_events:
                event_strs = []
                for evt in notable_events[-3:]:
                    etype = evt.get("type")
                    if etype == "damage_dealt":
                        event_strs.append(f"{evt.get('source','?')} deals {evt.get('amount',0)} dmg")
                    elif etype == "zone_transfer":
                        event_strs.append(f"{evt.get('card','?')} moved")
                    elif etype == "counter_added":
                        event_strs.append(f"+{evt.get('amount',1)} {evt.get('counter_type','').replace('CounterType_','')[:6]} on {evt.get('card','?')}")
                    elif etype == "counter_removed":
                        event_strs.append(f"-{evt.get('amount',1)} {evt.get('counter_type','').replace('CounterType_','')[:6]} on {evt.get('card','?')}")
                    elif etype == "token_created":
                        event_strs.append(f"Token: {evt.get('card','?')}")
                    elif etype == "controller_changed":
                        event_strs.append(f"{evt.get('card','?')} stolen")
                    elif etype == "card_revealed":
                        event_strs.append(f"Revealed: {evt.get('card','?')}")
                    elif etype == "game_end":
                        event_strs.append(f"Game {evt.get('result','over')}")
                if event_strs:
                    lines.append(f"[dim italic]{'  |  '.join(event_strs)}[/]")

            self.update("\n".join(lines))
        except Exception as e:
            self.update(f"[red]State Error: {e}[/]")


class Sidebar(Vertical):
    """Sidebar for settings and actions."""

    # Populated at startup from proxy API
    MODEL_OPTIONS = []

    @classmethod
    def load_model_options(cls) -> None:
        """Fetch all available models from the proxy (includes Ollama)."""
        from arenamcp.coach import fetch_proxy_models
        cls.MODEL_OPTIONS = fetch_proxy_models()

    def compose(self) -> ComposeResult:
        with Vertical(id="status-panel"):
            yield Static("Seat: Searching...", id="status-seat", classes="status-line")
            yield Static("Model: Default", id="status-model", classes="status-line")
            yield Static("Style: VERBOSE", id="status-style", classes="status-line")
            yield Static("Voice: Initializing...", id="status-voice", classes="status-line")

        with Vertical(id="actions-panel"):
            yield Button("Proxy: loading...", id="btn-provider", variant="primary")
            yield Button("Voice: loading...", id="btn-voice-select", variant="success")
            yield Button("Mute (F5)", id="btn-mute", variant="success")
            yield Button("Speed 1.0x (F8)", id="btn-speed", variant="success")
            yield Button("Debug (F7)", id="btn-debug", variant="default")
            yield Button("Analyze Screen (F3)", id="btn-screenshot", variant="primary")
            yield Button("Analyze Match", id="btn-analyze", variant="warning")
            yield Button("Update", id="btn-update", variant="warning")
            yield Button("Restart", id="btn-restart", variant="error")
            yield Button("", id="btn-win-plan", variant="warning", disabled=True)


class ArenaApp(App):
    """MTGA Coach TUI Application."""

    CSS = """
    Screen {
        layout: horizontal;
    }

    Sidebar {
        width: 30;
        dock: left;
        height: 100%;
        background: $surface-darken-1;
        border-right: solid $primary;
        padding: 0 1;
        overflow-y: auto;
    }

    #status-panel {
        height: auto;
        padding: 0;
    }

    .status-line {
        height: 1;
        color: $text-muted;
    }

    #actions-panel {
        height: auto;
    }

    Button {
        width: 100%;
        margin: 0;
        padding: 0 1;
        min-height: 1;
        border: none;
    }

    #btn-update {
        display: none;
    }

    #btn-win-plan:disabled {
        display: none;
    }

    #btn-win-plan {
        background: $warning-darken-1;
        color: $text;
        text-style: bold;
    }

    #main-area {
        width: 1fr;
        height: 100%;
        layout: vertical;
    }

    #game-state-display {
        height: 6;
        border: solid $success;
        background: $surface-darken-1;
        padding: 0 1;
        overflow-y: auto;
    }

    #subtask-display {
        height: auto;
        max-height: 2;
        padding: 0 1;
        display: none;
    }

    #log-view {
        height: 1fr;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
    }

    Input {
        dock: bottom;
        height: 3;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("f2", "toggle_style", "Style"),
        ("f3", "analyze_screen", "Screen"),
        ("f5", "toggle_mute", "Mute"),
        ("f6", "cycle_voice", "Voice"),
        ("f7", "copy_debug", "Debug"),
        ("f8", "cycle_speed", "Speed"),
        ("ctrl+0", "read_win_plan", "Win Plan"),
    ]

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.coach = None
        self.log_widget = None
        self.game_state_widget = None
        self._restart_requested = False
        self._pending_remote_version: str | None = None

        # Fetch proxy models before UI renders
        Sidebar.load_model_options()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Sidebar()
        with Vertical(id="main-area"):
            # Game State Display (Top Right)
            yield GameStateDisplay(id="game-state-display")
            # Advice Log (Bottom Right) - SelectableRichLog enables proper
            # text selection/copy and scroll-on-drag within the pane
            yield SelectableRichLog(id="log-view", markup=True, wrap=True)
            yield Static("", id="subtask-display")
            yield Input(placeholder="Ask the coach...", id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        """Start the coach thread when the app mounts."""
        self.log_widget = self.query_one("#log-view", SelectableRichLog)
        self.game_state_widget = self.query_one("#game-state-display", GameStateDisplay)

        from arenamcp import __version__
        self.title = f"ArenaMCP v{__version__}"
        self.write_log(f"[bold]ArenaMCP v{__version__}[/]")

        # Check for updates in background (non-blocking)
        threading.Thread(target=self._check_for_update, daemon=True).start()

        # Check for newly-installed backends in background
        threading.Thread(target=self._check_new_backends, daemon=True).start()

        # Start initial coach logic in a thread
        threading.Thread(target=self.start_coach, daemon=True).start()

        # Start game state polling
        threading.Thread(target=self._poll_game_state, daemon=True).start()

    def _check_new_backends(self):
        """Background thread: detect newly-installed LLM backends."""
        from arenamcp.backend_detect import detect_backends_quick
        from arenamcp.settings import get_settings

        try:
            detected = detect_backends_quick()
            available = [name for name, ok in detected.items() if ok]

            settings = get_settings()
            known = settings.get("known_backends", [])

            if not known:
                # First run — seed the list with whatever is present now
                settings.set("known_backends", available)
                return

            new_backends = [b for b in available if b not in known]
            if new_backends:
                for b in new_backends:
                    self.call_from_thread(
                        self.write_log,
                        f"[bold green]New backend detected: {b}[/] — switch via the sidebar or re-run the setup wizard.",
                    )
                settings.set("known_backends", list(set(known) | set(available)))
        except Exception as exc:
            logger.debug("Backend detection failed: %s", exc)

    def _check_for_update(self):
        """Background thread: check for a newer version on origin."""
        from arenamcp.updater import check_for_update

        try:
            available, local_ver, remote_ver = check_for_update()
            if available:
                self._pending_remote_version = remote_ver
                self.call_from_thread(
                    self.write_log,
                    f"[bold yellow]Update available: v{local_ver} \u2192 v{remote_ver}[/]",
                )
                self.call_from_thread(
                    self.write_log,
                    "[yellow]Type /update or click the Update button to download and restart.[/]",
                )
                # Unhide the sidebar button
                def _show_btn():
                    try:
                        btn = self.query_one("#btn-update", Button)
                        btn.label = f"Update \u2192 v{remote_ver}"
                        btn.display = True
                    except Exception:
                        pass
                self.call_from_thread(_show_btn)
            else:
                self.call_from_thread(
                    self.write_log,
                    f"[dim]Up to date (v{local_ver})[/]",
                )
        except Exception as exc:
            logger.debug("Update check failed: %s", exc)

    def _do_apply_update(self):
        """Background thread: run git pull and restart on success."""
        from arenamcp.updater import apply_update

        self.call_from_thread(self.write_log, "[yellow]Pulling latest changes...[/]")
        success, message = apply_update()
        if success:
            self.call_from_thread(self.write_log, f"[bold green]Updated: {message}[/]")
            self.call_from_thread(self.write_log, "[green]Restarting...[/]")
            self.call_from_thread(self.action_restart)
        else:
            self.call_from_thread(self.write_log, f"[bold red]Update failed: {message}[/]")

    def _poll_game_state(self):
        """Poll game state and update display every 500ms."""
        import time
        import logging
        logger = logging.getLogger(__name__)
        
        while True:
            if self.coach and self.coach._mcp:
                try:
                    from arenamcp.server import game_state
                    snapshot = game_state.get_snapshot()
                    self.call_from_thread(self.game_state_widget.update_state, snapshot)
                except Exception as e:
                    logger.error(f"Game state polling error: {e}", exc_info=True)
            time.sleep(0.5)

    def start_coach(self):
        """Initialize the StandaloneCoach with our TUI Adapter."""
        try:
            self.adapter = TUIAdapter(self)
            self.coach = StandaloneCoach(
                backend=self.args.backend,
                model=self.args.model,
                voice_mode=self.args.voice,
                draft_mode=self.args.draft,
                set_code=self.args.set_code,
                ui_adapter=self.adapter,
                register_hotkeys=False,  # Textual handles keys
                autopilot=getattr(self.args, 'autopilot', False),
                dry_run=getattr(self.args, 'dry_run', False),
                afk=getattr(self.args, 'afk', False),
            )
            
            # Sync initial state to UI
            self.call_from_thread(self.sync_ui_state)
            
            # Run the main loop
            self.coach.start()
        except Exception as e:
            import traceback
            self.call_from_thread(self.write_log, f"[bold red]Fatal Error: {e}[/]")
            self.call_from_thread(self.write_log, f"[red]{traceback.format_exc()}[/]")

    def sync_ui_state(self):
        """Update TUI widgets to match Coach state."""
        if not self.coach:
            return

        # Build composite provider string
        current_backend = self.coach.backend_name
        current_model = self.coach.model_name

        # Update provider button label
        combo = f"{current_backend}/{current_model}" if current_model else current_backend
        display_name = current_model or current_backend
        # Try to find a friendly display name from MODEL_OPTIONS
        for name, val in Sidebar.MODEL_OPTIONS:
            if str(val) == combo:
                display_name = name.split("(")[0].strip() if "(" in name else name
                break
        try:
            btn = self.query_one("#btn-provider", Button)
            btn.label = f"Proxy: {display_name}"
        except Exception:
            pass

        # Sync model status
        model_display = f"{current_backend}/{current_model}" if current_model else current_backend
        self.update_status("MODEL", model_display)
        self.update_status("STYLE", self.coach.advice_style.upper())

        # Sync voice output
        if self.coach._voice_output:
            curr_id, desc = self.coach._voice_output.current_voice
            self.update_status("VOICE_ID", desc)
            try:
                btn = self.query_one("#btn-voice-select", Button)
                btn.label = f"Voice: {desc}"
            except Exception:
                pass
            # Sync speed button label
            self._update_speed_button(self.coach._voice_output._speed)
            self.sub_title = desc

        # Start seat info polling
        threading.Thread(target=self._poll_seat_info, daemon=True).start()

    def _poll_seat_info(self):
        """Poll game state for seat info periodically."""
        import time
        while True:
            if self.coach and self.coach._mcp:
                try:
                    # Access internal game state directly for debug info
                    # Note: accessing private member _game_state via mcp server might be tricky
                    # But StandaloneCoach has ._mcp.mcp -> which is the FastMCP object
                    # We need the global game_state object from server.py
                    # Easier way: call a method on coach that gets it locally if possible,
                    # or just import it if running in same process (which we are)
                    from arenamcp.server import game_state
                    
                    seat_id = game_state.local_seat_id
                    source = game_state.get_seat_source_name()
                    
                    # Heuristic translation for 1v1:
                    # Seat 1 is usually "Bottom" (You), Seat 2 is "Top" (Opponent)
                    # This might vary, but it's better than "Seat 1"
                    seat_label = f"Seat {seat_id}"
                    if seat_id == 1:
                        seat_label = "Bottom (1)"
                    elif seat_id == 2:
                        seat_label = "Top (2)"
                    
                    val = f"{seat_label} [{source}]" if seat_id is not None else "Searching..."
                    self.call_from_thread(self.update_status, "SEAT_INFO", val)
                except Exception:
                    pass
            time.sleep(1.0)
            
    # --- UI Update Methods (Called via call_from_thread) ---

    def write_log(self, message: str, highlight: bool = False) -> None:
        """Write to the log widget."""
        if self.log_widget:
            self.log_widget.write(message)



    def write_advice(self, text: str, seat_info: str) -> None:
        """Write specialized advice block."""
        # Auto-start recording when advice is given and no recording is active
        self._auto_start_recording_if_needed()

        if self.log_widget:
            self.log_widget.write(f"\n[bold magenta]--- COACH ({seat_info}) ---[/]")
            self.log_widget.write(f"[bold white]{text}[/]")
            self.log_widget.write("[magenta]-----------------------[/]\n")

    def update_subtask(self, status: str) -> None:
        """Update the subtask progress display."""
        try:
            widget = self.query_one("#subtask-display", Static)
            if status:
                widget.update(f"[dim cyan]\u27f3 {status}[/]")
                widget.display = True
            else:
                widget.update("")
                widget.display = False
        except Exception:
            pass

    def _auto_start_recording_if_needed(self) -> None:
        """Auto-start recording if advisor is running and no recording active."""
        from datetime import datetime

        current = get_current_recording()
        if current:
            return  # Already recording

        # Check if we have an active game
        if not self.coach or not hasattr(self.coach, '_mcp') or not self.coach._mcp:
            return

        try:
            gs = self.coach._mcp.get_game_state()
            if not gs:
                return

            turn_num = gs.get("turn", {}).get("turn_number", 0)
            if turn_num < 1:
                return  # No active game yet

            # We have an active game - auto-start recording
            match_id = gs.get("match_id") or f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            start_recording(match_id)

            # Update analyze button to show recording is active
            try:
                btn = self.query_one("#btn-analyze", Button)
                btn.label = "Recording..."
                btn.variant = "error"
            except:
                pass

            if self.log_widget:
                self.log_widget.write("[dim]Match recording started automatically[/]")

        except Exception as e:
            pass  # Don't fail advice delivery if auto-record fails

    def update_status(self, key: str, value: str) -> None:
        """Update status labels."""
        key = key.upper()
        if key == "STYLE":
            self.query_one("#status-style", Static).update(f"Style: {value}")
        elif key == "MODEL":
            self.query_one("#status-model", Static).update(f"Model: {value}")
        elif key == "VOICE_ID" or key == "VOICE":
            self.query_one("#status-voice", Static).update(f"Voice: {value}")
            self.sub_title = value
        elif key == "SEAT_INFO":
            self.query_one("#status-seat", Static).update(f"Seat: {value}")
        elif key == "WIN-PLAN":
            btn = self.query_one("#btn-win-plan", Button)
            if value:
                btn.label = value
                btn.disabled = False
            else:
                btn.label = ""
                btn.disabled = True

    # --- Actions ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self.coach:
            return

        btn_id = event.button.id
        if btn_id == "btn-provider":
            self._cycle_provider()
        elif btn_id == "btn-voice-select":
            self._cycle_voice_select()
        elif btn_id == "btn-mute":
            self.action_toggle_mute()
        elif btn_id == "btn-speed":
            self.action_cycle_speed()
        elif btn_id == "btn-debug":
            self.action_copy_debug()
        elif btn_id == "btn-screenshot":
            self.action_analyze_screen()
        elif btn_id == "btn-analyze":
            self.action_analyze_match()
        elif btn_id == "btn-update":
            if self._pending_remote_version:
                threading.Thread(target=self._do_apply_update, daemon=True).start()
        elif btn_id == "btn-restart":
            self.action_restart()
        elif btn_id == "btn-win-plan":
            self.action_read_win_plan()

    def _cycle_provider(self) -> None:
        """Cycle to next provider/model on click."""
        options = Sidebar.MODEL_OPTIONS
        if not options:
            return

        current_backend = self.coach.backend_name
        current_model = self.coach.model_name
        current_combo = f"{current_backend}/{current_model}" if current_model else current_backend

        # Find current index
        idx = -1
        for i, (_, val) in enumerate(options):
            if str(val) == current_combo:
                idx = i
                break
        if idx == -1:
            for i, (_, val) in enumerate(options):
                if str(val).startswith(current_backend):
                    idx = i
                    break

        next_idx = ((idx if idx >= 0 else 0) + 1) % len(options)
        display_name, next_val = options[next_idx]

        # Parse provider/model
        selection = str(next_val)
        if "/" in selection:
            new_provider, new_model = selection.split("/", 1)
        else:
            new_provider = selection
            new_model = None

        # Update button label immediately
        short_name = display_name.split("(")[0].strip() if "(" in display_name else display_name
        btn = self.query_one("#btn-provider", Button)
        btn.label = f"Proxy: {short_name}"

        # Switch backend in thread
        threading.Thread(
            target=self._verify_and_switch,
            args=(new_provider, new_model),
            daemon=True
        ).start()

    def _cycle_voice_select(self) -> None:
        """Cycle to next TTS voice on click."""
        if not self.coach or not self.coach._voice_output:
            return

        voice_id, desc = self.coach._voice_output.next_voice()
        btn = self.query_one("#btn-voice-select", Button)
        btn.label = f"Voice: {desc}"
        self.update_status("VOICE_ID", desc)
        threading.Thread(
            target=lambda: self.coach._voice_output.speak("Voice changed.", blocking=False),
            daemon=True
        ).start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle chat input."""
        text = event.value.strip()
        if not text:
            return

        event.input.value = ""

        # Handle /update command
        if text.lower() == "/update":
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            if self._pending_remote_version:
                threading.Thread(target=self._do_apply_update, daemon=True).start()
            else:
                self.write_log("[dim]No update available. Already up to date.[/]")
            return

        # Handle /bugreport command (optional message after the command)
        if text.lower().startswith("/bugreport"):
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            user_msg = text[len("/bugreport"):].strip() or None
            threading.Thread(target=self._do_submit_bugreport, args=(user_msg,), daemon=True).start()
            return

        self.write_log(f"\n[bold cyan]YOU: {text}[/]")

        if self.coach and self.coach._mcp:
            # Run in thread to avoid blocking UI
            threading.Thread(
                target=self._process_chat, 
                args=(text,), 
                daemon=True
            ).start()
            
    def _process_chat(self, text: str):
        try:
            game_state = self.coach._mcp.get_game_state()
            advice = self.coach._coach.get_advice(game_state, question=text)
            self.write_advice(advice, "Chat Response")
        except Exception as e:
            self.write_log(f"[red]Chat error: {e}[/]")

    def _verify_and_switch(self, provider, model):
        """Verify model exists (if ollama) then switch."""
        if provider == "ollama":
            self.write_log(f"Verifying {model} in ollama...")
            try:
                import subprocess
                result = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True)
                if model not in result.stdout:
                    self.write_log(f"[bold red]WARNING: Model '{model}' not found in Ollama![/]")
                    self.write_log(f"[red]Please run: ollama pull {model}[/]")
                else:
                    self.write_log(f"[green]Verified {model} exists.[/]")
            except Exception as e:
                self.write_log(f"[red]Failed to verify ollama models: {e}[/]")

        # Proceed with switch
        self.coach.set_backend(provider, model)

    # --- Hotkey Actions ---

    def action_analyze_screen(self) -> None:
        """Analyze the current screen (F3) - mulligan, board state, etc."""
        if self.coach:
            threading.Thread(target=self.coach.take_screenshot_analysis, daemon=True).start()

    def action_copy_debug(self) -> None:
        """Copy current debug state to clipboard (F7)."""
        if not self.coach:
            self.write_log("[yellow]Coach not initialized[/]")
            return

        threading.Thread(target=self._do_copy_debug, daemon=True).start()

    def _do_copy_debug(self):
        """Save debug report and copy path to clipboard."""
        if self.coach and hasattr(self.coach, 'save_bug_report'):
            bug_path = self.coach.save_bug_report("Copy Debug (F7)")
            if bug_path:
                file_url = f"file:///{str(bug_path).replace(chr(92), '/')}"
                self.call_from_thread(self.write_log, "[green]Bug report saved.[/]")
                self.call_from_thread(self.write_log, f"[dim]{file_url}[/] (copied to clipboard)")
                self.call_from_thread(
                    self.write_log,
                    "[yellow]Type /bugreport to submit to GitHub.[/]",
                )
            else:
                self.call_from_thread(self.write_log, "[red]Failed to save debug report[/]")
        else:
            self.call_from_thread(self.write_log, "[yellow]Coach not available for debug report[/]")

    def _do_submit_bugreport(self, user_message: str = None):
        """Submit the most recent bug report as a GitHub issue.

        Args:
            user_message: Optional user description (from ``/bugreport <msg>``).
                          Used in the issue title and body when provided.
        """
        import shutil
        from pathlib import Path

        bug_dir = LOG_DIR / "bug_reports"
        if not bug_dir.exists():
            self.call_from_thread(self.write_log, "[red]No bug reports found. Press F7 first.[/]")
            return

        # Find the most recent bug report JSON
        reports = sorted(bug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not reports:
            self.call_from_thread(self.write_log, "[red]No bug reports found. Press F7 first.[/]")
            return

        report_path = reports[0]
        try:
            import json as _json
            report_data = _json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.call_from_thread(self.write_log, f"[red]Failed to read bug report: {exc}[/]")
            return

        timestamp = report_data.get("timestamp", report_path.stem)
        version = report_data.get("version", "unknown")
        if user_message:
            title = f"Bug Report: {user_message}"
        else:
            title = f"Bug Report: {timestamp}"

        # Build a rich issue body from the report
        config = report_data.get("config", {})
        system = report_data.get("system", {})
        mtga_log = report_data.get("mtga_log", {})
        match_ctx = report_data.get("match_context", {})
        game_state = report_data.get("game_state", {})
        errors = report_data.get("errors", [])

        body_parts = []
        if user_message:
            body_parts.append(f"**Description:** {user_message}")
            body_parts.append("")
        body_parts.append(f"**Timestamp:** {timestamp}")
        body_parts.append(f"**Version:** {version}")

        if report_data.get("reason"):
            body_parts.append(f"**Trigger:** {report_data['reason']}")
        body_parts.append(f"**Backend:** {config.get('backend', 'unknown')}")
        body_parts.append(f"**Model:** {config.get('model', 'unknown')}")

        # System
        body_parts.append("")
        body_parts.append("### System")
        body_parts.append(f"- OS: {system.get('platform', 'unknown')}")
        body_parts.append(f"- Python: {system.get('python_version', 'unknown')}")
        body_parts.append(f"- Arch: {system.get('machine', 'unknown')}")
        pkgs = system.get("packages", {})
        if pkgs:
            missing = [p for p, v in pkgs.items() if v == "not installed"]
            if missing:
                body_parts.append(f"- Missing packages: {', '.join(missing)}")

        # MTGA log
        if mtga_log:
            body_parts.append("")
            body_parts.append("### MTGA Log")
            body_parts.append(f"- Path: `{mtga_log.get('path', 'unknown')}`")
            body_parts.append(f"- Exists: {mtga_log.get('exists', 'unknown')}")
            if mtga_log.get("size_bytes") is not None:
                size_mb = mtga_log["size_bytes"] / (1024 * 1024)
                body_parts.append(f"- Size: {size_mb:.1f} MB")
                body_parts.append(f"- Last modified: {mtga_log.get('last_modified', 'unknown')}")

        # Game state summary
        if game_state:
            turn = game_state.get("turn", {})
            players = game_state.get("players", [])
            body_parts.append("")
            body_parts.append("### Game State")
            body_parts.append(f"- Turn: {turn.get('turn_number', '?')} | Phase: {turn.get('phase', '?')}")
            for p in players:
                label = "You" if p.get("is_local") else "Opp"
                body_parts.append(f"- {label} (Seat {p.get('seat_id', '?')}): {p.get('life_total', '?')} life")
            hand = game_state.get("hand", [])
            bf = game_state.get("battlefield", [])
            body_parts.append(f"- Hand: {len(hand)} | Battlefield: {len(bf)}")
            if match_ctx.get("match_id"):
                body_parts.append(f"- Match ID: `{match_ctx['match_id']}`")

        # Errors
        if errors:
            body_parts.append("")
            body_parts.append("### Recent Errors")
            for err in errors[-5:]:
                err_str = str(err)[:200]
                body_parts.append(f"- `{err_str}`")

        repo = "josharmour/ArenaMCP"

        # Try gh CLI first
        gh_bin = shutil.which("gh")
        if gh_bin:
            self.call_from_thread(self.write_log, "[yellow]Creating GitHub issue via gh CLI...[/]")
            try:
                import subprocess

                # Upload bug report JSON as a gist
                gist_url = None
                gist_result = subprocess.run(
                    [gh_bin, "gist", "create", "--public", "--desc",
                     f"ArenaMCP Bug Report {timestamp}", str(report_path)],
                    capture_output=True, text=True, timeout=30,
                )
                if gist_result.returncode == 0:
                    gist_url = gist_result.stdout.strip()
                    body_parts.append("")
                    body_parts.append(f"**Debug Report:** {gist_url}")
                else:
                    body_parts.append("")
                    body_parts.append("*(Failed to upload debug report gist)*")

                body = "\n".join(body_parts)
                result = subprocess.run(
                    [gh_bin, "issue", "create", "--repo", repo, "--title", title, "--body", body],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    issue_url = result.stdout.strip()
                    self.call_from_thread(
                        self.write_log,
                        f"[bold green]Issue created: {issue_url}[/]",
                    )
                    return
                else:
                    err = (result.stderr or result.stdout or "").strip()
                    self.call_from_thread(
                        self.write_log,
                        f"[yellow]gh CLI failed: {err}. Falling back to browser...[/]",
                    )
            except Exception as exc:
                self.call_from_thread(
                    self.write_log,
                    f"[yellow]gh CLI error: {exc}. Falling back to browser...[/]",
                )

        # Fallback: open browser with pre-filled issue
        import urllib.parse
        import webbrowser

        # Ensure body is set for the browser fallback (gh path may not have run)
        if not body_parts[-1].startswith("**Debug"):
            body_parts.append("")
            body_parts.append("*(Please attach the bug report JSON from your ~/.arenamcp/bug_reports/ folder)*")
        body = "\n".join(body_parts)
        params = urllib.parse.urlencode({"title": title, "body": body})
        url = f"https://github.com/{repo}/issues/new?{params}"
        try:
            webbrowser.open(url)
            self.call_from_thread(
                self.write_log,
                "[green]Opened GitHub new issue page in browser.[/]",
            )
        except Exception as exc:
            self.call_from_thread(
                self.write_log,
                f"[red]Failed to open browser: {exc}[/]",
            )
            self.call_from_thread(
                self.write_log,
                f"[dim]Manual URL: https://github.com/{repo}/issues/new[/]",
            )

    def action_restart(self) -> None:
        """Handle restart request - cleanly stops coach and exits app for restart."""
        self._restart_requested = True

        if self.coach:
            self.write_log("[yellow]Restarting...[/]")
            # Stop the coach cleanly in a thread, then signal exit
            def do_restart():
                try:
                    self.coach.stop()
                except Exception as e:
                    logger.error(f"Error stopping coach during restart: {e}")

                # Clear __pycache__ to ensure fresh module imports during development
                import shutil
                import pathlib
                src_dir = pathlib.Path(__file__).parent
                pycache_dir = src_dir / "__pycache__"
                if pycache_dir.exists():
                    try:
                        shutil.rmtree(pycache_dir)
                        logger.info(f"Cleared pycache: {pycache_dir}")
                    except Exception as e:
                        logger.warning(f"Failed to clear pycache: {e}")

            thread = threading.Thread(target=do_restart, daemon=True)
            thread.start()
            # Wait briefly for cleanup, then force exit (don't rely on thread completing)
            thread.join(timeout=3.0)

        # Exit on the main thread directly — more reliable than call_from_thread
        self.exit("restart")

    def action_analyze_match(self) -> None:
        """Trigger strategic post-match analysis using advice history."""
        if not self.coach:
            self.write_log("[yellow]Coach not initialized.[/]")
            return

        self.write_log("[bold green]Generating match analysis...[/]")
        self.coach.trigger_match_analysis()

    def action_toggle_style(self) -> None:
        if self.coach:
            threading.Thread(target=self.coach._on_style_toggle_hotkey, daemon=True).start()

    def action_toggle_freq(self) -> None:
        if self.coach:
            threading.Thread(target=self.coach._on_frequency_toggle_hotkey, daemon=True).start()

    def action_toggle_mute(self) -> None:
        """Toggle TTS mute."""
        if self.coach:
            threading.Thread(target=self.coach._on_mute_hotkey, daemon=True).start()

    def action_cycle_voice(self) -> None:
        if self.coach:
            threading.Thread(target=self.coach._on_voice_cycle_hotkey, daemon=True).start()

    def action_cycle_speed(self) -> None:
        """Cycle TTS speed (1.0x → 1.2x → 1.4x → 1.0x)."""
        if not self.coach or not self.coach._voice_output:
            return

        def _do():
            speed = self.coach._voice_output.cycle_speed()
            self.call_from_thread(self._update_speed_button, speed)
            try:
                self.coach._voice_output.speak("Speed changed.", blocking=False)
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    def _update_speed_button(self, speed: float) -> None:
        """Update the speed button label."""
        btn = self.query_one("#btn-speed", Button)
        btn.label = f"Speed {speed}x (F8)"

    def action_cycle_model(self) -> None:
        """Cycle through available models (F12)."""
        if not self.coach:
            return
        self._cycle_provider()

    def action_read_win_plan(self) -> None:
        """Read pending win plan aloud (Ctrl+0 or click)."""
        if self.coach:
            threading.Thread(target=self.coach._on_read_win_plan, daemon=True).start()

    def action_quit(self) -> None:
        if self.coach:
            self.coach.stop()
        self.exit()

def run_tui(args):
    """Run the TUI application with restart support.

    Args:
        args: Command line arguments from argparse.

    The TUI will restart if the user clicks the Restart button.
    When running under the launcher, it exits with code 42 to signal restart.
    Otherwise, it loops and re-creates the app in-process.
    """
    import os

    # Check if running under the launcher
    under_launcher = os.environ.get("ARENAMCP_LAUNCHER") == "1"

    while True:
        app = ArenaApp(args)
        result = app.run()

        # Check if restart was requested
        if result == "restart" or app._restart_requested:
            if under_launcher:
                # Signal the launcher to restart us
                sys.exit(42)
            else:
                # Restart by re-execing the process for a clean slate
                import subprocess
                python = sys.executable
                os.execv(python, [python, "-m", "arenamcp.standalone"] + sys.argv[1:])
                # execv replaces the process, so this line is never reached
        else:
            # Normal exit
            sys.exit(0)
