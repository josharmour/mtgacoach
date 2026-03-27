
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, RichLog, Input, Button, Static
from textual.selection import Selection

from textual import events

import threading
import sys
import logging

# Import core logic
from arenamcp.standalone import StandaloneCoach, UIAdapter, LOG_DIR, copy_to_clipboard
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
            if (
                hasattr(self.widget, 'app')
                and self.widget.app
                and getattr(self.widget.app, "is_running", False)
            ):
                self.widget.app.call_from_thread(self.widget.write, msg)
        except RuntimeError as e:
            if "App is not running" in str(e):
                return
            self.handleError(record)
        except Exception:
            self.handleError(record)

class TUIAdapter(UIAdapter):
    """Adapter to route coach output to Textual widgets."""
    def __init__(self, app: "ArenaApp"):
        self.app = app

    @staticmethod
    def _is_app_not_running_error(exc: Exception) -> bool:
        return isinstance(exc, RuntimeError) and "App is not running" in str(exc)

    def _safe_call(self, method, *args, **kwargs):
        """Invoke method on main thread, skipping calls after app shutdown."""
        # Textual apps primarily run on the main thread (usually)
        # We can check if we are in the same thread as the app loop
        try:
            # If Textual is already torn down, background workers should noop.
            if not getattr(self.app, "is_running", False):
                return

            # accessing private _thread_id is risky but standard in Textual hacking
            # better to just try/except or check threading
            if threading.get_ident() == self.app._thread_id:
                method(*args, **kwargs)
            else:
                self.app.call_from_thread(method, *args, **kwargs)
        except Exception as e:
            if self._is_app_not_running_error(e):
                return
            try:
                if not getattr(self.app, "is_running", False):
                    return
                self.app.call_from_thread(method, *args, **kwargs)
            except Exception as inner:
                if self._is_app_not_running_error(inner):
                    return
                raise

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


