"""Pipe-based UI adapter for headless operation with a native GUI frontend.

Writes JSON lines to stdout (coach → GUI) and reads JSON lines from stdin
(GUI → coach). Designed for use with the WinUI 3 launcher app.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arenamcp.standalone import StandaloneCoach

logger = logging.getLogger(__name__)

# Regex to strip Textual/Rich markup tags like [bold], [red], [/], [link=...]
_MARKUP_RE = re.compile(r"\[/?[a-zA-Z_][a-zA-Z0-9_ =.:#/\"'-]*\]|\[/\]")


def strip_markup(text: str) -> str:
    """Remove Rich/Textual markup tags from text."""
    return _MARKUP_RE.sub("", text).strip()


class PipeAdapter:
    """UIAdapter that communicates via JSON lines over stdin/stdout."""

    def __init__(self) -> None:
        self._coach: StandaloneCoach | None = None
        self._stdin_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()
        # Non-blocking write queue — prevents coaching loop from freezing
        # when the parent WinUI process is slow to read stdout
        import queue
        self._write_queue: queue.Queue[str] = queue.Queue(maxsize=500)

    def bind_coach(self, coach: StandaloneCoach) -> None:
        """Bind to a coach instance for command dispatch."""
        self._coach = coach
        # Startup beacon — confirms pipe is alive
        self._emit({"type": "log", "message": "Pipe adapter connected."})

    def start_stdin_reader(self) -> None:
        """Start background threads for stdin reading and stdout writing."""
        self._running = True
        self._stdin_thread = threading.Thread(
            target=self._stdin_loop, daemon=True, name="pipe-stdin"
        )
        self._stdin_thread.start()
        self._stdout_thread = threading.Thread(
            target=self._stdout_loop, daemon=True, name="pipe-stdout"
        )
        self._stdout_thread.start()

    def stop(self) -> None:
        self._running = False

    # ── UIAdapter interface ──────────────────────────────────────────

    def log(self, message: str) -> None:
        self._emit({"type": "log", "message": strip_markup(message)})

    def advice(self, text: str, seat_info: str) -> None:
        self._emit({"type": "advice", "text": strip_markup(text), "seat_info": seat_info})

    def status(self, key: str, value: str) -> None:
        self._emit({"type": "status", "key": key, "value": strip_markup(value)})

    def error(self, message: str) -> None:
        self._emit({"type": "error", "message": strip_markup(message)})

    def speak(self, text: str) -> None:
        # Forward to voice output if available
        if self._coach and self._coach._voice_output:
            self._coach._voice_output.speak(text, blocking=False)
        self._emit({"type": "speak", "text": strip_markup(text)})

    def subtask(self, status: str) -> None:
        self._emit({"type": "subtask", "status": strip_markup(status)})

    # ── Game state emission ──────────────────────────────────────────

    def emit_game_state(self, snapshot: dict[str, Any]) -> None:
        """Emit a game state snapshot to the GUI."""
        self._emit({"type": "game_state", "data": snapshot})

    # ── Internal ─────────────────────────────────────────────────────

    def _emit(self, event: dict[str, Any]) -> None:
        """Queue a JSON line for the stdout writer thread.

        Non-blocking: if the queue is full (parent not reading), drops
        the oldest event to prevent the coaching loop from freezing.
        """
        try:
            line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        except Exception as e:
            logger.error("pipe _emit JSON encode failed: %s (event type: %s)", e,
                         event.get("type", "?"))
            return

        import queue
        try:
            self._write_queue.put_nowait(line)
        except queue.Full:
            # Drop oldest to make room — better than blocking the coaching loop
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._write_queue.put_nowait(line)
            except queue.Full:
                pass

    def _stdout_loop(self) -> None:
        """Background thread that drains the write queue to stdout."""
        while self._running:
            try:
                line = self._write_queue.get(timeout=0.5)
                sys.stdout.write(line)
                sys.stdout.flush()
            except Exception:
                # queue.Empty on timeout, or broken pipe
                if not self._running:
                    break

    def _stdin_loop(self) -> None:
        """Read JSON line commands from stdin and dispatch to coach."""
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    # stdin closed (GUI exited)
                    logger.info("stdin closed, stopping coach")
                    if self._coach:
                        self._coach._running = False
                    break
                line = line.strip()
                if not line:
                    continue
                cmd = json.loads(line)
                self._dispatch(cmd)
            except json.JSONDecodeError as e:
                logger.warning("Bad JSON from stdin: %s", e)
            except Exception as e:
                logger.error("stdin reader error: %s", e)

    def _dispatch(self, cmd: dict[str, Any]) -> None:
        """Dispatch a command dict to the appropriate coach method."""
        coach = self._coach
        if coach is None:
            return

        action = cmd.get("cmd", "")
        try:
            if action == "toggle_autopilot":
                enabled = coach.toggle_autopilot()
                self.status("AUTOPILOT", "AP:ON" if enabled else "AP:OFF")
            elif action == "toggle_mute":
                if coach._voice_output:
                    muted = coach._voice_output.toggle_mute()
                    self.status("MUTE", "Muted" if muted else "Unmuted")
                    self.log(f"Voice {'muted' if muted else 'unmuted'}")
            elif action == "cycle_mode":
                self._cycle_mode()
            elif action == "cycle_model":
                self._cycle_model()
            elif action == "cycle_voice":
                coach._on_voice_cycle_hotkey()
            elif action == "cycle_speed":
                coach._on_speed_hotkey()
            elif action == "toggle_style":
                freq = coach._advice_frequency
                coach._advice_frequency = (
                    "every_priority" if freq == "start_of_turn" else "start_of_turn"
                )
                label = "VERBOSE" if coach._advice_frequency == "every_priority" else "CONCISE"
                self.status("STYLE", label)
            elif action == "toggle_afk":
                if coach._autopilot:
                    coach._autopilot._afk = not coach._autopilot._afk
                    state = "ON" if coach._autopilot._afk else "OFF"
                    self.status("AFK", state)
                    self.log(f"AFK mode: {state}")
            elif action == "toggle_land_only":
                if coach._autopilot:
                    coach._autopilot._land_only = not getattr(
                        coach._autopilot, "_land_only", False
                    )
                    state = "ON" if coach._autopilot._land_only else "OFF"
                    self.status("LAND_ONLY", state)
                    self.log(f"Land-only mode: {state}")
            elif action == "autopilot_cancel":
                if coach._autopilot:
                    coach._autopilot.on_cancel()
            elif action == "autopilot_abort":
                if coach._autopilot:
                    coach._autopilot.on_abort()
            elif action == "analyze_screen":
                threading.Thread(
                    target=coach.take_screenshot_analysis, daemon=True
                ).start()
            elif action == "debug_report":
                threading.Thread(
                    target=self._handle_debug_report, daemon=True
                ).start()
            elif action == "read_win_plan":
                coach._on_read_win_plan()
            elif action == "chat":
                text = cmd.get("text", "")
                if text:
                    threading.Thread(
                        target=self._handle_chat, args=(text,), daemon=True
                    ).start()
            elif action == "restart":
                coach._restart_requested = True
                coach._running = False
            else:
                logger.warning("Unknown pipe command: %s", action)
        except Exception as e:
            logger.error("Command dispatch error (%s): %s", action, e)
            self.error(f"Command failed: {action}: {e}")

    def _handle_debug_report(self) -> None:
        """Save a bug report and notify the GUI of the path."""
        coach = self._coach
        if coach is None:
            return
        try:
            bug_path = coach.save_bug_report("Launcher Debug Report", announce=False)
            if bug_path:
                self.log(f"Bug report saved: {bug_path}")
            else:
                self.error("Failed to save bug report")
        except Exception as e:
            self.error(f"Bug report failed: {e}")

    def _handle_chat(self, text: str) -> None:
        """Process a chat message from the GUI."""
        coach = self._coach
        if coach is None or coach._coach is None:
            return
        try:
            game_state = coach._mcp.get_game_state() if coach._mcp else {}
            response = coach._coach.get_advice(game_state, question=text)
            if response:
                self.advice(response, "CHAT")
                coach.speak_advice(response)
        except Exception as e:
            self.error(f"Chat failed: {e}")

    def _cycle_mode(self) -> None:
        """Cycle between online and local backends."""
        coach = self._coach
        if coach is None:
            return
        current = coach._backend_name
        new_mode = "local" if current in ("online", "proxy", "auto") else "online"

        def _do_switch():
            try:
                from arenamcp.backend_detect import validate_backend
                mode_label = "Online" if new_mode == "online" else "Local"
                self.log(f"Connecting to {mode_label}...")

                if new_mode == "online":
                    from arenamcp.settings import get_settings
                    key = get_settings().get("license_key", "")
                    if not key:
                        self.error("No license key configured.")
                        return

                ok, err = validate_backend(new_mode)
                if not ok:
                    self.error(f"{mode_label} unavailable: {err}")
                    return

                coach.set_backend(new_mode, None)
                actual = coach.backend_name
                model = coach.model_name
                self.status("BACKEND", f"{actual} ({model or 'default'})")
                self.status("MODEL", model or "default")
                self.log(f"Switched to {actual}/{model or 'default'}")
            except Exception as e:
                self.error(f"Mode switch failed: {e}")

        threading.Thread(target=_do_switch, daemon=True).start()

    def _cycle_model(self) -> None:
        """Cycle through available models for the current backend."""
        coach = self._coach
        if coach is None:
            return

        try:
            from arenamcp.coach import get_models_for_mode

            mode = coach.backend_name
            if mode not in ("online", "local"):
                mode = "local"

            models = get_models_for_mode(mode)
            if len(models) <= 1:
                self.log(f"Only one model for {mode}")
                return

            current = coach.model_name
            idx = -1
            for i, (_, mid) in enumerate(models):
                if mid == current:
                    idx = i
                    break

            next_idx = (idx + 1) % len(models)
            next_name, next_id = models[next_idx]
            self.log(f"Switching model to {next_name}...")

            def _do_switch():
                try:
                    coach.set_backend(mode, next_id)
                    actual_model = coach.model_name
                    self.status("MODEL", actual_model or "default")
                    self.log(f"Switched to {actual_model}")
                except Exception as e:
                    self.error(f"Model switch failed: {e}")

            threading.Thread(target=_do_switch, daemon=True).start()
        except Exception as e:
            self.error(f"Cycle model failed: {e}")
