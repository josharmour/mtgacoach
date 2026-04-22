"""Pipe-based UI adapter for headless operation with a native GUI frontend.

Writes JSON lines to stdout (coach -> GUI) and reads JSON lines from stdin
(GUI -> coach). Used by the desktop app to keep the coaching engine isolated
from the UI process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import webbrowser
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from arenamcp.standalone import StandaloneCoach

logger = logging.getLogger(__name__)

# Regex to strip Textual/Rich markup tags like [bold], [red], [/], [link=...]
_MARKUP_RE = re.compile(r"\[/?[a-zA-Z_][a-zA-Z0-9_ =.:#/\"'-]*\]|\[/\]")
_ISSUE_URL_RE = re.compile(r"/issues/(\d+)(?:$|[?#])")


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
        # Start the stdout writer thread IMMEDIATELY — before coach.start()
        # emits initialization events. Otherwise those events queue up
        # unbounded and eventually get dropped.
        self._running = True
        if self._stdout_thread is None:
            self._stdout_thread = threading.Thread(
                target=self._stdout_loop, daemon=True, name="pipe-stdout"
            )
            self._stdout_thread.start()
        # Startup beacon — confirms pipe is alive
        self._emit({"type": "log", "message": "Pipe adapter connected."})

    def start_stdin_reader(self) -> None:
        """Start the stdin reader thread. Stdout thread was already started
        in bind_coach() so no events are dropped during coach init."""
        self._running = True
        if self._stdin_thread is None:
            self._stdin_thread = threading.Thread(
                target=self._stdin_loop, daemon=True, name="pipe-stdin"
            )
            self._stdin_thread.start()
        # Stdout thread is already running from bind_coach(); start it here
        # only if it somehow wasn't started (defensive)
        if self._stdout_thread is None:
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

    def emit_speech_audio(
        self,
        *,
        path: str,
        text: str,
        duration: float,
        voice_id: str,
        voice_name: str,
    ) -> None:
        self._emit(
            {
                "type": "speak_audio",
                "path": path,
                "text": strip_markup(text),
                "duration": round(duration, 3),
                "voice_id": voice_id,
                "voice_name": voice_name,
            }
        )

    def emit_speech_request(
        self,
        *,
        text: str,
        voice_id: str,
        voice_name: str,
        speed: float,
    ) -> None:
        self._emit(
            {
                "type": "speak_request",
                "text": strip_markup(text),
                "voice_id": voice_id,
                "voice_name": voice_name,
                "speed": speed,
            }
        )

    def emit_speech_stop(self) -> None:
        self._emit({"type": "speak_stop"})

    def subtask(self, status: str) -> None:
        self._emit({"type": "subtask", "status": strip_markup(status)})

    # ── Game state emission ──────────────────────────────────────────

    def emit_game_state(self, snapshot: dict[str, Any]) -> None:
        """Emit a game state snapshot to the GUI."""
        self._emit({"type": "game_state", "data": snapshot})

    def emit_draft_state(self, snapshot: dict[str, Any]) -> None:
        """Emit a draft state snapshot to the GUI."""
        self._emit({"type": "draft_state", "data": snapshot})

    def emit_suggested_actions(self, actions: list[dict[str, Any]]) -> None:
        """Emit an ordered list of suggested match actions.

        Each action is a dict with keys:
          action_type, instance_id, grp_id, card_name, reason,
          target_instance_ids (optional list).

        The match overlay uses this to draw sequenced highlights
        (numbered rings) over the target cards in MTGA.
        """
        self._emit({"type": "suggested_actions", "actions": list(actions or [])})

    def emit_card_positions(self, payload: dict[str, Any]) -> None:
        """Forward a `get_card_positions` bridge response to the UI.

        Only the coach process owns the GRE bridge (single-instance pipe).
        The UI's match overlay needs card screen rects for highlighting,
        so the coach polls the bridge and relays the result via this event.
        """
        self._emit({"type": "card_positions", "data": payload or {}})

    def emit_post_match_feedback_request(self, analysis: str, match_result: str) -> None:
        self._emit(
            {
                "type": "post_match_feedback_request",
                "analysis": strip_markup(analysis),
                "match_result": match_result,
            }
        )

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

    def _copy_to_clipboard(self, text: str) -> bool:
        """Copy text to the clipboard without depending on UI thread state."""
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except ImportError:
            pass
        except Exception as e:
            logger.debug("pyperclip failed in pipe adapter: %s", e)

        try:
            process = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            process.communicate(input=text.encode("utf-8"), timeout=2)
            return process.returncode == 0
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception:
                pass
            logger.debug("clip command timed out in pipe adapter")
            return False
        except Exception as e:
            logger.debug("clip command failed in pipe adapter: %s", e)
            return False

    @staticmethod
    def _subprocess_kwargs_for_detached_io(*, capture_output: bool = False) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
        }
        if capture_output:
            kwargs.update(
                {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                }
            )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return kwargs

    def _get_github_token(self) -> str:
        token = (
            os.environ.get("MTGACOACH_GITHUB_TOKEN", "").strip()
            or os.environ.get("GITHUB_TOKEN", "").strip()
        )
        if token:
            return token

        import shutil

        gh = shutil.which("gh")
        if not gh:
            return ""

        try:
            result = subprocess.run(
                [gh, "auth", "token"],
                timeout=5,
                **self._subprocess_kwargs_for_detached_io(capture_output=True),
            )
            if result.returncode == 0:
                return (result.stdout or "").strip()
            logger.warning("gh auth token failed: %s", (result.stderr or "").strip())
        except Exception as e:
            logger.warning("Unable to read GitHub token from gh: %s", e)
        return ""

    def _format_issue_success_message(
        self,
        *,
        url: str,
        issue_number: int | None = None,
    ) -> str:
        if issue_number is None:
            match = _ISSUE_URL_RE.search(url)
            if match:
                try:
                    issue_number = int(match.group(1))
                except Exception:
                    issue_number = None
        if issue_number is not None:
            return f"Bug report submitted: #{issue_number} {url}"
        return f"Bug report submitted: {url}"

    def _stdout_loop(self) -> None:
        """Background thread that drains the write queue to stdout.

        Writes directly to the underlying binary buffer. On Windows this
        uses WriteFile which blocks, but the Python buffered writer's
        Lock is avoided and the write is one syscall per line.
        """
        # Get the raw binary writer (bypass text encoding layer)
        try:
            raw_out = sys.stdout.buffer
        except AttributeError:
            raw_out = sys.stdout

        while self._running:
            try:
                line = self._write_queue.get(timeout=0.5)
            except Exception:
                if not self._running:
                    break
                continue

            try:
                if isinstance(line, str):
                    raw_out.write(line.encode("utf-8"))
                else:
                    raw_out.write(line)
                raw_out.flush()
            except (OSError, ValueError, BrokenPipeError):
                # Parent exited or pipe broken
                self._running = False
                if self._coach:
                    self._coach._running = False
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
            elif action == "sync_voice_preferences":
                self._handle_sync_voice_preferences(cmd)
            elif action == "cycle_mode":
                self._cycle_mode()
            elif action == "cycle_model":
                self._cycle_model()
            elif action == "cycle_voice":
                coach._on_voice_cycle_hotkey()
            elif action == "cycle_speed":
                coach._on_speed_hotkey()
            elif action == "toggle_style":
                coach._on_style_toggle_hotkey()
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
                # Optional screenshot paths provided by the UI (coach + MTGA window).
                screenshots = cmd.get("screenshots") or {}
                threading.Thread(
                    target=self._handle_debug_report,
                    args=(screenshots,),
                    daemon=True,
                ).start()
            elif action == "read_win_plan":
                coach._on_read_win_plan()
            elif action == "win_probability":
                threading.Thread(
                    target=self._handle_win_probability, daemon=True
                ).start()
            elif action == "deck_strategy":
                threading.Thread(
                    target=self._handle_deck_strategy, daemon=True
                ).start()
            elif action == "bugreport":
                msg = cmd.get("text", "")
                extra_context = {
                    "source": cmd.get("source", ""),
                    "analysis": cmd.get("analysis", ""),
                    "match_result": cmd.get("match_result", ""),
                    "user_feedback": msg,
                }
                threading.Thread(
                    target=self._handle_bugreport, args=(msg, extra_context), daemon=True
                ).start()
            elif action == "subscribe":
                threading.Thread(
                    target=self._handle_subscribe, daemon=True
                ).start()
            elif action == "set_local_endpoint":
                self._handle_local_config(cmd.get("text", ""))
            elif action == "switch_online":
                threading.Thread(
                    target=self._handle_switch_online, daemon=True
                ).start()
            elif action == "set_license_key":
                self._handle_set_key(cmd.get("text", ""))
            elif action == "analyze_match":
                threading.Thread(
                    target=self._handle_analyze_match, daemon=True
                ).start()
            elif action == "chat":
                text = cmd.get("text", "")
                if text:
                    # Intercept slash commands from the chat input
                    if self._try_slash_command(text):
                        pass  # Handled
                    else:
                        threading.Thread(
                            target=self._handle_chat, args=(text,), daemon=True
                        ).start()
            elif action == "restart":
                coach._restart_requested = True
                coach._running = False
            elif action == "toggle_fallback_mode":
                self._handle_toggle_fallback_mode()
            else:
                logger.warning("Unknown pipe command: %s", action)
        except Exception as e:
            logger.error("Command dispatch error (%s): %s", action, e)
            self.error(f"Command failed: {action}: {e}")

    def _handle_toggle_fallback_mode(self) -> None:
        """Toggle autopilot's bridge-only-when-connected flag.

        When on (default): if the bridge can't submit an action, autopilot
        gives up and emits MANUAL REQUIRED advice.
        When off (legacy): autopilot falls back to mouse-click execution.
        """
        coach = self._coach
        if coach is None:
            return
        try:
            engine = getattr(coach._autopilot, "_engine", None) or getattr(coach._autopilot, "engine", None)
            # Try common places the autopilot engine lives
            cfg = None
            for holder in (coach._autopilot, engine, coach):
                if holder is None:
                    continue
                cfg = getattr(holder, "_config", None) or getattr(holder, "config", None)
                if cfg is not None and hasattr(cfg, "bridge_only_when_connected"):
                    break
            if cfg is None or not hasattr(cfg, "bridge_only_when_connected"):
                self.error("Autopilot config not available — fallback mode unchanged.")
                return
            cfg.bridge_only_when_connected = not cfg.bridge_only_when_connected
            mode = "advice" if cfg.bridge_only_when_connected else "mouse"
            self.status("FALLBACK_MODE", mode)
            self.log(f"Fallback mode: {mode}")
        except Exception as e:
            self.error(f"Failed to toggle fallback mode: {e}")

    def _handle_debug_report(self, screenshots: Optional[dict[str, str]] = None) -> None:
        """Save a bug report and notify the GUI of the path.

        Args:
            screenshots: Optional dict {'coach': path, 'mtga': path} of PNGs
                already saved by the UI. Embedded into the JSON report so
                they can be referenced from the GitHub issue body and kept
                alongside the local report.

        Emits a structured `bug_report_saved` event so the UI can copy the
        path to the clipboard and offer an upload dialog.
        """
        coach = self._coach
        if coach is None:
            return
        try:
            extra = {"screenshots": screenshots or {}}
            bug_path = coach.save_bug_report(
                "Launcher Debug Report",
                announce=False,
                extra_context=extra,
            )
            if bug_path:
                self.log(f"Bug report saved: {bug_path}")
                if screenshots:
                    for kind, p in screenshots.items():
                        self.log(f"  Screenshot ({kind}): {p}")
                self._emit({
                    "type": "bug_report_saved",
                    "path": str(bug_path),
                    "screenshots": screenshots or {},
                })
            else:
                self.error("Failed to save bug report")
                self._emit({
                    "type": "bug_report_saved",
                    "path": "",
                    "error": "Failed to save bug report",
                })
        except Exception as e:
            self.error(f"Bug report failed: {e}")
            self._emit({
                "type": "bug_report_saved",
                "path": "",
                "error": str(e),
            })

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

    def _handle_sync_voice_preferences(self, cmd: dict[str, Any]) -> None:
        coach = self._coach
        if coach is None or coach.settings is None:
            return

        voice_id = str(cmd.get("voice", "") or "").strip()
        speed_value = cmd.get("voice_speed")
        muted_value = cmd.get("muted")

        if voice_id:
            coach.settings.set("voice", voice_id)
        if speed_value is not None:
            try:
                coach.settings.set("voice_speed", float(speed_value))
            except (TypeError, ValueError):
                pass
        if muted_value is not None:
            coach.settings.set("muted", bool(muted_value))

        voice_output = coach._voice_output
        if voice_output is None:
            return

        if voice_id:
            try:
                voice_output.set_voice(voice_id)
            except Exception as e:
                logger.warning("Failed to sync voice preference %s: %s", voice_id, e)

        if speed_value is not None:
            try:
                applied_speed = voice_output.set_speed(float(speed_value))
                self.status("SPEED", f"{applied_speed:.1f}x")
            except (TypeError, ValueError):
                pass
            except Exception as e:
                logger.warning("Failed to sync voice speed %s: %s", speed_value, e)

        if muted_value is not None:
            try:
                should_mute = bool(muted_value)
                if bool(getattr(voice_output, "muted", False)) != should_mute:
                    voice_output.toggle_mute()
                self.status("MUTE", "Muted" if should_mute else "Unmuted")
            except Exception as e:
                logger.warning("Failed to sync mute preference: %s", e)

        try:
            _voice_id, voice_name = voice_output.current_voice
            self.status("VOICE", voice_name)
        except Exception:
            pass

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
            # Online is proxied — the server controls the active model so
            # users can't cycle between them. Keeps upstream model swaps
            # invisible.
            if mode == "online":
                return
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

    # --- Slash command interception for chat input ---

    def _try_slash_command(self, text: str) -> bool:
        """Intercept slash commands typed in the chat input. Returns True if handled."""
        t = text.strip().lower()
        if t in ("/chance", "/winrate", "/odds"):
            threading.Thread(target=self._handle_win_probability, daemon=True).start()
            return True
        if t in ("/deck-strategy", "/deckstrategy", "/deck"):
            threading.Thread(target=self._handle_deck_strategy, daemon=True).start()
            return True
        if t.startswith("/bugreport"):
            msg = text.strip()[len("/bugreport"):].strip()
            threading.Thread(target=self._handle_bugreport, args=(msg,), daemon=True).start()
            return True
        if t == "/subscribe":
            threading.Thread(target=self._handle_subscribe, daemon=True).start()
            return True
        if t.startswith("/local"):
            self._handle_local_config(text.strip())
            return True
        if t == "/online":
            threading.Thread(target=self._handle_switch_online, daemon=True).start()
            return True
        if t.startswith("/key"):
            self._handle_set_key(text.strip())
            return True
        if t == "/analyze":
            threading.Thread(target=self._handle_analyze_match, daemon=True).start()
            return True
        return False

    # --- Handler implementations ---

    def _handle_win_probability(self) -> None:
        """Estimate win probability and display/speak it."""
        coach = self._coach
        if not coach or not coach._coach:
            self.log("Coach not available")
            return
        self.log("Evaluating win probability...")
        try:
            game_state = coach._mcp.get_game_state() if coach._mcp else {}
            coach._inject_library_summary_if_needed(game_state)
            opp_cards = getattr(coach, '_opponent_played_cards', None)
            if opp_cards is None:
                opp_cards = game_state.get("_match_context", {}).get("opponent_played_cards", [])
            result = coach._coach.generate_win_probability(game_state, opp_cards)
            if result:
                self.advice(result, "WIN PROBABILITY")
                coach.speak_advice(result, blocking=False)
            else:
                self.log("Could not estimate win probability.")
        except Exception as e:
            self.error(f"Win probability error: {e}")

    def _handle_deck_strategy(self) -> None:
        """Generate or recall deck strategy."""
        coach = self._coach
        if not coach:
            self.log("Coach not available")
            return
        existing = coach.get_deck_strategy()
        if existing:
            self.advice(existing, "DECK STRATEGY")
            coach.speak_advice(existing, blocking=False)
            return
        self.log("Generating deck strategy...")
        coach._generate_deck_strategy_brief()

    def _handle_bugreport(
        self,
        user_message: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        """Save and submit a bug report to GitHub."""
        coach = self._coach
        if not coach:
            self.error("Coach not available")
            return

        self.log("Preparing debug report...")

        # Auto-save a bug report
        report_path = None
        if hasattr(coach, 'save_bug_report'):
            report_path = coach.save_bug_report(
                f"/bugreport {user_message}".strip(),
                announce=False,
                progress_cb=self.log,
                extra_context=extra_context,
            )
        if report_path:
            self.log(f"Bug report saved: {report_path}")
        else:
            self.error("Failed to save bug report.")
            return

        from arenamcp.logging_config import LOG_DIR
        from arenamcp.bugreport import GITHUB_REPO, build_issue_payload, build_issue_url
        bug_dir = LOG_DIR / "bug_reports"
        if not bug_dir.exists():
            self.error("No bug reports found.")
            return

        reports = sorted(bug_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not reports:
            self.error("No bug reports found.")
            return

        report_path = reports[0]
        try:
            import json as _json
            report_data = _json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.error(f"Failed to read bug report: {e}")
            return

        title, body = build_issue_payload(report_data, report_path, user_message)

        try:
            import requests

            token = self._get_github_token()
            if token:
                self.log("Submitting bug report to GitHub API...")
                response = requests.post(
                    f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"title": title, "body": body, "labels": ["bug"]},
                    timeout=12,
                )
                if response.ok:
                    payload = response.json()
                    url = str(payload.get("html_url", "")).strip()
                    issue_number = payload.get("number")
                    self.log(
                        self._format_issue_success_message(
                            url=url,
                            issue_number=int(issue_number) if issue_number is not None else None,
                        )
                    )
                    if url and self._copy_to_clipboard(url):
                        self.log("GitHub issue URL copied to clipboard.")
                    return
                self.error(f"GitHub API issue creation failed: {response.status_code} {response.text[:240]}")
        except Exception as e:
            logger.warning("GitHub API submission failed: %s", e)

        import shutil
        gh = shutil.which("gh")
        if gh:
            try:
                self.log("GitHub API unavailable. Trying GitHub CLI...")
                result = subprocess.run(
                    [gh, "issue", "create", "--repo", GITHUB_REPO, "--title", title, "--body", body],
                    timeout=10,
                    **self._subprocess_kwargs_for_detached_io(capture_output=True),
                )
                if result.returncode == 0:
                    url = result.stdout.strip()
                    self.log(self._format_issue_success_message(url=url))
                    if url and self._copy_to_clipboard(url):
                        self.log("GitHub issue URL copied to clipboard.")
                    return
                stderr = (result.stderr or "").strip()
                logger.warning("gh issue create failed: %s", stderr)
                if stderr:
                    self.log(f"GitHub CLI issue creation failed: {stderr}")
            except Exception as e:
                logger.warning("gh issue submission failed: %s", e)
                self.log(f"GitHub CLI issue creation failed: {e}")

        issue_url = build_issue_url(title, body)
        self.log("Direct submission unavailable. Opening a prefilled GitHub issue in the browser...")
        if self._copy_to_clipboard(issue_url):
            self.log("Prefilled GitHub issue URL copied to clipboard.")
        try:
            opened = webbrowser.open(issue_url)
            if opened:
                self.log("Browser opened with prefilled issue draft.")
            else:
                self.log("Browser launch was not confirmed. Open the saved report and submit manually if needed.")
        except Exception as e:
            self.error(f"Bug report submission failed: {e}")

    def _handle_subscribe(self) -> None:
        """Show subscription status."""
        try:
            from arenamcp.settings import get_settings
            from arenamcp.subscription import check_subscription, SUBSCRIBE_URL

            license_key = get_settings().get("license_key", "")
            if not license_key:
                self.log(f"No license key configured. Visit {SUBSCRIBE_URL} to subscribe, "
                         "then use /key YOUR_LICENSE_KEY")
                return

            status = check_subscription(license_key, force=True)
            if status.is_valid:
                msg = f"Subscription active! Status: {status.status}"
                if status.expires_at:
                    msg += f", expires: {status.expires_at}"
                self.log(msg)
            else:
                self.log(f"Subscription issue: {status.message}. Visit {SUBSCRIBE_URL}")
        except Exception as e:
            self.error(f"Subscription check failed: {e}")

    def _handle_local_config(self, text: str) -> None:
        """Configure local model endpoint."""
        from arenamcp.settings import get_settings
        settings = get_settings()

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            url = settings.get("local_url", "http://localhost:11434/v1")
            model = settings.get("local_model", "auto-detect")
            api_key = settings.get("local_api_key", "ollama")
            self.log(f"Local config: URL={url}, Model={model or 'auto-detect'}, Key={api_key}")
            return

        arg_parts = arg.split(maxsplit=1)
        provider = arg_parts[0].lower()

        if provider == "ollama":
            settings.set("local_url", "http://localhost:11434/v1", save=False)
            settings.set("local_api_key", "ollama", save=False)
            settings.set("local_model", None, save=True)
            self.log("Local config set to Ollama (localhost:11434)")
        elif provider in ("lmstudio", "lm-studio", "lm_studio"):
            settings.set("local_url", "http://localhost:1234/v1", save=False)
            settings.set("local_api_key", "lm-studio", save=False)
            settings.set("local_model", None, save=True)
            self.log("Local config set to LM Studio (localhost:1234)")
        elif provider.startswith("http"):
            url = provider
            api_key = arg_parts[1].strip() if len(arg_parts) > 1 else "no-key"
            settings.set("local_url", url, save=False)
            settings.set("local_api_key", api_key, save=False)
            settings.set("local_model", None, save=True)
            self.log(f"Local config set to {url}")
        else:
            self.error(f"Unknown provider: {provider}. Use ollama, lmstudio, or a URL.")
            return

        # Switch to local mode
        coach = self._coach
        if coach:
            threading.Thread(target=self._handle_switch_local, daemon=True).start()

    def _handle_switch_online(self) -> None:
        """Switch to online mode with validation."""
        coach = self._coach
        if not coach:
            return
        try:
            from arenamcp.backend_detect import validate_backend
            from arenamcp.settings import get_settings

            key = get_settings().get("license_key", "")
            if not key:
                self.error("No license key configured. Use /key to set one.")
                return

            self.log("Connecting to Online...")
            ok, err = validate_backend("online")
            if not ok:
                self.error(f"Online unavailable: {err}")
                return

            coach.set_backend("online", None)
            self.status("BACKEND", f"{coach.backend_name} ({coach.model_name or 'default'})")
            self.status("MODEL", coach.model_name or "default")
            self.log(f"Switched to {coach.backend_name}/{coach.model_name or 'default'}")
        except Exception as e:
            self.error(f"Online switch failed: {e}")

    def _handle_switch_local(self) -> None:
        """Switch to local mode with validation."""
        coach = self._coach
        if not coach:
            return
        try:
            from arenamcp.backend_detect import validate_backend

            self.log("Connecting to Local...")
            ok, err = validate_backend("local")
            if not ok:
                self.error(f"Local unavailable: {err}")
                return

            coach.set_backend("local", None)
            self.status("BACKEND", f"{coach.backend_name} ({coach.model_name or 'default'})")
            self.status("MODEL", coach.model_name or "default")
            self.log(f"Switched to {coach.backend_name}/{coach.model_name or 'default'}")
        except Exception as e:
            self.error(f"Local switch failed: {e}")

    def _handle_set_key(self, text: str) -> None:
        """Set license key."""
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            self.log("Usage: /key YOUR_LICENSE_KEY")
            return

        key = parts[1].strip()
        from arenamcp.settings import get_settings
        get_settings().set("license_key", key)
        self.log(f"License key set: {key[:8]}...")

    def _handle_analyze_match(self) -> None:
        """Run post-match analysis on current game state."""
        coach = self._coach
        if not coach or not coach._coach:
            self.log("Coach not available")
            return
        try:
            coach.trigger_match_analysis()
        except Exception as e:
            self.error(f"Match analysis failed: {e}")