class TopBar(Vertical):
    """Top bar for status info and action buttons."""

    # Two-mode architecture: online (mtgacoach.com) or local (Ollama/LM Studio)
    MODES = [
        ("Online", "online"),
        ("Local", "local"),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-panel"):
            yield Static("Seat: Searching...", id="status-seat", classes="status-line")
            yield Static("Style: CONCISE", id="status-style", classes="status-line")
            yield Static("Voice: Initializing...", id="status-voice", classes="status-line")
            yield Static("Backend: Starting...", id="status-backend", classes="status-line")
            yield Static("Model: Default", id="status-model", classes="status-line")
            yield Static("", id="status-log-health", classes="status-line")

        with Horizontal(id="actions-panel"):
            yield Button("Online", id="btn-mode", variant="primary")
            yield Button("Model", id="btn-model", variant="primary")
            yield Button("Voice", id="btn-voice-select", variant="success")
            yield Button("Mute", id="btn-mute", variant="success")
            yield Button("1.4x", id="btn-speed", variant="success")
            yield Button("Debug", id="btn-debug", variant="default")
            yield Button("Screen", id="btn-screenshot", variant="primary")
            yield Button("AP:OFF", id="btn-autopilot", variant="warning")
            yield Button("Analyze", id="btn-analyze", variant="warning")
            yield Button("Update", id="btn-update", variant="warning")
            yield Button("Restart", id="btn-restart", variant="error")
            yield Button("", id="btn-win-plan", variant="warning", disabled=True)


class ArenaApp(App):
    """MTGA Coach TUI Application."""

    CSS = """
    Screen {
        layout: vertical;
    }

    TopBar {
        dock: top;
        height: auto;
        max-height: 3;
        background: $surface-darken-1;
        border-bottom: solid $primary;
        padding: 0 0;
    }

    #status-panel {
        height: 1;
        layout: horizontal;
    }

    .status-line {
        height: 1;
        width: auto;
        color: $text-muted;
        margin-right: 2;
    }

    #actions-panel {
        height: 1;
        layout: horizontal;
        overflow: hidden;
    }

    Button {
        width: auto;
        min-width: 4;
        margin: 0;
        padding: 0 0;
        min-height: 1;
        max-height: 1;
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
        height: 1fr;
        layout: vertical;
    }

    #game-state-display {
        height: auto;
        min-height: 4;
        max-height: 14;
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
        ("f1", "autopilot_cancel", "AP Cancel"),
        ("f2", "toggle_style", "Style"),
        ("f3", "analyze_screen", "Screen"),
        ("f4", "autopilot_abort", "AP Abort"),
        ("f5", "toggle_mute", "Mute"),
        ("f6", "cycle_voice", "Voice"),
        ("f7", "copy_debug", "Debug"),
        ("f8", "cycle_speed", "Speed"),
        ("f9", "autopilot_toggle_afk", "AP AFK"),
        ("f10", "autopilot_toggle_land", "AP Land"),
        ("f12", "autopilot_toggle", "AP On/Off"),
        ("ctrl+0", "read_win_plan", "Win Plan"),
    ]

    _MODE_BUTTON_LABELS = {
        "online": "Online",
        "local": "Local",
    }
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.coach = None
        self.log_widget = None
        self.game_state_widget = None
        self._restart_requested = False
        self._pending_remote_version: str | None = None
        self._debug_report_in_progress = False

    @staticmethod
    def _compact_button_label(value: str, max_len: int) -> str:
        """Trim long button labels so the action bar stays readable."""
        text = (value or "").strip()
        if not text:
            return ""
        if len(text) <= max_len:
            return text
        return text[: max_len - 3].rstrip() + "..."

    def _mode_button_label(self, mode: str) -> str:
        """Return a short, plain-text mode label for the top bar button."""
        return self._MODE_BUTTON_LABELS.get((mode or "").lower(), mode or "Online")

    def _model_button_label(self, model: str | None) -> str:
        """Return a compact model label for the top bar button."""
        return self._compact_button_label(model or "Default", 16)

    def _voice_button_label(self, voice_desc: str) -> str:
        """Return a compact voice label for the top bar button."""
        return self._compact_button_label(voice_desc, 12)

    def _set_mode_model_buttons(
        self,
        mode: str,
        model: str | None,
    ) -> None:
        """Update mode/model buttons without Rich markup."""
        mode_btn = self.query_one("#btn-mode", Button)
        mode_btn.label = self._mode_button_label(mode)
        model_btn = self.query_one("#btn-model", Button)
        model_btn.label = self._model_button_label(model)

    def compose(self) -> ComposeResult:
        yield Header()
        yield TopBar()
        with Vertical(id="main-area"):
            yield GameStateDisplay(id="game-state-display")
            yield SelectableRichLog(id="log-view", markup=True, wrap=True)
            yield Static("", id="subtask-display")
            yield Input(placeholder="Ask the coach...", id="chat-input")
        yield Footer()

    def on_mount(self) -> None:
        """Start the coach thread when the app mounts."""
        self.log_widget = self.query_one("#log-view", SelectableRichLog)
        self.game_state_widget = self.query_one("#game-state-display", GameStateDisplay)

        from arenamcp import __version__
        self.title = f"mtgacoach v{__version__}"
        self.write_log(f"[bold]mtgacoach v{__version__}[/]")

        # Check for updates in background (non-blocking)
        threading.Thread(target=self._check_for_update, daemon=True).start()

        # Check subscription status in background
        threading.Thread(target=self._check_subscription, daemon=True).start()

        # Start initial coach logic in a thread
        threading.Thread(target=self.start_coach, daemon=True).start()

        # Start game state polling
        threading.Thread(target=self._poll_game_state, daemon=True).start()

    def _check_subscription(self):
        """Background thread: check subscription status and show messages."""
        from arenamcp.settings import get_settings
        from arenamcp.subscription import check_subscription, SubscriptionStatus

        try:
            settings = get_settings()
            license_key = settings.get("license_key", "")
            mode = settings.get("mode", "online")

            if not license_key and mode == "online":
                self.call_from_thread(
                    self.write_log,
                    "[yellow]No license key configured. Use /subscribe to get one, "
                    "or switch to Local mode.[/]",
                )
                return

            if license_key:
                status = check_subscription(license_key)
                if status.is_valid:
                    self.call_from_thread(
                        self.write_log,
                        f"[dim]Subscription: {status.status}[/]",
                    )
                elif status.needs_subscription:
                    self.call_from_thread(
                        self.write_log,
                        f"[bold red]Subscription issue: {status.message}[/]",
                    )

                # Show service messages
                last_seen = settings.get("last_seen_message_id")
                for msg in status.messages:
                    msg_id = msg.get("id")
                    if last_seen and msg_id and msg_id <= last_seen:
                        continue
                    title = msg.get("title", "")
                    body = msg.get("body", "")
                    self.call_from_thread(
                        self.write_log,
                        f"[bold cyan]📢 {title}[/]: {body}",
                    )
                    if msg_id:
                        settings.set("last_seen_message_id", msg_id)
        except Exception as exc:
            logger.debug("Subscription check failed: %s", exc)

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

        # Build mode / model display strings
        current_mode = self.coach.backend_name
        current_model = self.coach.model_name

        try:
            self._set_mode_model_buttons(current_mode, current_model)
        except Exception:
            pass

        # Status bar
        model_display = f"{current_mode}/{current_model}" if current_model else current_mode
        self.update_status("MODEL", model_display)
        self.update_status("STYLE", self.coach.advice_style.upper())
        self.update_status("BACKEND", f"OK ({current_mode})")

        # Sync voice output
        if self.coach._voice_output:
            curr_id, desc = self.coach._voice_output.current_voice
            self.update_status("VOICE_ID", desc)
            try:
                btn = self.query_one("#btn-voice-select", Button)
                btn.label = self._voice_button_label(desc)
            except Exception:
                pass
            # Sync speed button label
            self._update_speed_button(self.coach._voice_output._speed)
            self.sub_title = desc

        # Autopilot button
        self._sync_autopilot_button()

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

            # Keep analyze button functional — just note recording in log
            try:
                btn = self.query_one("#btn-analyze", Button)
                btn.label = "Analyze Match"
                btn.variant = "warning"
            except:
                pass

            if self.log_widget:
                self.log_widget.write("[dim]Match recording started automatically[/]")

        except Exception:
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
            try:
                self.query_one("#btn-voice-select", Button).label = self._voice_button_label(value)
            except Exception:
                pass
            self.sub_title = value
        elif key == "SEAT_INFO":
            self.query_one("#status-seat", Static).update(f"Seat: {value}")
        elif key == "BACKEND":
            self.query_one("#status-backend", Static).update(f"Backend: {value}")
        elif key == "LOG":
            widget = self.query_one("#status-log-health", Static)
            if value:
                widget.update(f"[bold yellow]Log: {value}[/]")
            else:
                widget.update("")
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

        try:
            btn_id = event.button.id
        except Exception:
            return

        if btn_id == "btn-mode":
            try:
                self._cycle_mode()
            except Exception as e:
                logger.error(f"Mode cycle crash: {e}")
        elif btn_id == "btn-model":
            try:
                self._cycle_model()
            except Exception as e:
                logger.error(f"Model cycle crash: {e}")
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
        elif btn_id == "btn-autopilot":
            self.action_autopilot_toggle()
        elif btn_id == "btn-analyze":
            self.action_analyze_match()
        elif btn_id == "btn-update":
            if self._pending_remote_version:
                threading.Thread(target=self._do_apply_update, daemon=True).start()
        elif btn_id == "btn-restart":
            self.action_restart()
        elif btn_id == "btn-win-plan":
            self.action_read_win_plan()

    def _cycle_mode(self) -> None:
        """Toggle between online and local mode."""
        current_mode = self.coach.backend_name

        if current_mode == "online":
            new_mode = "local"
        else:
            new_mode = "online"

        # Show "switching..." while verifying in background
        btn = self.query_one("#btn-mode", Button)
        btn.label = "Switching..."

        threading.Thread(
            target=self._verify_and_switch,
            args=(new_mode, None),
            daemon=True,
        ).start()

    def _cycle_model(self) -> None:
        """Cycle to next model within the current mode."""
        try:
            from arenamcp.coach import get_models_for_mode

            mode = self.coach.backend_name
            # Normalize old backend names to new mode names
            if mode not in ("online", "local"):
                mode = "local"

            if getattr(self, '_model_list_for', None) != mode:
                self._model_list = get_models_for_mode(mode)
                self._model_list_for = mode

            models = self._model_list
            if len(models) <= 1:
                self.write_log(f"[yellow]Only one model for {mode}[/]")
                return

            current_model = self.coach.model_name

            idx = -1
            for i, (_, mid) in enumerate(models):
                if mid == current_model:
                    idx = i
                    break
            if idx == -1 and current_model is None:
                for i, (_, mid) in enumerate(models):
                    if mid is None:
                        idx = i
                        break

            next_idx = (idx + 1) % len(models)
            display_name, new_model = models[next_idx]

            # Run backend switch in a thread (it may make HTTP calls)
            def _do_switch():
                self.coach.set_backend(mode, new_model)
                model_label = self._model_button_label(new_model)
                model_display = f"{mode}/{new_model}" if new_model else mode
                self._model_list_for = None
                def _update():
                    try:
                        self.query_one("#btn-model", Button).label = model_label
                        self.update_status("MODEL", model_display)
                    except Exception:
                        pass
                self.call_from_thread(_update)

            threading.Thread(target=_do_switch, daemon=True).start()

        except Exception as e:
            logger.error(f"Model cycle error: {e}")
            self.write_log(f"[red]Error cycling model: {e}[/]")

    def _cycle_voice_select(self) -> None:
        """Cycle to next TTS voice on click."""
        if not self.coach or not self.coach._voice_output:
            return

        voice_id, desc = self.coach._voice_output.next_voice()
        btn = self.query_one("#btn-voice-select", Button)
        btn.label = self._voice_button_label(desc)
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

        # Handle /chance command — win probability estimate
        if text.lower() in ("/chance", "/winrate", "/odds"):
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            threading.Thread(target=self._do_win_probability, daemon=True).start()
            return

        # Handle /deck-strategy command
        if text.lower() in ("/deck-strategy", "/deckstrategy", "/deck"):
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            threading.Thread(target=self._do_deck_strategy, daemon=True).start()
            return

        # Handle /subscribe command
        if text.lower() == "/subscribe":
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            self._handle_subscribe()
            return

        # Handle /local command — configure local model endpoint
        if text.lower().startswith("/local"):
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            self._handle_local_config(text)
            return

        # Handle /online command — switch to online mode
        if text.lower() == "/online":
            self.write_log(f"\n[bold cyan]YOU: {text}[/]")
            threading.Thread(
                target=self._verify_and_switch,
                args=("online", None),
                daemon=True,
            ).start()
            return

        # Handle /key command — set license key
        if text.lower().startswith("/key"):
            self.write_log(f"\n[bold cyan]YOU: /key ****[/]")
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self.write_log("[yellow]Usage: /key YOUR_LICENSE_KEY[/]")
            else:
                key = parts[1].strip()
                from arenamcp.settings import get_settings
                get_settings().set("license_key", key)
                self.write_log("[green]License key saved.[/]")
                # Verify in background
                threading.Thread(target=self._check_subscription, daemon=True).start()
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
            self.coach._inject_library_summary_if_needed(game_state)
            advice = self.coach._coach.get_advice(game_state, question=text, style=self.coach.advice_style)

            # Check for backend auth/billing failures → auto-fallback
            if self.coach.check_advice_for_backend_failure(advice):
                # Retry with fallback backend
                advice = self.coach._coach.get_advice(game_state, question=text, style=self.coach.advice_style)

            # Suppress raw error strings from being displayed as advice
            from arenamcp.backend_detect import is_query_failure_retriable
            if (
                advice.startswith("Error")
                or "didn't catch that" in advice
                or (is_query_failure_retriable(advice) and len(advice) < 200)
            ):
                self.call_from_thread(self.update_status, "BACKEND", f"ERROR ({self.coach.backend_name})")
                self.call_from_thread(self.write_log, f"[red]Backend error: {advice}[/]")
            else:
                self.call_from_thread(self.update_status, "BACKEND", f"OK ({self.coach.backend_name})")
                self.call_from_thread(self.write_advice, advice, "Chat Response")
        except Exception as e:
            self.call_from_thread(self.write_log, f"[red]Chat error: {e}[/]")

    def _handle_subscribe(self):
        """Handle /subscribe command — show subscription status or open browser."""
        from arenamcp.settings import get_settings
        from arenamcp.subscription import check_subscription, open_subscribe_page, SUBSCRIBE_URL

        settings = get_settings()
        license_key = settings.get("license_key", "")

        if not license_key:
            self.write_log(
                f"[bold]No license key configured.[/]\n"
                f"Visit [link={SUBSCRIBE_URL}]{SUBSCRIBE_URL}[/link] to subscribe.\n"
                f"Then enter your key with: /key YOUR_LICENSE_KEY"
            )
            open_subscribe_page()
            return

        status = check_subscription(license_key, force=True)
        if status.is_valid:
            self.write_log(
                f"[green]Subscription active![/] Status: {status.status}"
                + (f", expires: {status.expires_at}" if status.expires_at else "")
            )
        else:
            self.write_log(
                f"[bold red]Subscription issue:[/] {status.message}\n"
                f"Visit [link={SUBSCRIBE_URL}]{SUBSCRIBE_URL}[/link] to renew."
            )
            open_subscribe_page()

    def _handle_local_config(self, text: str):
        """Handle /local command — configure local model endpoint.

        Usage:
            /local                     — show current config
            /local ollama              — set to Ollama defaults
            /local lmstudio            — set to LM Studio defaults
            /local http://host:port/v1 — set custom endpoint
        """
        from arenamcp.settings import get_settings
        settings = get_settings()

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            # Show current config
            url = settings.get("local_url", "http://localhost:11434/v1")
            model = settings.get("local_model", "auto-detect")
            api_key = settings.get("local_api_key", "ollama")
            self.write_log(
                f"[bold]Local config:[/]\n"
                f"  URL: {url}\n"
                f"  Model: {model or 'auto-detect'}\n"
                f"  API Key: {api_key}\n\n"
                f"[dim]Usage:[/]\n"
                f"  /local ollama      — Ollama at localhost:11434\n"
                f"  /local lmstudio    — LM Studio at localhost:1234\n"
                f"  /local URL         — custom OpenAI-compatible endpoint\n"
                f"  /local URL KEY     — custom endpoint with API key"
            )
            return

        arg = parts[1].strip()
        arg_parts = arg.split(maxsplit=1)
        provider = arg_parts[0].lower()

        if provider == "ollama":
            settings.set("local_url", "http://localhost:11434/v1", save=False)
            settings.set("local_api_key", "ollama", save=False)
            settings.set("local_model", None, save=True)
            self.write_log("[green]Local config set to Ollama (localhost:11434)[/]")
        elif provider in ("lmstudio", "lm-studio", "lm_studio"):
            settings.set("local_url", "http://localhost:1234/v1", save=False)
            settings.set("local_api_key", "lm-studio", save=False)
            settings.set("local_model", None, save=True)
            self.write_log("[green]Local config set to LM Studio (localhost:1234)[/]")
        elif provider.startswith("http"):
            url = provider
            api_key = arg_parts[1].strip() if len(arg_parts) > 1 else "no-key"
            settings.set("local_url", url, save=False)
            settings.set("local_api_key", api_key, save=False)
            settings.set("local_model", None, save=True)
            self.write_log(f"[green]Local config set to {url}[/]")
        else:
            self.write_log(f"[yellow]Unknown provider: {provider}. Use ollama, lmstudio, or a URL.[/]")
            return

        # Switch to local mode
        if self.coach:
            threading.Thread(
                target=self._verify_and_switch,
                args=("local", None),
                daemon=True,
            ).start()

    def _verify_and_switch(self, mode, model):
        """Validate backend health then switch. Falls back to local on failure.

        Runs in a background thread — all UI updates go through call_from_thread.
        """
        from arenamcp.backend_detect import validate_backend

        mode_label = "Online" if mode == "online" else "Local"
        self.call_from_thread(self.write_log, f"Connecting to {mode_label}...")

        if mode == "online":
            # Check subscription before switching
            from arenamcp.settings import get_settings
            license_key = get_settings().get("license_key", "")
            if not license_key:
                self.call_from_thread(
                    self.write_log,
                    "[bold red]No license key configured.[/] "
                    "Use /subscribe to get one.",
                )
                self.call_from_thread(self._revert_mode_button)
                return

        ok, err = validate_backend(mode)
        if not ok:
            self.call_from_thread(
                self.write_log,
                f"[bold red]{mode_label} unavailable: {err}[/]",
            )
            self.call_from_thread(
                self.write_log,
                f"[yellow]Staying on current mode.[/]",
            )
            self.call_from_thread(self._revert_mode_button)
            return

        # Proceed with switch
        self.coach.set_backend(mode, model)
        actual_mode = self.coach.backend_name
        actual_model = self.coach.model_name

        model_display = f"{actual_mode}/{actual_model}" if actual_model else actual_mode

        # Invalidate cached model list so Model button rebuilds
        self._model_list_for = None

        def _update_btn():
            try:
                self._set_mode_model_buttons(actual_mode, actual_model)
                self.update_status("MODEL", model_display)
            except Exception:
                pass
        self.call_from_thread(_update_btn)

    def _revert_mode_button(self):
        """Revert mode and model buttons to the current active mode."""
        if not self.coach:
            return
        current = self.coach.backend_name
        current_model = self.coach.model_name

        try:
            self._set_mode_model_buttons(current, current_model)
        except Exception:
            pass

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
        if self._debug_report_in_progress:
            self.write_log("[yellow]Debug report already in progress...[/]")
            return

        self._debug_report_in_progress = True
        self.write_log("[yellow]Generating debug report...[/]")
        threading.Thread(target=self._do_copy_debug, daemon=True).start()

    def _do_copy_debug(self):
        """Save debug report and copy path to clipboard."""
        try:
            if self.coach and hasattr(self.coach, 'save_bug_report'):
                bug_path = self.coach.save_bug_report("Copy Debug (F7)", announce=False)
                if bug_path:
                    file_url = f"file:///{str(bug_path).replace(chr(92), '/')}"
                    self.call_from_thread(self.write_log, "[green]Bug report saved.[/]")
                    self.call_from_thread(self.write_log, f"[dim]{file_url}[/]")
                    self.call_from_thread(
                        self.write_log,
                        "[yellow]Type /bugreport to submit to GitHub.[/]",
                    )
                else:
                    self.call_from_thread(self.write_log, "[red]Failed to save debug report[/]")
            else:
                self.call_from_thread(self.write_log, "[yellow]Coach not available for debug report[/]")
        finally:
            self.call_from_thread(setattr, self, "_debug_report_in_progress", False)

    def _do_win_probability(self):
        """Estimate win probability and display/speak it."""
        if not self.coach or not self.coach._coach:
            self.call_from_thread(self.write_log, "[yellow]Coach not available[/]")
            return

        self.call_from_thread(self.write_log, "[dim]Evaluating win probability...[/]")
        try:
            game_state = self.coach._mcp.get_game_state()
            self.coach._inject_library_summary_if_needed(game_state)
            opp_cards = getattr(self.coach, '_opponent_played_cards', None)
            if opp_cards is None:
                opp_cards = game_state.get("_match_context", {}).get("opponent_played_cards", [])
            result = self.coach._coach.generate_win_probability(game_state, opp_cards)
            if result:
                self.call_from_thread(self.write_advice, result, "Win Probability")
                self.coach.speak_advice(result, blocking=False)
            else:
                self.call_from_thread(self.write_log, "[yellow]Could not estimate win probability.[/]")
        except Exception as e:
            self.call_from_thread(self.write_log, f"[red]Win probability error: {e}[/]")

    def _do_deck_strategy(self):
        """Generate or recall the deck strategy and display/speak it."""
        if not self.coach:
            self.call_from_thread(self.write_log, "[yellow]Coach not available[/]")
            return

        # If we already have a stored strategy, show and speak it
        existing = self.coach.get_deck_strategy()
        if existing:
            self.call_from_thread(self.write_advice, existing, "Deck Strategy")
            self.coach.speak_advice(existing, blocking=False)
            return

        # No stored strategy — generate from current game's library
        self.call_from_thread(self.write_log, "[dim]Generating deck strategy...[/]")
        self.coach._generate_deck_strategy_brief()  # uses deck_cards from game state

    def _do_submit_bugreport(self, user_message: str = None):
        """Submit the most recent bug report as a GitHub issue.

        Args:
            user_message: Optional user description (from ``/bugreport <msg>``).
                          Used in the issue title and body when provided.
        """
        import shutil

        bug_dir = LOG_DIR / "bug_reports"

        # Auto-save a bug report if none exists yet (so /bugreport works without F7)
        if self.coach and hasattr(self.coach, 'save_bug_report'):
            self.coach.save_bug_report("/bugreport", announce=False)

        if not bug_dir.exists():
            self.call_from_thread(self.write_log, "[red]No bug reports found.[/]")
            return

        # Find the most recent bug report JSON
        reports = sorted(bug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not reports:
            self.call_from_thread(self.write_log, "[red]No bug reports found.[/]")
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
        body_parts.append(f"**Mode:** {config.get('mode', config.get('backend', 'unknown'))}")
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

        # Include pending post-match analysis if available
        post_match = None
        if self.coach and hasattr(self.coach, '_pending_post_match_analysis'):
            post_match = self.coach._pending_post_match_analysis
        if post_match:
            body_parts.append("")
            body_parts.append("### Post-Match Coaching Analysis")
            pm_result = getattr(self.coach, '_pending_post_match_result', None) or "unknown"
            body_parts.append(f"**Result:** {pm_result}")
            body_parts.append("")
            body_parts.append(post_match)
            # Clear pending analysis after including it
            self.coach._pending_post_match_analysis = None
            self.coach._pending_post_match_result = None

        repo = "josharmour/mtgacoach"

        # Try gh CLI first
        gh_bin = shutil.which("gh")
        if gh_bin:
            self.call_from_thread(self.write_log, "[yellow]Creating GitHub issue via gh CLI...[/]")
            try:
                import subprocess
                # Skip gh path quickly when auth is missing/invalid.
                auth_result = subprocess.run(
                    [gh_bin, "auth", "status", "-h", "github.com"],
                    capture_output=True, text=True, timeout=8,
                )
                if auth_result.returncode != 0:
                    msg = (auth_result.stderr or auth_result.stdout or "").strip()
                    if msg:
                        self.call_from_thread(
                            self.write_log,
                            f"[yellow]gh not authenticated: {msg.splitlines()[0]}[/]",
                        )
                    raise RuntimeError("gh auth unavailable")

                # Upload bug report JSON as a gist
                gist_url = None
                gist_result = subprocess.run(
                    [gh_bin, "gist", "create", "--public", "--desc",
                     f"mtgacoach Bug Report {timestamp}", str(report_path)],
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
                    copy_to_clipboard(issue_url)
                    self.call_from_thread(
                        self.write_log,
                        f"[bold green]Issue created (URL copied): {issue_url}[/]",
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
            self.call_from_thread(
                self.write_log,
                f"[dim]Using debug report: {report_path}[/]",
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
            self.call_from_thread(
                self.write_log,
                f"[dim]Attach this file: {report_path}[/]",
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

        # Show watchdog summary before launching analysis
        missed = getattr(self.coach, '_missed_decisions', [])
        if missed:
            self.write_log(
                f"[bold yellow]Including {len(missed)} vision watchdog "
                f"detection(s) in analysis[/]"
            )
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
        btn.label = f"{speed}x"

    def action_cycle_model(self) -> None:
        """Cycle through available modes (F12)."""
        if not self.coach:
            return
        self._cycle_mode()

    def action_read_win_plan(self) -> None:
        """Read pending win plan aloud (Ctrl+0 or click)."""
        if self.coach:
            threading.Thread(target=self.coach._on_read_win_plan, daemon=True).start()

    def _get_autopilot(self):
        if not self.coach:
            return None
        return getattr(self.coach, "_autopilot", None)

    def action_autopilot_cancel(self) -> None:
        """Cancel pending autopilot countdown/confirmation."""
        ap = self._get_autopilot()
        if not ap:
            self.write_log("[dim]Autopilot not enabled[/]")
            return
        try:
            ap.on_spacebar()
            self.write_log("[yellow]Autopilot: cancel requested[/]")
        except Exception as e:
            self.write_log(f"[red]Autopilot cancel failed: {e}[/]")

    def action_autopilot_abort(self) -> None:
        """Abort current autopilot plan immediately."""
        ap = self._get_autopilot()
        if not ap:
            self.write_log("[dim]Autopilot not enabled[/]")
            return
        try:
            ap.on_abort()
            self.write_log("[yellow]Autopilot: abort requested[/]")
        except Exception as e:
            self.write_log(f"[red]Autopilot abort failed: {e}[/]")

    def action_autopilot_toggle_afk(self) -> None:
        """Toggle autopilot AFK mode."""
        ap = self._get_autopilot()
        if not ap:
            self.write_log("[dim]Autopilot not enabled[/]")
            return
        try:
            enabled = ap.toggle_afk()
            state = "ON" if enabled else "OFF"
            self.write_log(f"[cyan]Autopilot AFK: {state}[/]")
        except Exception as e:
            self.write_log(f"[red]Autopilot AFK toggle failed: {e}[/]")

    def action_autopilot_toggle_land(self) -> None:
        """Toggle autopilot land-drop-only mode."""
        ap = self._get_autopilot()
        if not ap:
            self.write_log("[dim]Autopilot not enabled[/]")
            return
        try:
            enabled = ap.toggle_land_drop()
            state = "ON" if enabled else "OFF"
            self.write_log(f"[cyan]Autopilot Land-drop: {state}[/]")
        except Exception as e:
            self.write_log(f"[red]Autopilot land-drop toggle failed: {e}[/]")

    def _sync_autopilot_button(self) -> None:
        """Sync the Autopilot button label/variant with actual state."""
        try:
            btn = self.query_one("#btn-autopilot", Button)
            if self.coach and hasattr(self.coach, '_autopilot_enabled') and self.coach._autopilot_enabled:
                btn.label = "AP:ON"
                btn.variant = "success"
            else:
                btn.label = "AP:OFF"
                btn.variant = "warning"
        except Exception:
            pass

    def action_autopilot_toggle(self) -> None:
        """Toggle autopilot on/off at runtime (button or F12)."""
        if not self.coach:
            self.write_log("[dim]Coach not running[/]")
            return
        try:
            enabled = self.coach.toggle_autopilot()
            if enabled:
                self.write_log("[bold green]Autopilot: ON[/]")
            else:
                self.write_log("[bold yellow]Autopilot: OFF[/]")
            self._sync_autopilot_button()
        except Exception as e:
            self.write_log(f"[red]Autopilot toggle failed: {e}[/]")

    def action_quit(self) -> None:
        if self.coach:
            self.coach.stop()
        self.exit()

def _set_console_icon():
    """Set the Windows console window icon and ungroup from other terminals.

    Does two things:
    1. Sets a unique AppUserModelID so Windows treats this as a separate app
       in the taskbar (not grouped with cmd.exe / Terminal windows).
    2. Loads the custom .ico file and sets it as the window icon (title bar,
       alt-tab, and taskbar).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes
        import shutil
        import tempfile
        from pathlib import Path

        # --- 1. Set unique AppUserModelID to ungroup from terminal ---
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "mtgacoach.Coach.TUI"
            )
        except Exception:
            pass

        # --- 2. Find and load the icon ---
        icon_path = Path(__file__).parent.parent.parent / "icon.ico"
        if not icon_path.exists():
            icon_path = Path(__file__).parent.parent.parent / "mtga_coach.ico"
        if not icon_path.exists():
            return

        # Copy to local temp if on a network/UNC path
        # (LoadImageW doesn't handle UNC paths reliably)
        icon_str = str(icon_path.resolve())
        if icon_str.startswith("\\\\"):
            tmp = Path(tempfile.gettempdir()) / "arenamcp_icon.ico"
            shutil.copy2(icon_path, tmp)
            icon_str = str(tmp)

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        # LoadImageW with proper arg/return types for 64-bit
        user32.LoadImageW.restype = ctypes.wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            ctypes.wintypes.HINSTANCE,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.wintypes.UINT,
        ]
        user32.SendMessageW.restype = ctypes.c_long
        user32.SendMessageW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010

        # Big icon (32x32 for alt-tab / taskbar)
        icon_big = user32.LoadImageW(
            None, icon_str, IMAGE_ICON, 32, 32, LR_LOADFROMFILE,
        )
        # Small icon (16x16 for title bar)
        icon_small = user32.LoadImageW(
            None, icon_str, IMAGE_ICON, 16, 16, LR_LOADFROMFILE,
        )

        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        if icon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, icon_big)
        if icon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, icon_small)
    except Exception:
        pass  # Non-critical — just skip if anything fails


def run_tui(args):
    """Run the TUI application with restart support.

    Args:
        args: Command line arguments from argparse.

    The TUI will restart if the user clicks the Restart button.
    When running under the launcher, it exits with code 42 to signal restart.
    Otherwise, it loops and re-creates the app in-process.
    """
    import os

    _set_console_icon()

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
                python = sys.executable
                os.execv(python, [python, "-m", "arenamcp.standalone"] + sys.argv[1:])
                # execv replaces the process, so this line is never reached
        else:
            # Normal exit — use os._exit() to force-kill all daemon threads
            # and child subprocesses (claude CLI, gemini CLI, etc.) that may
            # outlive a normal sys.exit() when blocked in I/O.
            os._exit(0)
