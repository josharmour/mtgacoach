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
import contextlib
import logging
import os
import re
import signal
import subprocess  # noqa: F401  # test contract: monkeypatched via standalone.subprocess.Popen
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any

from arenamcp.backend_health import (
    BackendHealth,
    HealthState,
    check_gateway_health,
    is_backend_error_text,
    strip_health_tags,
)
from arenamcp.decision_arbiter import arbitrate
from arenamcp.logging_config import LOG_DIR, LOG_FILE, configure_logging
from arenamcp.settings import get_settings
from arenamcp.standalone_deck import _DeckAnalysisMixin
from arenamcp.standalone_diagnostics import _DiagnosticsMixin
from arenamcp.standalone_mcp import MCPClient
from arenamcp.standalone_postmatch import _PostMatchMixin
from arenamcp.standalone_hotkeys import _StandaloneHotkeysMixin
from arenamcp.standalone_tempo import _TempoTracker
from arenamcp.standalone_windows import _StandaloneWindowsMixin
from arenamcp.standalone_ui import CLIAdapter, UIAdapter
from arenamcp.standalone_voice import _PipeVoiceOutput, _probe_sounddevice_import, _SAPIVoice

# Configure logging (shared with server.py via logging_config)
# Console handler disabled -- GUI/pipe adapter handles user-facing output.
configure_logging(console=False)

WATCHDOG_SCREENSHOT_DIR = LOG_DIR / "watchdog_screenshots"
WATCHDOG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
WATCHDOG_SCREENSHOT_MAX = 20  # Keep last N screenshots (pruned at match end)

logger = logging.getLogger(__name__)


# Import dependencies
# The `keyboard` package must never be imported on macOS: its darwin backend
# calls abort() during import when the process lacks root/Accessibility
# rights, killing the interpreter before any except clause can run.
keyboard = None
if sys.platform != "darwin":
    try:
        import keyboard
    except ImportError:
        logger.warning("keyboard module not available - hotkeys disabled")
else:
    logger.info("keyboard hotkeys disabled on macOS (unsupported backend)")


