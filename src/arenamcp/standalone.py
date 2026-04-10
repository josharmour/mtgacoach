"""Standalone MTGA Coach - Lightweight MCP client with voice I/O.

This app runs the MCP server and connects to it as an MCP client,
using an LLM via mtgacoach.com (online) or a local model (Ollama/LM Studio)
for coaching advice with voice support.

Usage:
    python -m arenamcp.standalone --backend online
    python -m arenamcp.standalone --backend local
    python -m arenamcp.standalone --draft --set MH3

The MCP server handles all game state tracking; this client just:
- Polls MCP tools for state changes
- Passes state to local LLM for advice
- Handles voice I/O (PTT/VOX input, TTS output)
"""

# Load .env before other imports
def _load_dotenv():
    """Load environment variables from .env file if it exists."""
    import os
    from pathlib import Path
    for env_path in [Path(".env"), Path(__file__).parent.parent.parent / ".env"]:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
            break

_load_dotenv()

import argparse
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from arenamcp.settings import get_settings
from arenamcp.logging_config import configure_logging, LOG_DIR, LOG_FILE

# Configure logging (shared with server.py via logging_config)
# Console handler disabled -- TUI handles user-facing output.
configure_logging(console=False)

WATCHDOG_SCREENSHOT_DIR = LOG_DIR / "watchdog_screenshots"
WATCHDOG_SCREENSHOT_DIR.mkdir(exist_ok=True)
WATCHDOG_SCREENSHOT_MAX = 20  # Keep last N screenshots (pruned at match end)

logger = logging.getLogger(__name__)


class _SAPIVoice:
    """Lightweight Windows SAPI TTS — no numpy, no sounddevice, no PortAudio.

    Uses PowerShell's System.Speech.Synthesis for speech output.
    Drop-in replacement for VoiceOutput in pipe mode.
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._muted = False

    @property
    def current_voice(self) -> tuple[str, str]:
        return ("sapi", "Windows SAPI")

    def speak(self, text: str, blocking: bool = True) -> None:
        if self._muted or not text or not text.strip():
            return
        # Clean markup
        import re
        text = text.replace("**", "").replace("*", "").replace("#", "")
        text = text.replace("```", "").replace("`", "").replace("...", " ")
        text = re.sub(r"\[[A-Z][A-Za-z0-9_,:{}/ ]*\]", "", text)
        # Escape for PowerShell
        safe = text.replace("'", "''").replace('"', '\\"')
        cmd = (
            'Add-Type -AssemblyName System.Speech; '
            '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
            f"$s.Speak('{safe}')"
        )
        try:
            self.stop()
            self._proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", cmd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            if blocking:
                self._proc.wait(timeout=30)
        except Exception:
            pass

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except Exception:
                pass
        self._proc = None

    @property
    def is_speaking(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def next_voice(self):
        pass  # SAPI uses system default


def _probe_sounddevice_import(timeout_seconds: float = 8.0) -> tuple[bool, str]:
    """Probe sounddevice import in a subprocess.

    Importing sounddevice can block inside PortAudio initialization when an audio
    driver is misbehaving. Probing in a subprocess keeps the main process safe.
    stdin=DEVNULL prevents inheriting the parent's pipe when running under a GUI.
    """
    cmd = [sys.executable, "-c", "import sounddevice"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {int(timeout_seconds)}s"
    except Exception as e:
        return False, str(e)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            # Keep only the final line to avoid flooding logs/UI.
            detail = detail.splitlines()[-1]
        else:
            detail = f"exit code {result.returncode}"
        return False, detail

    return True, "ok"


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard.

    Tries pyperclip first, falls back to Windows clip command.
    Returns True if successful, False otherwise.
    """
    # Try pyperclip first (if installed)
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"pyperclip failed: {e}")

    # Fallback: Windows clip command
    try:
        process = subprocess.Popen(
            ['clip'],
            stdin=subprocess.PIPE,
            shell=True
        )
        process.communicate(input=text.encode('utf-8'), timeout=2)
        return process.returncode == 0
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception as e:
            logger.debug(f"Failed to kill timed-out clip process: {e}")
        logger.debug("clip command timed out")
        return False
    except Exception as e:
        logger.debug(f"clip command failed: {e}")
        return False


# Import dependencies
try:
    import keyboard
except ImportError:
    keyboard = None
    logger.warning("keyboard module not available - hotkeys disabled")


class UIAdapter:
    """Interface for UI feedback (CLI or TUI)."""
    def log(self, message: str) -> None: pass
    def advice(self, text: str, seat_info: str) -> None: pass
    def status(self, key: str, value: str) -> None: pass
    def error(self, message: str) -> None: pass
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None: pass

class CLIAdapter(UIAdapter):
    """Default adapter for CLI output."""
    def log(self, message: str) -> None:
        print(message)
    def advice(self, text: str, seat_info: str) -> None:
        print(f"\n[COACH|{seat_info}] {text}\n")
    def status(self, key: str, value: str) -> None:
        print(f"[{key}] {value}")
    def error(self, message: str) -> None:
        print(f"ERROR: {message}")
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None:
        print(f"  ⟳ {status}", end="\r")

class MCPClient:
    """Simple in-process MCP client that calls server tools directly.

    Since the MCP server runs in-process, we import and call tools directly
    rather than going through STDIO transport.
    """

    def __init__(self):
        """Initialize MCP client by importing server module."""
        # Import server module - this starts the log watcher
        from arenamcp import server
        self._server = server

        # Ensure watcher is running
        server.start_watching()
        logger.info("MCP server initialized")

    def get_game_state(self) -> dict[str, Any]:
        """Call get_game_state MCP tool."""
        return self._server.get_game_state()

    def clear_pending_combat_steps(self) -> None:
        """Clear pending combat steps after trigger processing."""
        self._server.clear_pending_combat_steps()

    def poll_log(self) -> None:
        """Manually poll for new log content (backup for missed watchdog events)."""
        self._server.poll_log()

    def get_draft_pack(self) -> dict[str, Any]:
        """Call get_draft_pack MCP tool."""
        return self._server.get_draft_pack()

    def get_draft_picked_ids(self) -> list[int]:
        """Return raw grpIds of cards picked during the current draft."""
        return list(self._server.draft_state.picked_cards)

    def get_card_info(self, arena_id: int) -> dict[str, Any]:
        """Call get_card_info MCP tool."""
        return self._server.get_card_info(arena_id)

    def start_draft_helper(self, set_code: Optional[str] = None) -> dict[str, Any]:
        """Start the built-in draft helper."""
        return self._server.start_draft_helper_tool(set_code)

    def stop_draft_helper(self) -> dict[str, Any]:
        """Stop the draft helper."""
        return self._server.stop_draft_helper_tool()

    def get_draft_helper_status(self) -> dict[str, Any]:
        """Get draft helper status."""
        return self._server.get_draft_helper_status()

    def evaluate_draft_pack(self) -> dict[str, Any]:
        """Evaluate draft pack with composite scoring (colors, synergy, WR)."""
        return self._server.evaluate_draft_pack_for_standalone()

    def get_sealed_pool(self) -> dict[str, Any]:
        """Get sealed pool analysis."""
        return self._server.get_sealed_pool()

    def analyze_draft_pool(self) -> dict[str, Any]:
        """Analyze drafted cards for deck building."""
        return self._server.analyze_draft_pool()


class ConsoleAdapter(UIAdapter):
    """Fallback for CLI mode."""
    def log(self, message: str) -> None: print(message, end='')
    def advice(self, text: str, seat_info: str) -> None: print(f"\n[COACH|{seat_info}] {text}\n")
    def status(self, key: str, value: str) -> None: pass
    def error(self, message: str) -> None: print(f"ERROR: {message}")
    def speak(self, text: str) -> None: pass
    def subtask(self, status: str) -> None: pass


class _TempoTracker:
    """Tracks game state progression cadence for anomaly detection.

    During normal play, game state transitions happen every 0.3-0.5s
    (auto-pass, resolves, opponent actions). A stall >1s without any
    state change likely means the game is waiting for player input
    that the log parser didn't capture as a decision.
    """

    def __init__(self, stall_threshold: float = 1.5, min_samples: int = 5):
        self._stall_threshold = stall_threshold
        self._min_samples = min_samples
        self._last_state_hash: Optional[str] = None
        self._last_change_time: float = 0.0
        self._intervals: list[float] = []  # Recent inter-change intervals
        self._max_intervals = 30

    def update(self, game_state: dict) -> bool:
        """Feed a new game state snapshot.  Returns True if a stall is detected.

        A stall is when:
          - We have enough baseline samples (min_samples)
          - Time since last state change exceeds stall_threshold
          - The game is active (turn > 0)
        """
        now = time.time()

        # Cheap hash of the fields that change on every GRE update
        turn = game_state.get("turn", {})
        sig = (
            turn.get("turn_number", 0),
            turn.get("phase", ""),
            turn.get("step", ""),
            turn.get("priority_player", 0),
            game_state.get("pending_decision"),
            len(game_state.get("hand", [])),
            len(game_state.get("battlefield", [])),
            len(game_state.get("stack", [])),
        )
        state_hash = str(sig)

        if state_hash != self._last_state_hash:
            # State changed — record interval
            if self._last_change_time > 0:
                interval = now - self._last_change_time
                self._intervals.append(interval)
                if len(self._intervals) > self._max_intervals:
                    self._intervals.pop(0)
            self._last_state_hash = state_hash
            self._last_change_time = now
            return False

        # State hasn't changed — check for stall
        if self._last_change_time <= 0:
            self._last_change_time = now
            return False

        turn_num = turn.get("turn_number", 0)
        if turn_num == 0:
            return False  # Game not active

        elapsed = now - self._last_change_time
        if len(self._intervals) >= self._min_samples and elapsed > self._stall_threshold:
            return True

        return False

    @property
    def avg_interval(self) -> float:
        """Average seconds between state changes."""
        if not self._intervals:
            return 0.0
        return sum(self._intervals) / len(self._intervals)

    @property
    def stall_duration(self) -> float:
        """How long the current stall has lasted."""
        if self._last_change_time <= 0:
            return 0.0
        return time.time() - self._last_change_time

    def reset(self) -> None:
        """Reset tracker for a new match."""
        self._last_state_hash = None
        self._last_change_time = 0.0
        self._intervals.clear()