class StandaloneCoach(
    _DeckAnalysisMixin,
    _PostMatchMixin,
    _DiagnosticsMixin,
    _StandaloneWindowsMixin,
    _StandaloneHotkeysMixin,
):
    """Standalone coaching app using MCP client + local LLM."""

    def __init__(
        self,
        backend: str = "proxy",
        model: str | None = None,
        voice_mode: str = "ptt",
        draft_mode: bool = False,
        set_code: str | None = None,
        ui_adapter: UIAdapter | None = None,
        register_hotkeys: bool = True,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
    ):
        self._register_keyboard = register_hotkeys

        # Load settings
        self.settings = get_settings()

        # Online-only: local mode was removed from the product (2026-06-11).
        # api.mtgacoach.com owns model routing; developer machines cycle
        # through its served models instead of pointing at local servers.
        requested_backend = backend or self.settings.get("mode", "online")
        if requested_backend not in (None, "online"):
            logger.info(f"Ignoring requested backend {requested_backend!r}: app is online-only")
        self._backend_name = "online"
        self._voice_mode = voice_mode or self.settings.get("voice_mode", "ptt")

        # Model resolution: only carry over saved model if it was saved for
        # online mode.
        if model:
            self._model_name = model
        else:
            saved_model = self.settings.get("model")
            saved_mode = self.settings.get("mode", "online")
            if saved_model and saved_mode == self._backend_name:
                self._model_name = saved_model
            else:
                self._model_name = None

        self.draft_mode = draft_mode
        self.set_code = set_code.upper() if set_code else None

        # Autopilot
        # If the caller didn't explicitly opt in via --autopilot, restore
        # the last saved state so a launcher restart doesn't silently
        # leave autopilot off (previously caused long "pauses" where
        # priority was on the user but nothing was acting on advice).
        saved_autopilot = bool(self.settings.get("autopilot_enabled", False))
        self._autopilot_enabled = bool(autopilot or saved_autopilot)
        self._autopilot_restored_from_settings = bool(saved_autopilot) and not bool(autopilot)
        self._autopilot_dry_run = dry_run
        self._autopilot_afk = afk
        self._autopilot: Any | None = None  # AutopilotEngine instance
        self._autopilot_backend: Any | None = None  # Separate LLM backend for autopilot

        # State
        # advice_style can be "quick" (terse, speakable) or "chatty" (longer,
        # explanatory). Legacy values "concise"/"verbose" are still accepted.
        self.advice_style = "quick"
        self._advice_frequency = self.settings.get("advice_frequency", "every_priority")
        self._auto_deck_strategy = bool(self.settings.get("auto_deck_strategy", True))
        self._auto_post_match_analysis = bool(self.settings.get("auto_post_match_analysis", False))

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
        self._mcp: MCPClient | None = None

        # Voice components
        self._voice_input = None
        self._voice_output = None

        # LLM backend
        self._coach = None
        self._trigger = None

        # Threads
        self._coaching_thread: threading.Thread | None = None
        self._voice_thread: threading.Thread | None = None

        # Background win plan
        self._win_plan_turn = 0  # Last turn a win plan was launched
        self._thinking_model = None  # Cached thinking model ID (lazy-init)
        self._pending_win_plan: str | None = None  # Stored viable plan text
        self._pending_win_plan_turns: int = 0  # N in "win-in-N"
        self._pending_win_plan_turn: int = 0  # Game turn when plan was generated

        # Match tracking for LLM context
        self._match_number: int = 0  # Incremented on each new match

        # Rolling in-match advice history (used for post-match analysis)
        self._advice_history: list[dict] = []

        # Post-match analysis
        # Bug B: per-match staging dict keyed by match_id string.
        # Value is {"advice_history", "missed_decisions", "result",
        #           "final_state", "replay_path"}.
        self._staged_analyses: dict[str, dict] = {}
        # Bug A: deep-copied snapshot from analysis-completion time,
        # plus the completed analysis text, for F7 bug reports.
        self._post_match_snapshot: dict | None = None
        self._post_match_analysis_text: str | None = None
        self._post_match_result: str | None = None
        self._game_end_handled: bool = False  # Prevents duplicate triggers
        self._match_boundary_ts: float = 0.0  # Suppress stale triggers after reset
        self._last_game_end_check_error: str = ""
        self._post_match_analysis_running: bool = False

        # Vision watchdog: tempo anomaly detection + missed decision tracking
        self._tempo_tracker = _TempoTracker()
        self._missed_decisions: list[dict] = []  # Accumulated per match
        self._vision_mapper: Any | None = None  # VisionMapper (shared with autopilot)
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
        self._original_backend: str | None = None
        self._original_model: str | None = None

        # Autopilot decision backstop: force decision triggers when parser noise
        # causes missed trigger edges after an executed action.
        self._last_forced_decision_sig: str | None = None
        self._last_forced_decision_ts: float = 0.0
        self._last_advised_decision_sig: str | None = None

        # Bridge decision poller: proactive decision detection via BepInEx plugin
        from arenamcp.gre_bridge import get_poller

        self._bridge_poller = get_poller()

    @staticmethod
    def _build_pending_decision_signature(game_state: dict[str, Any]) -> str | None:
        pending = str(game_state.get("pending_decision") or "").strip()
        if not pending:
            return None

        decision_context = game_state.get("decision_context") or {}
        decision_type = str(decision_context.get("type") or "").strip()
        source_card = str(decision_context.get("source_card") or "").strip()
        source_id = decision_context.get("source_id")
        option_cards = decision_context.get("option_cards") or []
        legal_actions = game_state.get("legal_actions") or []

        if pending == "Action Required":
            return f"{pending}|{decision_type}|{len(legal_actions)}"

        parts = [
            pending,
            decision_type,
            source_card,
            str(source_id or ""),
            "|".join(str(card) for card in option_cards[:8]),
            "|".join(str(action) for action in legal_actions[:8]),
        ]
        return "||".join(parts)

    def _queue_screenshot_analysis(self) -> None:
        """Run screenshot analysis in the background."""
        threading.Thread(
            target=self.take_screenshot_analysis,
            daemon=True,
        ).start()

    def _handle_unresolved_generic_selection(
        self,
        curr_state: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Refresh a generic selection prompt and kick off vision if needed.

        Returns the refreshed state plus a boolean indicating whether the
        current trigger batch should stop so the selection can be resolved
        before lower-priority triggers fire.
        """
        pending = curr_state.get("pending_decision") or ""
        decision_context = curr_state.get("decision_context") or {}
        decision_type = str(decision_context.get("type") or "").lower()
        if pending not in ("Group Selection", "Order Cards", "Select Cards"):
            return curr_state, False

        time.sleep(0.2)
        try:
            self._mcp.poll_log()
        except Exception as e:
            logger.debug(f"poll_log failed before generic selection refresh: {e}")
        try:
            curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
        except Exception as e:
            logger.debug(f"Failed to refresh generic selection state: {e}")

        pending = curr_state.get("pending_decision") or pending
        decision_context = curr_state.get("decision_context") or {}
        decision_type = str(decision_context.get("type") or "").lower()
        if decision_type in ("scry", "surveil"):
            logger.info(
                "Resolved generic selection to %s without vision",
                decision_type,
            )
            return curr_state, False

        logger.info(
            f"Generic selection still unresolved ({pending}/{decision_type or 'unknown'}) — triggering screenshot analysis"
        )
        self._queue_screenshot_analysis()
        return curr_state, True

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
    def _get_local_seat_from_state(game_state: dict[str, Any]) -> int | None:
        """Return the local seat id from a serialized snapshot, if known."""
        for player in game_state.get("players", []):
            if player.get("is_local"):
                return player.get("seat_id")
        return None

    def speak_advice(self, text: str, blocking: bool = True) -> None:
        """Speak advice using local Kokoro TTS."""
        if not text:
            return

        # TTS must never read health tags aloud — display text keeps them.
        text = strip_health_tags(text)
        if not text:
            return

        # Filter out passive calls from TTS (User Request): Wait, Pass, etc.
        if self._is_passive_advice(text):
            return

        # Pass narrations are spoken at most once per cooldown window — one
        # "no responses, passing" explanation is useful, five per opponent
        # turn are not.
        if self._is_pass_narration(text):
            now = time.time()
            if now - getattr(self, "_last_pass_narration_at", 0.0) < self._PASS_NARRATION_COOLDOWN_S:
                logger.info(f"Muting repeated pass narration: {text[:60]!r}")
                return
            self._last_pass_narration_at = now

        # Use local Kokoro TTS
        if self._voice_output:
            try:
                self._voice_output.speak(text, blocking=blocking)
            except Exception as e:
                logger.error(f"Kokoro TTS error: {e}")
                # Kokoro ONNX sometimes raises access violations on the first
                # synthesis after model load. Retry once — subsequent calls
                # typically succeed.
                try:
                    import time as _time

                    _time.sleep(0.1)
                    self._voice_output.speak(text, blocking=blocking)
                    logger.info("Kokoro speak retry succeeded")
                except Exception as e2:
                    logger.error(f"Kokoro speak retry also failed: {e2}")

    def _probe_backend_health_at_startup(self) -> None:
        """Probe the LLM gateway once at boot and wire health transitions to the UI.

        A dead gateway must be announced at startup — not discovered mid-match
        as a stream of tagged error advice.
        """
        try:
            backend = getattr(self._coach, "_backend", None) if self._coach else None
            if backend is None:
                return

            def _on_health_transition(snapshot: dict[str, Any]) -> None:
                try:
                    emit = getattr(self.ui, "emit_backend_health", None)
                    if callable(emit):
                        emit(snapshot)
                    logger.info(
                        f"Backend health transition: {snapshot.get('state')} — {snapshot.get('detail')}"
                    )
                except Exception as e:
                    logger.debug(f"Backend health transition emit failed: {e}")

            BackendHealth.instance().add_listener(_on_health_transition)

            # Only HTTP-style backends expose a probeable base URL.
            if not getattr(backend, "_base_url", None):
                logger.debug("Backend has no _base_url; skipping startup health probe")
                return

            state, detail = check_gateway_health(backend)
            logger.info(f"Backend health: {state.value.upper()} ({detail})")
            self.ui.log(f"Backend health: {state.value.upper()} ({detail})")
            if state is HealthState.DOWN:
                logger.warning(
                    "Backend health: DOWN — coaching will emit [BACKEND ERROR] until the gateway recovers"
                )
                with contextlib.suppress(Exception):
                    self.ui.log(
                        "[red]LLM gateway unreachable — coaching will show "
                        "[BACKEND ERROR] until it recovers.[/]"
                    )
            # Emit the boot snapshot so pipe-mode UIs get an initial state even
            # when no transition fired (probe OK from a fresh OK tracker).
            _on_health_transition(BackendHealth.instance().snapshot())
        except Exception as e:
            logger.debug(f"Startup backend health probe failed (non-fatal): {e}")

    def _emit_coach_game_plan(self) -> None:
        """Push the coach's structured game plan to the UI strategy card.

        Advice-mode counterpart of the autopilot's ui_game_plan_fn emission:
        deduped on payload, never raises into the coaching loop.
        """
        ui_fn = getattr(self.ui, "game_plan", None) if self.ui else None
        if not callable(ui_fn) or self._coach is None:
            return
        try:
            mgr = getattr(self._coach, "_game_plan_mgr", None)
            plan = getattr(mgr, "current", None) if mgr is not None else None
            payload = dict(plan.as_payload(), source="coach") if plan is not None else {}
            if payload != getattr(self, "_last_coach_game_plan", None):
                self._last_coach_game_plan = payload
                ui_fn(payload)
        except Exception as e:
            logger.debug(f"coach game-plan emit failed: {e}")

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @backend_name.setter
    def backend_name(self, value: str):
        self._backend_name = value
        self.settings.set("mode", value)

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @model_name.setter
    def model_name(self, value: str | None):
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

    def _init_mcp(self) -> None:
        """Initialize MCP client connection."""
        logger.info("Initializing MCP server...")
        self._mcp = MCPClient()

        # Warm local databases in the background so the UI becomes usable
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
        actual_model = getattr(llm_backend, "model", "unknown")

        # If auto-selected, update our backend_name to reflect the actual choice
        if requested == "auto":
            from arenamcp.backend_detect import auto_select_mode

            resolved_mode, _ = auto_select_mode()
            self._backend_name = resolved_mode
            self.settings.set("mode", "auto", save=False)  # Keep "auto" in settings
            self.ui.log(f"[bold green]Auto-detected mode: {resolved_mode} (model: {actual_model})[/]")

        logger.info(f"Created {self.backend_name} backend with model: {actual_model}")
        self._validate_model_on_launch()
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

    def _init_autopilot(self) -> None:
        """Initialize autopilot components (requires LLM backend + MCP)."""
        try:
            from arenamcp.action_planner import ActionPlanner
            from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
            from arenamcp.coach import create_backend
            from arenamcp.input_controller import InputController

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

            planner = ActionPlanner(
                autopilot_backend,
                timeout=config.planning_timeout,
                land_drop_first=config.land_drop_first,
            )

            # Reuse shared VisionMapper if available, otherwise fall back to static coords
            if self._vision_mapper:
                mapper = self._vision_mapper
            else:
                from arenamcp.screen_mapper import ScreenMapper

                mapper = ScreenMapper()
                self.ui.log("[yellow]Autopilot: using static coords (VisionMapper not available)[/]")

            controller = InputController(dry_run=self._autopilot_dry_run)

            self._autopilot = AutopilotEngine(
                planner=planner,
                mapper=mapper,
                controller=controller,
                get_game_state=self._mcp.get_game_state,
                config=config,
                speak_fn=self.speak_advice,
                ui_advice_fn=self.ui.advice if self.ui else None,
                bug_report_fn=self._auto_bug_report_bridge_fallback,
                ui_turn_plan_fn=(self.ui.turn_plan if self.ui and hasattr(self.ui, "turn_plan") else None),
                ui_game_plan_fn=(self.ui.game_plan if self.ui and hasattr(self.ui, "game_plan") else None),
            )
            # Give the autopilot a way to write into advice_history so
            # auto-handled decisions (auto-target, auto-pay, etc.) show
            # up in bug reports alongside LLM advice.
            self._autopilot._advice_recorder = self._record_advice

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

    def set_autopilot(self, enabled: bool) -> bool:
        """Idempotently set the autopilot state. Returns the resulting state.

        Unlike toggle_autopilot, repeated or raced calls converge on the
        requested state instead of flip-flopping (fable-improvements.md
        item 6 — live 2026-06-09: a state probe and a UI click
        double-toggled autopilot off mid-match).
        """
        currently_on = bool(self._autopilot_enabled and self._autopilot)
        if currently_on == bool(enabled):
            return currently_on
        return self.toggle_autopilot()

    def toggle_autopilot(self) -> bool:
        """Toggle autopilot on/off at runtime. Returns new enabled state."""
        if self._autopilot_enabled and self._autopilot:
            # Turn OFF: abort any in-flight plan, disable
            self._autopilot.on_abort()
            self._autopilot_enabled = False
            # Clean up the separate autopilot backend
            ap_backend = getattr(self, "_autopilot_backend", None)
            if ap_backend:
                if hasattr(ap_backend, "close"):
                    try:
                        ap_backend.close()
                    except Exception as e:
                        logger.debug(f"Autopilot backend close error: {e}")
                self._autopilot_backend = None
            logger.info("Autopilot toggled OFF")
            try:
                self.settings.set("autopilot_enabled", False)
            except Exception as e:
                logger.debug(f"Failed to persist autopilot_enabled=False: {e}")
            return False
        else:
            # Turn ON: initialize if needed, then enable
            if not self._autopilot:
                self._autopilot_dry_run = False
                self._autopilot_afk = False
                self._init_autopilot()
            if self._autopilot:
                # Reset lock only if the owner thread is gone. Force-releasing
                # a lock held by a live thread silently corrupts state.
                if self._autopilot._lock.locked():
                    owner_id = self._autopilot._lock_owner_thread_id
                    owner_alive = owner_id is not None and any(
                        t.ident == owner_id and t.is_alive() for t in threading.enumerate()
                    )
                    if owner_alive:
                        logger.error(
                            f"Autopilot: lock held by alive thread {owner_id} — "
                            f"refusing to force-release. Wait a few seconds or restart the coach."
                        )
                        self.ui.log(
                            "[red]Autopilot: previous session still running. "
                            "Try again shortly or restart the coach.[/]"
                        )
                        return False
                    logger.warning("Autopilot: lock owner thread is gone, safely resetting")
                    self._autopilot._release_lock()
                # Clear abort/skip/confirm events from previous session —
                # on_abort() sets _abort_event which persists across toggles
                # and causes process_trigger() to bail out immediately.
                self._autopilot._clear_events()
                self._autopilot_enabled = True
                logger.info("Autopilot toggled ON")
                try:
                    self.settings.set("autopilot_enabled", True)
                except Exception as e:
                    logger.debug(f"Failed to persist autopilot_enabled=True: {e}")
                return True
            else:
                logger.warning("Autopilot toggle failed: init unsuccessful")
                return False

    def _validate_model_on_launch(self) -> None:
        """Warn (and fall back) if the configured model isn't actually served.

        The gateway can advertise stale aliases; a saved model that no longer
        exists silently misroutes. Query the endpoint's live /v1/models and,
        if our model isn't there, reset to the server-assigned default.
        Best-effort: never blocks startup.
        """
        model = self.model_name
        if not model:
            return  # None == server-assigned default; always valid
        try:
            from arenamcp.coach import get_models_for_mode

            served = {mid for _label, mid in get_models_for_mode(self.backend_name) if mid}
            if served and model not in served:
                self.ui.log(
                    f"[yellow]Configured model '{model}' is not served by this "
                    f"endpoint — using the default. Available: "
                    f"{', '.join(sorted(served))}[/]"
                )
                logger.warning(
                    "Configured model %r not in served list %s — falling back to default",
                    model,
                    sorted(served),
                )
                self.model_name = None
                self.settings.set("model", None, save=True)
        except Exception as e:
            logger.debug("model validation skipped: %s", e)

    def _init_voice(self) -> None:
        """Initialize voice I/O components.

        Uses a subprocess probe to detect when sounddevice/PortAudio hangs
        during audio device enumeration (e.g. problematic ASIO/virtual drivers).
        """
        logger.info(f"_init_voice called, backend_name={self.backend_name}")

        # In pipe mode (native GUI), try Kokoro directly with a hard timeout.
        # If it hangs (numpy/PortAudio DLL issues), fall back to Windows SAPI.
        if hasattr(self.ui, "emit_game_state"):
            self.ui.log("Initializing TTS...")
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
                self._voice_output = _PipeVoiceOutput(self.ui, kokoro_result[0])
                voice_id, voice_desc = kokoro_result[0].current_voice
                logger.info(f"TTS voice (Kokoro): {voice_desc}")
                self.ui.status("VOICE", f"{voice_desc}")
                self.ui.log(f"TTS ready: {voice_desc}")
                # DO NOT warm up here — the ONNX Kokoro() constructor holds
                # the GIL for 10+ seconds during model loading, which blocks
                # the coaching thread from ever starting via threading.Thread.
                # Warmup is deferred until the coaching loop is running.
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

            # Warm up ONNX model in background so first speak() is fast
            threading.Thread(target=self._voice_output.warmup, daemon=True, name="tts-warmup").start()
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
            # Warm up ONNX model in background so first speak() is fast
            threading.Thread(target=self._voice_output.warmup, daemon=True, name="tts-warmup").start()
        except Exception as e:
            logger.error(f"Voice init failed: {e}")
            self.ui.status("VOICE", "TTS init failed")
            self.ui.log(f"TTS unavailable: {e}")

    def _emit_control_status_snapshot(self, actual_model: str | None) -> None:
        """Emit the current control-state snapshot for GUI frontends."""
        model_value = actual_model or self.model_name or "default"
        self.ui.status("MODEL", str(model_value))
        self.ui.status("STYLE", self.advice_style)
        self.ui.status("AUTOPILOT", "AP:ON" if self._autopilot_enabled else "AP:OFF")

        afk_enabled = self._autopilot_afk
        if self._autopilot is not None:
            afk_enabled = bool(getattr(self._autopilot, "_afk", afk_enabled))
        self.ui.status("AFK", "ON" if afk_enabled else "OFF")

        land_only = False
        if self._autopilot is not None:
            land_only = bool(getattr(self._autopilot, "_land_only", False))
        self.ui.status("LAND_ONLY", "ON" if land_only else "OFF")

        voice_output = self._voice_output
        if voice_output is not None:
            try:
                _voice_id, voice_desc = voice_output.current_voice
                self.ui.status("VOICE", voice_desc)
            except Exception:
                pass

            try:
                speed = float(voice_output.speed)
                self.ui.status("SPEED", f"{speed:.1f}x")
            except Exception:
                pass

            try:
                muted = bool(voice_output.muted)
                self.ui.status("MUTE", "Muted" if muted else "Unmuted")
            except Exception:
                pass

    # --- Urgency-aware polling intervals ---
    _POLL_BRIDGE = 0.15  # Bridge connected with pending decision (fast)
    _POLL_URGENT = 0.5  # Pending decision, mulligan, stack interaction
    _POLL_ACTIVE = 1.0  # Our turn with priority, combat phase
    _POLL_NORMAL = 1.5  # Opponent's turn, calm board state
    _POLL_IDLE = 2.5  # No active match

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

    def _emit_pipe_snapshots(
        self,
        *,
        game_state: dict[str, Any] | None = None,
        draft_state: dict[str, Any] | None = None,
    ) -> None:
        """Forward pipe-mode snapshots to the desktop frontend when available."""
        emit_game_state = getattr(self.ui, "emit_game_state", None)
        if callable(emit_game_state) and isinstance(game_state, dict):
            emit_game_state(game_state)

        emit_draft_state = getattr(self.ui, "emit_draft_state", None)
        if callable(emit_draft_state) and isinstance(draft_state, dict):
            emit_draft_state(draft_state)

        # Emit periodic heartbeat to desktop frontend
        now_ts = time.time()
        if now_ts - getattr(self, "_last_pipe_heartbeat_time", 0.0) >= 2.0:
            self._last_pipe_heartbeat_time = now_ts
            emit_hb = getattr(self.ui, "emit_heartbeat", None)
            if callable(emit_hb):
                emit_hb({"time": now_ts})

        emit_mcts_tree = getattr(self.ui, "emit_mcts_tree", None)
        if callable(emit_mcts_tree) and isinstance(game_state, dict) and game_state.get("turn"):
            turn = game_state.get("turn", {})
            sig = (
                turn.get("turn_number"),
                turn.get("phase"),
                turn.get("step"),
                turn.get("active_player"),
                len(game_state.get("hand", [])),
                len(game_state.get("battlefield", [])),
                len(game_state.get("stack", [])),
                game_state.get("pending_decision"),
            )
            if getattr(self, "_last_emitted_mcts_sig", None) != sig:
                self._last_emitted_mcts_sig = sig
                try:
                    from arenamcp.mcts_evaluator import MCTSEvaluator

                    mcts_eval = MCTSEvaluator.evaluate(game_state)
                    if mcts_eval:
                        emit_mcts_tree(mcts_eval.to_dict())
                except Exception as exc:
                    logger.debug(f"emit_mcts_tree failed: {exc}")

    def _reconcile_wedged_target(self, curr_state: dict[str, Any]) -> bool:
        """Reconcile a bridge-idle reading against a wedged target decision.

        The arbiter drops ``decision_required`` when the bridge is connected
        and idle (the 2026-06-09 ghost-decision guard). But if the snapshot
        shows a target/selection decision whose source spell is STILL on the
        stack, "idle" may be a premature "cleared" from the plugin — the
        multi-target wedge (Sheltered by Ghosts / Shardmage's Rescue), where
        an Aura sits on the stack waiting for a target the bridge thinks it
        already submitted.

        Force ONE fresh live poll. If MTGA now reports a pending request, let
        the trigger through (the overlay merely lagged). If the live poll is
        also idle while the spell is still stuck, surface a manual-required
        notice ONCE per wedged spell, then keep suppressing so we don't spam
        autopilot/TTS.

        Returns True to DROP the trigger (suppress), False to LET IT THROUGH.
        """
        if not hasattr(self, "_wedge_source"):
            self._wedge_source: int | None = None
            self._wedge_since: float = 0.0
            self._wedge_notified: bool = False

        ctx = curr_state.get("decision_context") or {}
        pending = str(curr_state.get("pending_decision") or "")
        is_target_wait = (
            str(ctx.get("type") or "") in ("target_selection", "select_n", "search")
            or "Target" in pending
            or "Select" in pending
        )
        if not is_target_wait:
            return True  # nothing target-shaped to reconcile; drop as arbitered

        try:
            source_id = int(ctx.get("source_id") or 0)
        except (TypeError, ValueError):
            source_id = 0
        stack_ids = {
            int(c.get("instance_id") or 0) for c in (curr_state.get("stack") or []) if isinstance(c, dict)
        }
        if not source_id or source_id not in stack_ids:
            # Source already resolved / left the stack → genuinely stale.
            self._wedge_source = None
            self._wedge_notified = False
            return True

        # Snapshot overlay may lag MTGA — force a fresh live poll.
        bridge = getattr(self._bridge_poller, "_bridge", None)
        if bridge is not None:
            try:
                live = bridge.get_pending_actions()
            except Exception as e:
                logger.debug(f"wedge reconcile live poll failed: {e}")
                live = None
            if live and live.get("has_pending"):
                logger.info(
                    "Arbiter reconcile: live bridge poll shows a pending request "
                    "— letting decision_required through"
                )
                return False

        # Genuine wedge: spell stuck on the stack, bridge can't see the request.
        now = time.time()
        if self._wedge_source != source_id:
            self._wedge_source = source_id
            self._wedge_since = now
            self._wedge_notified = False
        if (now - self._wedge_since) > 2.5 and not self._wedge_notified:
            self._wedge_notified = True
            name = str(ctx.get("source_card") or "a spell")
            logger.warning(
                "Autopilot wedged: %s is waiting for a target the bridge can't submit — manual action needed",
                name,
            )
            with contextlib.suppress(Exception):
                self.ui.status("MANUAL", f"Select target for {name} in MTGA")
        return True

    def _coaching_loop(self) -> None:
        """Poll MCP for game state and provide coaching, with auto-draft detection."""
        logger.info("Coaching loop started")

        # Now that the coaching loop is running, we can optionally kick off
        # TTS warmup in a background thread. On this machine the Kokoro ONNX
        # load can still monopolize the GIL for long stretches even from a
        # worker thread, which stalls the live coaching loop after its first
        # iteration and leaves the GUI stuck on "waiting for MTGA".
        #
        # Keep eager warmup for the legacy in-process UI, but skip it for the
        # pipe/desktop frontend where coaching responsiveness matters more than
        # hiding first-speech latency.
        if self._voice_output and hasattr(self._voice_output, "warmup"):
            if hasattr(self.ui, "emit_game_state"):
                logger.info("Skipping eager TTS warmup in pipe mode to keep coaching loop responsive")
            else:
                threading.Thread(target=self._voice_output.warmup, daemon=True, name="tts-warmup").start()

        prev_state: dict[str, Any] = {}
        seat_announced = False
        last_priority_log_signature = None
        last_priority_state_line = ""
        last_priority_progress_note = "startup"
        last_actionable_window_signature = None
        last_actionable_window_started_at = 0.0
        last_actionable_window_log_at = 0.0
        actionable_window_stall_seconds = 4.0

        last_advice_turn = 0
        last_advice_phase = ""
        _hb_consecutive_failures = 0
        # Critical triggers that always fire regardless of frequency setting
        # Combat triggers removed - too noisy for "start_of_turn" mode
        # decision_required added - scry, discard, target choices need immediate advice
        CRITICAL_PRIORITY = {
            "stack_spell",
            "stack_spell_yours",
            "stack_spell_opponent",
            "low_life",
            "opponent_low_life",
            "decision_required",
            "threat_detected",
            "losing_badly",
        }

        # Match ID tracking — reset coaching state when match changes
        last_match_id = None

        # Draft/Sealed detection state
        in_draft_mode = False
        in_sealed_mode = False
        sealed_analyzed = False
        last_draft_pack = 0
        last_draft_pick = 0
        last_active_draft_at = 0.0
        draft_inactive_grace_seconds = 5.0

        while self._running:
            try:
                # Poll for new log content (watchdog backup - Windows often misses events)
                self._mcp.poll_log()

                # Emit a game_state heartbeat EVERY iteration so the UI panel
                # stays in sync, even when the loop later `continue`s out
                # (draft path, stale-advice retry, etc.) and skips the
                # bottom-of-loop emit. Without this, the Game State panel
                # gets stuck on its initial placeholder while advice still
                # flows through the LLM-based events.
                try:
                    hb_state = self._mcp.get_game_state()
                    if isinstance(hb_state, dict):
                        _hb_consecutive_failures = 0
                        self._emit_pipe_snapshots(game_state=hb_state)
                except Exception:
                    _hb_consecutive_failures += 1
                    if _hb_consecutive_failures >= 5:
                        logger.warning(
                            "MCP heartbeat failed %d consecutive times — "
                            "coach is blind (log parsing or MCP may be down)",
                            _hb_consecutive_failures,
                        )

                # Poll card positions from the bridge and forward to the UI
                # match overlay. The UI no longer runs its own GREBridge
                # instance (two servers on the same pipe name was causing
                # constant disconnect/reconnect loops), so this relay is
                # how the overlay gets ground-truth card coords.
                try:
                    emit_cp = getattr(self.ui, "emit_card_positions", None)
                    if callable(emit_cp):
                        from arenamcp.gre_bridge import get_bridge

                        bridge = get_bridge()
                        if bridge and bridge.connected:
                            positions = bridge.get_card_positions()
                            if positions:
                                emit_cp(positions)
                except Exception as e:
                    logger.debug(f"card positions emit failed: {e}")

                # Check for active draft/sealed first
                draft_pack = self._mcp.get_draft_pack()
                self._emit_pipe_snapshots(draft_state=draft_pack)

                if draft_pack.get("is_active"):
                    pack_num = draft_pack.get("pack_number", 0)
                    pick_num = draft_pack.get("pick_number", 0)
                    is_sealed = draft_pack.get("is_sealed", False)

                    # Don't reset the active-draft timer on stale pack ghosts.
                    # Once the draft ends, MTGA stops sending events but the old
                    # pack data lives in is_active = True forever. Without this
                    # guard the timer resets every loop and the draft-ended
                    # detection at line ~2135 never fires — meaning the post-draft
                    # pool analysis and deck suggestion are silently skipped.
                    if not (pack_num == last_draft_pack and pick_num == last_draft_pick and in_draft_mode):
                        last_active_draft_at = time.time()

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
                                    wr = f"{e['gih_wr'] * 100:.0f}%" if e.get("gih_wr") else "N/A"
                                    reasons = ", ".join(e.get("all_reasons", []))
                                    detail_parts.append(
                                        f"  {e['name']}: score={e['score']:.0f} WR={wr} [{reasons}]"
                                    )
                                detail_log = "\n".join(detail_parts)
                                self.ui.log(
                                    f"\n[DRAFT P{pack_num}P{pick_num}] ({picked} picked)\n{detail_log}\n"
                                )
                                logger.info(f"DRAFT: P{pack_num}P{pick_num} - {advice}")
                                self.speak_advice(advice)
                                last_draft_pack = pack_num
                                last_draft_pick = pick_num
                            elif eval_result.get("is_active"):
                                self.ui.log(f"\n[DRAFT P{pack_num}P{pick_num}] No evaluated picks\n")
                                logger.warning(
                                    f"Draft eval returned no evaluations for P{pack_num}P{pick_num}"
                                )
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

                self._emit_pipe_snapshots(game_state=curr_state, draft_state=draft_pack)

                turn = curr_state.get("turn", {})
                turn_num = turn.get("turn_number", 0)
                phase = turn.get("phase", "")
                curr_match_id = curr_state.get("match_id")
                step = turn.get("step", "")
                active_player = turn.get("active_player", 0)
                priority_player = turn.get("priority_player", 0)
                local_seat = self._get_local_seat_from_state(curr_state)
                pending_decision = str(curr_state.get("pending_decision") or "").strip()
                legal_actions = curr_state.get("legal_actions", []) or []
                priority_owner = "YOU" if local_seat and priority_player == local_seat else "OPP"
                active_owner = "YOU" if local_seat and active_player == local_seat else "OPP"
                priority_signature = (
                    curr_match_id,
                    turn_num,
                    phase,
                    step,
                    active_player,
                    priority_player,
                    local_seat,
                    pending_decision,
                    len(legal_actions),
                )
                pending_suffix = f" | decision={pending_decision}" if pending_decision else ""
                state_line = (
                    f"T{turn_num} {phase}/{step or '-'} | active={active_owner}({active_player}) "
                    f"| priority={priority_owner}({priority_player}) | legal={len(legal_actions)}"
                    f"{pending_suffix}"
                )
                if turn_num > 0 and priority_signature != last_priority_log_signature:
                    self.ui.log(f"[STATE] {state_line}")
                    last_priority_log_signature = priority_signature
                    last_priority_state_line = state_line

                if turn_num > 0 and self._has_actionable_priority_window(curr_state):
                    actionable_window_signature = (
                        priority_signature,
                        tuple(str(action or "") for action in legal_actions[:8]),
                    )
                    now = time.time()
                    if actionable_window_signature != last_actionable_window_signature:
                        last_actionable_window_signature = actionable_window_signature
                        last_actionable_window_started_at = now
                        last_actionable_window_log_at = 0.0
                    else:
                        elapsed = now - last_actionable_window_started_at
                        if (
                            elapsed >= actionable_window_stall_seconds
                            and now - last_actionable_window_log_at >= actionable_window_stall_seconds
                        ):
                            window_summary = self._summarize_actionable_window(curr_state)
                            self.ui.log(
                                "[STUCK?] "
                                f"priority still YOU for {elapsed:.1f}s | {last_priority_state_line or state_line} "
                                f"| window={window_summary} | last={last_priority_progress_note}"
                            )
                            last_actionable_window_log_at = now
                else:
                    last_actionable_window_signature = None
                    last_actionable_window_started_at = 0.0
                    last_actionable_window_log_at = 0.0

                # Resolve unknown cards via VLM (only when using a local VLM backend)
                # Skip entirely for cloud backends like Azure — the card DB
                # or Scryfall fallback handles unknown cards without VLM.
                if (
                    turn_num > 0
                    and self._vision_mapper
                    and self.backend_name == "local"
                    and not getattr(self, "_vlm_resolve_in_progress", False)
                ):
                    self._vlm_resolve_in_progress = True

                    def _bg_resolve(state):
                        try:
                            self._resolve_unknown_cards(state)
                        except Exception as exc:
                            logger.debug(f"VLM card resolution error: {exc}")
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
                            logger.info(f"Game ended (event signal): {game_result}")
                            # Use pre-reset snapshot (full final state) if available,
                            # otherwise fall back to current (already-reset) state
                            self._stage_post_match_analysis(
                                match_result=game_result,
                                final_state=snapshot or dict(curr_state),
                                replay_path=self._get_latest_replay_path(),
                                reason="event-signal",
                            )
                            try:
                                from arenamcp.match_packets import stop_match_packet

                                packet = stop_match_packet()
                                if packet:
                                    packet.result = game_result
                                    packet.replay_path = self._get_latest_replay_path()
                                    if self._coach:
                                        packet.deck_strategy = self._coach._deck_strategy
                                    if packet.replay_path:
                                        try:
                                            from arenamcp.match_history import parse_replay_cosmetics

                                            cosmetics = parse_replay_cosmetics(packet.replay_path)
                                            if cosmetics:
                                                # Header shape: {Local, Opponent:{ScreenName,...}, BattlefieldId}
                                                packet.opponent_name = (cosmetics.get("Opponent") or {}).get(
                                                    "ScreenName"
                                                )
                                        except Exception:
                                            pass
                                    packet.save()
                            except Exception as e:
                                logger.warning(f"Failed to save match packet on event-signal: {e}")
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
                    logger.info(
                        f"Match boundary detected ({last_match_id} -> {curr_match_id}), match #{self._match_number}, resetting coaching state"
                    )

                    # New game state can name dynamic grpIds the previous
                    # one couldn't — allow the bridge to re-ask.
                    try:
                        from arenamcp import dynamic_cards

                        dynamic_cards.reset_asked()
                    except Exception:
                        pass

                    # Trigger analysis if game_end detection above missed it
                    if self._advice_history and not self._game_end_handled:
                        self._game_end_handled = True
                        self._stage_post_match_analysis(
                            match_result=self._detect_match_result(),
                            final_state=dict(prev_state) if prev_state else None,
                            replay_path=self._get_latest_replay_path(),
                            reason="match-boundary",
                        )
                    try:
                        from arenamcp.match_packets import stop_match_packet

                        packet = stop_match_packet()
                        if packet:
                            packet.result = self._detect_match_result() or "unknown"
                            packet.replay_path = self._get_latest_replay_path()
                            if self._coach:
                                packet.deck_strategy = self._coach._deck_strategy
                            if packet.replay_path:
                                try:
                                    from arenamcp.match_history import parse_replay_cosmetics

                                    cosmetics = parse_replay_cosmetics(packet.replay_path)
                                    if cosmetics:
                                        # Header shape: {Local, Opponent:{ScreenName,...}, BattlefieldId}
                                        packet.opponent_name = (cosmetics.get("Opponent") or {}).get(
                                            "ScreenName"
                                        )
                                except Exception:
                                    pass
                            packet.save()
                    except Exception as e:
                        logger.warning(f"Failed to save match packet on match-boundary: {e}")

                    prev_state = {}
                    last_advice_turn = 0
                    last_advice_phase = ""
                    seat_announced = False
                    self._advice_history = []
                    self._deck_analyzed = False
                    self._game_end_handled = False
                    # Note: _post_match_analysis_text is NOT cleared here —
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
                    if curr_match_id is not None:
                        try:
                            from arenamcp.match_packets import start_match_packet

                            packet = start_match_packet(curr_match_id)
                            if packet and self._coach:
                                packet.deck_strategy = self._coach._deck_strategy
                        except Exception as e:
                            logger.warning(f"Failed to start match packet: {e}")

                # Debug: Log if turn_num is 0 (every 30 seconds)
                if turn_num == 0:
                    if not hasattr(self, "_last_turn0_log"):
                        self._last_turn0_log = 0
                    if time.time() - self._last_turn0_log > 30:
                        logger.debug(
                            f"turn_num=0, players={len(curr_state.get('players', []))}, battlefield={len(curr_state.get('battlefield', []))}"
                        )
                        self._last_turn0_log = time.time()

                # TERTIARY: Detect new game (turn number decreased) — fallback for same-match restarts
                if turn_num > 0 and turn_num < last_advice_turn:
                    self._match_number += 1
                    logger.info(
                        f"New game detected in coaching loop (turn {last_advice_turn} -> {turn_num}), match #{self._match_number}, resetting advice tracking"
                    )

                    # Only launch fallback post-match analysis when we have
                    # explicit end-of-game evidence. A turn drop can also happen
                    # after relaunch or mid-game resync, and using it alone
                    # causes false "post-match analysis" loops.
                    if (
                        self._advice_history
                        and not self._game_end_handled
                        and self._has_explicit_game_end_evidence()
                    ):
                        self._game_end_handled = True
                        self._stage_post_match_analysis(
                            match_result=self._detect_match_result(),
                            final_state=dict(prev_state) if prev_state else None,
                            replay_path=self._get_latest_replay_path(),
                            reason="turn-drop",
                        )
                    elif self._advice_history and not self._game_end_handled:
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
                    self._game_end_handled = False
                    # Note: _post_match_analysis_text is NOT cleared here —
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
                if not seat_announced:
                    players = curr_state.get("players", [])
                    for p in players:
                        if p.get("is_local"):
                            seat_id = p.get("seat_id")
                            self.ui.status("SEAT_INFO", f"Seat {seat_id}")
                            self.ui.log(f"Detected as Seat {seat_id} (F8 to swap)")
                            logger.info(f"Game detected, local seat = {seat_id}")
                            seat_announced = True
                            # Auto-enable replay recording for debug reports
                            self._enable_replay_recording()
                            break

                # Deck strategy analysis (once per match, after turn 1 starts and mulligan is complete)
                if (
                    self._auto_deck_strategy
                    and not self._deck_analyzed
                    and self._coach
                    and turn_num >= 1
                    and not self._is_mulligan_pending(curr_state)
                ):
                    deck_cards = list(curr_state.get("deck_cards") or [])

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
                                    f"Reconstructed deck from visible zones: {len(deck_cards)} unique cards"
                                )

                    # Require at least 20 cards so deck strategy does not run on partial hands
                    if len(deck_cards) >= 20:
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
                                    except Exception as exc:
                                        logger.debug(f"Card enrichment failed for grp_id={grp_id}: {exc}")
                                        enriched.append((f"Unknown({grp_id})", "", ""))

                                # Use a SEPARATE backend instance so deck analysis
                                # doesn't hold the advice backend's lock
                                from arenamcp.coach import create_backend

                                deck_backend = create_backend(backend_name, model=model_name)

                                # Full strategy analysis (stored, injected into every prompt)
                                try:
                                    strategy = coach.analyze_deck(enriched, backend=deck_backend)
                                    brief = coach.get_deck_strategy_brief(enriched, backend=deck_backend)
                                finally:
                                    if hasattr(deck_backend, "close"):
                                        deck_backend.close()

                                if strategy:
                                    first_line = strategy.split("\n")[0].strip()
                                    ui.status("DECK", first_line[:60])
                                    logger.info(f"Deck strategy stored: {len(strategy)} chars")

                                if brief:
                                    ui.log(f"\n[bold green]DECK STRATEGY:[/] {brief}\n")
                                    speak_fn(brief, blocking=False)
                            except Exception as exc:
                                logger.error(f"Background deck analysis failed: {exc}")

                        t = threading.Thread(
                            target=_analyze_deck_bg,
                            args=(
                                self._coach,
                                self._mcp,
                                deck_cards,
                                self.ui,
                                self._backend_name,
                                self.model_name,
                                self.speak_advice,
                            ),
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
                        # Only enrich the snapshot when poll() returns a new decision.
                        # When poll() returns None, _last_poll_result is stale and
                        # enrich_snapshot would overlay stale bridge actions/targets
                        # onto the current game state — a correctness bug (#205).
                        if bridge_trigger:
                            self._bridge_poller.enrich_snapshot(curr_state)

                        # Update bridge status in UI (only on change). Where a
                        # bridge can never exist (native Mac client), report
                        # the designed state — "Log mode" — instead of an
                        # alarming red "Disconnected" for something that was
                        # never going to connect.
                        _bridge_now = self._bridge_poller.connected
                        if not hasattr(self, "_last_bridge_ui_status"):
                            self._last_bridge_ui_status = None
                        if _bridge_now != self._last_bridge_ui_status:
                            self._last_bridge_ui_status = _bridge_now
                            if _bridge_now:
                                self.ui.status("BRIDGE", "Connected")
                            elif self._bridge_capable_install():
                                self.ui.status("BRIDGE", "Disconnected")
                            else:
                                self.ui.status("BRIDGE", "Log mode")

                        # BRIDGE-DRIVEN MATCH END:
                        # The bridge sees IntermissionRequest as soon as MTGA
                        # transitions to the post-match screen. The log-side
                        # GREMessageType_IntermissionReq path (which calls
                        # prepare_for_game_end + reset) can lag behind by minutes
                        # if Player.log hasn't flushed. Drive the same pipeline
                        # directly when the bridge sees intermission so the coach
                        # stops running on stale mid-match state.
                        if curr_state.get("_bridge_in_intermission") and not self._game_end_handled:
                            try:
                                from arenamcp.server import game_state as gs

                                if gs.match_id and not gs.game_ended_event.is_set():
                                    logger.info(
                                        "Bridge Intermission detected (match=%s) — "
                                        "triggering match-end pipeline",
                                        gs.match_id,
                                    )
                                    gs.prepare_for_game_end()
                                    gs.reset()
                            except Exception as e:
                                logger.debug(f"Bridge-driven match end failed: {e}")

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
                    if not pending_now:
                        self._last_advised_decision_sig = None

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
                            # Don't re-force a window the autopilot has already
                            # handed to the user (MANUAL REQUIRED). Re-forcing
                            # replans + re-speaks the same advice every ~2s
                            # against a window only the user can resolve.
                            _given_up = False
                            with contextlib.suppress(Exception):
                                _given_up = self._autopilot.is_window_given_up(curr_state)
                            # Arbiter: never force a decision the bridge says
                            # doesn't exist (connected + idle ⇒ pending_now is
                            # stale log state).
                            _arb = arbitrate(curr_state, bridge_connected=bool(bridge_active))
                            dec_ctx = curr_state.get("decision_context") or {}
                            dec_type = dec_ctx.get("type", "")
                            legal = curr_state.get("legal_actions", []) or []
                            sig = f"{pending_now}|{dec_type}|{len(legal)}"
                            now = time.time()
                            if (
                                not _given_up
                                and _arb is not None
                                and (
                                    sig != self._last_forced_decision_sig
                                    or (now - self._last_forced_decision_ts) > 2.0
                                )
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
                    _boundary_age = time.time() - getattr(self, "_match_boundary_ts", 0)
                    is_mulligan_pending = self._is_mulligan_pending(curr_state)
                    if triggers and _boundary_age < 2.0 and not bridge_trigger and not is_mulligan_pending:
                        logger.info(
                            f"Suppressing {len(triggers)} stale triggers "
                            f"{triggers} ({_boundary_age:.1f}s after match boundary)"
                        )
                        triggers = []

                    # Debug: Log trigger results
                    if triggers:
                        logger.info(f"Triggers detected: {triggers}")
                        last_priority_progress_note = f"triggers={','.join(triggers[:2])}"

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
                        if not hasattr(self, "_last_no_trigger_log"):
                            self._last_no_trigger_log = 0
                        if time.time() - self._last_no_trigger_log > 30:
                            local_s = curr_state.get("turn", {}).get("active_player", 0)
                            priority = curr_state.get("turn", {}).get("priority_player", 0)
                            logger.debug(
                                f"No triggers: turn={turn_num}, active={local_s}, priority={priority}, phase={phase}"
                            )
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
                        "land_played": 7,  # After land drop, what's next?
                        "spell_resolved": 7,  # After spell resolves, what's next?
                        "combat_attackers": 6,
                        "combat_blockers": 6,
                        "new_turn": 5,
                        "priority_gained": 1,
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
                            # Arbiter (fable-improvements.md item 4): when the
                            # bridge is connected and idle, a log-derived
                            # pending decision is stale — drop the trigger
                            # before it reaches autopilot OR coaching/TTS.
                            _bridge_up = bool(self._bridge_poller and self._bridge_poller.connected)
                            if arbitrate(curr_state, bridge_connected=_bridge_up) is None:
                                # Bridge idle — but if a spell is wedged on the
                                # stack waiting for a target, reconcile before
                                # dropping (multi-target wedge recovery).
                                if self._reconcile_wedged_target(curr_state):
                                    logger.info(
                                        "Arbiter: no real decision (bridge connected "
                                        "and idle) — dropping decision_required"
                                    )
                                    continue
                                logger.info(
                                    "Arbiter reconcile: wedged target decision is "
                                    "live — proceeding with decision_required"
                                )
                            pending = curr_state.get("pending_decision")
                            if (
                                pending == "Action Required"
                                and turn_num == last_advice_turn
                                and phase == last_advice_phase
                                and not (self._autopilot_enabled and self._autopilot)
                            ):
                                logger.info(
                                    "Suppressing decision_required: 'Action Required' already advised this turn+phase"
                                )
                                continue

                        # New turn triggers once per turn
                        # DELAY BUFFER: For new_turn triggers, wait briefly for Hand zone to update
                        # This prevents "missing draw" bugs where we advise before the drawn card arrives
                        if raw_new_turn:
                            # Reset seen threats on new game (turn 1)
                            if turn_num == 1 and hasattr(self._trigger, "_seen_threats"):
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
                                logger.info(
                                    f"Clearing stale win plan (plan turn {self._pending_win_plan_turn}, now {turn_num})"
                                )
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
                            curr_state, waiting_on_vision = self._handle_unresolved_generic_selection(
                                curr_state
                            )
                            if waiting_on_vision:
                                # Do not immediately burn time on another critical
                                # trigger from the same batch while a real decision
                                # is still unresolved on screen.
                                break

                        # Best-effort mulligan refresh: SubmitDeckReq arrives just
                        # before the GameStateMessage with hand cards, so a snapshot
                        # taken on the same poll tick can briefly miss the opening
                        # hand. Skip when bridge detected the decision — bridge data
                        # is already live. Otherwise poll-and-refetch up to ~150ms,
                        # exiting as soon as the hand has cards.
                        bridge_detected = curr_state.get("_bridge_trigger") is not None
                        if (
                            trigger == "decision_required"
                            and curr_state.get("pending_decision") == "Mulligan"
                            and not bridge_detected
                        ):
                            for _ in range(3):
                                try:
                                    self._mcp.poll_log()
                                except Exception as e:
                                    logger.debug(f"poll_log failed during mulligan refresh: {e}")
                                try:
                                    curr_state = self._normalize_turn_snapshot(self._mcp.get_game_state())
                                except Exception as e:
                                    logger.debug(f"Failed to re-fetch state during mulligan refresh: {e}")
                                    break
                                hand = curr_state.get("hand") or []
                                if hand:
                                    break
                                time.sleep(0.05)

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
                        pending_decision_sig = (
                            self._build_pending_decision_signature(curr_state)
                            if has_pending_decision
                            else None
                        )

                        # Step-by-step triggers: land_played, spell_resolved, and combat
                        # BUT suppress if there's a pending decision - wait for it to resolve first
                        is_step_by_step = (
                            trigger
                            in ("land_played", "spell_resolved", "combat_attackers", "combat_blockers")
                            and not has_pending_decision
                        )

                        # Log when we're waiting for a decision
                        if trigger in ("land_played", "spell_resolved") and has_pending_decision:
                            logger.debug(f"Suppressing {trigger} - waiting for decision: {pending_decision}")

                        # Combat and priority triggers only in "every_priority" mode.
                        # NOTE: "every_priority" now means "every *meaningful*
                        # priority" — the meaningful-window gate below drops
                        # pass-only/no-instant filler, so these fire frequently
                        # but only when the human has a real choice.
                        is_frequent = (
                            self.advice_frequency in ("every_priority", "smart")
                            and trigger
                            in (
                                "priority_gained",
                                "combat_attackers",
                                "combat_blockers",
                                "decision_required",
                                "land_played",
                            )
                            and (
                                turn_num > last_advice_turn
                                or phase != last_advice_phase
                                or has_pending_decision
                                or trigger in ("decision_required", "land_played")
                            )
                        )

                        # Additional check: Don't spam priority triggers if we just advised on new_turn
                        # unless distinct phase
                        if trigger == "priority_gained" and is_new_turn:
                            continue

                        # DECISION PRIORITY: If there's a decision required, skip non-critical triggers
                        # in the same batch to ensure the decision is the primary focus.
                        if (
                            "decision_required" in triggers
                            and trigger != "decision_required"
                            and not is_critical
                        ):
                            continue

                        # MEANINGFUL-WINDOW GATE: the coach should talk frequently
                        # but only on windows where the human actually has a real
                        # choice. For the noisy "filler" triggers below, skip
                        # trivial windows entirely — pass-only priority, opponent
                        # priority with no instant-speed response, or empty legal
                        # moves with no pending decision. Skipping here means NO
                        # LLM call, NO Coach-Log line, and NO TTS for filler.
                        #
                        # The gate is deliberately scoped to FILLER triggers only.
                        # It must NOT touch real decision points:
                        #   - critical triggers (decision_required/low_life/
                        #     threat_detected/stack_spell*/...) always fire;
                        #   - combat_attackers/combat_blockers are genuine attack/
                        #     block decisions (and combat_blockers fires on the
                        #     OPPONENT's turn, where priority_player can read as the
                        #     opponent and a block is not an instant — gating it
                        #     would wrongly silence a real block);
                        #   - new_turn is the once-per-turn plan and stays.
                        # The predicate itself biases toward speaking when
                        # uncertain. This is what makes "every_priority" mean
                        # "every *meaningful* priority" rather than literally every
                        # pass/wait window.
                        if not is_critical and trigger in self._MEANINGFUL_GATE_TRIGGERS:
                            window_has_instants = self._trigger._has_castable_instants(curr_state)
                            if not self._is_meaningful_advice_window(
                                curr_state, has_castable_instants=window_has_instants
                            ):
                                # INFO (not debug) so the quiet decision is
                                # visible in the normal Coach-Log: a silent
                                # window should be distinguishable from a hung
                                # coach. Mirrors the existing "Quiet:" lines.
                                logger.info(
                                    "Quiet: %s (no meaningful play) "
                                    "[turn=%s phase=%s prio=%s pending=%s instants=%s legal=%s]",
                                    trigger,
                                    turn_num,
                                    phase,
                                    curr_state.get("turn", {}).get("priority_player"),
                                    pending_decision,
                                    window_has_instants,
                                    len(curr_state.get("legal_actions", []) or []),
                                )
                                continue

                        should_advise = (
                            is_critical or is_new_turn or is_opponent_turn or is_step_by_step or is_frequent
                        )

                        if not should_advise:
                            continue

                        suppress_coach_advice = (
                            has_pending_decision
                            and pending_decision != "Action Required"
                            and pending_decision_sig
                            and pending_decision_sig == self._last_advised_decision_sig
                        )
                        if suppress_coach_advice and not (self._autopilot_enabled and self._autopilot):
                            # Advice-only mode: coach already spoke this
                            # decision; nothing else to do.
                            logger.info(
                                "Suppressing duplicate unresolved decision advice: %s via %s",
                                pending_decision,
                                trigger,
                            )
                            continue
                        # When autopilot is on, fall through so autopilot can
                        # retry the action even if the coach has already
                        # advised. Coach re-entry is blocked below using the
                        # same flag so we don't pay for another LLM call.

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
                        QUIET_TRIGGERS = {
                            "stack_spell_yours",
                            "stack_spell_opponent",
                            "priority_gained",
                            "spell_resolved",
                            "opponent_turn",
                        }
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

                        # THREAT DETECTION: fast targeted coaching for dangerous permanents.
                        if trigger == "losing_badly" and self._coach:
                            logger.info("Proactive win probability check (losing badly)")
                            self._inject_library_summary_if_needed(curr_state)
                            opp_cards = self._get_match_context().get("opponent_played_cards", [])
                            prob = self._coach.generate_win_probability(curr_state, opp_cards)
                            if prob:
                                self._record_advice(prob, trigger, game_state=curr_state)
                                last_advice_turn = turn_num
                                last_advice_phase = phase
                                self.ui.advice(prob, "WIN PROBABILITY")
                                self.speak_advice(prob)
                            continue

                        if trigger == "threat_detected" and hasattr(self._trigger, "_last_threat"):
                            threat = self._trigger._last_threat
                            advice = (
                                self._coach.get_advice(
                                    curr_state,
                                    trigger="threat_detected",
                                    style=self.advice_style,
                                    threat=threat,
                                )
                                if self._coach
                                else f"Warning! {threat['name']}. {threat['warning']}"
                            )
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
                                handled = self._autopilot.process_trigger(curr_state, trigger)
                                if handled:
                                    last_priority_progress_note = f"autopilot handled {trigger}"
                                    last_actionable_window_signature = None
                                    last_actionable_window_started_at = time.time()
                                    last_actionable_window_log_at = 0.0
                                    last_advice_turn = turn_num
                                    last_advice_phase = phase
                                    continue  # Autopilot handled it
                                else:
                                    last_priority_progress_note = f"autopilot fell through {trigger}"
                                    logger.info(
                                        f"Autopilot failed for trigger '{trigger}' — "
                                        "falling through to coaching"
                                    )
                            except Exception as e:
                                last_priority_progress_note = f"autopilot error {trigger}"
                                logger.error(f"Autopilot error: {e}", exc_info=True)
                            # Fall through to coaching below

                        # When autopilot retried a decision the coach has
                        # already advised on, skip re-running the LLM. The
                        # user already heard the advice; just wait for the
                        # autopilot to succeed (engine_busy window expiring,
                        # bridge reconnecting, etc.).
                        if suppress_coach_advice:
                            logger.debug(
                                "Autopilot retry without re-advising: %s via %s",
                                pending_decision,
                                trigger,
                            )
                            continue

                        if self._coach:
                            # Snapshot turn state BEFORE the (slow) LLM call
                            pre_advice_turn = turn_num
                            pre_advice_phase = phase
                            pre_advice_active_player = turn.get("active_player")

                            # Inject library targets when a tutor spell is in hand
                            self._inject_library_summary_if_needed(curr_state)

                            # Inject match identifier so persistent backends
                            # know when a new game starts and reset context
                            curr_state["_match_number"] = self._match_number

                            # Unified advice path: use the action planner for
                            # both autopilot actions AND coaching advice. The
                            # planner constrains itself to legal actions only.
                            #
                            # Exception (2026-07-16): the planner's voice_advice
                            # is terse by design ("Play Forest"), which silently
                            # overrode the user's chatty style — advice-mode
                            # chatty routes to the style-aware LLM instead so
                            # the coach narrates the turn (what to do with the
                            # mana, not just the land drop). Autopilot and the
                            # quick style keep the fast planner path.
                            use_planner_advice = (
                                self._autopilot
                                and hasattr(self._autopilot, "_planner")
                                and (
                                    self._autopilot_enabled or self.advice_style not in ("chatty", "verbose")
                                )
                            )
                            if use_planner_advice:
                                # P2-3: when autopilot just planned this exact
                                # window and fell through, reuse its advice
                                # instead of re-running plan_actions on the
                                # identical state (8 duplicate calls / ~58s on
                                # 2026-07-05, incl. the night's largest 28.8s
                                # call — most of it then discarded as stale).
                                plan = None
                                reused = None
                                try:
                                    reused = self._autopilot.get_reusable_advice(curr_state)
                                except Exception:
                                    reused = None
                                if reused:
                                    logger.info(
                                        "Coach: reusing autopilot plan advice for "
                                        "this window (no duplicate LLM call)"
                                    )
                                    advice = reused
                                else:
                                    legal_actions = self._autopilot._get_legal_actions(curr_state)
                                    decision_context = curr_state.get("decision_context")
                                    plan = self._autopilot._planner.plan_actions(
                                        curr_state, trigger, legal_actions, decision_context
                                    )
                                    advice = plan.voice_advice or plan.overall_strategy
                                if not advice and plan is not None and plan.actions:
                                    advice = str(plan.actions[0])
                                if not advice:
                                    advice = self._coach.get_advice(
                                        curr_state, trigger=trigger, style=self.advice_style
                                    )
                                if advice and advice.strip().lower().rstrip(".").startswith("no actionable play"):
                                    advice = None
                            else:
                                advice = self._coach.get_advice(
                                    curr_state, trigger=trigger, style=self.advice_style
                                )
                                if advice and advice.strip().lower().rstrip(".").startswith("no actionable play"):
                                    advice = None
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
                                is_stale = fresh_turn_num != pre_advice_turn or "Combat" not in fresh_phase
                            else:
                                # General advice: stale if turn number or active player changed
                                fresh_active = fresh_turn.get("active_player")
                                is_stale = (fresh_turn_num != pre_advice_turn) or (
                                    fresh_active != pre_advice_active_player
                                )

                            if is_stale:
                                stale_label = "[STALE - discarded]"
                                logger.info(
                                    f"Discarding stale advice: turn {pre_advice_turn}->{fresh_turn_num}, "
                                    f"phase {pre_advice_phase}->{fresh_phase}"
                                )
                                self._record_advice(f"{stale_label} {advice}", trigger, game_state=curr_state)
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
                            untapped_lands = sum(
                                1
                                for c in your_cards
                                if "land" in c.get("type_line", "").lower() and not c.get("is_tapped")
                            )
                            seat_info = (
                                f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}"
                                if local_seat
                                else "Seat ?"
                            )

                            # Skip empty responses (e.g. from timeout/lock busy)
                            # NOTE: Do NOT update last_advice_turn before this check.
                            # Empty responses should not suppress future triggers.
                            if not advice or not advice.strip():
                                self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
                                max_errors = getattr(self, "_max_errors_before_fallback", 3)
                                logger.warning(
                                    f"Empty advice response ({self._consecutive_errors}/{max_errors}) — "
                                    "model timeout or backend hung"
                                )
                                self._report_backend_failure(
                                    "Empty advice response (model timeout or backend hung)"
                                )
                                if self._consecutive_errors >= max_errors:
                                    # Try restarting the backend process first
                                    logger.warning("Too many empty responses, restarting backend...")
                                    self.ui.log("\n[BACKEND] Restarting (too many empty responses)...")
                                    try:
                                        be = self._coach._backend
                                        if hasattr(be, "close"):
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
                                is_backend_error_text(advice)
                                or "didn't catch that" in advice
                                or (_is_err(advice) and len(advice) < 200)
                            ):
                                logger.warning(f"Suppressing error advice from TTS: {advice[:80]}")
                                self._report_backend_failure(advice)
                                self.ui.error(advice)
                            elif (
                                not is_critical
                                and trigger in self._MEANINGFUL_GATE_TRIGGERS
                                and not has_pending_decision
                                and self._is_passive_advice(advice)
                            ):
                                # The meaningful-window gate let this filler
                                # window through (e.g. an instant was technically
                                # castable), but the model decided to pass/wait.
                                # That's low-value: skip it entirely like a
                                # trivial window — no Coach-Log line, no TTS, and
                                # don't update dedup state (so a later real play
                                # this turn/phase still fires).
                                logger.info(
                                    "Quiet: %s (model advised pass/wait: %r)",
                                    trigger,
                                    advice[:60],
                                )
                                continue
                            else:
                                # Advice was successfully generated — NOW update dedup state
                                # so only real, delivered advice suppresses future triggers.
                                last_priority_progress_note = f"coached {trigger}"
                                last_actionable_window_signature = None
                                last_actionable_window_started_at = time.time()
                                last_actionable_window_log_at = 0.0
                                last_advice_turn = turn_num
                                last_advice_phase = phase
                                self._last_advised_decision_sig = pending_decision_sig
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
                    untapped_lands = sum(
                        1
                        for c in your_cards
                        if "land" in c.get("type_line", "").lower() and not c.get("is_tapped")
                    )

                    seat_info = (
                        f"Seat {local_seat}|{untapped_lands} mana|{self.backend_name}"
                        if local_seat
                        else "Seat ?"
                    )

                    # Check if we can use direct audio with Gemini
                    audio_data = self._voice_input.get_last_audio()
                    use_direct_audio = (
                        audio_data is not None
                        and len(audio_data) > 0
                        and hasattr(self._coach._backend, "complete_with_audio")
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
                            self._coach._system_prompt, user_message, audio_data
                        )
                    elif text and text.strip():
                        logger.info(f"QUESTION: {text}")
                        self.ui.log(f"\n[YOU] {text}")
                        advice = self._coach.get_advice(game_state, question=text, style=self.advice_style)
                    else:
                        logger.info("QUICK ADVICE (F4 tap)")
                        self.ui.log("\n[QUICK] Analyzing...")
                        advice = self._coach.get_advice(
                            game_state, trigger="user_request", style=self.advice_style
                        )

                    logger.info(f"RESPONSE: {advice}")

                    # Check for backend auth/billing failures → auto-fallback
                    from arenamcp.backend_detect import is_query_failure_retriable as _is_err2

                    is_error_response = (
                        is_backend_error_text(advice)
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
                    trigger = (
                        "voice_audio" if use_direct_audio else ("voice_question" if text else "voice_quick")
                    )
                    self._record_advice(advice, trigger, game_state=game_state)

            except Exception as e:
                if self._running:
                    logger.error(f"Voice loop error: {e}")
                    self._record_error(str(e), "voice_loop")

        self._voice_input.stop()
        logger.info("Voice loop stopped")

    def start(self) -> None:
        """Start the standalone coach."""
        logger.info(
            f"start() called: backend_name={self.backend_name}, model={self.model_name}, draft={self.draft_mode}"
        )
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
            self._probe_backend_health_at_startup()
            # Get actual model name from backend
            if self._coach and hasattr(self._coach, "_backend"):
                actual_model = getattr(self._coach._backend, "model", self.model_name)

            # Initialize VisionMapper (shared: watchdog + autopilot)
            self.ui.log("Initializing vision mapper...")
            self._init_vision_mapper()

            # Initialize autopilot if enabled. Restoring from a prior
            # saved-on state surfaces a log/UI nudge so the user isn't
            # surprised that it came up already armed.
            if self._autopilot_enabled:
                self._init_autopilot()
                if getattr(self, "_autopilot_restored_from_settings", False):
                    logger.info("Autopilot re-enabled automatically from last session")
                    with contextlib.suppress(Exception):
                        self.ui.log(
                            "[cyan]Autopilot re-enabled automatically "
                            "(saved from previous session). Toggle off if "
                            "you want to play manually.[/]"
                        )

            # Start coaching and voice threads
            logger.info(f"Starting threads for backend: {self.backend_name}")
            logger.info("Starting PTT voice loop + coaching loop")
            self._coaching_thread = threading.Thread(target=self._coaching_loop, daemon=True, name="coaching")
            self._coaching_thread.start()

            # Only launch voice thread if PTT/VOX is wanted
            if self._voice_mode in ("ptt", "vox"):
                self._voice_thread = threading.Thread(target=self._voice_loop, daemon=True, name="voice")
                self._voice_thread.start()

        # Register hotkeys in a background thread (the keyboard module's
        # low-level Windows hook install can take a few seconds).
        threading.Thread(target=self._register_hotkeys, daemon=True, name="hotkey-register").start()

        # Print status
        _is_pipe = hasattr(self.ui, "emit_game_state")

        self._emit_control_status_snapshot(actual_model)

        if _is_pipe:
            # Pipe mode: status snapshot is already emitted above.
            self.ui.status("BACKEND", f"{self.backend_name} ({actual_model or 'default'})")
            self.ui.log("Waiting for MTGA...")
        else:
            # CLI mode: full banner with hotkeys
            self.ui.log("\n" + "=" * 50)
            if self.draft_mode:
                self.ui.log("MTGA DRAFT HELPER")
                self.ui.log("=" * 50)
                self.ui.log(f"Set: {self.set_code or 'auto-detect'}")
                self.ui.log("Using MCP server's draft evaluation")
            else:
                if self._autopilot_enabled:
                    mode = "DRY-RUN" if self._autopilot_dry_run else "LIVE"
                    afk = " AFK" if self._autopilot_afk else ""
                    self.ui.log(f"MTGA AUTOPILOT ({mode}{afk})")
                else:
                    self.ui.log("MTGA COACH")
                self.ui.log("=" * 50)
                self.ui.status("BACKEND", f"{self.backend_name} ({actual_model or 'default'})")
                self.ui.status("VOICE", "PTT (F4) + Kokoro")
            self.ui.log("-" * 50)
            self.ui.log("F5=mute F6=voice F7=bug F8=seat F9=restart F10=speed F12=model Num1=land")
            self.ui.log("=" * 50)
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
                for _i in range(3):
                    start_req = time.perf_counter()
                    response = backend.complete("You are a helpful assistant.", "Say 'ok' and nothing else.")
                    req_ms = (time.perf_counter() - start_req) * 1000

                    if is_backend_error_text(response):
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
        """,
    )

    parser.add_argument(
        "--backend",
        "-b",
        choices=["auto", "online", "local"],
        default=None,
        help="(legacy) Accepted for compatibility; the app is online-only and always uses api.mtgacoach.com",
    )
    parser.add_argument("--model", "-m", help="Model name override")
    parser.add_argument("--provider", help="(deprecated) Alias for --model")
    parser.add_argument(
        "--voice", "-v", choices=["ptt", "vox"], default=None, help="Voice input: ptt (F4) or vox (auto)"
    )
    parser.add_argument("--draft", action="store_true", help="Draft helper mode (no LLM needed)")
    parser.add_argument("--set", "-s", dest="set_code", help="Set code for draft (e.g., MH3, BLB)")
    parser.add_argument(
        "--autopilot", action="store_true", help="Enable autopilot mode (AI plays via the GRE bridge)"
    )
    parser.add_argument(
        "--afk", action="store_true", help="Start in AFK mode (auto-pass all priority without LLM)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Autopilot dry run: plan actions but log instead of clicking"
    )
    parser.add_argument("--show-log", action="store_true", help="Show log file and exit")
    parser.add_argument(
        "--language", "-l", default=None, help="Language code for voice (e.g., en, nl, es, fr, de, ja)"
    )
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode (no GUI)")
    parser.add_argument(
        "--pipe", action="store_true", help="Pipe mode: JSON lines on stdout/stdin (for native GUI launcher)"
    )
    parser.add_argument("--diagnose", action="store_true", help="Run diagnostic checks and exit")

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
            afk=getattr(args, "afk", False),
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
        # Pipe-mode restart = full process restart: the desktop frontend
        # respawns us on exit (fresh interpreter, fresh code from disk).
        # Plain `return` left the process half-dead when any non-daemon
        # thread (bridge poller, TTS glue) survived run_forever — the
        # frontend never saw an exit, so the Restart Coach button did
        # nothing. Force the exit.
        with contextlib.suppress(Exception):
            coach.stop()
        logger.info(
            "Pipe-mode coach exiting (restart_requested=%s)",
            coach._restart_requested,
        )
        logging.shutdown()
        os._exit(0)

    if args.show_log:
        print(f"Log: {LOG_FILE}")
        if LOG_FILE.exists():
            with open(LOG_FILE) as f:
                for line in f.readlines()[-30:]:
                    print(line, end="")
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
            afk=getattr(args, "afk", False),
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
            print("\n" + "=" * 50)
            print("RESTARTING...")
            print("=" * 50 + "\n")
            logger.info("Restarting coach...")
            continue
        else:
            break  # Normal exit


if __name__ == "__main__":
    main()