class StandaloneCoach:
    """Standalone coaching app using MCP client + local LLM."""

    def __init__(
        self,
        backend: str = "proxy",
        model: Optional[str] = None,
        voice_mode: str = "ptt",
        draft_mode: bool = False,
        set_code: Optional[str] = None,
        ui_adapter: Optional[UIAdapter] = None,
        register_hotkeys: bool = True,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
    ):
        self._register_keyboard = register_hotkeys

        # Load settings
        self.settings = get_settings()

        # Resolve configuration (Args > Settings > Defaults)
        self._backend_name = backend or self.settings.get("mode", "auto")
        self._voice_mode = voice_mode or self.settings.get("voice_mode", "ptt")

        # Model resolution: only carry over saved model if the mode matches.
        if model:
            self._model_name = model
        else:
            saved_model = self.settings.get("model")
            saved_mode = self.settings.get("mode", "auto")
            if saved_model and saved_mode == self._backend_name:
                self._model_name = saved_model
            else:
                self._model_name = None

        self.draft_mode = draft_mode
        self.set_code = set_code.upper() if set_code else None

        # Autopilot
        self._autopilot_enabled = autopilot
        self._autopilot_dry_run = dry_run
        self._autopilot_afk = afk
        self._autopilot: Optional[Any] = None  # AutopilotEngine instance
        self._autopilot_backend: Optional[Any] = None  # Separate LLM backend for autopilot

        # State
        self.advice_style = "concise"
        self._advice_frequency = self.settings.get("advice_frequency", "start_of_turn")

        # TTS always enabled
        self._auto_speak = True
        self._screenshot_analysis_in_progress = False

        self.ui = ui_adapter or CLIAdapter()

        # Save validated configuration back to settings (ensure consistency)
        self.settings.set("mode", self._backend_name, save=False)
        self.settings.set("model", self._model_name, save=False)
        self.settings.set("voice_mode", self._voice_mode, save=False)
        self.settings.set("advice_frequency", self._advice_frequency, save=True)

        self._start_time = datetime.now()
        self._running = False
        self._restart_requested = False
        self._deck_analyzed = False
        self._mcp: Optional[MCPClient] = None

        # Voice components
        self._voice_input = None
        self._voice_output = None

        # LLM backend
        self._coach = None
        self._trigger = None

        # Threads
        self._coaching_thread: Optional[threading.Thread] = None
        self._voice_thread: Optional[threading.Thread] = None

        # Background win plan
        self._win_plan_turn = 0       # Last turn a win plan was launched
        self._thinking_model = None   # Cached thinking model ID (lazy-init)
        self._pending_win_plan: Optional[str] = None    # Stored viable plan text
        self._pending_win_plan_turns: int = 0            # N in "win-in-N"
        self._pending_win_plan_turn: int = 0             # Game turn when plan was generated

        # Match tracking for LLM context
        self._match_number: int = 0  # Incremented on each new match

        # Rolling in-match advice history (used for post-match analysis)
        self._advice_history: list[dict] = []

        # Post-match analysis
        self._saved_advice_history: list[dict] = []
        self._saved_missed_decisions: list[dict] = []
        self._last_match_result: Optional[str] = None
        self._last_match_final_state: Optional[dict] = None
        self._game_end_handled: bool = False  # Prevents duplicate triggers
        self._match_boundary_ts: float = 0.0  # Suppress stale triggers after reset
        self._last_game_end_check_error: str = ""
        self._pending_post_match_analysis: Optional[str] = None  # For GH issue filing via F7
        self._pending_post_match_result: Optional[str] = None

        # Vision watchdog: tempo anomaly detection + missed decision tracking
        self._tempo_tracker = _TempoTracker()
        self._missed_decisions: list[dict] = []  # Accumulated per match
        self._vision_mapper: Optional[Any] = None  # VisionMapper (shared with autopilot)
        self._vlm_card_cache: dict[int, str] = {}  # grpId -> resolved name (persists per match)
        self._vlm_card_failures: set[int] = set()  # grpIds we already tried and failed
        self._recent_gre_log: list[str] = []  # Ring buffer of recent GRE/decision log lines
        self._recent_gre_log_max = 30

        # Backend health status (deduped to avoid noisy UI writes)
        self._last_backend_status: str = ""
        self._last_backend_error: str = ""

        # Backend failure fallback state: when a backend fails with
        # auth/billing errors, we temporarily switch ALL calls to Ollama
        # and show a persistent error.  Cleared when user changes provider.
        self._backend_failed: bool = False
        self._original_backend: Optional[str] = None
        self._original_model: Optional[str] = None

        # Autopilot decision backstop: force decision triggers when parser noise
        # causes missed trigger edges after an executed action.
        self._last_forced_decision_sig: Optional[str] = None
        self._last_forced_decision_ts: float = 0.0

        # Bridge decision poller: proactive decision detection via BepInEx plugin
        from arenamcp.gre_bridge import get_poller
        self._bridge_poller = get_poller()

    def _set_backend_status(self, status: str) -> None:
        """Update backend status in UI only when the value actually changes."""
        if status == self._last_backend_status:
            return
        self._last_backend_status = status
        try:
            self.ui.status("BACKEND", status)
        except Exception as e:
            logger.debug(f"UI status update failed: {e}")

    def _report_backend_failure(self, detail: str) -> None:
        """Surface backend failures in UI/logs with deduping."""
        self._set_backend_status(f"ERROR ({self.backend_name})")
        short = (detail or "backend failure").strip().replace("\n", " ")[:180]
        if short and short != self._last_backend_error:
            self._last_backend_error = short
            try:
                self.ui.log(f"\n[BACKEND] {short}\n")
            except Exception as e:
                logger.debug(f"UI log update failed: {e}")

    def _mark_backend_healthy(self) -> None:
        """Clear backend failure status after successful responses."""
        self._last_backend_error = ""
        self._set_backend_status(f"OK ({self.backend_name})")

    @staticmethod
    def _get_local_seat_from_state(game_state: dict[str, Any]) -> Optional[int]:
        """Return the local seat id from a serialized snapshot, if known."""
        for player in game_state.get("players", []):
            if player.get("is_local"):
                return player.get("seat_id")
        return None

    @classmethod
    def _normalize_turn_snapshot(cls, game_state: dict[str, Any]) -> dict[str, Any]:
        """Repair stale turn ownership in a local snapshot using strong signals."""
        turn = game_state.get("turn")
        if not isinstance(turn, dict):
            return game_state

        # When the bridge is connected, the turn payload already comes from
        # MtgGameState and priority is sourced from deciding_player.
        # Do not overwrite that authoritative engine state with heuristics.
        if game_state.get("_bridge_connected") or game_state.get("bridge_connected"):
            return game_state

        local_seat = cls._get_local_seat_from_state(game_state)
        if local_seat is None:
            return game_state

        opponent_seat = next(
            (
                player.get("seat_id")
                for player in game_state.get("players", [])
                if player.get("seat_id") != local_seat
            ),
            None,
        )

        decision_type = ((game_state.get("decision_context") or {}).get("type") or "").lower()
        raw_actions = game_state.get("legal_actions_raw") or []
        action_types = {
            action.get("actionType")
            for action in raw_actions
            if isinstance(action, dict) and action.get("actionType")
        }
        phase = turn.get("phase", "")
        stack = game_state.get("stack", []) or []

        inferred_active = None
        if decision_type == "declare_attackers" or action_types & {"ActionType_Attack", "ActionType_AttackWithGroup"}:
            inferred_active = local_seat
        elif (
            decision_type == "declare_blockers"
            or action_types & {"ActionType_Block", "ActionType_BlockWithGroup"}
        ):
            inferred_active = opponent_seat
        elif (
            action_types & {"ActionType_Play", "ActionType_PlayMDFC"}
            and "Main" in phase
            and not stack
        ):
            inferred_active = local_seat

        if inferred_active is not None and turn.get("active_player") != inferred_active:
            logger.debug(
                "Normalized active_player from %s to %s using decision/actions state",
                turn.get("active_player"),
                inferred_active,
            )
            turn["active_player"] = inferred_active

        if inferred_active is not None or decision_type in {
            "actions_available",
            "declare_attackers",
            "declare_blockers",
        }:
            if turn.get("priority_player") != local_seat:
                logger.debug(
                    "Normalized priority_player from %s to %s using decision/actions state",
                    turn.get("priority_player"),
                    local_seat,
                )
                turn["priority_player"] = local_seat

        return game_state

    @classmethod
    def _has_meaningful_local_action_window(cls, game_state: dict[str, Any]) -> bool:
        """Return True when the local player still has a fresh actionable window."""
        turn = game_state.get("turn", {})
        local_seat = cls._get_local_seat_from_state(game_state)
        if local_seat is None:
            return False
        if turn.get("active_player") != local_seat or turn.get("priority_player") != local_seat:
            return False

        if game_state.get("pending_decision"):
            return True

        legal_actions = game_state.get("legal_actions", []) or []
        meaningful_prefixes = ("Cast ", "Play ", "Activate Ability", "Action: Activate", "Action: Attack", "Action: Block")
        return any(action.startswith(meaningful_prefixes) for action in legal_actions)

    @staticmethod
    def _is_garbled(text: str, threshold: float = 0.4) -> bool:
        """Detect garbled VLM output (e.g. non-vision model processing image tokens).

        Returns True if the text has an abnormally high ratio of punctuation
        and special characters relative to alphanumeric + space content.
        """
        if not text or len(text) < 20:
            return False
        alnum_space = sum(1 for c in text if c.isalnum() or c.isspace())
        ratio = alnum_space / len(text)
        return ratio < threshold

    def speak_advice(self, text: str, blocking: bool = True) -> None:
        """Speak advice using local Kokoro TTS."""
        if not text:
            return

        # Filter out passive calls from TTS (User Request)
        # We silence: Wait, Pass, No actions
        clean_text = text.lower().strip(" .!")
        silence_triggers = [
            "wait",
            "pass",
            "pass priority",
            "no actions",
            "wait for opponent",
            "opponent has priority"
        ]

        # Check if text starts with or is substantially just these phrases
        # We use a simple heuristic: if it contains no active verbs (Cast, Attack, Block, Play),
        # and matches a silence trigger, we skip it.
        is_passive = any(trigger in clean_text for trigger in silence_triggers)
        has_action = any(verb in clean_text for verb in ["cast", "play", "attack", "block", "activate", "kill", "destroy"])

        if is_passive and not has_action and len(text) < 60:
            return

        # Use local Kokoro TTS
        if self._voice_output:
            try:
                self._voice_output.speak(text, blocking=blocking)
            except Exception as e:
                logger.error(f"Kokoro TTS error: {e}")

    @property
    def backend_name(self) -> str:
        return self._backend_name
    
    @backend_name.setter
    def backend_name(self, value: str):
        self._backend_name = value
        self.settings.set("backend", value)
        
    @property
    def model_name(self) -> Optional[str]:
        return self._model_name
    
    @model_name.setter
    def model_name(self, value: Optional[str]):
        self._model_name = value
        if value:
            self.settings.set("model", value)
            
    @property
    def voice_mode(self) -> str:
        return self._voice_mode
    
    @voice_mode.setter
    def voice_mode(self, value: str):
        self._voice_mode = value
        self.settings.set("voice_mode", value)
        if hasattr(self, "_voice_input") and self._voice_input:
            # Propagate to input handler if running
            # (Note: VoiceInput might need restart to change mode fully, preventing hot-swap here)
            pass

    @property
    def advice_frequency(self) -> str:
        return self._advice_frequency

    @advice_frequency.setter
    def advice_frequency(self, value: str):
        self._advice_frequency = value
        self.settings.set("advice_frequency", value)

    def _generate_deck_strategy_brief(self, card_ids: Optional[list[int]] = None) -> None:
        """Generate and speak a brief deck strategy.

        Runs in a background thread so it doesn't block the coaching loop.
        Works for any game mode — draft, sealed, or constructed.

        Args:
            card_ids: Optional pre-captured list of grpIds. If not provided,
                      uses deck_cards from the current game state (library).
        """
        if not self._coach or not self._mcp:
            return

        # Capture the list now so the background thread has it
        pre_captured = list(card_ids) if card_ids else None

        def _run():
            try:
                deck_grp_ids = pre_captured or []

                # Fallback: use current game's deck (library), or reconstruct
                # from visible zones if ConnectResp was missed
                if not deck_grp_ids:
                    try:
                        gs = self._mcp.get_game_state()
                        deck_grp_ids = gs.get("deck_cards", [])
                        if not deck_grp_ids:
                            local_seat = self._get_local_seat_from_state(gs)
                            if local_seat is not None:
                                seen = set()
                                for zone in ("hand", "battlefield", "graveyard", "exile", "command"):
                                    for card in gs.get(zone, []):
                                        if card.get("owner_seat_id") == local_seat:
                                            gid = card.get("grp_id", 0)
                                            if gid and gid not in seen:
                                                seen.add(gid)
                                                deck_grp_ids.append(gid)
                    except Exception:
                        pass

                if not deck_grp_ids:
                    self.ui.log("[yellow]No deck available yet. Start a game first.[/]")
                    logger.info("No deck cards available for strategy brief")
                    return

                enriched = []
                for grp_id in deck_grp_ids:
                    try:
                        info = self._mcp.get_card_info(grp_id)
                        enriched.append((
                            info.get("name", f"Unknown({grp_id})"),
                            info.get("type_line", ""),
                            info.get("oracle_text", ""),
                        ))
                    except Exception:
                        enriched.append((f"Unknown({grp_id})", "", ""))

                from arenamcp.coach import create_backend
                brief_backend = create_backend(self._backend_name, model=self.model_name)
                strategy = self._coach.get_deck_strategy_brief(enriched, backend=brief_backend)
                if hasattr(brief_backend, "close"):
                    brief_backend.close()

                if strategy:
                    # Also store as the deck strategy so /deck-strategy can recall it
                    self._coach._deck_strategy = strategy
                    self.ui.log(f"\n[bold green]DECK STRATEGY:[/] {strategy}\n")
                    self.speak_advice(strategy)
            except Exception as e:
                logger.error(f"Deck strategy brief failed: {e}")

        threading.Thread(target=_run, daemon=True, name="deck-strategy-brief").start()

    def get_deck_strategy(self) -> Optional[str]:
        """Return the stored deck strategy, or None if not yet analyzed."""
        if self._coach:
            return self._coach._deck_strategy
        return None

    def _init_mcp(self) -> None:
        """Initialize MCP client connection."""
        logger.info("Initializing MCP server...")
        self._mcp = MCPClient()

        # Warm local databases in the background so the TUI becomes usable
        # immediately. Scryfall stays fully lazy to avoid startup downloads.
        if not getattr(self, "_card_cache_warm_started", False):
            self._card_cache_warm_started = True
            threading.Thread(
                target=self._warm_local_card_caches,
                daemon=True,
                name="card-cache-warm",
            ).start()

    def _warm_local_card_caches(self) -> None:
        """Warm local card data sources without blocking startup."""
        logger.info("Warming local card databases in background...")
        try:
            from arenamcp.card_db import get_card_database

            get_card_database()
            logger.info("Local card database warmup complete")
        except Exception as e:
            logger.warning(f"Failed to warm local card databases: {e}")

    def _init_llm(self) -> None:
        """Initialize LLM backend for coaching."""
        if self.draft_mode:
            return  # Draft mode uses MCP's built-in draft helper

        from arenamcp.coach import CoachEngine, GameStateTrigger, create_backend

        # Pass UI subtask callback for real-time progress display
        progress_cb = self.ui.subtask if self.ui else None

        # "auto" mode: detect and report which backend was selected
        requested = self.backend_name
        llm_backend = create_backend(self.backend_name, model=self.model_name, progress_callback=progress_cb)
        actual_model = getattr(llm_backend, 'model', 'unknown')

        # If auto-selected, update our backend_name to reflect the actual choice
        if requested == "auto":
            from arenamcp.backend_detect import auto_select_mode
            resolved_mode, _ = auto_select_mode()
            self._backend_name = resolved_mode
            self.settings.set("mode", "auto", save=False)  # Keep "auto" in settings
            self.ui.log(f"[bold green]Auto-detected mode: {resolved_mode} (model: {actual_model})[/]")

        logger.info(f"Created {self.backend_name} backend with model: {actual_model}")
        self._coach = CoachEngine(backend=llm_backend)
        # Log full backend diagnostics at startup
        backend_info = self._coach.get_backend_info()
        logger.info(f"[BACKEND-DIAG] {backend_info}")
        self.ui.log(f"  Backend: {backend_info['backend_name']} | Model: {backend_info['model']}")
        self._trigger = GameStateTrigger()

        # Track consecutive failures for automatic fallback
        self._consecutive_errors = 0
        self._max_errors_before_fallback = 3

    def _init_vision_mapper(self) -> None:
        """Initialize VisionMapper for vision watchdog and autopilot.

        Sets self._vision_mapper if Ollama VLM is available.
        Called regardless of autopilot mode so coaching-only users
        still get missed-decision detection.
        """
        try:
            from arenamcp.vision_mapper import VisionMapper
            backend = self._coach._backend if self._coach else None
            mapper = VisionMapper(
                ollama_model="qwen2.5-vl:3b",
                enable_local_vlm=True,
                enable_cloud_vlm=True,
            )
            if backend:
                mapper.set_cloud_backend(backend)
            self._vision_mapper = mapper
            self.ui.log("[bold cyan]VisionMapper enabled (Ollama + cache)[/]")
        except Exception as e:
            logger.info(f"VisionMapper unavailable: {e}")
            self.ui.log(f"[yellow]VisionMapper unavailable ({e}) — vision watchdog disabled[/]")

    def _resolve_unknown_cards(self, game_state: dict) -> None:
        """Use VLM to identify unknown cards in the game state.

        Scans all zones for cards with names like "Unknown (ID: 123)" or
        "Card#123" and asks the VLM to read the card name from the screen.
        Results are cached per grpId so we only try once per unknown card.
        """
        if not self._vision_mapper:
            return

        # Collect unknown cards across all zones
        import re
        unknown_pattern = re.compile(r'Unknown|Card#\d+')
        zones_to_check: dict[str, list[dict]] = {}  # zone_name -> [card_dicts]

        for zone_name in ("hand", "battlefield", "stack", "graveyard", "exile"):
            cards = game_state.get(zone_name, [])
            for card in cards:
                if not isinstance(card, dict):
                    continue
                name = card.get("name", "")
                grp_id = card.get("grp_id", 0)
                if not grp_id or grp_id in self._vlm_card_cache or grp_id in self._vlm_card_failures:
                    continue
                if unknown_pattern.search(name):
                    zones_to_check.setdefault(zone_name, []).append(card)

        if not zones_to_check:
            return

        total = sum(len(v) for v in zones_to_check.values())
        logger.info(f"Found {total} unknown card(s) — attempting VLM identification")

        # Take a screenshot
        try:
            mapper = self._vision_mapper
            window_rect = mapper.window_rect
            if not window_rect:
                window_rect = mapper.refresh_window()
            if not window_rect:
                return

            from PIL import ImageGrab
            import io as _io
            left, top, width, height = window_rect
            screenshot = ImageGrab.grab(bbox=(left, top, left + width, top + height))
            buf = _io.BytesIO()
            screenshot.save(buf, format='PNG')
            png_bytes = buf.getvalue()
        except Exception as e:
            logger.debug(f"Screenshot for card identification failed: {e}")
            return

        # Ask VLM per zone (batch unknown cards by zone)
        for zone_name, cards in zones_to_check.items():
            grp_ids = [c.get("grp_id") for c in cards]
            hint = f"{len(cards)} unknown card(s), grpIds: {grp_ids}"
            try:
                identified = mapper.identify_unknown_cards(png_bytes, zone_name, hint)
            except Exception as e:
                logger.debug(f"VLM card identification failed for {zone_name}: {e}")
                for c in cards:
                    self._vlm_card_failures.add(c.get("grp_id", 0))
                continue

            if not identified:
                for c in cards:
                    self._vlm_card_failures.add(c.get("grp_id", 0))
                continue

            # Match identified names back to unknown cards (best effort: order-based)
            for i, card in enumerate(cards):
                grp_id = card.get("grp_id", 0)
                if i < len(identified):
                    resolved_name = identified[i].get("name", "")
                    conf = identified[i].get("confidence", 0)
                    if resolved_name:
                        self._vlm_card_cache[grp_id] = resolved_name
                        # Patch the card in-place in game state
                        card["name"] = f"{resolved_name} (vision)"
                        self.ui.log(
                            f"[dim cyan]Vision ID: {resolved_name} "
                            f"(grpId={grp_id}, {zone_name}, conf={conf:.0%})[/]"
                        )
                        # Also try to enrich via Scryfall now that we have a name
                        self._enrich_vlm_resolved_card(card, resolved_name)
                        continue
                self._vlm_card_failures.add(grp_id)

    @staticmethod
    def _enrich_vlm_resolved_card(card: dict, name: str) -> None:
        """Try to fill oracle_text using the unified card database."""
        try:
            from arenamcp.card_db import get_card_database
            card_db = get_card_database()
            result = card_db.get_card_by_name(name)
            if result:
                card["oracle_text"] = result.oracle_text or card.get("oracle_text", "")
                card["type_line"] = result.type_line or card.get("type_line", "")
                card["mana_cost"] = result.mana_cost or card.get("mana_cost", "")
                card["name"] = f"{result.name} (vision)"  # Use canonical name
        except Exception as e:
            logger.debug(f"Card enrichment failed for '{name}' (best effort): {e}")

    def _init_autopilot(self) -> None:
        """Initialize autopilot components (requires LLM backend + MCP)."""
        try:
            from arenamcp.autopilot import AutopilotEngine, AutopilotConfig
            from arenamcp.action_planner import ActionPlanner
            from arenamcp.input_controller import InputController
            from arenamcp.coach import create_backend

            if not self._coach:
                self.ui.log("[red]Autopilot: no LLM backend available[/]")
                return

            # Create a SEPARATE backend instance for autopilot so it has its
            # own subprocess/connection and lock — eliminates lock contention
            # with the coaching backend.
            autopilot_backend = create_backend(self._backend_name, model=self._model_name)
            self._autopilot_backend = autopilot_backend

            config = AutopilotConfig(
                dry_run=self._autopilot_dry_run,
                afk_mode=self._autopilot_afk,
                enable_tts_preview=True,
            )

            planner = ActionPlanner(autopilot_backend, timeout=config.planning_timeout)

            # Reuse shared VisionMapper if available, otherwise fall back to static coords
            if self._vision_mapper:
                mapper = self._vision_mapper
            else:
                from arenamcp.screen_mapper import ScreenMapper
                mapper = ScreenMapper()
                self.ui.log(f"[yellow]Autopilot: using static coords (VisionMapper not available)[/]")

            controller = InputController(dry_run=self._autopilot_dry_run)

            self._autopilot = AutopilotEngine(
                planner=planner,
                mapper=mapper,
                controller=controller,
                get_game_state=self._mcp.get_game_state,
                config=config,
                speak_fn=self.speak_advice,
                ui_advice_fn=self.ui.advice if self.ui else None,
            )

            mode = "DRY-RUN" if self._autopilot_dry_run else "LIVE"
            afk = " (AFK)" if self._autopilot_afk else ""
            self.ui.log(f"[bold green]Autopilot initialized: {mode}{afk}[/]")
            logger.info(f"Autopilot initialized: {mode}{afk}")
        except ImportError as e:
            self.ui.log(f"[red]Autopilot unavailable (missing deps): {e}[/]")
            self._autopilot_enabled = False
        except Exception as e:
            self.ui.log(f"[red]Autopilot init failed: {e}[/]")
            logger.error(f"Autopilot init failed: {e}", exc_info=True)
            self._autopilot_enabled = False

    def toggle_autopilot(self) -> bool:
        """Toggle autopilot on/off at runtime. Returns new enabled state."""
        if self._autopilot_enabled and self._autopilot:
            # Turn OFF: abort any in-flight plan, disable
            self._autopilot.on_abort()
            self._autopilot_enabled = False
            # Clean up the separate autopilot backend
            ap_backend = getattr(self, '_autopilot_backend', None)
            if ap_backend:
                if hasattr(ap_backend, 'close'):
                    try:
                        ap_backend.close()
                    except Exception as e:
                        logger.debug(f"Autopilot backend close error: {e}")
                self._autopilot_backend = None
            logger.info("Autopilot toggled OFF")
            return False
        else:
            # Turn ON: initialize if needed, then enable
            if not self._autopilot:
                self._autopilot_dry_run = False
                self._autopilot_afk = False
                self._init_autopilot()
            if self._autopilot:
                # Reset lock in case previous session left it stuck
                if self._autopilot._lock.locked():
                    logger.warning("Autopilot: releasing stuck lock from previous session")
                    try:
                        self._autopilot._lock.release()
                    except RuntimeError:
                        pass  # Lock wasn't held by this thread
                # Clear abort/skip/confirm events from previous session —
                # on_abort() sets _abort_event which persists across toggles
                # and causes process_trigger() to bail out immediately.
                self._autopilot._clear_events()
                self._autopilot_enabled = True
                logger.info("Autopilot toggled ON")
                return True
            else:
                logger.warning("Autopilot toggle failed: init unsuccessful")
                return False

    def _init_voice(self) -> None:
        """Initialize voice I/O components.

        Uses a subprocess probe to detect when sounddevice/PortAudio hangs
        during audio device enumeration (e.g. problematic ASIO/virtual drivers).
        """
        logger.info(f"_init_voice called, backend_name={self.backend_name}")

        # In pipe mode (native GUI), try Kokoro directly with a hard timeout.
        # If it hangs (numpy/PortAudio DLL issues), fall back to Windows SAPI.
        if hasattr(self.ui, 'emit_game_state'):
            self.ui.log("Initializing TTS...")
            kokoro_ok = False
            kokoro_result = [None]  # mutable container for thread result

            def _try_kokoro():
                try:
                    from arenamcp.tts import VoiceOutput
                    kokoro_result[0] = VoiceOutput()
                except Exception as e:
                    logger.error(f"Kokoro init failed: {e}")

            t = threading.Thread(target=_try_kokoro, daemon=True)
            t.start()
            t.join(timeout=10.0)

            if kokoro_result[0] is not None:
                self._voice_output = kokoro_result[0]
                voice_id, voice_desc = self._voice_output.current_voice
                logger.info(f"TTS voice (Kokoro): {voice_desc}")
                self.ui.status("VOICE", f"{voice_desc}")
                self.ui.log(f"TTS ready: {voice_desc}")
            else:
                reason = "timeout" if t.is_alive() else "init failed"
                logger.warning(f"Kokoro unavailable ({reason}), using Windows SAPI")
                self._voice_output = _SAPIVoice()
                self.ui.status("VOICE", "Windows SAPI")
                self.ui.log(f"Kokoro unavailable ({reason}) — using Windows SAPI")
            return

        sd_ok, sd_reason = _probe_sounddevice_import(timeout_seconds=8.0)

        if not sd_ok:
            logger.error(f"sounddevice probe failed - disabling voice: {sd_reason}")
            self.ui.status("VOICE", "Audio init failed - voice disabled")
            self.ui.error("Audio driver issue: voice/TTS disabled. Check audio devices.")
            return

        try:
            from arenamcp.tts import VoiceOutput
        except Exception as e:
            logger.error(f"TTS import failed - disabling voice: {e}")
            self.ui.status("VOICE", "TTS unavailable - voice disabled")
            self.ui.error("Voice/TTS modules unavailable. Check install/audio setup.")
            return

        # Initialize local TTS
        try:
            logger.info("Initializing TTS...")
            self._voice_output = VoiceOutput()

            voice_id, voice_desc = self._voice_output.current_voice
            logger.info(f"TTS voice: {voice_desc}")
            self.ui.status("VOICE", f"TTS Voice: {voice_desc}")
        except Exception as e:
            logger.error(f"TTS init failed - disabling voice: {e}")
            self._voice_output = None
            self.ui.status("VOICE", "TTS init failed - voice disabled")
            self.ui.error("TTS failed to initialize. Check audio devices/drivers.")
            return

        # Initialize local STT (Whisper via VoiceInput) only if PTT/VOX mode
        if self._voice_mode in ("ptt", "vox"):
            try:
                from arenamcp.voice import VoiceInput
                logger.info(f"Initializing voice input ({self.voice_mode})...")
                self._voice_input = VoiceInput(mode=self.voice_mode)
            except Exception as e:
                logger.error(f"Voice input init failed - keeping TTS only: {e}")
                self._voice_input = None
        else:
            logger.info(f"Voice input disabled (mode={self._voice_mode})")

    def _init_voice_background(self) -> None:
        """Initialize voice in a background thread (pipe mode only).

        No probe — just import VoiceOutput directly. If PortAudio hangs during
        device enumeration, this daemon thread hangs forever but the coaching
        loop keeps running unaffected.
        """
        try:
            from arenamcp.tts import VoiceOutput
            logger.info("Initializing TTS (pipe mode)...")
            self.ui.log("Initializing TTS...")
            self._voice_output = VoiceOutput()
            voice_id, voice_desc = self._voice_output.current_voice
            logger.info(f"TTS voice: {voice_desc}")
            self.ui.status("VOICE", f"{voice_desc}")
            self.ui.log(f"TTS ready: {voice_desc}")
        except Exception as e:
            logger.error(f"Voice init failed: {e}")
            self.ui.status("VOICE", "TTS init failed")
            self.ui.log(f"TTS unavailable: {e}")

    # --- Urgency-aware polling intervals ---
    _POLL_BRIDGE = 0.15     # Bridge connected with pending decision (fast)
    _POLL_URGENT = 0.5      # Pending decision, mulligan, stack interaction
    _POLL_ACTIVE = 1.0      # Our turn with priority, combat phase
    _POLL_NORMAL = 1.5      # Opponent's turn, calm board state
    _POLL_IDLE = 2.5        # No active match

    def _get_poll_interval(self, game_state: dict[str, Any]) -> float:
        """Determine polling interval based on game state urgency.

        Uses short-lived bursts during high-urgency windows (pending decisions,
        combat, stack) and calmer intervals during idle or opponent turns.

        Args:
            game_state: Current game state dict.

        Returns:
            Sleep interval in seconds.
        """
        # No match active — idle polling
        turn = game_state.get("turn", {})
        turn_num = turn.get("turn_number", 0)
        if turn_num == 0:
            return self._POLL_IDLE

        # Pending decision — urgent (player must act)
        # Use faster bridge polling when bridge is providing decision data
        if game_state.get("pending_decision"):
            if self._bridge_poller.connected:
                return self._POLL_BRIDGE
            return self._POLL_URGENT

        # Stack has items — something is resolving, need quick updates
        stack = game_state.get("stack", [])
        if stack:
            return self._POLL_URGENT

        # Combat phase — fast transitions between declare/block/damage
        phase = turn.get("phase", "")
        if "Combat" in phase:
            return self._POLL_ACTIVE

        # Our turn with priority — we may need to act
        local_seat = game_state.get("local_seat_id")
        priority = turn.get("priority_player")
        if local_seat and priority == local_seat:
            return self._POLL_ACTIVE

        # Default: opponent's turn or calm state
        return self._POLL_NORMAL

    def _coaching_loop(self) -> None:
        """Poll MCP for game state and provide coaching, with auto-draft detection."""
        logger.info("Coaching loop started")
        prev_state: dict[str, Any] = {}
        seat_announced = False

        last_advice_turn = 0
        last_advice_phase = ""
        # Critical triggers that always fire regardless of frequency setting
        # Combat triggers removed - too noisy for "start_of_turn" mode
        # decision_required added - scry, discard, target choices need immediate advice
        CRITICAL_PRIORITY = {"stack_spell", "stack_spell_yours", "stack_spell_opponent", "low_life", "opponent_low_life", "decision_required", "threat_detected", "losing_badly"}

        # Match ID tracking — reset coaching state when match changes
        last_match_id = None

        # Draft/Sealed detection state
        in_draft_mode = False
        in_sealed_mode = False
        sealed_analyzed = False
        last_draft_pack = 0
        last_draft_pick = 0
        last_active_draft_at = 0.0
        last_inactive_log = 0
        draft_inactive_grace_seconds = 5.0

        while self._running:
            try:
                # Poll for new log content (watchdog backup - Windows often misses events)
                self._mcp.poll_log()

                # Check for active draft/sealed first
                draft_pack = self._mcp.get_draft_pack()

                if draft_pack.get("is_active"):
                    last_active_draft_at = time.time()
                    is_sealed = draft_pack.get("is_sealed", False)

                    if is_sealed:
                        # SEALED MODE
                        if not in_sealed_mode:
                            in_sealed_mode = True
                            in_draft_mode = False
                            self.draft_mode = True
                            set_code = draft_pack.get("set_code", "???")
                            if not self.set_code:
                                self.set_code = set_code
                            self.ui.status("SEALED", f"Detected sealed event: {set_code}")
                            self.ui.log("[SEALED] Waiting for pool to be opened...\n")
                            logger.info(f"Auto-detected sealed: {set_code}")

                        # Check if pool is ready for analysis
                        if not sealed_analyzed:
                            sealed_result = self._mcp.get_sealed_pool()
                            pool_size = sealed_result.get("pool_size", 0)

                            if pool_size > 0:
                                sealed_analyzed = True
                                self.ui.log(f"\n[SEALED] Pool opened ({pool_size} cards)")
                                self.ui.log(sealed_result.get("detailed_text", ""))
                                self.ui.log("")

                                # Speak the recommendation
                                advice = sealed_result.get("spoken_advice", "")
                                if advice:
                                    logger.info(f"SEALED ADVICE: {advice}")
                                    self.speak_advice(advice)

                        time.sleep(2.0)  # Slower polling for sealed
                        continue

                    else:
                        # DRAFT MODE
                        pack_num = draft_pack.get("pack_number", 0)
                        pick_num = draft_pack.get("pick_number", 0)
                        cards = draft_pack.get("cards", [])

                        # New pack detected
                        if cards and (pack_num != last_draft_pack or pick_num != last_draft_pick):
                            if not in_draft_mode:
                                in_draft_mode = True
                                in_sealed_mode = False
                                self.draft_mode = True
                                set_code = draft_pack.get("set_code", "???")
                                if not self.set_code:
                                    self.set_code = set_code
                                self.ui.status("DRAFT", f"Detected draft: {set_code}")
                                self.ui.log("[DRAFT] Auto-switching to draft advice mode\n")
                                logger.info(f"Auto-detected draft: {set_code}")

                            # Use composite evaluation (WR + on-color + synergy + card type)
                            eval_result = self._mcp.evaluate_draft_pack()
                            if eval_result.get("is_active") and eval_result.get("evaluations"):
                                advice = eval_result["spoken_advice"]
                                picked = eval_result.get("picked_count", 0)

                                # Log detailed scores for the top picks
                                top_evals = eval_result["evaluations"]
                                detail_parts = []
                                for e in top_evals[:3]:
                                    wr = f"{e['gih_wr']*100:.0f}%" if e.get("gih_wr") else "N/A"
                                    reasons = ", ".join(e.get("all_reasons", []))
                                    detail_parts.append(f"  {e['name']}: score={e['score']:.0f} WR={wr} [{reasons}]")
                                detail_log = "\n".join(detail_parts)
                                self.ui.log(f"\n[DRAFT P{pack_num}P{pick_num}] ({picked} picked)\n{detail_log}\n")
                                logger.info(f"DRAFT: P{pack_num}P{pick_num} - {advice}")
                                self.speak_advice(advice)
                                last_draft_pack = pack_num
                                last_draft_pick = pick_num
                            elif eval_result.get("is_active"):
                                self.ui.log(f"\n[DRAFT P{pack_num}P{pick_num}] No evaluated picks\n")
                                logger.warning(f"Draft eval returned no evaluations for P{pack_num}P{pick_num}")
                                last_draft_pack = pack_num
                                last_draft_pick = pick_num

                        time.sleep(1.0)  # Faster polling during draft
                        continue

                else:
                    if in_draft_mode or in_sealed_mode:
                        inactive_for = time.time() - last_active_draft_at
                        if inactive_for < draft_inactive_grace_seconds:
                            # MTGA briefly clears the current pack between picks.
                            # Keep draft mode alive until the next pack arrives.
                            time.sleep(0.5)
                            continue

                # Not in draft/sealed - regular game coaching
                if in_draft_mode or in_sealed_mode:
                    mode_name = "Sealed" if in_sealed_mode else "Draft"
                    was_draft = in_draft_mode
                    in_draft_mode = False
                    in_sealed_mode = False
                    sealed_analyzed = False
                    self.draft_mode = False
                    last_active_draft_at = 0.0
                    self.ui.log(f"\n[{mode_name.upper()}] {mode_name} complete, switching to game coaching\n")
                    logger.info(f"{mode_name} ended, resuming game coaching")
                    last_draft_pack = 0
                    last_draft_pick = 0

                    # Analyze drafted pool and suggest a deck build
                    if was_draft:
                        try:
                            pool_result = self._mcp.analyze_draft_pool()
                            pool_size = pool_result.get("pool_size", 0)
                            if pool_size > 0:
                                detailed = pool_result.get("detailed_text", "")
                                spoken = pool_result.get("spoken_advice", "")
                                if detailed:
                                    self.ui.log(f"\n{detailed}\n")
                                if spoken:
                                    logger.info(f"Draft deck suggestion: {spoken}")
                                    self.speak_advice(spoken)
                            else:
                                logger.warning("No picked cards found for post-draft analysis")
                        except Exception as e:
                            logger.error(f"Post-draft deck analysis failed: {e}")
                        # Deck strategy brief fires later when the match starts
                        # and deck_cards arrive via ConnectResp.

                curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())

                # Emit game state to pipe adapter (native GUI)
                if hasattr(self.ui, 'emit_game_state'):
                    self.ui.emit_game_state(curr_state)

                turn = curr_state.get("turn", {})
                turn_num = turn.get("turn_number", 0)
                phase = turn.get("phase", "")
                curr_match_id = curr_state.get("match_id")

                # Resolve unknown cards via VLM (only when using a local VLM backend)
                # Skip entirely for cloud backends like Azure — the card DB
                # or Scryfall fallback handles unknown cards without VLM.
                if (
                    turn_num > 0
                    and self._vision_mapper
                    and self.backend_name == "local"
                    and not getattr(self, '_vlm_resolve_in_progress', False)
                ):
                    self._vlm_resolve_in_progress = True
                    def _bg_resolve(state):
                        try:
                            self._resolve_unknown_cards(state)
                        except Exception as e:
                            logger.debug(f"VLM card resolution error: {e}")
                        finally:
                            self._vlm_resolve_in_progress = False
                    threading.Thread(target=_bg_resolve, args=(curr_state,), daemon=True).start()

                # ── GAME END DETECTION ──
                # PRIMARY: Check threading.Event set by parser thread
                # (IntermissionReq or finalMatchResult). This fires immediately
                # regardless of whether the coaching loop was blocked on LLM.
                try:
                    from arenamcp.server import game_state as gs
                    if gs.game_ended_event.is_set() and not self._game_end_handled:
                        # Guard: If we haven't coached a full game yet (fresh
                        # start / reconnect), the event is stale — from a
                        # previous game found during log catchup.  Consume and
                        # discard it so it doesn't fire mid-game.
                        if last_match_id is None:
                            stale_result, _ = gs.consume_game_end()
                            self._game_end_handled = True
                            logger.info(
                                f"Discarded stale game-end event on startup "
                                f"(result={stale_result}), current game still active"
                            )
                        elif self._advice_history:
                            self._game_end_handled = True
                            result, snapshot = gs.consume_game_end()
                            game_result = result or "unknown"
                            logger.info(f"Game ended (event signal): {game_result} — launching post-match analysis")
                            self._saved_advice_history = list(self._advice_history)
                            self._saved_missed_decisions = list(self._missed_decisions)
                            self._last_match_result = game_result
                            # Use pre-reset snapshot (full final state) if available,
                            # otherwise fall back to current (already-reset) state
                            self._last_match_final_state = snapshot or dict(curr_state)
                            threading.Thread(
                                target=self._post_match_analysis_worker,
                                daemon=True,
                            ).start()
                except Exception as e:
                    msg = str(e)
                    if msg != self._last_game_end_check_error:
                        self._last_game_end_check_error = msg
                        logger.warning(f"Game-end event check failed: {e}")

                # SECONDARY: Detect match boundary via match_id change.
                # Two cases:
                #   (a) match_id goes FROM something TO a different value (new match started)
                #   (b) match_id goes FROM something TO None (match ended, back to menu)
                match_id_changed = curr_match_id != last_match_id
                if match_id_changed and last_match_id is not None:
                    self._match_number += 1
                    logger.info(f"Match boundary detected ({last_match_id} -> {curr_match_id}), match #{self._match_number}, resetting coaching state")

                    # Trigger analysis if game_end detection above missed it
                    if self._advice_history and not self._saved_advice_history:
                        self._saved_advice_history = list(self._advice_history)
                        self._saved_missed_decisions = list(self._missed_decisions)
                        self._last_match_result = self._detect_match_result()
                        self._last_match_final_state = dict(prev_state) if prev_state else None
                        threading.Thread(
                            target=self._post_match_analysis_worker,
                            daemon=True,
                        ).start()

                    prev_state = {}
                    last_advice_turn = 0
                    last_advice_phase = ""
                    seat_announced = False
                    self._advice_history = []
                    self._deck_analyzed = False
                    self._game_end_handled = False
                    # Note: _pending_post_match_analysis is NOT cleared here —
                    # it persists until F7 is pressed or a new analysis replaces it.
                    self._tempo_tracker.reset()
                    self._missed_decisions = []
                    self._recent_gre_log.clear()
                    self._vlm_card_cache.clear()
                    self._vlm_card_failures.clear()
                    self._bridge_poller.reset()
                    if self._coach:
                        self._coach.clear_deck_strategy()
                    # Suppress stale triggers for one cycle after match
                    # boundary reset. prev_state={} causes check_triggers to
                    # fire false positives (new_turn, land_played, etc.)
                    # because it sees the reconstructed state as entirely new.
                    self._match_boundary_ts = time.time()
                if match_id_changed:
                    last_match_id = curr_match_id

                # Debug: Log if turn_num is 0 (every 30 seconds)
                if turn_num == 0:
                    if not hasattr(self, '_last_turn0_log'):
                        self._last_turn0_log = 0
                    if time.time() - self._last_turn0_log > 30:
                        logger.debug(f"turn_num=0, players={len(curr_state.get('players', []))}, battlefield={len(curr_state.get('battlefield', []))}")
                        self._last_turn0_log = time.time()

                # TERTIARY: Detect new game (turn number decreased) — fallback for same-match restarts
                if turn_num > 0 and turn_num < last_advice_turn:
                    self._match_number += 1
                    logger.info(f"New game detected in coaching loop (turn {last_advice_turn} -> {turn_num}), match #{self._match_number}, resetting advice tracking")

                    # Only launch fallback post-match analysis when we have
                    # explicit end-of-game evidence. A turn drop can also happen
                    # after relaunch or mid-game resync, and using it alone
                    # causes false "post-match analysis" loops.
                    if (
                        self._advice_history
                        and not self._saved_advice_history
                        and self._has_explicit_game_end_evidence()
                    ):
                        self._saved_advice_history = list(self._advice_history)
                        self._saved_missed_decisions = list(self._missed_decisions)
                        self._last_match_result = self._detect_match_result()
                        self._last_match_final_state = dict(prev_state) if prev_state else None
                        threading.Thread(
                            target=self._post_match_analysis_worker,
                            daemon=True,
                        ).start()
                    elif self._advice_history and not self._saved_advice_history:
                        logger.info(
                            "Skipping fallback post-match analysis on turn-drop: "
                            "no explicit game-end evidence"
                        )

                    prev_state = {}
                    last_advice_turn = 0
                    last_advice_phase = ""
                    seat_announced = False  # Re-announce seat for new game
                    # Clear advice history for new match
                    self._advice_history = []
                    self._deck_analyzed = False
                    self._game_end_handled = False
                    # Note: _pending_post_match_analysis is NOT cleared here —
                    # it persists until F7 is pressed or a new analysis replaces it.
                    self._tempo_tracker.reset()
                    self._missed_decisions = []
                    self._recent_gre_log.clear()
                    self._vlm_card_cache.clear()
                    self._vlm_card_failures.clear()
                    self._bridge_poller.reset()
                    if self._coach:
                        self._coach.clear_deck_strategy()
                    self._match_boundary_ts = time.time()
                    logger.info("Cleared advice history for new match")

                # Announce seat detection when game starts
                if not seat_announced and turn_num > 0:
                    players = curr_state.get("players", [])
                    for p in players:
                        if p.get("is_local"):
                            seat_id = p.get("seat_id")
                            self.ui.status("GAME", f"Detected as Seat {seat_id} - press F8 if this is wrong")
                            logger.info(f"Game detected, local seat = {seat_id}")
                            seat_announced = True
                            # Auto-enable replay recording for debug reports
                            self._enable_replay_recording()
                            break

                # Deck strategy analysis (once per match)
                if not self._deck_analyzed and self._coach and turn_num > 0:
                    deck_cards = curr_state.get("deck_cards", [])

                    # Fallback for mid-game join: ConnectResp was missed,
                    # so reconstruct deck from all known local-player cards
                    # across all zones (hand, battlefield, graveyard, etc.)
                    if not deck_cards:
                        local_seat = self._get_local_seat_from_state(curr_state)
                        if local_seat is not None:
                            seen_grp_ids = set()
                            for zone in ("hand", "battlefield", "graveyard", "exile", "command"):
                                for card in curr_state.get(zone, []):
                                    if card.get("owner_seat_id") == local_seat:
                                        grp_id = card.get("grp_id", 0)
                                        if grp_id and grp_id not in seen_grp_ids:
                                            seen_grp_ids.add(grp_id)
                                            deck_cards.append(grp_id)
                            if deck_cards:
                                logger.info(
                                    f"Reconstructed deck from visible zones: "
                                    f"{len(deck_cards)} unique cards"
                                )

                    if deck_cards:
                        self._deck_analyzed = True
                        logger.info(f"Starting deck analysis for {len(deck_cards)} cards")

                        def _analyze_deck_bg(coach, mcp, card_ids, ui, backend_name, model_name, speak_fn):
                            try:
                                # Enrich grpIds to (name, type, oracle_text) tuples
                                enriched = []
                                for grp_id in card_ids:
                                    try:
                                        info = mcp.get_card_info(grp_id)
                                        name = info.get("name", f"Unknown({grp_id})")
                                        card_type = info.get("type_line", "")
                                        oracle = info.get("oracle_text", "")
                                        enriched.append((name, card_type, oracle))
                                    except Exception as e:
                                        logger.debug(f"Card enrichment failed for grp_id={grp_id}: {e}")
                                        enriched.append((f"Unknown({grp_id})", "", ""))

                                # Use a SEPARATE backend instance so deck analysis
                                # doesn't hold the advice backend's lock
                                from arenamcp.coach import create_backend
                                deck_backend = create_backend(backend_name, model=model_name)

                                # Full strategy analysis (stored, injected into every prompt)
                                strategy = coach.analyze_deck(enriched, backend=deck_backend)

                                # Brief spoken summary (3-5 sentences for TTS)
                                brief = coach.get_deck_strategy_brief(enriched, backend=deck_backend)

                                if hasattr(deck_backend, 'close'):
                                    deck_backend.close()

                                if strategy:
                                    first_line = strategy.split("\n")[0].strip()
                                    ui.status("DECK", first_line[:60])
                                    logger.info(f"Deck strategy stored: {len(strategy)} chars")

                                if brief:
                                    ui.log(f"\n[bold green]DECK STRATEGY:[/] {brief}\n")
                                    speak_fn(brief, blocking=False)
                            except Exception as e:
                                logger.error(f"Background deck analysis failed: {e}")

                        t = threading.Thread(
                            target=_analyze_deck_bg,
                            args=(self._coach, self._mcp, deck_cards, self.ui,
                                  self._backend_name, self.model_name,
                                  self.speak_advice),
                            daemon=True,
                        )
                        t.start()

                # FORCE CHECK: Always check triggers if trigger detector exists.
                # prev_state starts as {} (falsy) but check_triggers handles empty
                # prev_state gracefully via .get() defaults — this allows mulligan
                # triggers to fire on the very first poll cycle.
                if self._trigger:
                    # Auto-detect draft mode
                    try:
                        draft_state = self._mcp.get_draft_pack()
                        is_draft_active = draft_state.get("is_active", False)
                        
                        if is_draft_active and not self.draft_mode:
                            logger.info("Auto-detected draft - enabling draft mode")
                            self.draft_mode = True
                            self.ui.status("MODE", "Draft")
                        elif not is_draft_active and self.draft_mode:
                            logger.info("Draft ended - disabling draft mode")
                            self.draft_mode = False
                            self.ui.status("MODE", "Game")
                    except Exception as e:
                        logger.debug(f"Draft detection error: {e}")

                    # --- Bridge-first decision detection ---
                    # Poll the GRE bridge BEFORE log-based triggers. When the
                    # bridge is connected, it authoritatively detects decision
                    # state changes (new pending interaction, cleared, or
                    # action list changed) — no log-diff heuristics needed.
                    bridge_trigger = None
                    if self._bridge_poller:
                        bridge_trigger = self._bridge_poller.poll()
                        if bridge_trigger:
                            self._bridge_poller.enrich_snapshot(curr_state)
                        elif self._bridge_poller.connected:
                            # No change, but still enrich snapshot with latest bridge data
                            self._bridge_poller.enrich_snapshot(curr_state)

                        # Update bridge status in UI (only on change)
                        _bridge_now = self._bridge_poller.connected
                        if not hasattr(self, '_last_bridge_ui_status'):
                            self._last_bridge_ui_status = None
                        if _bridge_now != self._last_bridge_ui_status:
                            self._last_bridge_ui_status = _bridge_now
                            if _bridge_now:
                                self.ui.status("BRIDGE", "Connected")
                            else:
                                self.ui.status("BRIDGE", "Disconnected")

                    triggers = self._trigger.check_triggers(prev_state, curr_state)

                    # Bridge-detected decision takes priority over log-based detection
                    if bridge_trigger and bridge_trigger["trigger"] == "decision_required":
                        if "decision_required" not in triggers:
                            triggers.insert(0, "decision_required")
                        # Attach bridge data for downstream consumers (autopilot, prompts)
                        curr_state["_bridge_trigger"] = bridge_trigger

                    # BACKSTOP: Force decision_required for pending decisions
                    # that trigger detection may have missed (short-lived scry,
                    # autopilot continuation, malformed GRE chunks).
                    # Only needed when bridge is NOT providing decision detection.
                    bridge_active = self._bridge_poller and self._bridge_poller.connected
                    pending_now = curr_state.get("pending_decision")

                    if not bridge_active:
                        if pending_now and "decision_required" not in triggers:
                            # Scry/surveil are time-critical — always force trigger
                            if pending_now in ("Group Selection", "Order Cards"):
                                triggers.append("decision_required")
                                logger.info(f"Forced decision_required for {pending_now}")

                    # Autopilot backstop: force decision_required when autopilot
                    # is enabled and a decision is pending but no trigger fired.
                    # Runs regardless of bridge status — the bridge detects
                    # *transitions* but the autopilot needs to act on *any*
                    # pending decision that hasn't been handled yet.
                    if self._autopilot_enabled and self._autopilot and pending_now:
                        if "decision_required" not in triggers:
                            dec_ctx = curr_state.get("decision_context") or {}
                            dec_type = dec_ctx.get("type", "")
                            legal = curr_state.get("legal_actions", []) or []
                            sig = f"{pending_now}|{dec_type}|{len(legal)}"
                            now = time.time()
                            if (
                                sig != self._last_forced_decision_sig
                                or (now - self._last_forced_decision_ts) > 2.0
                            ):
                                triggers.append("decision_required")
                                self._last_forced_decision_sig = sig
                                self._last_forced_decision_ts = now
                                logger.info(
                                    f"Autopilot backstop: forced decision_required for '{pending_now}'"
                                )
                    else:
                        self._last_forced_decision_sig = None

                    # Suppress stale triggers right after a match boundary
                    # reset. prev_state={} causes check_triggers to see the
                    # reconstructed game state as entirely new, firing false
                    # new_turn/land_played/opponent_low_life triggers against
                    # a game that already ended (bridge shows Intermission).
                    # BUT: keep bridge-detected triggers — those are real
                    # (e.g. mulligan prompt in a new game).
                    _boundary_age = time.time() - getattr(self, '_match_boundary_ts', 0)
                    if triggers and _boundary_age < 2.0 and not bridge_trigger:
                        logger.info(
                            f"Suppressing {len(triggers)} stale triggers "
                            f"{triggers} ({_boundary_age:.1f}s after match boundary)"
                        )
                        triggers = []

                    # Debug: Log trigger results
                    if triggers:
                        logger.info(f"Triggers detected: {triggers}")

                    # Feed the GRE log ring buffer for watchdog context
                    _turn = curr_state.get("turn", {})
                    _gre_line = (
                        f"{datetime.now().strftime('%H:%M:%S')} "
                        f"T{_turn.get('turn_number', 0)} "
                        f"{_turn.get('phase', '')} "
                        f"{_turn.get('step', '')} "
                        f"active={_turn.get('active_player', '?')} "
                        f"prio={_turn.get('priority_player', '?')} "
                        f"decision={curr_state.get('pending_decision')} "
                        f"last_cleared={curr_state.get('last_cleared_decision')} "
                        f"triggers={triggers or 'none'}"
                    )
                    self._recent_gre_log.append(_gre_line)
                    if len(self._recent_gre_log) > self._recent_gre_log_max:
                        self._recent_gre_log.pop(0)

                    if not triggers:
                        # Log why no triggers (every 30 seconds to avoid spam)
                        if not hasattr(self, '_last_no_trigger_log'):
                            self._last_no_trigger_log = 0
                        if time.time() - self._last_no_trigger_log > 30:
                            local_s = curr_state.get("turn", {}).get("active_player", 0)
                            priority = curr_state.get("turn", {}).get("priority_player", 0)
                            logger.debug(f"No triggers: turn={turn_num}, active={local_s}, priority={priority}, phase={phase}")
                            self._last_no_trigger_log = time.time()

                    # Clear pending combat steps after checking (they're now processed)
                    self._mcp.clear_pending_combat_steps()

                    # Sort triggers by priority to ensure we handle the most critical one only
                    # Priority order: Decision > Stack > Action > Combat > Turn > Priority
                    trigger_priorities = {
                        "decision_required": 11,
                        "stack_spell": 10,
                        "stack_spell_yours": 10,
                        "stack_spell_opponent": 10,
                        "losing_badly": 9,
                        "low_life": 9,
                        "opponent_low_life": 8,
                        "land_played": 7,      # After land drop, what's next?
                        "spell_resolved": 7,   # After spell resolves, what's next?
                        "combat_attackers": 6,
                        "combat_blockers": 6,
                        "new_turn": 5,
                        "priority_gained": 1
                    }

                    triggers.sort(key=lambda x: trigger_priorities.get(x, 0), reverse=True)

                    stale_retry_enqueued = False
                    for trigger in triggers:
                        raw_new_turn = trigger == "new_turn"
                        # Critical triggers always fire (stack spells, low life)
                        # BUT: "Action Required" is just generic main-phase priority,
                        # not a real decision (Mulligan/Scry/Discard/Target). Suppress
                        # it if we already advised this turn+phase to avoid duplicates.
                        is_critical = trigger in CRITICAL_PRIORITY
                        if trigger == "decision_required":
                            pending = curr_state.get("pending_decision")
                            if (
                                pending == "Action Required"
                                and turn_num == last_advice_turn
                                and phase == last_advice_phase
                                and not (self._autopilot_enabled and self._autopilot)
                            ):
                                logger.info(f"Suppressing decision_required: 'Action Required' already advised this turn+phase")
                                continue

                        # New turn triggers once per turn
                        # DELAY BUFFER: For new_turn triggers, wait briefly for Hand zone to update
                        # This prevents "missing draw" bugs where we advise before the drawn card arrives
                        if raw_new_turn:
                            # Reset seen threats on new game (turn 1)
                            if turn_num == 1 and hasattr(self._trigger, '_seen_threats'):
                                self._trigger._seen_threats.clear()
                                logger.info("New game detected - cleared seen threats")
                            time.sleep(0.4)  # 400ms to allow Draw Step zone update
                            # Force a log poll to ensure we have latest updates
                            try:
                                self._mcp.poll_log()
                            except Exception as e:
                                logger.debug(f"poll_log failed after new_turn delay: {e}")
                            # Re-fetch game state to get updated hand
                            try:
                                curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after new_turn delay: {e}")

                            # Clear stale pending win plan if game has advanced
                            if self._pending_win_plan and turn_num > self._pending_win_plan_turn + 1:
                                logger.info(f"Clearing stale win plan (plan turn {self._pending_win_plan_turn}, now {turn_num})")
                                self._pending_win_plan = None
                                self.ui.status("WIN-PLAN", "")

                            # Spawn background win plan worker (non-blocking)
                            # Skip when autopilot is active — it handles its own strategy
                            _active = curr_state.get("turn", {}).get("active_player", 0)
                            _local = self._get_local_seat_from_state(curr_state)
                            _is_my_turn = (_active == _local) if _local else False
                            if _is_my_turn and turn_num > self._win_plan_turn:
                                self._win_plan_turn = turn_num
                                threading.Thread(
                                    target=self._win_plan_worker,
                                    args=(curr_state,),
                                    daemon=True,
                                ).start()

                        # VISION TRIGGER: Scry/Surveil (Group Selection) decisions
                        # need a screenshot because the card identity isn't in the
                        # game state — only visible on screen.
                        if trigger == "decision_required":
                            pending = curr_state.get("pending_decision") or ""
                            if pending in ("Group Selection", "Order Cards"):
                                logger.info(f"Scry/Surveil detected ({pending}) — triggering screenshot analysis")
                                threading.Thread(
                                    target=self.take_screenshot_analysis,
                                    daemon=True,
                                ).start()
                                continue  # Skip text-only coaching for this trigger

                        # DELAY BUFFER: For mulligan decisions, wait for hand zone to populate.
                        # SubmitDeckReq arrives before the GameStateMessage with hand cards.
                        # Skip when bridge detected the decision — bridge data is already live.
                        bridge_detected = curr_state.get("_bridge_trigger") is not None
                        if trigger == "decision_required" and curr_state.get("pending_decision") == "Mulligan" and not bridge_detected:
                            time.sleep(0.5)  # 500ms to allow hand zone update
                            try:
                                self._mcp.poll_log()
                            except Exception as e:
                                logger.debug(f"poll_log failed after mulligan delay: {e}")
                            try:
                                curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after mulligan delay: {e}")

                        # DELAY BUFFER: For spell_resolved, wait briefly for ETB triggers to resolve.
                        # When spells like Sheltered by Ghosts resolve, the exile/removal happens
                        # via a subsequent ETB trigger that needs another game state diff.
                        if trigger == "spell_resolved":
                            time.sleep(0.4)  # 400ms to allow ETB triggers to resolve
                            try:
                                self._mcp.poll_log()
                            except Exception as e:
                                logger.debug(f"poll_log failed after spell_resolved delay: {e}")
                            try:
                                curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
                            except Exception as e:
                                logger.debug(f"Failed to re-fetch state after spell_resolved delay: {e}")

                        turn = curr_state.get("turn", {})
                        turn_num = turn.get("turn_number", 0)
                        phase = turn.get("phase", "")
                        local_seat = self._get_local_seat_from_state(curr_state)
                        active_seat = turn.get("active_player", 0)
                        is_my_turn = (active_seat == local_seat) if local_seat else False

                        # CRITICAL: Filter turn-specific triggers based on refreshed turn ownership.
                        # "new_turn" advice only makes sense on YOUR turn (play lands, cast spells)
                        # On opponent's turn, rename to "opponent_turn" for strategy analysis
                        # "combat_attackers" only on YOUR turn (you declare attackers)
                        # "combat_blockers" only on OPPONENT's turn (you declare blockers)
                        if raw_new_turn and not is_my_turn:
                            trigger = "opponent_turn"
                            logger.info(f"Opponent's turn started (turn {turn_num})")
                        if trigger == "combat_attackers" and not is_my_turn:
                            logger.debug("Suppressing combat_attackers trigger (opponent's turn)")
                            continue
                        if trigger == "combat_blockers" and is_my_turn:
                            logger.debug("Suppressing combat_blockers trigger (my turn, not blocking)")
                            continue

                        # New turn triggers once per turn
                        is_new_turn = trigger == "new_turn" and turn_num > last_advice_turn

                        # Opponent turn triggers once per opponent turn
                        is_opponent_turn = trigger == "opponent_turn" and turn_num > last_advice_turn

                        # Check if there's a pending decision (scry, discard, target, etc.)
                        # If so, suppress step-by-step "what's next" triggers until decision resolves
                        pending_decision = curr_state.get("pending_decision")
                        has_pending_decision = pending_decision is not None

                        # Step-by-step triggers: land_played, spell_resolved, and combat
                        # BUT suppress if there's a pending decision - wait for it to resolve first
                        is_step_by_step = (
                            trigger in ("land_played", "spell_resolved", "combat_attackers", "combat_blockers")
                            and not has_pending_decision
                        )

                        # Log when we're waiting for a decision
                        if trigger in ("land_played", "spell_resolved") and has_pending_decision:
                            logger.debug(f"Suppressing {trigger} - waiting for decision: {pending_decision}")

                        # Combat and priority triggers only in "every_priority" mode
                        is_frequent = (
                            self.advice_frequency == "every_priority" and
                            trigger in ("priority_gained", "combat_attackers", "combat_blockers") and
                            (turn_num > last_advice_turn or phase != last_advice_phase)
                        )

                        # Additional check: Don't spam priority triggers if we just advised on new_turn
                        # unless distinct phase
                        if trigger == "priority_gained" and is_new_turn:
                            continue

                        # DECISION PRIORITY: If there's a decision required, skip non-critical triggers
                        # in the same batch to ensure the decision is the primary focus.
                        if "decision_required" in triggers and trigger != "decision_required" and not is_critical:
                            continue

                        should_advise = is_critical or is_new_turn or is_opponent_turn or is_step_by_step or is_frequent

                        if not should_advise:
                            continue

                        logger.info(f"TRIGGER: {trigger}")

                        # NOISE SUPPRESSION: Skip advice when something is on the stack
                        # and player can't respond. Prevents confusing "let it resolve" advice
                        # when an ETB trigger (yours or opponent's) is just passing through.
                        # NOTE: "new_turn" is excluded — when the player's turn starts, they
                        # always need advice even if a stale ability is still on the stack.
                        stack = curr_state.get("stack", [])
                        if (
                            stack
                            and trigger in ("land_played", "spell_resolved", "priority_gained")
                            and not has_pending_decision
                        ):
                            has_instants = self._trigger._has_castable_instants(curr_state)
                            if not has_instants:
                                logger.info(f"Quiet: {trigger} (stack active, no responses)")
                                continue

                        # NOISE SUPPRESSION: Skip LLM call when player has no meaningful options.
                        # Saves ~3-5s API call + TTS for obvious "pass priority" situations.
                        QUIET_TRIGGERS = {"stack_spell_yours", "stack_spell_opponent", "priority_gained", "spell_resolved", "opponent_turn"}
                        if trigger in QUIET_TRIGGERS and not has_pending_decision:
                            has_instants = self._trigger._has_castable_instants(curr_state)
                            stack = curr_state.get("stack", [])

                            # Own spell on stack with no instants to respond → auto-pass
                            if trigger == "stack_spell_yours" and not has_instants:
                                logger.info(f"Quiet: {trigger} (own spell, no responses)")
                                continue

                            # Opponent spell/ability on stack with no instant-speed responses → quiet
                            # This covers both opponent's turn AND your turn (e.g. opponent ETB triggers)
                            if trigger == "stack_spell_opponent" and not has_instants:
                                logger.info(f"Quiet: {trigger} (no instant-speed responses)")
                                continue

                            # Opponent's action or priority with no instant-speed options → quiet
                            if not is_my_turn and not has_instants:
                                logger.info(f"Quiet: {trigger} (opp turn, no instants)")
                                continue

                            # Spell resolved but nothing castable in hand and not my main phase
                            if trigger == "spell_resolved" and not has_instants and not is_my_turn:
                                logger.info(f"Quiet: {trigger} (resolved, no options)")
                                continue

                            # Opponent's turn with no instant-speed responses → quiet
                            if trigger == "opponent_turn" and not has_instants:
                                logger.info(f"Quiet: {trigger} (no instant-speed responses)")
                                continue

                        # THREAT DETECTION: Direct speaking for instant response (no LLM needed)
                        if trigger == "losing_badly" and self._coach:
                            logger.info("Proactive win probability check (losing badly)")
                            self._inject_library_summary_if_needed(curr_state)
                            opp_cards = self._match_context.get("opponent_played_cards", [])
                            prob = self._coach.generate_win_probability(curr_state, opp_cards)
                            if prob:
                                self._record_advice(prob, trigger, game_state=curr_state)
                                last_advice_turn = turn_num
                                last_advice_phase = phase
                                self.ui.advice(prob, "WIN PROBABILITY")
                                self.speak_advice(prob)
                            continue

                        if trigger == "threat_detected" and hasattr(self._trigger, '_last_threat'):
                            threat = self._trigger._last_threat
                            advice = f"Warning! {threat['name']}. {threat['warning']}"
                            logger.info(f"THREAT ALERT: {advice}")
                            self._record_advice(advice, trigger, game_state=curr_state)
                            last_advice_turn = turn_num
                            last_advice_phase = phase

                            # Speak immediately and display
                            self.ui.advice(advice, "THREAT")
                            self.speak_advice(advice)
                            continue  # Don't send to LLM

                        # AUTOPILOT: If enabled, route trigger through autopilot.
                        # On success, skip coaching. On failure, fall through to
                        # regular coaching so the user still gets advice.
                        if self._autopilot_enabled and self._autopilot:
                            try:
                                handled = self._autopilot.process_trigger(
                                    curr_state, trigger
                                )
                                if handled:
                                    last_advice_turn = turn_num
                                    last_advice_phase = phase
                                    continue  # Autopilot handled it
                                else:
                                    logger.info(
                                        f"Autopilot failed for trigger '{trigger}' — "
                                        "falling through to coaching"
                                    )
                            except Exception as e:
                                logger.error(f"Autopilot error: {e}", exc_info=True)
                            # Fall through to coaching below

                        if self._coach:
                            # Snapshot turn state BEFORE the (slow) LLM call
                            pre_advice_turn = turn_num
                            pre_advice_phase = phase

                            # Inject library targets when a tutor spell is in hand
                            self._inject_library_summary_if_needed(curr_state)

                            # Inject match identifier so persistent backends
                            # know when a new game starts and reset context
                            curr_state["_match_number"] = self._match_number

                            # Unified advice path: use the action planner for
                            # both autopilot actions AND coaching advice. The
                            # planner constrains itself to legal actions only.
                            if self._autopilot and hasattr(self._autopilot, '_planner'):
                                legal_actions = self._autopilot._get_legal_actions(curr_state)
                                decision_context = curr_state.get("decision_context")
                                plan = self._autopilot._planner.plan_actions(
                                    curr_state, trigger, legal_actions, decision_context
                                )
                                advice = plan.voice_advice or plan.overall_strategy
                                if not advice and plan.actions:
                                    advice = str(plan.actions[0])
                                if not advice:
                                    advice = "No actionable play right now."
                            else:
                                advice = self._coach.get_advice(
                                    curr_state,
                                    trigger=trigger,
                                    style=self.advice_style
                                )
                            logger.info(f"ADVICE: {advice}")

                            # STALENESS CHECK: Re-poll game state after the LLM call.
                            # Only discard advice when the TURN changed (whole turn
                            # advanced while waiting).  Phase/step changes within the
                            # same turn are normal during a 5-12s LLM call and the
                            # advice is usually still relevant (e.g. "Play X" during
                            # Main1 is fine even if we're now in BeginCombat).
                            # Combat-specific triggers are the exception — "attack
                            # with X" is useless once combat is over.
                            fresh_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
                            fresh_turn = fresh_state.get("turn", {})
                            fresh_turn_num = fresh_turn.get("turn_number", 0)
                            fresh_phase = fresh_turn.get("phase", "")

                            if trigger == "opponent_turn":
                                # Opponent analysis: allow turn to advance by 1
                                is_stale = fresh_turn_num > pre_advice_turn + 1
                            elif trigger in ("combat_attackers", "combat_blockers"):
                                # Combat advice: stale if no longer in combat
                                is_stale = (
                                    fresh_turn_num != pre_advice_turn
                                    or "Combat" not in fresh_phase
                                )
                            else:
                                # General advice: only stale if the turn number changed
                                is_stale = fresh_turn_num != pre_advice_turn

                            if is_stale:
                                stale_label = "[STALE - discarded]"
                                logger.info(
                                    f"Discarding stale advice: turn {pre_advice_turn}->{fresh_turn_num}, "
                                    f"phase {pre_advice_phase}->{fresh_phase}"
                                )
                                self._record_advice(
                                    f"{stale_label} {advice}", trigger, game_state=curr_state
                                )
                                curr_state = fresh_state
                                turn = curr_state.get("turn", {})
                                turn_num = turn.get("turn_number", 0)
                                phase = turn.get("phase", "")
                                if (
                                    not stale_retry_enqueued
                                    and self._has_meaningful_local_action_window(fresh_state)
                                    and "decision_required" not in triggers
                                ):
                                    stale_retry_enqueued = True
                                    triggers.append("decision_required")
                                    logger.info(
                                        "Re-queued decision_required after stale discard "
                                        "(fresh actionable local window)"
                                    )
                                # DON'T update last_advice_turn — stale advice shouldn't
                                # suppress future triggers on the same turn
                                continue

                            # Build seat info for display
                            local_seat = None
                            for p in curr_state.get("players", []):
                                if p.get("is_local"):
                                    local_seat = p.get("seat_id")
                                    break

                            battlefield = curr_state.get("battlefield", [])
                            your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat]
                            untapped_lands = sum(1 for c in your_cards
                                                 if "land" in c.get("type_line", "").lower()
                                                 and not c.get("is_tapped"))
                            seat_info = f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}" if local_seat else "Seat ?"

                            # Skip empty responses (e.g. from timeout/lock busy)
                            # NOTE: Do NOT update last_advice_turn before this check.
                            # Empty responses should not suppress future triggers.
                            if not advice or not advice.strip():
                                self._consecutive_errors = getattr(self, '_consecutive_errors', 0) + 1
                                max_errors = getattr(self, '_max_errors_before_fallback', 3)
                                logger.warning(
                                    f"Empty advice response ({self._consecutive_errors}/{max_errors}) — "
                                    "model timeout or backend hung"
                                )
                                self._report_backend_failure("Empty advice response (model timeout or backend hung)")
                                if self._consecutive_errors >= max_errors:
                                    # Try restarting the backend process first
                                    logger.warning("Too many empty responses, restarting backend...")
                                    self.ui.log("\n[BACKEND] Restarting (too many empty responses)...")
                                    try:
                                        be = self._coach._backend
                                        if hasattr(be, 'close'):
                                            be.close()
                                        self._reinit_coach()
                                        self._consecutive_errors = 0
                                        self.ui.log("[BACKEND] Restarted successfully\n")
                                    except Exception as e:
                                        logger.error(f"Backend restart failed: {e}")
                                        # Final fallback — switch to Ollama
                                        if self.fallback_to_ollama(reason="Backend hung (empty responses)"):
                                            continue
                                continue

                            # Check for backend auth/billing failures → auto-fallback
                            if self.check_advice_for_backend_failure(advice):
                                continue  # Fallback triggered, retry with new backend

                            # Don't speak error/fallback messages aloud
                            from arenamcp.backend_detect import is_query_failure_retriable as _is_err
                            if (
                                advice.startswith("Error")
                                or "didn't catch that" in advice
                                or (_is_err(advice) and len(advice) < 200)
                            ):
                                logger.warning(f"Suppressing error advice from TTS: {advice[:80]}")
                                self._report_backend_failure(advice)
                                self.ui.error(advice)
                            else:
                                # Advice was successfully generated — NOW update dedup state
                                # so only real, delivered advice suppresses future triggers.
                                last_advice_turn = turn_num
                                last_advice_phase = phase
                                self._record_advice(advice, trigger, game_state=curr_state)
                                self._mark_backend_healthy()
                                self.ui.advice(advice, seat_info)
                                # Non-blocking TTS: lets the loop poll for new
                                # game states (e.g. Select Targets) immediately.
                                # New advice will interrupt stale speech.
                                self.speak_advice(advice, blocking=False)

                prev_state = curr_state

            except Exception as e:
                logger.error(f"Coaching loop error: {e}")
                logger.debug(traceback.format_exc())
                self._record_error(str(e), "coaching_loop")

            # Urgency-aware polling: shorter sleep during decisions/combat,
            # longer sleep during idle/opponent turns
            try:
                poll_interval = self._get_poll_interval(curr_state)
            except NameError:
                poll_interval = self._POLL_NORMAL
            time.sleep(poll_interval)

        logger.info("Coaching loop stopped")

    def _voice_loop(self) -> None:
        """Handle voice input for questions (PTT mode with Whisper + Kokoro)."""
        if not self._voice_input:
            return

        logger.info(f"Voice loop started ({self.voice_mode})")
        if self.voice_mode == "ptt":
            self.ui.log("\n[MIC] Press F4 to ask (tap for quick advice)\n")
        else:
            self.ui.log("\n[MIC] Voice activation enabled\n")

        self._voice_input.start()

        while self._running:
            try:
                text = self._voice_input.wait_for_speech(timeout=2.0)

                if not self._voice_input._result_ready.is_set():
                    continue

                if self._coach and self._mcp:
                    # Force a log poll to get freshest state before advice
                    self._mcp.poll_log()
                    game_state = self._mcp.get_game_state()

                    # Get current seat and mana for display
                    local_seat = None
                    for p in game_state.get("players", []):
                        if p.get("is_local"):
                            local_seat = p.get("seat_id")
                            break

                    # Count untapped lands for mana display
                    battlefield = game_state.get("battlefield", [])
                    your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat]
                    untapped_lands = sum(1 for c in your_cards
                                         if "land" in c.get("type_line", "").lower()
                                         and not c.get("is_tapped"))

                    seat_info = f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}" if local_seat else "Seat ?"

                    # Check if we can use direct audio with Gemini
                    audio_data = self._voice_input.get_last_audio()
                    use_direct_audio = (
                        audio_data is not None and
                        len(audio_data) > 0 and
                        hasattr(self._coach._backend, 'complete_with_audio')
                    )

                    # Inject library targets when a tutor spell is in hand
                    self._inject_library_summary_if_needed(game_state)

                    if use_direct_audio:
                        # Direct audio to Gemini - skip local transcription
                        logger.info(f"AUDIO INPUT: {len(audio_data)} samples -> Gemini")
                        self.ui.log("\n[AUDIO] Sending to Gemini...")
                        context = self._coach._format_game_context(game_state)

                        # FORCE specific answer mode
                        user_message = (
                            f"{context}\n\n"
                            "IMPORTANT: The user just asked a specific question via audio (attached). "
                            "Do NOT give generic gameplay advice. "
                            "Listen to the audio and answer EXACTLY what they asked. "
                            "If they asked about a specific card, interaction, or rule, explain it in detail. "
                            "Ignore your usual brevity constraints if needed to answer fully."
                        )
                        advice = self._coach._backend.complete_with_audio(
                            self._coach._system_prompt,
                            user_message,
                            audio_data
                        )
                    elif text and text.strip():
                        logger.info(f"QUESTION: {text}")
                        self.ui.log(f"\n[YOU] {text}")
                        advice = self._coach.get_advice(game_state, question=text, style=self.advice_style)
                    else:
                        logger.info("QUICK ADVICE (F4 tap)")
                        self.ui.log("\n[QUICK] Analyzing...")
                        advice = self._coach.get_advice(game_state, trigger="user_request", style=self.advice_style)

                    logger.info(f"RESPONSE: {advice}")

                    # Check for backend auth/billing failures → auto-fallback
                    from arenamcp.backend_detect import is_query_failure_retriable as _is_err2
                    is_error_response = (
                        advice.startswith("Error")
                        or "didn't catch that" in advice
                        or (_is_err2(advice) and len(advice) < 200)
                    )
                    if not self.check_advice_for_backend_failure(advice) and not is_error_response:
                        self._mark_backend_healthy()
                        self.ui.advice(advice, seat_info)
                        self.speak_advice(advice)
                    elif is_error_response:
                        logger.warning(f"Suppressing error advice from voice TTS: {advice[:80]}")
                        self._report_backend_failure(advice)
                        self.ui.error(advice)

                    # Record for debug history with the same game state
                    trigger = "voice_audio" if use_direct_audio else ("voice_question" if text else "voice_quick")
                    self._record_advice(advice, trigger, game_state=game_state)

            except Exception as e:
                if self._running:
                    logger.error(f"Voice loop error: {e}")
                    self._record_error(str(e), "voice_loop")

        self._voice_input.stop()
        logger.info("Voice loop stopped")

    def _on_mute_hotkey(self) -> None:
        """F5 - Toggle TTS mute."""
        if self._voice_output:
            muted = self._voice_output.toggle_mute()
            self.ui.status("VOICE", f"{'MUTED' if muted else 'UNMUTED'} (saved)")
        else:
            self.ui.status("VOICE", "TTS not enabled")

    def _on_voice_hotkey(self) -> None:
        """F6 - Change TTS voice."""
        if self._voice_output:
            voice_id, desc = self._voice_output.next_voice()
            self.ui.status("VOICE", f"Changed to: {desc} (saved)")
            try:
                self._voice_output.speak("Voice changed.", blocking=False)
            except Exception as e:
                logger.debug(f"TTS voice confirmation failed: {e}")
        else:
            self.ui.status("VOICE", "TTS not enabled")


    def _on_speed_hotkey(self) -> None:
        """F8 - Cycle TTS speed."""
        if self._voice_output:
            speed = self._voice_output.cycle_speed()
            self.ui.status("SPEED", f"{speed}x")
            try:
                self._voice_output.speak("Speed changed.", blocking=False)
            except Exception as e:
                logger.debug(f"TTS speed confirmation failed: {e}")
        else:
            self.ui.status("SPEED", "TTS not enabled")

    def save_bug_report(
        self,
        reason: str = "User Request",
        *,
        announce: bool = True,
    ) -> Optional["Path"]:
        """Save comprehensive bug report and return path.

        When ``announce`` is false, skip UI logging and only write the report.
        This is used by the TUI background worker so all widget updates stay on
        the Textual thread.
        """
        bug_dir = LOG_DIR / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bug_file = bug_dir / f"bug_{timestamp}.json"

        try:
            # Collect comprehensive debug info
            report = self._collect_debug_info()
            report["reason"] = reason

            with open(bug_file, "w") as f:
                json.dump(report, f, indent=2, default=str)

            # Make path clickable and copy a shareable link to clipboard
            file_url = f"file:///{str(bug_file).replace(chr(92), '/')}"
            clickable = f"\x1b]8;;{file_url}\x1b\\{bug_file}\x1b]8;;\x1b\\"

            clipboard_success = copy_to_clipboard(file_url)

            if announce:
                if clipboard_success:
                    self.ui.log(f"\n[BUG] Saved: {clickable}")
                    self.ui.log(f"[BUG] Link copied to clipboard: {file_url}\n")
                else:
                    self.ui.log(f"\n[BUG] Saved: {clickable}")
                    self.ui.log(f"[BUG] Link: {file_url}\n")

            return bug_file
        except Exception as e:
            if announce:
                self.ui.error(f"\n[BUG] Failed: {e}\n")
            logger.exception("Bug report failed")
            return None

    def _on_bug_report_hotkey(self) -> None:
        """F7 - Save comprehensive bug report and copy path to clipboard."""
        bug_path = self.save_bug_report("Hotkey F7")
        if bug_path:
            self.ui.log("[BUG] Path copied to clipboard. Paste it anywhere.")

    def take_screenshot_analysis(self) -> None:
        """Capture screen and request visual analysis (e.g. Mulligan).

        Tries local VLM first (fast, free), then falls back to online.
        """
        if self._screenshot_analysis_in_progress:
            self.ui.log("[yellow]Screenshot analysis already in progress...[/]")
            return

        self._screenshot_analysis_in_progress = True
        backend_lower = self.backend_name.lower()
        has_local_vlm = self._vision_mapper and hasattr(self._vision_mapper, '_local_vlm') and self._vision_mapper._local_vlm.available
        vision_capable = has_local_vlm or backend_lower == "online"

        if not vision_capable:
            self.ui.log("[red]No vision backend available. Need Ollama VLM or a cloud backend.[/]")
            self._screenshot_analysis_in_progress = False
            return

        try:
            from PIL import ImageGrab
            import io

            # Capture MTGA window if possible, otherwise full screen
            img = None
            if self._vision_mapper:
                window_rect = self._vision_mapper.window_rect
                if not window_rect:
                    window_rect = self._vision_mapper.refresh_window()
                if window_rect:
                    left, top, width, height = window_rect
                    img = ImageGrab.grab(bbox=(left, top, left + width, top + height))

            if img is None:
                img = ImageGrab.grab()

            img.thumbnail((1920, 1080))

            buf = io.BytesIO()
            img.save(buf, format='PNG')
            png_bytes = buf.getvalue()

            self.ui.log("[yellow]Analyzing screenshot...[/]")
            self.ui.subtask("Analyzing screenshot")

            # Context
            try:
                game_state = self.get_game_state()
            except Exception as e:
                logger.debug(f"Could not get game state for screenshot analysis: {e}")
                game_state = {}

            ctx = ""
            if game_state:
                turn_num = game_state.get('turn', {}).get('turn_number', '?')
                phase = game_state.get('turn', {}).get('phase', '')
                life_you = 20
                life_opp = 20
                for p in game_state.get('players', []):
                    if p.get('is_local'):
                        life_you = p.get('life_total', 20)
                    else:
                        life_opp = p.get('life_total', 20)
                ctx = f" Turn {turn_num}, {phase}. Life: You {life_you}, Opp {life_opp}."

            screen_prompt = (
                "You are an expert Magic: The Gathering Arena coach. "
                "Look at this screenshot and give immediate, actionable advice.\n\n"
                "DETECT THE SITUATION AND RESPOND:\n"
                "- Keep/Mulligan: Start with KEEP or MULLIGAN, then brief reason.\n"
                "- Scry: Say TOP or BOTTOM with one-sentence reasoning.\n"
                "- Surveil: Say GRAVEYARD or LIBRARY.\n"
                "- Modal/choice: Recommend which option and why.\n"
                "- Target selection: Say which target to pick.\n"
                "- Combat: Give optimal attacks/blocks.\n"
                "- Your turn: Recommend the best play.\n"
                "- Opponent acting: Note if you should respond.\n\n"
                "BE DECISIVE. 1-2 sentences max, spoken aloud.\n"
                f"Game context:{ctx}"
            )

            advice = None
            system_prompt = screen_prompt

            # Try the current backend's multimodal API if online
            if backend_lower == "online" and self._coach and self._coach._backend:
                self.ui.log("[yellow]Analyzing via online backend...[/]")
                try:
                    be = self._coach._backend
                    if hasattr(be, 'complete_with_image'):
                        advice = be.complete_with_image(
                            system_prompt,
                            f"What should I do here?{ctx}",
                            png_bytes,
                        )
                        if advice:
                            logger.info(f"[Online screen analysis] {len(advice)} chars")
                except Exception as e:
                    logger.error(f"Online screen analysis failed: {e}")
                    self.ui.log(f"[dim red]Online vision failed: {e}[/]")

            # Fall back to local VLM if online didn't produce advice
            if not advice and has_local_vlm:
                self.ui.log("[dim cyan]Falling back to local Ollama VLM...[/]")
                try:
                    import urllib.request
                    import base64

                    vlm = self._vision_mapper._local_vlm
                    b64_image = base64.b64encode(png_bytes).decode("utf-8")
                    payload = json.dumps({
                        "model": vlm.model,
                        "prompt": screen_prompt,
                        "images": [b64_image],
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 200},
                    }).encode("utf-8")
                    req = urllib.request.Request(
                        f"{vlm.endpoint}/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=vlm.timeout) as resp:
                        raw = json.loads(resp.read())
                    advice = raw.get("response", "").strip()
                    if advice:
                        logger.info(f"[Ollama screen analysis] {len(advice)} chars")
                except Exception as e:
                    logger.error(f"Ollama screen analysis failed: {e}")
                    self.ui.log(f"[dim red]Ollama VLM fallback failed: {e}[/]")

            # Detect garbled VLM output (non-vision model processing image tokens)
            if advice and self._is_garbled(advice):
                logger.warning(f"VLM returned garbled response ({len(advice)} chars), discarding")
                self.ui.log("[red]VLM returned garbled output — is the Ollama model a vision model?[/]")
                advice = None

            if advice and not advice.startswith("Error"):
                self.ui.advice(advice, "Visual Analysis")
                if self._auto_speak:
                    self.speak_advice(advice, blocking=False)
            else:
                self.ui.log(f"[red]Vision analysis failed: {advice or 'no response'}[/]")

        except ImportError:
            self.ui.log("[red]Missing 'Pillow' library. Install with: pip install Pillow[/]")
        except Exception as e:
            self.ui.log(f"[red]Screenshot error: {e}[/]")
            print(f"Screenshot Error: {e}")
        finally:
            self.ui.subtask("")
            self._screenshot_analysis_in_progress = False

    def _collect_debug_info(self) -> dict:
        """Collect comprehensive debug information for bug reports."""
        import platform
        from arenamcp import __version__

        report = {
            "timestamp": datetime.now().isoformat(),
            "version": __version__,

            # System info
            "system": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "machine": platform.machine(),
                "packages": self._get_package_versions(),
            },

            # Coach configuration
            "config": {
                "backend": self.backend_name,
                "model": self.model_name,
                "voice_mode": self.voice_mode,
                "advice_style": self.advice_style,
                "advice_frequency": self.advice_frequency,
                "draft_mode": self.draft_mode,
                "set_code": self.set_code,
                "auto_speak": self._auto_speak,
            },

            # Settings from disk
            "settings": dict(self.settings._data) if self.settings else {},

            # MTGA log file status
            "mtga_log": self._get_mtga_log_status(),

            # Current game state
            "game_state": self._mcp.get_game_state() if self._mcp else {},

            # Match context
            "match_context": self._get_match_context(),

            # Draft state if active
            "draft_state": self._mcp.get_draft_pack() if self._mcp else {},

            # Voice state
            "voice": {
                "tts_enabled": self._voice_output is not None,
                "tts_muted": self._voice_output._muted if self._voice_output else None,
                "tts_voice": self._voice_output.current_voice if self._voice_output else None,
                "stt_enabled": self._voice_input is not None,
            },

            # Recent advice history
            "advice_history": list(self._advice_history) if hasattr(self, '_advice_history') else [],

            # LLM context (what the coach sees)
            "llm_context": self._get_llm_context(),

            # Recent log entries (last 100 lines)
            "recent_logs": self._get_recent_logs(100),

            # Card enrichment failures (oracle text lookups that failed)
            "enrichment_failures": self._get_enrichment_failures(),

            # Autopilot state
            "autopilot": self._collect_autopilot_info(),

            # Bridge poller state (last poll result, connection, request type)
            "bridge_state": self._collect_bridge_state(),

            # Error state
            "errors": list(self._recent_errors) if hasattr(self, '_recent_errors') else [],

            # Uptime
            "uptime_seconds": (datetime.now() - self._start_time).total_seconds() if hasattr(self, '_start_time') else None,

            # BepInEx plugin log (for bridge debugging)
            "bepinex_log": self._read_bepinex_log(),

            # Replay recording state
            "replay": self._collect_replay_info(),
        }

        return report

    def _collect_autopilot_info(self) -> dict:
        """Collect autopilot state for bug reports."""
        ap = getattr(self, '_autopilot', None)
        info: dict = {
            "enabled": getattr(self, '_autopilot_enabled', False),
            "dry_run": getattr(self, '_autopilot_dry_run', False),
            "afk": getattr(self, '_autopilot_afk', False),
            "initialized": ap is not None,
        }
        if ap:
            try:
                info["engine"] = ap.get_debug_info()
            except Exception as e:
                info["engine_error"] = str(e)
        return info

    def _collect_bridge_state(self) -> dict:
        """Collect GRE bridge/poller state for bug reports."""
        bp = getattr(self, '_bridge_poller', None)
        if not bp:
            return {"available": False}
        try:
            info: dict = {
                "available": True,
                "connected": bp.connected,
                "bridge_connected": bp._bridge.connected if bp._bridge else False,
                "fallback_mode": bp._fallback_mode,
                "last_request_type": bp._last_request_type,
                "last_has_pending": bp._last_has_pending,
                "last_action_sig": bp._last_action_sig,
                "consecutive_errors": bp._consecutive_errors,
                "match_boundary_age_s": round(
                    time.time() - getattr(self, '_match_boundary_ts', 0), 1
                ),
            }
            # Include last poll result summary (actions count, request type)
            poll = bp._last_poll_result
            if poll:
                info["last_poll"] = {
                    "has_pending": poll.get("has_pending"),
                    "request_type": poll.get("request_type"),
                    "request_class": poll.get("request_class"),
                    "num_actions": len(poll.get("actions", [])),
                    "can_pass": poll.get("can_pass"),
                    "can_cancel": poll.get("can_cancel"),
                }
            else:
                info["last_poll"] = None
            return info
        except Exception as e:
            return {"available": True, "error": str(e)}

    def _enable_replay_recording(self) -> None:
        """Auto-enable MTGA replay recording at match start."""
        def _try_enable():
            import time
            # Bridge may not be connected yet at seat detection time —
            # retry for up to 5 seconds.
            for attempt in range(10):
                try:
                    bridge = self._bridge_poller._bridge if self._bridge_poller else None
                    if bridge and bridge.connected:
                        status = bridge.get_replay_status()
                        if status and status.get("recording"):
                            logger.debug("Replay recording already active")
                            return
                        result = bridge.enable_replay("mtgacoach")
                        if result:
                            folder = result.get("replay_folder", "unknown")
                            recording = result.get("recording", False)
                            recorder_type = result.get("recorder_type", "none")
                            logger.info(
                                f"Replay recording enabled: {folder} "
                                f"(recording={recording}, recorder={recorder_type})"
                            )
                        else:
                            logger.debug("enable_replay returned None (plugin may not support it)")
                        return
                except Exception as e:
                    logger.debug(f"Replay enable attempt {attempt + 1} failed: {e}")
                time.sleep(0.5)
            logger.debug("Replay enable: bridge never connected after 5s")

        import threading
        threading.Thread(target=_try_enable, daemon=True).start()

    def _collect_replay_info(self) -> dict:
        """Collect replay recording state for bug reports."""
        info: dict = {"available": False}
        try:
            bridge = self._bridge_poller._bridge if self._bridge_poller else None
            if not bridge or not bridge.connected:
                return info
            info["available"] = True
            status = bridge.get_replay_status()
            if status:
                info["recording"] = status.get("recording", False)
                info["recorder_found"] = status.get("recorder_found", False)
                info["recorder_type"] = status.get("recorder_type")
                info["replay_folder"] = status.get("replay_folder")
                info["replay_file"] = status.get("replay_file")
                info["message_count"] = status.get("message_count")
                info["prefs_enabled"] = status.get("prefs_enabled")
                if status.get("_debug_methods"):
                    info["_debug_methods"] = status.get("_debug_methods")
                if status.get("_debug_fields"):
                    info["_debug_fields"] = status.get("_debug_fields")
            replays = bridge.list_replays()
            if replays:
                replay_list = replays.get("replays", [])
                info["recent_replays"] = replay_list[:5]
                info["total_replays"] = len(replay_list)
                # Include path to most recent replay for easy attachment
                if replay_list:
                    info["latest_replay_path"] = replay_list[0].get("path")
        except Exception as e:
            info["error"] = str(e)
        return info

    @staticmethod
    def _get_package_versions() -> dict[str, str]:
        """Get versions of key installed packages."""
        import importlib

        versions: dict[str, str] = {}
        package_imports = {
            "textual": "textual",
            "openai": "openai",
            "mcp": "mcp",
            "watchdog": "watchdog",
            "requests": "requests",
            "faster_whisper": "faster_whisper",
            "kokoro-onnx": "kokoro_onnx",
            "anthropic": "anthropic",
            "google.genai": "google.genai",
        }
        for display_name, import_name in package_imports.items():
            try:
                mod = importlib.import_module(import_name)
                versions[display_name] = getattr(mod, "__version__", "installed")
            except ImportError:
                versions[display_name] = "not installed"
        return versions

    def _get_mtga_log_status(self) -> dict:
        """Get MTGA Player.log file status."""
        import os
        # Use the same path logic as watcher.py: LOCALAPPDATA (AppData\Local)
        # -> parent (AppData) -> LocalLow sibling
        _local_appdata = os.environ.get("LOCALAPPDATA", "")
        if _local_appdata:
            default_path = str(
                Path(os.path.dirname(_local_appdata)) / "LocalLow"
                / "Wizards Of The Coast" / "MTGA" / "Player.log"
            )
        else:
            default_path = ""
        log_path = os.environ.get("MTGA_LOG_PATH", default_path)
        result: dict = {"path": log_path}
        try:
            p = Path(log_path)
            result["exists"] = p.exists()
            if p.exists():
                stat = p.stat()
                result["size_bytes"] = stat.st_size
                result["last_modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except Exception as e:
            result["error"] = str(e)
        return result

    def _read_bepinex_log(self) -> Optional[str]:
        """Read BepInEx plugin log for bridge debugging."""
        try:
            import os
            # Standard MTGA install path
            candidates = [
                Path(os.environ.get("PROGRAMFILES", "C:\\Program Files"))
                / "Wizards of the Coast" / "MTGA" / "BepInEx" / "LogOutput.log",
            ]
            for p in candidates:
                if p.exists():
                    text = p.read_text(encoding="utf-8", errors="replace")
                    # Return last 4KB to keep report size reasonable
                    if len(text) > 4096:
                        return "...(truncated)...\n" + text[-4096:]
                    return text
        except Exception as e:
            return f"Error reading BepInEx log: {e}"
        return None

    def _get_match_context(self) -> dict:
        """Get match-level context: match ID, opponent cards, recent events."""
        ctx: dict = {}
        try:
            if not self._mcp:
                return ctx
            from arenamcp.server import game_state
            ctx["match_id"] = getattr(game_state, "match_id", None)
            ctx["local_seat_id"] = getattr(game_state, "local_seat_id", None)
            ctx["seat_source"] = game_state.get_seat_source_name() if hasattr(game_state, "get_seat_source_name") else None
            # Opponent played cards
            try:
                from arenamcp.server import get_opponent_played_cards
                opp = get_opponent_played_cards()
                ctx["opponent_played_cards"] = opp if opp else []
            except Exception as e:
                logger.debug(f"Could not get opponent played cards for bug report: {e}")
                ctx["opponent_played_cards"] = []
            # Recent game events
            snapshot = game_state.get_snapshot() if hasattr(game_state, "get_snapshot") else {}
            ctx["recent_events"] = snapshot.get("recent_events", [])[-20:]
            ctx["damage_taken"] = snapshot.get("damage_taken", {})
        except Exception as e:
            ctx["error"] = str(e)
        return ctx

    def _get_llm_context(self) -> dict:
        """Get the LLM context from the most recent advice (what the LLM actually saw).

        IMPORTANT: This captures the game state that was sent to the LLM during the last
        advice generation, NOT the current game state. This prevents timing bugs where
        the game state changes between advice generation and bug report generation.
        """
        context = {
            "system_prompt": None,
            "formatted_game_state": None,
        }

        try:
            if self._coach:
                context["system_prompt"] = getattr(self._coach, '_system_prompt', None)
                context["deck_strategy"] = getattr(self._coach, '_deck_strategy', None)

                # Use the game_context from the last advice_history entry instead of
                # regenerating it, to avoid timing issues where game state has changed
                if hasattr(self, '_advice_history') and self._advice_history:
                    context["formatted_game_state"] = self._advice_history[-1].get("game_context")
                # Fallback: if no advice history, generate from current state
                elif self._mcp and hasattr(self._coach, '_format_game_context'):
                    game_state = self._mcp.get_game_state()
                    context["formatted_game_state"] = self._coach._format_game_context(game_state)
        except Exception as e:
            context["error"] = str(e)

        return context

    def _get_enrichment_failures(self) -> list:
        """Get card enrichment (oracle text lookup) failures for bug reports."""
        try:
            from arenamcp.server import get_enrichment_failures
            return get_enrichment_failures()
        except Exception as e:
            logger.debug(f"Could not get enrichment failures: {e}")
            return []

    def _get_recent_logs(self, num_lines: int = 100) -> list:
        """Get recent log entries from standalone.log."""
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    # Read last N lines efficiently
                    lines = f.readlines()
                    return lines[-num_lines:]
        except Exception as e:
            return [f"Error reading logs: {e}"]
        return []

    def _record_advice(self, advice: str, trigger: str, game_context: str = None, game_state: dict = None) -> None:
        """Record advice for debug history with full game state.

        Args:
            advice: The advice text that was given
            trigger: What triggered this advice
            game_context: Pre-formatted context string (optional)
            game_state: Game state dict to use for context (optional, avoids re-polling)
        """
        if not hasattr(self, '_advice_history'):
            self._advice_history = []

        # Use provided game_state, or fetch fresh if needed
        # IMPORTANT: If game_state is provided, use it to avoid timing issues
        # where the game state changes between advice generation and recording
        if game_context is None and self._coach:
            try:
                if game_state is None and self._mcp:
                    game_state = self._mcp.get_game_state()
                if game_state and hasattr(self._coach, '_format_game_context'):
                    game_context = self._coach._format_game_context(game_state)
            except Exception as e:
                logger.debug(f"Could not format game context for advice record: {e}")

        # Extract key game state fields for bug reports (turn, life, board snapshot)
        game_snapshot = None
        if game_state:
            try:
                turn = game_state.get("turn", {})
                players = game_state.get("players", [])
                game_snapshot = {
                    "turn_number": turn.get("turn_number"),
                    "phase": turn.get("phase"),
                    "active_player": turn.get("active_player"),
                    "players": [
                        {"seat_id": p.get("seat_id"), "life_total": p.get("life_total")}
                        for p in players
                    ],
                    "battlefield_count": len(game_state.get("battlefield", [])),
                    "hand_count": len(game_state.get("hand", [])),
                }
            except Exception as e:
                logger.debug(f"Could not extract game snapshot for advice record: {e}")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger,
            "advice": advice,
            "game_context": game_context[:8000] if game_context else None,
            "game_snapshot": game_snapshot,
        }
        self._advice_history.append(entry)

        # Keep only last 50 entries (enough for post-match analysis)
        if len(self._advice_history) > 50:
            self._advice_history = self._advice_history[-50:]
        
        # Also record to match recording for post-match analysis
        try:
            from arenamcp.match_validator import get_current_recording
            current = get_current_recording()
            if current:
                # Extract turn/phase from game state
                turn_info = game_state.get("turn", {}) if game_state else {}
                parsed_turn = turn_info.get("turn_number", 0)
                parsed_phase = turn_info.get("phase", "")
                current.add_advice_event(
                    trigger=trigger,
                    advice=advice,
                    game_context=game_context or "",
                    parsed_turn=parsed_turn,
                    parsed_phase=parsed_phase
                )
        except Exception as e:
            logger.debug(f"Advice recording failed (non-fatal): {e}")

    def _record_error(self, error: str, context: str = None) -> None:
        """Record error for debug history."""
        if not hasattr(self, '_recent_errors'):
            self._recent_errors = []

        entry = {
            "timestamp": datetime.now().isoformat(),
            "error": error,
            "context": context,
        }
        self._recent_errors.append(entry)

        # Keep only last 10 errors
        if len(self._recent_errors) > 10:
            self._recent_errors = self._recent_errors[-10:]

    def _detect_match_result(self) -> str:
        """Detect win/loss from game state.

        Checks multiple sources in priority order:
        1. Persistent ``last_game_result`` field on GameState (survives reset)
        2. Pre-reset snapshot's ``last_game_result``
        3. ``recent_events`` (may be cleared by reset)

        Returns "win", "loss", "draw", or "unknown".
        """
        try:
            from arenamcp.server import game_state as gs

            # Preferred: persistent field set when game_end annotation fires
            if gs.last_game_result:
                return gs.last_game_result

            # Fallback 1: pre-reset snapshot
            if gs._pre_reset_snapshot:
                snap_result = gs._pre_reset_snapshot.get("last_game_result")
                if snap_result:
                    return snap_result

            # Fallback 2: scan recent_events from snapshot (live ones may be cleared)
            snapshot = gs._pre_reset_snapshot or {}
            recent = snapshot.get("recent_events", [])
            if not recent:
                # Try live recent_events
                game_state_dict = self._mcp.get_game_state()
                recent = game_state_dict.get("recent_events", [])
            for event in reversed(recent):
                if event.get("type") == "game_end":
                    return event.get("result", "unknown")
        except Exception as e:
            logger.debug(f"Could not detect match result: {e}")
        return "unknown"

    def _has_explicit_game_end_evidence(self) -> bool:
        """Return True only when GameState has explicit match-end evidence."""
        try:
            server_mod = importlib.import_module("arenamcp.server")
            gs = getattr(server_mod, "game_state", None)
            if gs is None:
                return False
            if gs.game_ended_event.is_set():
                return True
            if getattr(gs, "last_game_result", None):
                return True
            if getattr(gs, "_pre_reset_snapshot", None):
                return True
        except Exception as e:
            logger.debug(f"Could not inspect game-end evidence: {e}")
        return False

    def _post_match_analysis_worker(self) -> None:
        """Background worker: generate post-match strategic analysis.

        Spawned when a match ends. Uses a dedicated backend to avoid
        lock contention with real-time coaching.
        """
        try:
            advice_history = self._saved_advice_history
            match_result = self._last_match_result or "unknown"
            final_state = self._last_match_final_state

            if not advice_history:
                logger.info("No advice history for post-match analysis")
                return

            if not self._coach:
                logger.warning("No coach engine for post-match analysis")
                return

            logger.info(
                f"Post-match analysis started: {len(advice_history)} entries, result={match_result}"
            )
            self.ui.status("ANALYSIS", "Generating post-match analysis...")

            # Compute match duration from advice snapshots
            turns = [
                e.get("game_snapshot", {}).get("turn_number", 0)
                for e in advice_history
                if e.get("game_snapshot")
            ]
            match_duration = max(turns) if turns else 0

            # Extract final life totals
            final_life = {}
            if final_state:
                for p in final_state.get("players", []):
                    final_life[p.get("seat_id")] = p.get("life_total")

            # Extract opponent played cards (names from battlefield/graveyard)
            opponent_cards = []
            if final_state:
                opp_cards = final_state.get("opponent_played_cards", [])
                for card in opp_cards:
                    name = card.get("name") if isinstance(card, dict) else str(card)
                    if name:
                        opponent_cards.append(name)

            # Get deck strategy
            deck_strategy = getattr(self._coach, '_deck_strategy', "") or ""

            # Reuse the existing coaching backend — the match is over so
            # there's no lock contention risk, and the warm connection
            # avoids cold-start timeouts on the proxy server.
            analysis = self._coach.generate_post_match_analysis(
                advice_history=advice_history,
                match_result=match_result,
                match_duration_turns=match_duration,
                deck_strategy=deck_strategy,
                final_life_totals=final_life,
                opponent_played_cards=opponent_cards,
                missed_decisions=self._saved_missed_decisions,
            )

            if not analysis:
                logger.warning("Post-match analysis returned empty")
                self.ui.log("[yellow]Post-match analysis failed (timeout or empty response). "
                            "Try 'Analyze Match' button to retry.[/]")
                self.ui.status("ANALYSIS", "")
                # Don't clear saved data so manual retry can use it
                return

            # Split off the SPOKEN: summary for TTS
            spoken_summary = ""
            display_analysis = analysis
            if "SPOKEN:" in analysis:
                parts = analysis.rsplit("SPOKEN:", 1)
                display_analysis = parts[0].strip()
                spoken_summary = parts[1].strip()

            # Display in TUI
            result_label = (
                "VICTORY" if match_result == "win"
                else "DEFEAT" if match_result == "loss"
                else "DRAW" if match_result == "draw"
                else "MATCH ENDED"
            )
            self.ui.log("")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(f"[bold green]  POST-MATCH ANALYSIS — {result_label}[/]")
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log(display_analysis)
            self.ui.log(f"[bold cyan]{'═' * 50}[/]")
            self.ui.log("")

            self.ui.status("ANALYSIS", f"Match analysis complete ({match_result})")

            # Speak the short summary via TTS
            if spoken_summary:
                self.speak_advice(spoken_summary, blocking=False)

            logger.info(f"Post-match analysis complete: {len(analysis)} chars")

            # Store analysis for inclusion in /bugreport
            self._pending_post_match_analysis = display_analysis
            self._pending_post_match_result = match_result
            self.ui.log("[bold yellow]Type /bugreport to file coaching improvements to GitHub[/]")

            # Offer to file GH issue for missed decisions detected by vision watchdog
            self._offer_missed_decision_issue()

            # Clear saved data only on success
            self._saved_advice_history = []
            self._saved_missed_decisions = []
            self._last_match_result = None
            self._last_match_final_state = None

        except Exception as e:
            logger.error(f"Post-match analysis error: {e}", exc_info=True)
            self.ui.log(f"[red]Post-match analysis failed: {e}[/]")
            self.ui.status("ANALYSIS", "")

    def _offer_missed_decision_issue(self) -> None:
        """Offer to file a GH issue if vision detected missed decisions this match."""
        missed = self._saved_missed_decisions
        if not missed:
            return

        count = len(missed)
        self.ui.log("")
        self.ui.log(f"[bold yellow]Vision watchdog detected {count} missed decision point(s) this match.[/]")
        self.ui.log("[yellow]Filing GitHub issue with details...[/]")

        try:
            self._file_missed_decision_gh_issue(missed)
        except Exception as e:
            logger.error(f"Failed to file GH issue for missed decisions: {e}")
            self.ui.log(f"[red]Failed to file GH issue: {e}[/]")
            # Fall back to saving a local bug report
            self._save_missed_decisions_local(missed)

    def _file_missed_decision_gh_issue(self, missed: list[dict]) -> None:
        """File a GitHub issue with missed decision details."""
        # Build issue body
        lines = [
            "## Missed Decision Points Detected by Vision Watchdog",
            "",
            f"**Match date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"**Decisions detected:** {len(missed)}",
            "",
            "The vision watchdog detected game states where Arena was waiting for player input,",
            "but no predefined trigger fired. These represent gaps in the trigger/GRE message mapping.",
            "",
            "### Details",
            "",
        ]

        for i, d in enumerate(missed, 1):
            local_seat = d.get("local_seat", "?")
            active = d.get("active_player", "?")
            priority = d.get("priority_player", "?")
            is_ours = d.get("is_our_turn")
            turn_owner = "OURS" if is_ours else ("OPPONENT" if is_ours is False else "?")
            last_gre = d.get("last_gre_decision", "none")
            screenshot = d.get("screenshot", "")

            lines.append(f"#### Decision {i}")
            lines.append(f"- **Timestamp:** {d.get('timestamp', 'N/A')}")
            lines.append(f"- **Turn:** {d.get('turn', 'N/A')}")
            lines.append(f"- **Phase:** {d.get('phase', 'N/A')}")
            lines.append(f"- **Step:** {d.get('step', '')}")
            lines.append(f"- **Type:** {d.get('decision_type', 'N/A')}")
            lines.append(f"- **Prompt:** {d.get('prompt_text', 'N/A')}")
            lines.append(f"- **Confidence:** {d.get('confidence', 'N/A')}")
            lines.append(f"- **Stall duration:** {d.get('stall_duration_s', 'N/A')}s")
            lines.append(f"- **Avg tempo:** {d.get('avg_tempo_s', 'N/A')}s")
            lines.append(f"- **Num options:** {d.get('num_options', 'N/A')}")
            lines.append(f"- **Validation context:**")
            lines.append(f"  - Local seat: {local_seat} | Active player: {active} | Priority: {priority}")
            lines.append(f"  - Turn owner: **{turn_owner}** | We have priority: {priority == local_seat}")
            lines.append(f"  - Last cleared GRE decision: `{last_gre}`")
            if screenshot:
                lines.append(f"  - Screenshot: `~/.arenamcp/watchdog_screenshots/{screenshot}`")
            log_ctx = d.get("log_context", [])
            if log_ctx:
                # Show last 5 in GH issue (full 10 in local JSON sidecar)
                trimmed = log_ctx[-5:]
                lines.append(f"- **Game log ({len(trimmed)} of {len(log_ctx)} states before detection):**")
                lines.append("```")
                for log_line in trimmed:
                    lines.append(log_line)
                lines.append("```")
            lines.append("")

        lines.append("### Suggested Action")
        lines.append("")
        lines.append("Review each detection's validation context. Detections where turn owner is OPPONENT")
        lines.append("or where a GRE decision was recently cleared are likely **false positives**.")
        lines.append("True positives (our turn, our priority, no recent GRE decision) indicate gaps")
        lines.append("in the trigger/GRE message mapping.")
        lines.append("")
        lines.append("---")
        lines.append("*Auto-filed by mtgacoach vision watchdog*")

        body = "\n".join(lines)

        # Determine unique decision types for the title
        types = sorted(set(d.get("decision_type", "unknown") for d in missed))
        types_str = ", ".join(types[:3])
        if len(types) > 3:
            types_str += f" (+{len(types) - 3} more)"
        title = f"Missed decision points: {types_str}"
        if len(title) > 70:
            title = title[:67] + "..."

        result = subprocess.run(
            ["gh", "issue", "create",
             "--repo", "josharmour/mtgacoach",
             "--title", title,
             "--label", "bug,vision-watchdog",
             "--body", body],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            self.ui.log(f"[bold green]GitHub issue filed: {issue_url}[/]")
            logger.info(f"Filed GH issue for {len(missed)} missed decisions: {issue_url}")
        else:
            # Label might not exist yet — retry without labels
            if "label" in result.stderr.lower():
                result2 = subprocess.run(
                    ["gh", "issue", "create",
                     "--repo", "josharmour/mtgacoach",
                     "--title", title,
                     "--body", body],
                    capture_output=True, text=True, timeout=30,
                )
                if result2.returncode == 0:
                    issue_url = result2.stdout.strip()
                    self.ui.log(f"[bold green]GitHub issue filed: {issue_url}[/]")
                    logger.info(f"Filed GH issue for {len(missed)} missed decisions: {issue_url}")
                    return
            raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")

    def _save_missed_decisions_local(self, missed: list[dict]) -> None:
        """Save missed decisions to a local file as fallback when GH filing fails."""
        bug_dir = LOG_DIR / "bug_reports"
        bug_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = bug_dir / f"missed_decisions_{timestamp}.json"
        with open(path, "w") as f:
            json.dump({"missed_decisions": missed, "timestamp": timestamp}, f, indent=2)
        self.ui.log(f"[yellow]Saved missed decisions locally: {path}[/]")
        logger.info(f"Saved {len(missed)} missed decisions to {path}")

    def trigger_match_analysis(self) -> None:
        """Manually trigger post-match analysis (from Analyze Match button).

        Uses current advice history, or falls back to saved history from a
        previous failed analysis attempt (e.g. after timeout).
        """
        has_current = bool(self._advice_history)
        has_saved = bool(self._saved_advice_history)

        if not has_current and not has_saved:
            self.ui.log("[yellow]No advice history to analyze.[/]")
            return

        if has_saved and not has_current:
            # Retry with saved data from a previous failed analysis
            self.ui.log("[cyan]Retrying with saved match data...[/]")
        elif has_saved:
            self.ui.log("[yellow]Analysis already in progress...[/]")
            return
        else:
            self._saved_advice_history = list(self._advice_history)
            self._saved_missed_decisions = list(self._missed_decisions)
        self._last_match_result = self._detect_match_result()
        try:
            game_state = self._mcp.get_game_state()
            self._last_match_final_state = game_state
        except Exception as e:
            logger.debug(f"Could not capture final game state for post-match: {e}")
            self._last_match_final_state = None

        threading.Thread(
            target=self._post_match_analysis_worker,
            daemon=True,
        ).start()

    def _on_swap_seat_hotkey(self) -> None:
        """F8 - Swap local seat (fix wrong player detection)."""
        if not self._mcp:
            return

        try:
            from arenamcp.server import game_state
            # Get current state
            players = list(game_state.players.keys())
            current = game_state.local_seat_id

            if len(players) >= 2:
                # Swap to the other seat
                new_seat = [s for s in players if s != current][0] if current else players[0]
                # Use source=3 (User) to lock it
                game_state.set_local_seat_id(new_seat, source=3)
                self.ui.status("SEAT", f"Swapped to Seat {new_seat} (LOCKED - won't auto-change)")
                logger.info(f"Manual seat swap: {current} -> {new_seat} (locked by User)")
            else:
                self.ui.status("SEAT", f"Only {len(players)} player(s) detected, cannot swap")
        except Exception as e:
            self.ui.error(f"Seat swap failed: {e}")
            logger.error(f"Seat swap error: {e}")

    def _compute_library_summary(self, game_state: dict) -> str:
        """Compute remaining library by subtracting visible cards from deck_cards.

        Returns a compact summary like "~28 cards: 2x Mountain, 1x Lightning Bolt, ..."
        """
        deck_cards = game_state.get("deck_cards", [])
        if not deck_cards:
            return ""

        # Get local player seat
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        # Collect grp_ids of visible cards owned by local player
        visible_grp_ids = []
        for zone in ["hand", "battlefield", "graveyard", "exile", "stack", "command"]:
            for card in game_state.get(zone, []):
                if card.get("owner_seat_id") == local_seat:
                    grp_id = card.get("grp_id", 0)
                    if grp_id:
                        visible_grp_ids.append(grp_id)

        # Remove visible cards from deck list (handles duplicates correctly)
        remaining = list(deck_cards)
        for grp_id in visible_grp_ids:
            try:
                remaining.remove(grp_id)
            except ValueError:
                pass  # Card not in deck list (token, sideboard, etc.)

        if not remaining:
            return "~0 cards remaining"

        # Enrich grp_ids with card info (deduplicate lookups)
        basic_land_types = {"basic land"}
        card_info_cache: dict[int, dict] = {}
        name_counts: dict[str, int] = {}
        for grp_id in remaining:
            if grp_id not in card_info_cache:
                try:
                    card_info_cache[grp_id] = self._mcp.get_card_info(grp_id)
                except Exception as e:
                    logger.debug(f"Card info lookup failed for grp_id={grp_id} (remaining): {e}")
                    card_info_cache[grp_id] = {"name": f"Unknown({grp_id})"}
            name = card_info_cache[grp_id].get("name", f"Unknown({grp_id})")
            name_counts[name] = name_counts.get(name, 0) + 1

        # Sort by count descending, cap at top 15 unique cards
        sorted_cards = sorted(name_counts.items(), key=lambda x: -x[1])[:15]
        total = len(remaining)
        shown = sum(count for _, count in sorted_cards)

        # Build detailed summary with oracle text for non-basic lands
        # so the LLM doesn't hallucinate card abilities
        lines = [f"~{total} cards remaining in library:"]
        # Reverse map: name -> grp_id (for info lookup)
        name_to_grp: dict[str, int] = {}
        for grp_id, info in card_info_cache.items():
            name = info.get("name", "")
            if name not in name_to_grp:
                name_to_grp[name] = grp_id

        for name, count in sorted_cards:
            grp_id = name_to_grp.get(name)
            info = card_info_cache.get(grp_id, {}) if grp_id else {}
            type_line = info.get("type_line", "").lower()
            is_basic = any(bt in type_line for bt in basic_land_types)

            if is_basic:
                lines.append(f"  {count}x {name}")
            else:
                mana = info.get("mana_cost", "")
                oracle = info.get("oracle_text", "")
                detail = f"  {count}x {name}"
                if mana:
                    detail += f" {mana}"
                if oracle:
                    detail += f" — {oracle}"
                lines.append(detail)

        if shown < total:
            lines.append(f"  ... and {total - shown} more")

        return "\n".join(lines)

    def _has_tutor_in_hand(self, game_state: dict) -> bool:
        """Check if any card in hand is a tutor/search spell."""
        for card in game_state.get("hand", []):
            oracle = card.get("oracle_text", "").lower()
            if "search your library" in oracle:
                return True
        return False

    def _compute_tutor_library_targets(self, game_state: dict) -> str:
        """Compute library targets grouped by mana value for tutor spells.

        When a tutor/search spell is in hand, the LLM needs to know what
        creatures (and other cards) are available in the library and at
        what mana values, so it can recommend specific X values and targets.
        """
        import re

        deck_cards = game_state.get("deck_cards", [])
        if not deck_cards:
            return ""

        # Get local player seat
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        # Collect grp_ids of visible cards owned by local player
        visible_grp_ids = []
        for zone in ["hand", "battlefield", "graveyard", "exile", "stack", "command"]:
            for card in game_state.get(zone, []):
                if card.get("owner_seat_id") == local_seat:
                    grp_id = card.get("grp_id", 0)
                    if grp_id:
                        visible_grp_ids.append(grp_id)

        # Remove visible cards from deck list
        remaining = list(deck_cards)
        for grp_id in visible_grp_ids:
            try:
                remaining.remove(grp_id)
            except ValueError:
                pass

        if not remaining:
            return ""

        # Look up card info for remaining library cards
        card_info_cache: dict[int, dict] = {}
        for grp_id in remaining:
            if grp_id not in card_info_cache:
                try:
                    card_info_cache[grp_id] = self._mcp.get_card_info(grp_id)
                except Exception as e:
                    logger.debug(f"Card info lookup failed for grp_id={grp_id} (library): {e}")
                    card_info_cache[grp_id] = {"name": f"Unknown({grp_id})"}

        # Group non-land cards by CMC
        by_cmc: dict[int, list[str]] = {}
        for grp_id in remaining:
            info = card_info_cache.get(grp_id, {})
            type_line = info.get("type_line", "").lower()
            # Skip basic lands (not useful tutor targets)
            if "basic" in type_line and "land" in type_line:
                continue
            name = info.get("name", f"Unknown({grp_id})")
            mana_cost = info.get("mana_cost", "")

            # Calculate CMC
            cmc = 0
            if mana_cost:
                generic = re.findall(r"\{(\d+)\}", mana_cost)
                cmc += sum(int(g) for g in generic)
                for color in "WUBRGC":
                    cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", mana_cost)
                cmc += len(hybrid)

            # Build compact descriptor
            is_creature = "creature" in type_line
            power = info.get("power", "")
            toughness = info.get("toughness", "")
            pt = f" ({power}/{toughness})" if is_creature and power and toughness else ""

            # Type indicator for non-creatures
            type_tag = ""
            if not is_creature:
                if "instant" in type_line:
                    type_tag = " [instant]"
                elif "sorcery" in type_line:
                    type_tag = " [sorcery]"
                elif "enchantment" in type_line:
                    type_tag = " [enchant]"
                elif "artifact" in type_line:
                    type_tag = " [artifact]"
                elif "planeswalker" in type_line:
                    type_tag = " [PW]"
                elif "land" in type_line:
                    type_tag = " [land]"

            descriptor = f"{name}{pt}{type_tag}"
            if cmc not in by_cmc:
                by_cmc[cmc] = []
            # Avoid duplicate names at same CMC
            if descriptor not in by_cmc[cmc]:
                by_cmc[cmc].append(descriptor)

        if not by_cmc:
            return ""

        # Build compact summary grouped by CMC
        lines = [f"LIBRARY SEARCH TARGETS (~{len(remaining)} cards):"]
        for cmc in sorted(by_cmc.keys()):
            cards = by_cmc[cmc]
            lines.append(f"  MV {cmc}: {', '.join(cards)}")

        return "\n".join(lines)

    def _inject_library_summary_if_needed(self, game_state: dict) -> None:
        """If hand has a tutor/search spell, inject library targets into game_state."""
        if self._has_tutor_in_hand(game_state):
            try:
                summary = self._compute_tutor_library_targets(game_state)
                if summary:
                    game_state["library_summary"] = summary
            except Exception as e:
                logger.debug(f"Library summary computation failed: {e}")

    def _on_win_plan_hotkey(self, turns: int) -> None:
        """Handle win-in-N-turns hotkey press (keys 2-8)."""
        if not self._coach or not self._mcp:
            return

        # 5-second cooldown to prevent spam
        now = time.time()
        last = getattr(self, '_last_win_plan_time', 0.0)
        if now - last < 5.0:
            self.ui.status("WIN-PLAN", "Cooldown — wait a few seconds")
            return
        self._last_win_plan_time = now

        def _do():
            try:
                # Ensure latest log data is processed
                self._mcp.poll_log()
                game_state = self._mcp.get_game_state()

                turn_num = game_state.get("turn", {}).get("turn_number", 0)
                if turn_num <= 0:
                    self.ui.status("WIN-PLAN", "No active game")
                    logger.info("Win plan: no active game (turn=0)")
                    return

                self.ui.status("WIN-PLAN", f"Planning win in {turns} turns...")
                self.ui.log(f"\n[bold cyan]--- WIN-IN-{turns} PLAN (generating...) ---[/]")
                logger.info(f"Win plan: requesting {turns}-turn plan")

                library_summary = self._compute_library_summary(game_state)
                plan = self._coach.get_win_plan(game_state, turns, library_summary)

                logger.info(f"Win plan: got response, {len(plan)} chars")
                if plan:
                    self.ui.advice(plan, f"WIN-IN-{turns}")
                    self._record_advice(plan, f"win_in_{turns}", game_state=game_state)
                    self.speak_advice(plan, blocking=False)
                else:
                    self.ui.status("WIN-PLAN", "No plan generated (timeout or error)")
                    self.ui.log("[yellow]Win plan returned empty — API may have timed out[/]")
            except Exception as e:
                logger.error(f"Win plan error: {e}", exc_info=True)
                self.ui.error(f"Win plan failed: {e}")

        threading.Thread(target=_do, daemon=True).start()

    def _win_plan_worker(self, game_state: dict) -> None:
        """Background worker: compute win-in-2 and win-in-3 plans using a thinking model.

        Spawned automatically at the start of each of your turns. Uses a separate
        thinking-enabled backend so it doesn't interfere with real-time coaching.

        Parses VIABLE: YES/NO from the LLM response. Only stores viable plans
        and plays a sound alert. The plan is read aloud only on Ctrl+0 press.
        """
        import concurrent.futures

        try:
            # Lazy-init thinking model
            if self._thinking_model is None:
                from arenamcp.coach import pick_thinking_model
                self._thinking_model = pick_thinking_model()
                if self._thinking_model is None:
                    logger.info("No thinking model available — win plan worker disabled")
                    # Sentinel to avoid retrying every turn
                    self._thinking_model = ""
                    return
            if self._thinking_model == "":
                return  # Previously determined unavailable

            logger.info(f"Win plan worker started (thinking model: {self._thinking_model})")

            from arenamcp.coach import ProxyBackend
            thinking_backend = ProxyBackend(
                model=self._thinking_model, enable_thinking=True
            )

            library_summary = self._compute_library_summary(game_state)
            turn_num = game_state.get("turn", {}).get("turn_number", 0)

            # Submit win-in-2 and win-in-3 concurrently (no win-in-4)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            futures_ordered = []
            for n in (2, 3):
                f = executor.submit(
                    self._coach.get_win_plan,
                    game_state, n, library_summary,
                    backend=thinking_backend,
                )
                futures_ordered.append((n, f))

            # Process in order (prefer shortest viable plan: 2-turn over 3-turn)
            for n, future in futures_ordered:
                try:
                    plan = future.result()
                except Exception as e:
                    logger.warning(f"Win-in-{n} future failed: {e}")
                    continue

                if not plan or plan.startswith("Error"):
                    continue

                # Parse viability from first line
                first_line = plan.split("\n", 1)[0].strip()
                is_viable = first_line.upper().startswith("VIABLE: YES") or first_line.upper().startswith("VIABLE:YES")

                # Strip the VIABLE: line from the plan text
                if first_line.upper().startswith("VIABLE:"):
                    plan = plan.split("\n", 1)[1].strip() if "\n" in plan else ""

                if not is_viable:
                    logger.info(f"Win-in-{n} plan not viable, skipping")
                    continue

                # Staleness check: don't store if game has advanced >2 turns
                current_turn = 0
                try:
                    current_state = self._mcp.get_game_state()
                    current_turn = current_state.get("turn", {}).get("turn_number", 0)
                except Exception as e:
                    logger.debug(f"Could not check current turn for win plan staleness: {e}")
                if current_turn and current_turn - turn_num > 2:
                    logger.info(f"Win plan stale (started turn {turn_num}, now {current_turn})")
                    break

                logger.info(f"VIABLE win-in-{n} plan found ({len(plan)} chars)")

                # Store pending plan (no text output, no TTS — wait for Ctrl+0)
                self._pending_win_plan = plan
                self._pending_win_plan_turns = n
                self._pending_win_plan_turn = turn_num
                self._record_advice(plan, f"win_in_{n}", game_state=game_state)

                # Play ascending two-tone alert
                try:
                    from arenamcp.voice import play_beep
                    play_beep(frequency=1047, duration=0.12, volume=0.4)  # C6
                    time.sleep(0.08)
                    play_beep(frequency=1319, duration=0.12, volume=0.4)  # E6
                except Exception as e:
                    logger.debug(f"Win plan beep failed: {e}")

                self.ui.status("WIN-PLAN", f"WIN IN {n} FOUND — Ctrl+0 to hear")
                break  # First viable result wins

            executor.shutdown(wait=False)

            if hasattr(thinking_backend, 'close'):
                thinking_backend.close()

        except Exception as e:
            logger.error(f"Win plan worker error: {e}", exc_info=True)

    def _on_read_win_plan(self) -> None:
        """Numpad 0 — Read the pending win plan aloud via TTS."""
        plan = self._pending_win_plan
        if not plan:
            self.ui.status("WIN-PLAN", "No win plan available")
            return
        turns = self._pending_win_plan_turns
        self.ui.advice(plan, f"WIN-IN-{turns}")
        self.speak_advice(plan, blocking=False)
        # Clear pending state after reading
        self._pending_win_plan = None
        self.ui.status("WIN-PLAN", "")

    def _on_restart_hotkey(self) -> None:
        """F9 - Restart the coach."""
        self.ui.status("RESTART", "Restarting coach...")
        logger.info("F9 restart requested")
        self._restart_requested = True
        self._running = False

    def set_backend(self, provider: str, model: Optional[str] = None) -> None:
        """Explicitly set the backend provider.

        Fast path: when only the model changes (same provider), swap the model
        on the existing backend instead of recreating everything.
        """
        if self.draft_mode:
            self.ui.status("PROVIDER", "Not available in draft mode")
            return

        try:
            from arenamcp.coach import CoachEngine, create_backend

            same_provider = provider == self.backend_name and self._coach is not None
            old_backend = self._coach._backend if self._coach else None

            if same_provider and old_backend is not None:
                # Fast path: just swap the model on the existing backend.
                # Close persistent session so next call starts with new model.
                if hasattr(old_backend, 'close'):
                    old_backend.close()
                old_backend.model = model
                old_backend._turns = 0
                # Reset persistent-mode failure flag so new model gets a fresh try
                if hasattr(old_backend, '_persistent_failed'):
                    old_backend._persistent_failed = False
                actual_model = model or 'default'
            else:
                # Full switch: close old backend, create new one
                if old_backend and hasattr(old_backend, 'close'):
                    old_backend.close()
                progress_cb = self.ui.subtask if self.ui else None
                llm_backend = create_backend(provider, model=model, progress_callback=progress_cb)
                self._coach = CoachEngine(backend=llm_backend)
                actual_model = getattr(llm_backend, 'model', 'default')

                # Reconfigure voice input if needed
                if self._voice_input:
                    self._voice_input.transcription_enabled = True
                    logger.info("Voice transcription enabled: True")

            self.backend_name = provider
            self.model_name = model
            self.ui.status("PROVIDER", f"Switched to {provider.upper()} ({actual_model})")
            model_display = f"{provider}/{actual_model}" if actual_model else provider
            self.ui.status("MODEL", model_display)
            logger.info(f"Switched to {provider} backend, model: {actual_model}")
            self._consecutive_errors = 0  # Reset error counter on manual switch

            # Clear backend failure state — user explicitly chose a new provider
            if self._backend_failed:
                self._backend_failed = False
                self._original_backend = None
                self._original_model = None
                self._mark_backend_healthy()
                logger.info("Cleared backend failure state (user changed provider)")
        except Exception as e:
            self.ui.error(f"Failed to set provider {provider}: {e}")
            logger.error(f"Set provider error: {e}")
            logger.debug(traceback.format_exc())

    def fallback_to_local(self, reason: str = "") -> bool:
        """Fall back to local mode when online mode fails.

        Validates local endpoint before committing. Saves the original mode/model
        so ``set_backend`` can clear the error state.

        Returns True if fallback succeeded, False if no local backend available.
        """
        if self._backend_failed:
            return False

        if self.backend_name == "local":
            return False

        old_mode = self.backend_name
        old_model = self.model_name
        short_reason = (reason or "unknown error")[:180].replace("\n", " ")

        self._original_backend = old_mode
        self._original_model = old_model

        from arenamcp.backend_detect import DEFAULT_LOCAL_MODEL, detect_backends_quick
        detected = detect_backends_quick()

        if not detected.get("local"):
            self._backend_failed = True
            self.ui.log(
                f"\n[bold red]Online mode failed: {short_reason}[/]"
            )
            self.ui.log(
                "[bold yellow]No local fallback available. "
                "Configure a local model with /local, or fix online subscription.[/]\n"
            )
            self.ui.status("BACKEND", f"ERROR — online failed, no local fallback")
            logger.error(f"Local fallback unavailable: {reason}")
            return False

        try:
            from arenamcp.coach import CoachEngine, create_backend
            progress_cb = self.ui.subtask if self.ui else None
            llm_backend = create_backend("local", model=DEFAULT_LOCAL_MODEL, progress_callback=progress_cb)
            actual_model = getattr(llm_backend, 'model', DEFAULT_LOCAL_MODEL) or 'default'

            self._coach = CoachEngine(backend=llm_backend)
            self._backend_name = "local"
            self._model_name = DEFAULT_LOCAL_MODEL
            self._backend_failed = True
            self._consecutive_errors = 0

            self.ui.log(
                f"\n[bold red]Online mode failed: {short_reason}[/]"
            )
            self.ui.log(
                f"[bold yellow]Temporarily using local ({actual_model}). "
                f"Switch mode to retry online.[/]\n"
            )
            self.ui.status("BACKEND", f"LOCAL (temp) — online failed")
            self.ui.status("MODEL", f"local/{actual_model}")
            logger.info(f"Fallback to local from online: {reason}")
            return True
        except Exception as e:
            logger.debug(f"Local fallback failed: {e}")
            self._backend_failed = True
            self.ui.log(f"\n[bold red]Online failed and local fallback also failed: {e}[/]")
            self.ui.status("BACKEND", "ERROR — all backends failed")
            return False

    # Backward-compatible alias
    def fallback_to_ollama(self, reason: str = "") -> bool:
        return self.fallback_to_local(reason)

    def check_advice_for_backend_failure(self, advice: str) -> bool:
        """Check if an advice response indicates a backend auth/billing failure.

        Auth/billing errors (401, expired, credit) are deterministic — retrying
        won't help.  These trigger an immediate local fallback with a persistent
        error shown in the TUI.

        Transient errors (timeouts, rate limits) use a counter: after 3
        consecutive failures, fall back to a local backend.

        Returns True if a fallback was triggered.
        """
        if not advice:
            return False

        # Already in failed state — don't keep trying to fall back
        if self._backend_failed:
            return False

        from arenamcp.backend_detect import is_query_failure_retriable

        # Detect backend errors — either prefixed "Error …" from the backend wrapper,
        # or raw short error text (e.g. "Credit balance is too low") that the CLI
        # returns as normal assistant text.  The len<200 guard prevents false
        # positives on real advice that incidentally contains words like "account".
        if is_query_failure_retriable(advice) and (
            advice.startswith("Error") or len(advice) < 200
        ):
            self._report_backend_failure(advice)

            # Auth/billing errors are permanent — fall back immediately
            _AUTH_INDICATORS = (
                "401", "403", "authenticate", "unauthorized", "expired",
                "credit", "billing", "subscription", "api key", "not logged in",
            )
            advice_lower = advice.lower()
            is_auth_error = any(ind in advice_lower for ind in _AUTH_INDICATORS)

            if is_auth_error:
                logger.warning(f"Auth/billing failure — immediate Ollama fallback: {advice[:120]}")
                return self.fallback_to_ollama(reason=advice[:200])

            # Transient errors: count and fallback after threshold
            self._consecutive_errors = getattr(self, '_consecutive_errors', 0) + 1
            max_errors = getattr(self, '_max_errors_before_fallback', 3)
            logger.warning(
                f"Backend failure detected ({self._consecutive_errors}/{max_errors}): "
                f"{advice[:120]}"
            )

            if self._consecutive_errors >= max_errors:
                return self.fallback_to_ollama(reason=advice[:200])
        else:
            # Reset counter on successful response
            self._consecutive_errors = 0
            self._mark_backend_healthy()

        return False

    def _on_provider_cycle_hotkey(self) -> None:
        """F11 - Toggle between online and local mode."""
        if self.draft_mode:
            return

        new_mode = "local" if self.backend_name == "online" else "online"
        display_name = "Online" if new_mode == "online" else "Local"

        self.ui.log(f"\n[MODE] Switching to {display_name}...")
        self.set_backend(new_mode, None)
        # Invalidate cached model list
        self._model_list_for: Optional[str] = None
        self._model_list: list = []

    def _on_model_cycle_hotkey(self) -> None:
        """F12 - Cycle through models within the current mode."""
        if self.draft_mode:
            return

        from arenamcp.coach import get_models_for_mode

        mode = self.backend_name

        # Rebuild model list when mode changes
        if getattr(self, '_model_list_for', None) != mode:
            self._model_list = get_models_for_mode(mode)
            self._model_list_for = mode

        models = self._model_list
        if len(models) <= 1:
            self.ui.log(f"\n[MODEL] Only one model available for {mode}\n")
            return

        # Find current index
        current_idx = -1
        for i, (_, mid) in enumerate(models):
            if mid == self.model_name:
                current_idx = i
                break
        # If current model is None (default), match the None entry
        if current_idx == -1 and self.model_name is None:
            for i, (_, mid) in enumerate(models):
                if mid is None:
                    current_idx = i
                    break

        next_idx = (current_idx + 1) % len(models)
        display_name, new_model = models[next_idx]

        self.set_backend(provider, new_model)
        label = display_name if display_name != "Default" else "(default)"
        self.ui.log(f"\n[MODEL] {provider} -> {label}\n")


    def _on_style_toggle_hotkey(self) -> None:
        """F2 - Toggle advice style (Concise/Verbose)."""
        self.advice_style = "verbose" if self.advice_style == "concise" else "concise"
        self.ui.status("STYLE", self.advice_style.upper())
        self.ui.log(f"\n[STYLE] Changed to {self.advice_style.upper()}\n")

    def _on_frequency_toggle_hotkey(self) -> None:
        """F3 - Toggle advice frequency."""
        self.advice_frequency = "every_priority" if self.advice_frequency == "start_of_turn" else "start_of_turn"
        label = "EVERY PRIORITY" if self.advice_frequency == "every_priority" else "START OF TURN"
        self.ui.status("FREQ", label)
        self.ui.log(f"\n[FREQ] Changed to {label}\n")

    def _on_voice_cycle_hotkey(self) -> None:
        """F6 - Cycle TTS voice."""
        if self._voice_output:
            try:
                voice_id, desc = self._voice_output.next_voice(step=2)
                self.ui.status("VOICE_ID", desc)
                self.ui.log(f"\n[VOICE] Changed to: {desc}\n")
                self.ui.speak("Voice changed.")
            except Exception as e:
                self.ui.log(f"Error changing voice: {e}")

    def _reinit_coach(self):
        """Reinitialize the coach backend with current settings."""
        try:
            from arenamcp.coach import CoachEngine, create_backend
            llm_backend = create_backend(self.backend_name, model=self.model_name)
            self._coach = CoachEngine(backend=llm_backend)
            
            # Get actual model name for display if it was auto-selected
            actual_model = getattr(llm_backend, 'model', self.model_name or 'default')
            self.model_name = actual_model # Sync back
            
            # Configure voice input based on backend
            if self._voice_input:
                enable_transcription = True
                self._voice_input.transcription_enabled = enable_transcription
                logger.info(f"Voice transcription enabled: {enable_transcription} (Backend: {self.backend_name})")
            
            logger.info(f"Re-initialized {self.backend_name} backend, model: {actual_model}")
        except Exception as e:
            self.ui.log(f"\nbackend init failed: {e}\n")
            logger.error(f"Backend init error: {e}")

    def _register_hotkeys(self) -> None:
        """Register hotkeys."""
        if not self._register_keyboard:
            logger.info("Skipping global keyboard hotkey registration (TUI/Active Mode)")
            return

        if not keyboard:
            return
        try:
            keyboard.on_press_key("f2", lambda _: self._on_style_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f3", lambda _: self._on_frequency_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f5", lambda _: self._on_mute_hotkey(), suppress=False)
            keyboard.on_press_key("f6", lambda _: self._on_voice_cycle_hotkey(), suppress=False)
            keyboard.on_press_key("f7", lambda _: self._on_bug_report_hotkey(), suppress=False)
            keyboard.on_press_key("f8", lambda _: self._on_swap_seat_hotkey(), suppress=False)
            keyboard.on_press_key("f10", lambda _: self.run_speed_test(), suppress=False)
            keyboard.on_press_key("f11", lambda _: self._on_provider_cycle_hotkey(), suppress=False)
            keyboard.on_press_key("f12", lambda _: self._on_model_cycle_hotkey(), suppress=False)
            keyboard.add_hotkey("ctrl+0", lambda: self._on_read_win_plan(), suppress=False)
            logger.info("Hotkeys registered")
        except Exception as e:
            logger.warning(f"Hotkey registration failed: {e}")

    def _unregister_hotkeys(self) -> None:
        """Unregister hotkeys."""
        if keyboard:
            try:
                keyboard.unhook_all()
            except (ValueError, KeyError, Exception):
                pass  # Already unhooked or error

    def start(self) -> None:
        """Start the standalone coach."""
        logger.info(f"start() called: backend_name={self.backend_name}, model={self.model_name}, draft={self.draft_mode}")
        if self._running:
            logger.info("Already running, returning early")
            return

        self._running = True

        # Initialize components — emit progress to pipe so GUI shows what's happening
        self.ui.log("Initializing game state tracker...")
        self._init_mcp()
        self.ui.log("Initializing voice (background)...")
        self._init_voice()

        # Track actual model name for display
        actual_model = self.model_name

        if self.draft_mode:
            # Use MCP's built-in draft helper
            logger.info("Starting MCP draft helper...")
            result = self._mcp.start_draft_helper(self.set_code)
            logger.info(f"Draft helper: {result}")
        else:
            # Initialize LLM for coaching
            self.ui.log("Connecting to LLM backend...")
            self._init_llm()
            self.ui.log("LLM backend ready.")
            # Get actual model name from backend
            if self._coach and hasattr(self._coach, '_backend'):
                actual_model = getattr(self._coach._backend, 'model', self.model_name)

            # Initialize VisionMapper (shared: watchdog + autopilot)
            self.ui.log("Initializing vision mapper...")
            self._init_vision_mapper()

            # Initialize autopilot if enabled
            if self._autopilot_enabled:
                self._init_autopilot()

            # Start coaching and voice threads
            logger.info(f"Starting threads for backend: {self.backend_name}")
            logger.info("Starting PTT voice loop + coaching loop")
            self._coaching_thread = threading.Thread(
                target=self._coaching_loop, daemon=True, name="coaching"
            )
            self._coaching_thread.start()

            # Only launch voice thread if PTT/VOX is wanted
            if self._voice_mode in ("ptt", "vox"):
                self._voice_thread = threading.Thread(
                    target=self._voice_loop, daemon=True, name="voice"
                )
                self._voice_thread.start()

        self._register_hotkeys()

        # Print status
        _is_pipe = hasattr(self.ui, 'emit_game_state')

        if _is_pipe:
            # Pipe mode: minimal status, no TUI hotkey references
            self.ui.status("BACKEND", f"{self.backend_name} ({actual_model or 'default'})")
            self.ui.log("Waiting for MTGA...")
        else:
            # TUI/CLI mode: full banner with hotkeys
            self.ui.log("\n" + "="*50)
            if self.draft_mode:
                self.ui.log("MTGA DRAFT HELPER")
                self.ui.log("="*50)
                self.ui.log(f"Set: {self.set_code or 'auto-detect'}")
                self.ui.log("Using MCP server's draft evaluation")
            else:
                if self._autopilot_enabled:
                    mode = "DRY-RUN" if self._autopilot_dry_run else "LIVE"
                    afk = " AFK" if self._autopilot_afk else ""
                    self.ui.log(f"MTGA AUTOPILOT ({mode}{afk})")
                else:
                    self.ui.log("MTGA COACH")
                self.ui.log("="*50)
                self.ui.status("BACKEND", f"{self.backend_name} ({actual_model or 'default'})")
                self.ui.status("VOICE", f"PTT (F4) + Kokoro")
            self.ui.log("-"*50)
            self.ui.log("F5=mute F6=voice F7=bug F8=seat F9=restart F10=speed F12=model Num1=land")
            self.ui.log("="*50)
            self.ui.log("\nWaiting for MTGA...")
            self.ui.log("F8=swap seat if wrong | F9=restart coach\n")

    def stop(self) -> None:
        """Stop the coach and clean up all resources.

        This method ensures proper termination of all threads and resources:
        1. Signals threads to stop via _running flag
        2. Stops voice input/output
        3. Stops MCP server watcher
        4. Waits for threads to terminate
        """
        if not self._running:
            return

        logger.info("Stopping coach - beginning cleanup...")
        self._running = False

        # 0. Abort autopilot if active
        if self._autopilot:
            try:
                self._autopilot.on_abort()
            except Exception as e:
                logger.debug(f"Autopilot abort during cleanup failed: {e}")

        # 1. Unregister hotkeys first to prevent new events
        self._unregister_hotkeys()

        # 2. Stop voice input immediately (releases PTT hotkey, stops VOX stream)
        if self._voice_input:
            try:
                logger.debug("Stopping voice input...")
                self._voice_input.stop()
            except Exception as e:
                logger.debug(f"Voice input stop error (non-fatal): {e}")
            self._voice_input = None

        # 3. Stop voice output (TTS) - interrupts any playing audio
        if self._voice_output:
            try:
                logger.debug("Stopping voice output...")
                self._voice_output.stop()
            except Exception as e:
                logger.debug(f"Voice output stop error (non-fatal): {e}")
            self._voice_output = None

        # 4. Stop draft helper if active
        if self.draft_mode and self._mcp:
            try:
                logger.debug("Stopping draft helper...")
                self._mcp.stop_draft_helper()
            except Exception as e:
                logger.debug(f"Draft helper stop error (non-fatal): {e}")

        # 6. Stop MCP server's log watcher
        if self._mcp:
            try:
                logger.debug("Stopping MCP watcher...")
                from arenamcp.server import stop_watching
                stop_watching()
            except Exception as e:
                logger.debug(f"Watcher stop error (non-fatal): {e}")

        # 7. Wait for daemon threads to finish (with timeout)
        # These should exit quickly since _running is False
        if self._coaching_thread and self._coaching_thread.is_alive():
            logger.debug("Waiting for coaching thread...")
            self._coaching_thread.join(timeout=2.0)
            if self._coaching_thread.is_alive():
                logger.warning("Coaching thread did not terminate cleanly")
        self._coaching_thread = None

        if self._voice_thread and self._voice_thread.is_alive():
            logger.debug("Waiting for voice thread...")
            self._voice_thread.join(timeout=2.0)
            if self._voice_thread.is_alive():
                logger.warning("Voice thread did not terminate cleanly")
        self._voice_thread = None

        # 8. Clear references to allow garbage collection
        if self._coach and hasattr(self._coach, "_backend"):
            backend = self._coach._backend
            close_fn = getattr(backend, "close", None)
            if callable(close_fn):
                try:
                    logger.debug("Closing LLM backend...")
                    close_fn()
                except Exception as e:
                    logger.debug(f"Backend close error (non-fatal): {e}")

        self._mcp = None
        self._coach = None
        self._trigger = None

        logger.info("Coach stopped - cleanup complete")
        self.ui.log(f"\nStopped. Log: {LOG_FILE}")

    def run_speed_test(self):
        """Run latency test against all providers."""
        if not self.ui:
            return

        self.ui.log("\n[bold yellow]Running API Speed Test (3 passes)...[/]")
        
        # Define test cases: (Provider, Mode, Model Name)
        from arenamcp.coach import create_backend

        tests = [
            ("Online (default)", "online", None),
            ("Local (default)", "local", None),
        ]
        
        import time

        for name, mode, model_id in tests:
            try:
                self.ui.log(f"Testing {name}...")
                latencies = []

                # Init backend once
                backend = create_backend(mode, model=model_id)

                # Warmup / 3 passes
                for i in range(3):
                    start_req = time.perf_counter()
                    response = backend.complete("You are a helpful assistant.", "Say 'ok' and nothing else.")
                    req_ms = (time.perf_counter() - start_req) * 1000
                    
                    if response.startswith("Error"):
                        raise Exception(response)
                        
                    latencies.append(req_ms)
                    # Small delay between requests
                    time.sleep(0.1)

                avg_ms = sum(latencies) / len(latencies)
                min_ms = min(latencies)
                max_ms = max(latencies)
                
                self.ui.log(f"[green]PASS {name}: Avg {avg_ms:.0f}ms (Range: {min_ms:.0f}-{max_ms:.0f}ms)[/]")
                    
            except Exception as e:
                self.ui.log(f"[red]FAIL {name}: {e}[/]")

        self.ui.log("[bold yellow]Speed Test Complete.[/]\n")

    def run_forever(self) -> None:
        """Run until interrupted."""
        self.start()

        def signal_handler(sig, frame):
            print("\n\nShutting down...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        while self._running:
            time.sleep(1)


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="MTGA Coach - AI-powered game coaching via mtgacoach.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m arenamcp.standalone --backend online
  python -m arenamcp.standalone --backend local
  python -m arenamcp.standalone --draft --set MH3
        """
    )

    parser.add_argument("--backend", "-b",
                        choices=["auto", "online", "local"],
                        default=None, help="Backend mode (default: auto-detect)")
    parser.add_argument("--model", "-m", help="Model name override")
    parser.add_argument("--provider", help="(deprecated) Alias for --model")
    parser.add_argument("--voice", "-v", choices=["ptt", "vox"], default=None,
                        help="Voice input: ptt (F4) or vox (auto)")
    parser.add_argument("--draft", action="store_true",
                        help="Draft helper mode (no LLM needed)")
    parser.add_argument("--set", "-s", dest="set_code",
                        help="Set code for draft (e.g., MH3, BLB)")
    parser.add_argument("--autopilot", action="store_true",
                        help="Enable autopilot mode (AI plays via mouse clicks)")
    parser.add_argument("--afk", action="store_true",
                        help="Start in AFK mode (auto-pass all priority without LLM)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Autopilot dry run: plan actions but log instead of clicking")
    parser.add_argument("--show-log", action="store_true",
                        help="Show log file and exit")
    parser.add_argument("--language", "-l", default=None,
                        help="Language code for voice (e.g., en, nl, es, fr, de, ja)")
    parser.add_argument("--cli", action="store_true",
                        help="Run in legacy CLI mode (default is TUI)")
    parser.add_argument("--pipe", action="store_true",
                        help="Headless mode: JSON lines on stdout/stdin (for native GUI)")
    parser.add_argument("--diagnose", action="store_true",
                        help="Run diagnostic checks and exit")

    args = parser.parse_args()

    # --provider: deprecated alias for --model
    if args.provider and not args.model:
        args.model = args.provider

    # --language: persist to settings
    if args.language:
        settings = get_settings()
        settings.set("language", args.language)

    # --diagnose: run diagnostic checks and exit
    if args.diagnose:
        from arenamcp.diagnose import run_diagnostics
        sys.exit(run_diagnostics())

    # Pipe mode: headless JSON lines for native GUI frontend
    if args.pipe:
        from arenamcp.pipe_adapter import PipeAdapter
        pipe = PipeAdapter()
        coach = StandaloneCoach(
            backend=args.backend,
            model=args.model,
            voice_mode=args.voice,
            draft_mode=args.draft,
            set_code=args.set_code,
            ui_adapter=pipe,
            register_hotkeys=False,
            autopilot=args.autopilot,
            dry_run=args.dry_run,
            afk=getattr(args, 'afk', False),
        )
        pipe.bind_coach(coach)
        # Start stdin reader AFTER coach.start() is called inside run_forever()
        # to avoid a race where stdin EOF kills the coach before it starts.
        import threading
        def _delayed_stdin():
            # Wait for coach to be running before reading stdin
            for _ in range(50):
                if coach._running:
                    break
                time.sleep(0.1)
            pipe.start_stdin_reader()
        threading.Thread(target=_delayed_stdin, daemon=True).start()
        try:
            coach.run_forever()
        except KeyboardInterrupt:
            coach.stop()
        except Exception as e:
            logger.error(f"Fatal: {e}")
            pipe.error(str(e))
            sys.exit(1)
        return

    # Launch TUI unless CLI mode requested or show-log
    if not args.cli and not args.show_log:
        try:
            from arenamcp.tui import run_tui
            run_tui(args)
            return
        except ImportError as e:
            print(f"Failed to load TUI (install 'textual'): {e}")
            print("Falling back to CLI mode...")


    if args.show_log:
        print(f"Log: {LOG_FILE}")
        if LOG_FILE.exists():
            with open(LOG_FILE) as f:
                for line in f.readlines()[-30:]:
                    print(line, end='')
        return

    logger.info(f"Starting: backend={args.backend}, draft={args.draft}")

    while True:
        coach = StandaloneCoach(
            backend=args.backend,
            model=args.model,
            voice_mode=args.voice,
            draft_mode=args.draft,
            set_code=args.set_code,
            autopilot=args.autopilot,
            dry_run=args.dry_run,
            afk=getattr(args, 'afk', False),
        )

        try:
            coach.run_forever()
        except KeyboardInterrupt:
            coach.stop()
            break  # Exit on Ctrl+C
        except Exception as e:
            logger.error(f"Fatal: {e}")
            logger.debug(traceback.format_exc())
            print(f"\nError: {e}\nSee: {LOG_FILE}")
            sys.exit(1)

        # Check if restart was requested (F9)
        if coach._restart_requested:
            print("\n" + "="*50)
            print("RESTARTING...")
            print("="*50 + "\n")
            logger.info("Restarting coach...")
            continue
        else:
            break  # Normal exit


if __name__ == "__main__":
    main()
