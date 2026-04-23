"""Autopilot Mode - Core Orchestration Engine.

Ties ActionPlanner + ScreenMapper + InputController together with
human-in-the-loop confirmation gates (spacebar to confirm, escape to skip).

The autopilot layers onto the existing coaching loop without replacing it:

    GameState polling → Triggers → ActionPlanner.plan_actions() → Preview
    → [SPACEBAR confirm] → InputController.execute() → Verify state → Loop
"""

import logging
import re
import threading
import time
import io
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from PIL import ImageGrab

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.gre_bridge import (
    GREBridge,
    UNMAPPED_INTERACTION_TYPE,
    enrich_snapshot_from_pending_response,
    get_bridge,
)
from arenamcp.input_controller import ClickResult, InputController
from arenamcp.screen_mapper import ScreenCoord, ScreenMapper

logger = logging.getLogger(__name__)


class ExecutionPath:
    """Tracks which execution path was used for an action.

    gre-aware: Action has a GRE action reference (direct GRE command).
    deterministic-geometry: Coordinates resolved via deterministic math
        (arc-based hand layout, permanent heuristic, or fixed button coords).
    vision-fallback: Coordinates resolved via VLM screenshot analysis
        (used only when deterministic lookup fails).
    """
    GRE_AWARE = "gre-aware"
    DETERMINISTIC_GEOMETRY = "deterministic-geometry"
    VISION_FALLBACK = "vision-fallback"


class AutopilotState(Enum):
    """Current state of the autopilot engine."""
    IDLE = "idle"
    PLANNING = "planning"
    PREVIEWING = "previewing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class AutopilotConfig:
    """Configuration for autopilot behavior."""
    confirm_each_action: bool = False  # Per-action confirmation (legacy, slow)
    confirm_plan: bool = False  # Plan-level confirmation (legacy, slow)
    auto_execute_delay: float = 0.3  # Seconds before auto-executing (F1/F4 cancels)
    auto_pass_priority: bool = True
    auto_resolve: bool = True
    verify_after_action: bool = True
    verification_timeout: float = 2.5
    action_delay: float = 0.25
    post_action_delay: float = 0.4  # Delay after action to allow GRE to update
    planning_timeout: float = 8.0
    enable_vision_fallback: bool = True
    prefer_deterministic: bool = True  # When True, skip VLM for actions that have deterministic coordinates
    enable_tts_preview: bool = True
    dry_run: bool = False
    afk_mode: bool = False  # When True, auto-pass everything without LLM
    land_drop_mode: bool = False  # When True, auto-play one land per turn (no LLM)
    # When True AND the GRE bridge is connected, refuse to fall back to
    # mouse-click execution. The bridge submits actions directly without
    # touching the mouse — falling back to clicks is the only reason the
    # user's cursor gets pulled into MTGA mid-play. Actions that the bridge
    # can't handle are reported as failures instead of simulated via mouse.
    bridge_only_when_connected: bool = True


class AutopilotEngine:
    """Core autopilot orchestration engine.

    Coordinates action planning, screen mapping, input control, and
    human confirmation to execute MTGA actions automatically.
    """

    _MAX_CONTINUATION_DEPTH: int = 5
    _CRITICAL_DECISION_TYPES: frozenset[str] = frozenset({
        UNMAPPED_INTERACTION_TYPE,
        "declare_attackers",
        "declare_blockers",
        "modal_choice",
        "target_selection",
        "select_n",
        "search",
        "distribution",
        "numeric_input",
        "choose_starting_player",
        "select_replacement",
        "select_counters",
        "casting_time_options",
        "order_triggers",
        "select_n_group",
        "select_from_groups",
        "search_from_groups",
        "gather",
        "assign_damage",
        "order_combat_damage",
        "pay_costs",
    })

    def __init__(
        self,
        planner: ActionPlanner,
        mapper: ScreenMapper,
        controller: InputController,
        get_game_state: Callable[[], dict[str, Any]],
        config: Optional[AutopilotConfig] = None,
        speak_fn: Optional[Callable[[str, bool], None]] = None,
        ui_advice_fn: Optional[Callable[[str, str], None]] = None,
        bug_report_fn: Optional[Callable[[str, dict], None]] = None,
    ):
        """Initialize the autopilot engine.

        Args:
            planner: ActionPlanner for LLM-based action planning.
            mapper: ScreenMapper for coordinate calculations.
            controller: InputController for mouse/keyboard input.
            get_game_state: Callable that returns current game state dict.
            config: Optional autopilot configuration.
            speak_fn: Optional TTS function (text, blocking) for previewing actions.
            ui_advice_fn: Optional UI callback (text, label) for displaying actions.
            bug_report_fn: Optional callback (reason, extra_context) invoked
                whenever the GRE bridge can't submit an action and autopilot
                has to fall back. Used to auto-file a bug report so we have
                telemetry on every bridge miss.
        """
        self._planner = planner
        self._mapper = mapper
        self._controller = controller
        self._game_state_fn = get_game_state
        self._config = config or AutopilotConfig()
        self._speak_fn = speak_fn
        self._ui_advice_fn = ui_advice_fn
        self._bug_report_fn = bug_report_fn
        # Buffer of fallback bug events collected during the current match.
        # On match end, we sample up to `_max_fallback_bugs_per_match` at
        # random and dispatch those. Rest are discarded — goal is
        # representative telemetry without spam.
        self._pending_fallback_bugs: list[tuple[str, dict]] = []
        self._max_fallback_bugs_per_match: int = 5

        # State
        self._state = AutopilotState.IDLE
        self._current_plan = None
        self._current_action_idx = 0
        self._lock = threading.Lock()

        # Confirmation events
        self._confirm_event = threading.Event()
        self._skip_event = threading.Event()
        self._abort_event = threading.Event()

        # Statistics
        self._actions_executed = 0
        self._actions_skipped = 0
        self._plans_completed = 0
        self._consecutive_failed_verifications = 0

        # Land-drop dedup: track last turn we played a land to prevent
        # double-triggers when game state hasn't updated yet
        self._land_drop_last_turn: int = -1

        # Vision scan: track if mapper supports layout scanning
        self._has_vision_scan = hasattr(self._mapper, 'scan_layout')

        # GRE bridge for direct action submission (bypasses mouse clicks)
        self._gre_bridge: GREBridge = get_bridge()
        self._gre_bridge_failed_methods: set[str] = set()
        self._bridge_preloaded_actions: Optional[list[dict[str, Any]]] = None

        # Execution path tracking
        self._path_stats: dict[str, int] = {}

        # Consecutive planning failure tracking (timeout/empty plan escalation)
        self._consecutive_plan_failures: int = 0
        self._effective_planning_timeout: float = self._config.planning_timeout

        # Stashed combat decision context (survives across triggers)
        self._last_combat_context: Optional[dict[str, Any]] = None
        self._last_combat_context_time: float = 0.0
        self._last_combat_context_turn: int = -1

        # Post-plan continuation depth (prevents runaway recursion)
        self._continuation_depth: int = 0

        # Retry suppression for actions that failed to advance the GRE state
        self._blocked_action_keys: set[tuple[Any, ...]] = set()
        self._blocked_action_window_sig: Optional[tuple[Any, ...]] = None

    @property
    def afk_mode(self) -> bool:
        """Whether AFK mode is active."""
        return self._config.afk_mode

    def toggle_afk(self) -> bool:
        """Toggle AFK mode on/off. Returns new state."""
        self._config.afk_mode = not self._config.afk_mode
        status = "ON" if self._config.afk_mode else "OFF"
        logger.info(f"AFK mode toggled: {status}")
        self._notify("AFK", status)
        return self._config.afk_mode

    @property
    def land_drop_mode(self) -> bool:
        """Whether land-drop-only mode is active."""
        return self._config.land_drop_mode

    def toggle_land_drop(self) -> bool:
        """Toggle land-drop-only mode on/off. Returns new state."""
        self._config.land_drop_mode = not self._config.land_drop_mode
        status = "ON" if self._config.land_drop_mode else "OFF"
        logger.info(f"Land-drop mode toggled: {status}")
        self._notify("LAND_DROP", status)
        return self._config.land_drop_mode

    def _capture_screenshot(self) -> Optional[bytes]:
        """Capture MTGA window as PNG bytes for VLM analysis."""
        try:
            window_rect = self._mapper.window_rect
            if not window_rect:
                window_rect = self._mapper.refresh_window()
            if not window_rect:
                return None

            left, top, width, height = window_rect
            screenshot = ImageGrab.grab(bbox=(left, top, left + width, top + height))

            buf = io.BytesIO()
            screenshot.save(buf, format='PNG')
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Screenshot capture failed: {e}")
            return None

    def _scan_layout_if_needed(self, game_state: dict[str, Any]) -> None:
        """Trigger a VisionMapper layout scan if the mapper supports it.

        Captures a screenshot and asks the VisionMapper to scan for all
        visible UI elements. The scan only runs when the game state has
        changed (phase/turn/hand/battlefield) or the cache has expired.
        """
        if not self._has_vision_scan:
            return

        try:
            if not self._mapper.needs_rescan(game_state):
                logger.debug("Vision scan: cache still valid, skipping")
                return

            png_bytes = self._capture_screenshot()
            if not png_bytes:
                logger.warning("Vision scan: screenshot capture failed")
                return

            start = time.perf_counter()
            self._mapper.scan_layout(png_bytes, game_state)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(f"Vision scan completed: {elapsed_ms:.0f}ms, {self._mapper.cache_size} elements cached")
        except Exception as e:
            logger.error(f"Vision scan failed (non-fatal): {e}")

    def _should_prefetch_vision(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Whether to run a blocking layout scan before planning.

        GRE + deterministic geometry should stay on the critical path. Vision
        prefetch is only useful in vision-heavy mode; otherwise it just adds a
        large delay before the staleness snapshot and causes plans to be
        discarded in fast games.
        """
        del game_state, trigger
        return (
            self._has_vision_scan
            and self._config.enable_vision_fallback
            and not self._config.prefer_deterministic
        )

    @property
    def state(self) -> AutopilotState:
        """Current autopilot state."""
        return self._state

    @property
    def current_plan(self) -> Optional[ActionPlan]:
        """Currently active action plan."""
        return self._current_plan

    @property
    def stats(self) -> dict[str, int]:
        """Execution statistics."""
        return {
            "executed": self._actions_executed,
            "skipped": self._actions_skipped,
            "plans": self._plans_completed,
        }

    @property
    def path_stats(self) -> dict[str, int]:
        """Execution path usage statistics."""
        return dict(self._path_stats)

    def get_debug_info(self) -> dict[str, Any]:
        """Collect comprehensive autopilot state for bug reports."""
        info: dict[str, Any] = {
            "state": self._state.value,
            "config": {
                "dry_run": self._config.dry_run,
                "afk_mode": self._config.afk_mode,
                "land_drop_mode": self._config.land_drop_mode,
                "auto_pass_priority": self._config.auto_pass_priority,
                "auto_resolve": self._config.auto_resolve,
                "auto_execute_delay": self._config.auto_execute_delay,
                "planning_timeout": self._config.planning_timeout,
                "prefer_deterministic": self._config.prefer_deterministic,
                "enable_vision_fallback": self._config.enable_vision_fallback,
            },
            "stats": {
                "actions_executed": self._actions_executed,
                "actions_skipped": self._actions_skipped,
                "plans_completed": self._plans_completed,
                "consecutive_failed_verifications": self._consecutive_failed_verifications,
                "consecutive_plan_failures": self._consecutive_plan_failures,
                "effective_planning_timeout": self._effective_planning_timeout,
                "path_stats": dict(self._path_stats),
            },
            "current_action_idx": self._current_action_idx,
            "land_drop_last_turn": self._land_drop_last_turn,
            "has_vision_scan": self._has_vision_scan,
            "gre_bridge_connected": self._gre_bridge.connected,
            "blocked_actions": [list(key) for key in self._blocked_action_keys],
        }

        # Current plan details
        plan = self._current_plan
        if plan:
            info["current_plan"] = {
                "trigger": plan.trigger,
                "turn_number": plan.turn_number,
                "strategy": plan.overall_strategy,
                "num_actions": len(plan.actions),
                "actions": [
                    {
                        "type": a.action_type.value,
                        "card_name": a.card_name,
                        "target_names": a.target_names,
                        "reasoning": a.reasoning,
                        "confidence": a.confidence,
                        "has_gre_ref": a.gre_action_ref is not None,
                    }
                    for a in plan.actions
                ],
            }
        else:
            info["current_plan"] = None

        # Screen mapper state
        try:
            info["screen_mapper"] = {
                "window_rect": self._mapper.window_rect,
                "cache_size": getattr(self._mapper, 'cache_size', 0),
            }
        except Exception as e:
            logger.debug(f"Could not read screen_mapper state: {e}")
            info["screen_mapper"] = {"error": "unavailable"}

        # Planner backend info
        try:
            backend = self._planner._backend
            info["planner_backend"] = type(backend).__name__
        except Exception as e:
            logger.debug(f"Could not read planner backend info: {e}")
            info["planner_backend"] = "unknown"

        # Recent planner diagnostics (LLM calls, parse failures, fallback paths)
        try:
            info["planner_diagnostics"] = self._planner.get_recent_diagnostics()
        except Exception:
            info["planner_diagnostics"] = []

        return info

    @staticmethod
    def _decision_type(game_state: dict[str, Any]) -> str:
        """Return the normalized decision type for the current state."""
        ctx = game_state.get("decision_context") or {}
        dec_type = str(ctx.get("type", "") or "")
        if dec_type:
            return dec_type
        if game_state.get("pending_decision") == "Manual Required":
            return UNMAPPED_INTERACTION_TYPE
        return ""

    @staticmethod
    def _priority_window_signature(game_state: dict[str, Any]) -> tuple[Any, ...]:
        """Build a signature for the current bridge priority window."""
        turn = game_state.get("turn", {}) or {}
        return (
            int(game_state.get("_bridge_game_state_id", 0) or 0),
            game_state.get("_bridge_request_type"),
            game_state.get("_bridge_request_class"),
            game_state.get("pending_decision"),
            turn.get("turn_number", 0),
            turn.get("phase", ""),
            turn.get("step", ""),
        )

    def _refresh_blocked_action_window(self, game_state: dict[str, Any]) -> None:
        """Reset blocked-action suppression when the priority window changes."""
        sig = self._priority_window_signature(game_state)
        if sig != self._blocked_action_window_sig:
            self._blocked_action_window_sig = sig
            self._blocked_action_keys.clear()

    def _action_block_key(self, action: GameAction, game_state: dict[str, Any]) -> tuple[Any, ...]:
        """Return a stable key for suppressing reattempts in one window."""
        gre_ref = getattr(action, "gre_action_ref", None)
        instance_id = 0
        grp_id = 0
        ability_grp_id = 0
        if gre_ref is not None:
            instance_id = int(getattr(gre_ref, "instance_id", 0) or 0)
            grp_id = int(getattr(gre_ref, "grp_id", 0) or 0)
            ability_grp_id = int(getattr(gre_ref, "ability_grp_id", 0) or 0)

        if not instance_id and action.action_type == ActionType.PAY_COSTS:
            instance_id = int(((game_state.get("decision_context") or {}).get("source_id")) or 0)

        target_names = tuple(sorted(name.lower() for name in (action.target_names or [])))
        attacker_names = tuple(sorted(name.lower() for name in (action.attacker_names or [])))
        blocker_assignments = tuple(
            sorted((blocker.lower(), attacker.lower()) for blocker, attacker in (action.blocker_assignments or {}).items())
        )
        selection_names = tuple(sorted(name.lower() for name in (action.select_card_names or [])))
        distribution = tuple(sorted((name.lower(), amount) for name, amount in (action.distribution or {}).items()))

        return (
            action.action_type.value,
            instance_id,
            grp_id,
            ability_grp_id,
            action.card_name.lower() if action.card_name else "",
            target_names,
            attacker_names,
            blocker_assignments,
            selection_names,
            distribution,
            action.modal_index,
            action.numeric_value,
            action.play_or_draw.lower() if action.play_or_draw else "",
        )

    def _mark_action_blocked(self, action: GameAction, game_state: dict[str, Any], reason: str) -> None:
        """Block an action from being retried in the current priority window."""
        key = self._action_block_key(action, game_state)
        self._blocked_action_keys.add(key)
        logger.warning("Blocking action for current window: %s (%s)", action, reason)

    def _is_action_blocked(self, action: GameAction, game_state: dict[str, Any]) -> bool:
        """Whether this action already failed to advance the current window."""
        return self._action_block_key(action, game_state) in self._blocked_action_keys

    def _pause_for_manual(self, reason: str, game_state: Optional[dict[str, Any]] = None) -> None:
        """Pause the autopilot and surface that manual input is required."""
        self._state = AutopilotState.PAUSED
        details = ""
        if game_state:
            details = (
                f" pending={game_state.get('pending_decision')!r}"
                f" bridge={game_state.get('_bridge_request_type') or game_state.get('_bridge_request_class')!r}"
            )
        logger.warning("Autopilot manual required: %s%s", reason, details)
        self._notify("AUTOPILOT", f"MANUAL REQUIRED: {reason}")

    def _is_critical_decision_state(
        self,
        game_state: dict[str, Any],
        action: Optional[GameAction] = None,
    ) -> bool:
        """Whether the current state should never fall back to auto_respond."""
        if self._decision_type(game_state) in self._CRITICAL_DECISION_TYPES:
            return True

        if action and action.action_type in {
            ActionType.DECLARE_ATTACKERS,
            ActionType.DECLARE_BLOCKERS,
            ActionType.MODAL_CHOICE,
            ActionType.SELECT_TARGET,
            ActionType.SELECT_N,
            ActionType.PAY_COSTS,
            ActionType.SEARCH_LIBRARY,
            ActionType.DISTRIBUTE,
            ActionType.NUMERIC_INPUT,
            ActionType.CHOOSE_STARTING_PLAYER,
            ActionType.SELECT_REPLACEMENT,
            ActionType.SELECT_COUNTERS,
            ActionType.CASTING_OPTIONS,
            ActionType.ORDER_TRIGGERS,
            ActionType.ASSIGN_DAMAGE,
            ActionType.ORDER_COMBAT_DAMAGE,
        }:
            return True

        return False

    def _should_allow_auto_respond(
        self,
        game_state: dict[str, Any],
        action: Optional[GameAction] = None,
    ) -> bool:
        """Return True when auto_respond is a safe fallback."""
        if self._is_critical_decision_state(game_state, action):
            return False
        return self._decision_type(game_state) == "optional_action"

    def _get_game_state(self) -> dict[str, Any]:
        """Fetch a fresh game state and enrich it with live bridge metadata."""
        state = self._game_state_fn() or {}
        if not isinstance(state, dict):
            state = {}

        state.setdefault("_bridge_connected", False)
        state.setdefault("_bridge_game_state_id", int(state.get("_bridge_game_state_id", 0) or 0))
        state.setdefault("game_engine_busy", False)
        state.setdefault("engine_busy", {})

        bridge_connected = self._gre_bridge.connected or self._gre_bridge.connect()
        if bridge_connected:
            pending = self._gre_bridge.get_pending_actions()
            enrich_snapshot_from_pending_response(
                state,
                pending,
                bridge_connected=self._gre_bridge.connected,
            )
            has_pending = bool(pending and pending.get("has_pending"))
            state["_bridge_has_pending"] = has_pending
        else:
            state["_bridge_has_pending"] = False

        self._refresh_blocked_action_window(state)
        return state

    def _log_execution_path(self, path: str, action_desc: str) -> None:
        """Log which execution path was used for an action."""
        logger.info(f"[{path}] {action_desc}")
        self._path_stats[path] = self._path_stats.get(path, 0) + 1

    def on_spacebar(self) -> None:
        """Handle spacebar press (confirm current action/plan)."""
        logger.info("Autopilot: spacebar pressed (confirm)")
        self._confirm_event.set()

    def on_escape(self) -> None:
        """Handle escape press (skip current action)."""
        logger.info("Autopilot: escape pressed (skip)")
        self._skip_event.set()

    def on_abort(self) -> None:
        """Handle abort (double-escape or F11 toggle off)."""
        logger.info("Autopilot: abort requested")
        self._abort_event.set()
        self._confirm_event.set()  # Unblock any waiting
        self._skip_event.set()

    def _clear_events(self) -> None:
        """Clear all confirmation events."""
        self._confirm_event.clear()
        self._skip_event.clear()
        self._abort_event.clear()

    def _wait_for_cancel(self, timeout: Optional[float] = None) -> str:
        """Countdown timer that auto-executes unless user cancels.

        The autopilot previews its plan, then auto-executes after a brief
        countdown. Pressing F1 or F4 during the countdown cancels execution.

        Args:
            timeout: Seconds to wait. Defaults to config.auto_execute_delay.

        Returns:
            "execute" if countdown expires (no user input),
            "cancel" if user presses F1 or F4,
            "abort" if abort event is set.
        """
        if timeout is None:
            timeout = self._config.auto_execute_delay

        self._confirm_event.clear()
        self._skip_event.clear()

        remaining = timeout
        while remaining > 0:
            if self._abort_event.is_set():
                return "abort"
            # F1 (confirm_event) or F4 (skip_event) = cancel
            if self._confirm_event.wait(timeout=0.05):
                logger.info("User cancelled auto-execute (F1)")
                return "cancel"
            if self._skip_event.is_set():
                logger.info("User cancelled auto-execute (F4)")
                return "cancel"
            remaining -= 0.05

        # Timeout expired with no user input → auto-execute
        return "execute"

    def _wait_for_confirmation(self, timeout: float = 60.0) -> str:
        """Legacy: wait for explicit user confirmation.

        Only used when confirm_plan or confirm_each_action is True.

        Returns:
            "confirm" if F1, "skip" if F4, "abort" if abort.
        """
        self._confirm_event.clear()
        self._skip_event.clear()

        while True:
            if self._abort_event.is_set():
                return "abort"
            if self._confirm_event.wait(timeout=0.1):
                return "confirm"
            if self._skip_event.is_set():
                return "skip"
            timeout -= 0.1
            if timeout <= 0:
                return "skip"

    def process_trigger(
        self,
        game_state: dict[str, Any],
        trigger: str,
    ) -> bool:
        """Main entry point from the coaching loop.

        Processes a game state trigger through the full autopilot pipeline:
        1. PLANNING: Generate action plan via LLM
        2. PREVIEWING: Display plan, wait for confirmation
        3. EXECUTING: Execute each action with per-action confirmation
        4. VERIFYING: Verify state changes after each action

        Args:
            game_state: Current game state dict.
            trigger: Trigger name (e.g., "new_turn", "combat_attackers").

        Returns:
            True if plan was fully executed, False otherwise.
        """
        if not self._lock.acquire(timeout=10.0):
            # Lock held for >10 seconds — force release (previous call is hung)
            logger.warning(f"Autopilot: lock held >10s, force-releasing for {trigger}")
            try:
                self._lock.release()
            except RuntimeError:
                pass
            if not self._lock.acquire(blocking=False):
                logger.error(f"Autopilot: could not acquire lock even after force-release")
                return False

        try:
            if self._abort_event.is_set():
                self._state = AutopilotState.IDLE
                return False

            self._clear_events()

            # --- BRIDGE PRELOAD: stash bridge actions for execution phase ---
            # When the trigger was bridge-detected, actions are already fetched.
            # Avoids redundant get_pending_actions() call in _try_gre_bridge().
            bridge_trigger = game_state.get("_bridge_trigger")
            self._bridge_preloaded_actions = (
                bridge_trigger.get("actions") if bridge_trigger else None
            )
            self._refresh_blocked_action_window(game_state)

            if game_state.get("game_engine_busy"):
                logger.info("Autopilot: engine busy resolving internal loop/synthetic event")
                self._state = AutopilotState.IDLE
                return False

            bridge_connected = bool(
                game_state.get("_bridge_connected")
                or game_state.get("bridge_connected")
                or self._gre_bridge.connected
            )
            bridge_has_pending = bool(
                game_state.get("_bridge_has_pending")
                or game_state.get("_bridge_request_type")
                or game_state.get("_bridge_request_class")
                or game_state.get("bridge_pending_interaction")
                or (bridge_trigger and bridge_trigger.get("has_pending"))
            )
            if bridge_connected and not bridge_has_pending:
                # The bridge plugin polls Unity's main thread, which can lag
                # behind GRE log messages by hundreds of milliseconds.  Retry
                # once after a short delay before giving up on the trigger.
                time.sleep(0.35)
                retry_pending = self._gre_bridge.get_pending_actions()
                if retry_pending and retry_pending.get("has_pending"):
                    bridge_has_pending = True
                    enrich_snapshot_from_pending_response(
                        game_state,
                        retry_pending,
                        bridge_connected=self._gre_bridge.connected,
                    )
                    game_state["_bridge_has_pending"] = True
                    logger.info(
                        "Autopilot: bridge caught up on retry — proceeding with trigger '%s'",
                        trigger,
                    )
                else:
                    # Bridge still idle.  If the log already captured real
                    # game data (legal actions, a pending decision) trust it
                    # rather than silently dropping the trigger.
                    log_has_data = bool(
                        game_state.get("pending_decision")
                        or game_state.get("legal_actions")
                    )
                    if log_has_data:
                        logger.info(
                            "Autopilot: bridge idle but log has data; proceeding with trigger '%s'",
                            trigger,
                        )
                    else:
                        logger.info(
                            "Autopilot: bridge connected but idle; refusing non-authoritative trigger '%s'",
                            trigger,
                        )
                        self._state = AutopilotState.IDLE
                        return False

            if self._decision_type(game_state) == UNMAPPED_INTERACTION_TYPE:
                self._pause_for_manual("Unmapped GRE interaction", game_state)
                return False

            # --- VISION PREFETCH: only in vision-heavy mode ---
            if self._should_prefetch_vision(game_state, trigger):
                self._scan_layout_if_needed(game_state)

            # --- AFK MODE: auto-pass everything without LLM ---
            if self._config.afk_mode:
                return self._handle_afk(game_state, trigger)

            # --- LAND DROP MODE: auto-play one land per turn without LLM ---
            if self._config.land_drop_mode:
                return self._handle_land_drop(game_state, trigger)

            # --- Quick shortcuts: auto-pass/resolve without LLM ---
            # These save 5-15s by not calling the LLM for obvious actions.
            pending = game_state.get("pending_decision")
            has_decision = (
                pending is not None
                and pending != "Action Required"
                and pending != "Priority (Pass Only)"
            )
            bridge_request_type = game_state.get("_bridge_request_type") or ""
            bridge_request_class = game_state.get("_bridge_request_class") or ""
            turn = game_state.get("turn", {})
            local_seat = None
            for p in game_state.get("players", []):
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
            is_my_turn = turn.get("active_player") == local_seat if local_seat else False

            if pending == "Intermission" or bridge_request_type.startswith("Intermission") or bridge_request_class.startswith("Intermission"):
                logger.info("Autopilot: ignoring non-actionable intermission request")
                self._state = AutopilotState.IDLE
                return True

            # Fetch legal actions once for all shortcut checks below
            legal = self._get_legal_actions(game_state)

            # PayCostsRequest — accept autotap if available, otherwise only
            # cancel when we genuinely have no resolvable payment route.
            if (
                bridge_request_type in ("PayCosts", "PayCostsReq", "pay_costs")
                or bridge_request_class in ("PayCostsRequest",)
                or (game_state.get("decision_context") or {}).get("type") == "pay_costs"
            ):
                if any(a.lower() == "auto-pay" for a in legal):
                    logger.info("Autopilot: auto-paying (accepting autotap)")
                    if not self._config.dry_run:
                        if self._gre_bridge.connected or self._gre_bridge.connect():
                            if self._gre_bridge.submit_pass():
                                self._log_execution_path(ExecutionPath.GRE_AWARE, "auto-pay via bridge")
                                return True
                    self._exec_pass_priority()
                    return True

                # MTGA usually auto-taps and just asks for confirmation.
                # Accept the autotap solution by submitting pass.
                logger.info("Autopilot: accepting autotap for PayCostsRequest")
                if not self._config.dry_run:
                    if self._gre_bridge.connected or self._gre_bridge.connect():
                        if self._gre_bridge.submit_pass():
                            self._log_execution_path(ExecutionPath.GRE_AWARE, "accept autotap PayCosts")
                            return True
                        # Pass failed — try cancelling instead
                        logger.info("Autopilot: autotap accept failed, cancelling PayCostsRequest")
                        if self._gre_bridge.cancel_action():
                            self._log_execution_path(ExecutionPath.GRE_AWARE, "cancel PayCosts")
                            return True
                self._pause_for_manual("Pay Costs requires manual payment", game_state)
                return False

            # "Done (confirm attackers/blockers)" — auto-submit when it's
            # the only meaningful action. MTGA auto-selected creatures;
            # just confirm via bridge SubmitAttackers/SubmitBlockers.
            has_done_confirm = any(a.lower().startswith("done (confirm") for a in legal)
            meaningful_non_done = [
                a for a in legal
                if not a.lower().startswith("done (confirm")
                and a.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                and "Wait" not in a
            ]
            if has_done_confirm and not meaningful_non_done:
                done_action = next(a for a in legal if a.lower().startswith("done (confirm"))
                logger.info(f"Autopilot: auto-confirming '{done_action}'")
                if not self._config.dry_run and (self._gre_bridge.connected or self._gre_bridge.connect()):
                    # Use the right submit method based on request type
                    if "attacker" in done_action.lower():
                        if self._gre_bridge.submit_attackers([]):
                            self._log_execution_path(ExecutionPath.GRE_AWARE, f"auto-confirm: {done_action}")
                            return True
                    elif "blocker" in done_action.lower():
                        if self._gre_bridge.submit_blockers([]):
                            self._log_execution_path(ExecutionPath.GRE_AWARE, f"auto-confirm: {done_action}")
                            return True
                    # Fallback to pass
                    if self._gre_bridge.submit_pass():
                        self._log_execution_path(ExecutionPath.GRE_AWARE, f"auto-confirm: {done_action}")
                        return True
                self._click_fixed("done")
                return True

            # Optional actions with no meaningful actions — auto-decline via
            # submit_optional(False). submit_pass() would fail here because the
            # pending request is OptionalActionMessageRequest, not an
            # ActionsAvailableRequest, and the plugin rejects pass in that state.
            if (
                bridge_request_type in ("OptionalAction", "OptionalActionReq", "OptionalActionRequest", "OptionalActionMessage", "OptionalActionMessageRequest", "OptionalActionMessageReq")
                or bridge_request_class in ("OptionalAction", "OptionalActionReq", "OptionalActionRequest", "OptionalActionMessage", "OptionalActionMessageRequest", "OptionalActionMessageReq")
                or ((game_state.get("decision_context") or {}).get("type") == "optional_action")
            ):
                legal = self._get_legal_actions(game_state)
                # Accept / Decline are the actual meaningful choices and must
                # flow through to the planner so the LLM decides — they are
                # NOT filtered out here on purpose.
                meaningful = [
                    a for a in legal
                    if a.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                    and "Wait" not in a
                ]
                if not meaningful:
                    logger.info("Autopilot: auto-declining optional action (no meaningful actions)")
                    if not self._config.dry_run:
                        if self._gre_bridge.connected or self._gre_bridge.connect():
                            if self._gre_bridge.submit_optional(False):
                                self._log_execution_path(ExecutionPath.GRE_AWARE, "auto-decline optional via submit_optional(False)")
                                return True
                            logger.warning("Autopilot: submit_optional(False) failed — cannot auto-decline")
                    return True

            # "Priority (Pass Only)" means only Pass is legal — auto-pass immediately
            # without LLM planning. MTGA may also auto-pass these, so speed is key.
            if pending == "Priority (Pass Only)":
                logger.info("Autopilot: auto-passing (pass-only priority)")
                if not self._config.dry_run:
                    # Try GRE bridge first (faster, no window focus needed)
                    if self._gre_bridge.connected or self._gre_bridge.connect():
                        if self._gre_bridge.submit_pass():
                            self._log_execution_path(ExecutionPath.GRE_AWARE, "auto-pass via GRE bridge")
                            return True
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                self._exec_pass_priority()
                return True

            # NEVER auto-pass when there's a pending decision (scry, discard, target, etc.)
            if not has_decision:
                # Get legal actions once to check if we can actually do anything
                legal = self._get_legal_actions(game_state)
                # Filter out mana/utility actions that don't represent real decisions.
                # During combat, "Activate Ability" + mana taps are optional and
                # shouldn't prevent auto-pass (the "Next" button in MTGA).
                _PASSTHROUGH = {"pass", "action: activate_mana", "action: floatmana"}
                meaningful = [a for a in legal if a.lower() not in _PASSTHROUGH]

                # During combat, optional ability activations aren't worth calling
                # the LLM for — treat as passthrough (click "Next").
                phase = turn.get("phase", "")
                if "Combat" in phase and meaningful:
                    # Only cast/play/declare actions are meaningful during combat
                    combat_meaningful = [
                        a for a in meaningful
                        if not a.lower().startswith("activate ")
                    ]
                    if not combat_meaningful:
                        logger.info(
                            f"Autopilot: combat auto-pass (only optional activations: "
                            f"{[a for a in meaningful]})"
                        )
                        meaningful = []

                can_do_anything = bool(meaningful) and not all("Wait" in a for a in meaningful)

                if self._config.auto_pass_priority and trigger == "priority_gained":
                    if not can_do_anything:
                        logger.info("Autopilot: auto-passing priority (no actions)")
                        if not self._config.dry_run:
                            self._controller.focus_mtga_window()
                            time.sleep(0.06)
                        self._exec_pass_priority()
                        return True

                if self._config.auto_resolve and trigger == "spell_resolved":
                    if not is_my_turn and not can_do_anything:
                        logger.info("Autopilot: auto-resolving (opponent's spell, no responses)")
                        if not self._config.dry_run:
                            self._controller.focus_mtga_window()
                            time.sleep(0.06)
                        self._exec_resolve()
                        return True

                # Auto-pass stack triggers with no instant-speed responses
                if trigger in ("stack_spell_yours", "stack_spell_opponent"):
                    if not can_do_anything:
                        logger.info(f"Autopilot: auto-passing {trigger} (no instant responses)")
                        if not self._config.dry_run:
                            self._controller.focus_mtga_window()
                            time.sleep(0.06)
                        self._exec_pass_priority()
                        return True

                # Auto-pass opponent's turn with no responses
                if trigger == "opponent_turn":
                    if not can_do_anything:
                        logger.info("Autopilot: auto-passing opponent turn (no responses)")
                        return True  # Just skip, don't click anything

            # --- Clear stashed combat context on turn change ---
            current_turn_num = turn.get("turn_number", 0)
            if current_turn_num != self._last_combat_context_turn and self._last_combat_context is not None:
                logger.debug("Clearing stashed combat context (turn changed)")
                self._last_combat_context = None

            # --- COMBAT STEP GUARD ---
            # During DeclareBlock/DeclareAttack, the LLM often fails to parse
            # and the fallback picks "Pass" which is wrong.  If the game is in
            # a combat step that needs creature selection, handle it directly:
            # click Done (submit with current selection — "no blocks" or
            # "no attacks" if nothing was selected by the planner).
            step = turn.get("step", "")
            if step in ("Step_DeclareBlock", "Step_DeclareAttack"):
                decision_ctx = game_state.get("decision_context") or {}
                dec_type = decision_ctx.get("type", "")
                if dec_type in ("declare_blockers", "declare_attackers"):
                    # Stash this combat context so we can recover it if the
                    # planning call times out and a follow-up trigger fires
                    # without decision_context.
                    self._last_combat_context = dict(decision_ctx)
                    self._last_combat_context_time = time.time()
                    self._last_combat_context_turn = current_turn_num
                    # Let the LLM plan — but if it fails, don't fall back to Pass.
                    # Instead fall through to the planning section which will call
                    # the LLM.  We'll fix the fallback below.
                    pass
                elif trigger in ("combat_blockers", "combat_attackers"):
                    # We got a combat trigger but no decision_context — check
                    # if we have a stashed context from a recent trigger.
                    stashed = self._last_combat_context
                    stashed_age = time.time() - self._last_combat_context_time if stashed else 999
                    stashed_type = (stashed or {}).get("type", "")
                    expected = "declare_attackers" if trigger == "combat_attackers" else "declare_blockers"
                    if stashed and stashed_age < 10.0 and stashed_type == expected:
                        logger.info(
                            f"Autopilot: restoring stashed combat context "
                            f"({stashed_type}, {stashed_age:.1f}s old)"
                        )
                        game_state["decision_context"] = stashed
                        self._last_combat_context = None
                        # Fall through to planning with restored context
                    else:
                        # No usable stashed context — click Done to submit.
                        logger.info(f"Autopilot: combat step {step} without decision context — clicking Done")
                        if not self._config.dry_run:
                            self._controller.focus_mtga_window()
                            time.sleep(0.06)
                        self._click_fixed("done")
                        return True

            # --- 1. PLANNING ---
            self._state = AutopilotState.PLANNING
            self._notify("AUTOPILOT", f"Planning: {trigger}...")

            # Apply escalated timeout to planner if failures have accumulated
            if self._effective_planning_timeout != self._planner._timeout:
                self._planner._timeout = self._effective_planning_timeout

            # Snapshot state before planning (for staleness check)
            pre_plan_turn = game_state.get("turn", {})
            pre_turn_num = pre_plan_turn.get("turn_number", 0)
            pre_phase = pre_plan_turn.get("phase", "")
            pre_active = pre_plan_turn.get("active_player", 0)

            legal_actions = self._get_legal_actions(game_state)
            decision_context = game_state.get("decision_context")

            logger.info(
                f"Autopilot planning: trigger={trigger}, "
                f"legal_actions={len(legal_actions or [])} "
                f"({legal_actions[:3] if legal_actions else []}{'...' if legal_actions and len(legal_actions) > 3 else ''}), "
                f"decision={decision_context.get('type') if decision_context else None}, "
                f"bridge={game_state.get('_bridge_request_type')}"
            )

            plan = self._planner.plan_actions(
                game_state, trigger, legal_actions, decision_context
            )

            if not plan.actions:
                self._consecutive_plan_failures += 1
                logger.warning(
                    f"Autopilot: planner returned no actions "
                    f"(consecutive failures: {self._consecutive_plan_failures})"
                )

                # After 2 failures: escalate timeout (×1.5, cap 15s)
                if self._consecutive_plan_failures >= 2:
                    new_timeout = min(
                        self._effective_planning_timeout * 1.5,
                        15.0,
                    )
                    if new_timeout != self._effective_planning_timeout:
                        self._effective_planning_timeout = new_timeout
                        logger.info(
                            f"Autopilot: escalated planning timeout to "
                            f"{self._effective_planning_timeout:.1f}s"
                        )

                # After 4 failures: use deterministic fallback
                if self._consecutive_plan_failures >= 4:
                    logger.warning("Autopilot: 4+ consecutive failures, using deterministic fallback")
                    plan = self._deterministic_fallback(
                        game_state, trigger, legal_actions, decision_context
                    )

                if not plan.actions:
                    if self._is_critical_decision_state(game_state):
                        self._pause_for_manual("Planner produced no safe action", game_state)
                        return False

                    # Planner couldn't produce actions. Try auto_respond only
                    # for explicitly safe low-risk fallback cases.
                    if (
                        not self._config.dry_run
                        and self._should_allow_auto_respond(game_state)
                        and (self._gre_bridge.connected or self._gre_bridge.connect())
                    ):
                        if self._gre_bridge.auto_respond():
                            self._log_execution_path(ExecutionPath.GRE_AWARE, "auto_respond (planner empty)")
                            logger.warning(
                                f"AUTO_RESPOND_FALLBACK (planner empty): trigger={trigger}, "
                                f"legal_actions={legal_actions}, "
                                f"decision={(decision_context or {}).get('type')}, "
                                f"bridge={game_state.get('_bridge_request_type')} — "
                                "needs proper planner/bridge handling"
                            )
                            self._state = AutopilotState.IDLE
                            return True
                    # Last resort: try pass
                    meaningful = [
                        a for a in (legal_actions or [])
                        if a.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                        and "Wait" not in a
                    ]
                    if not meaningful:
                        logger.info("Autopilot: auto-passing (planner empty, no meaningful actions)")
                        self._exec_pass_priority()
                        self._state = AutopilotState.IDLE
                        return True
                    self._state = AutopilotState.IDLE
                    return False

            # --- STALENESS CHECK ---
            # Re-poll game state after planning (LLM call may take 5-15s).
            # If the game has moved on (different turn, phase, or active player),
            # discard the stale plan instead of executing outdated actions.
            # Skip for pre-game actions (mulligan, choose starting player) —
            # nothing to go stale and the bridge poll can block during animations.
            _skip_staleness = (
                plan.actions
                and plan.actions[0].action_type in (
                    ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL,
                    ActionType.CHOOSE_STARTING_PLAYER,
                    ActionType.PASS_PRIORITY, ActionType.RESOLVE,
                    ActionType.CLICK_BUTTON,
                )
            )
            if not _skip_staleness:
                try:
                    fresh_state = self._get_game_state()
                    fresh_turn = fresh_state.get("turn", {})
                    stale = False

                    if fresh_turn.get("turn_number", 0) != pre_turn_num:
                        logger.warning(f"STALE: turn advanced {pre_turn_num} → {fresh_turn.get('turn_number')}")
                        stale = True
                    elif fresh_turn.get("active_player", 0) != pre_active:
                        logger.warning(f"STALE: active player changed {pre_active} → {fresh_turn.get('active_player')}")
                        stale = True
                    elif fresh_turn.get("phase", "") != pre_phase:
                        is_sorcery_play = any(a.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL) for a in plan.actions)
                        now_combat = "Combat" in fresh_turn.get("phase", "")

                        if is_sorcery_play and now_combat:
                            logger.warning(f"STALE: phase changed {pre_phase} → {fresh_turn.get('phase')} (sorcery plan in combat)")
                            stale = True
                        else:
                            logger.info(f"Phase changed {pre_phase} → {fresh_turn.get('phase')}, proceeding with caution")

                    if stale:
                        self._notify("AUTOPILOT", "Plan discarded (game moved on)")
                        self._record_user_takeover(
                            plan, fresh_state,
                            reason="plan_went_stale_after_llm",
                        )
                        self._state = AutopilotState.IDLE
                        return False

                    # Use the fresh state for execution (more accurate coordinates)
                    game_state = fresh_state
                except Exception as e:
                    logger.error(f"Staleness check failed: {e}")
                    # Continue with original state if re-poll fails

            self._current_plan = plan
            self._current_action_idx = 0

            # --- 2. PREVIEWING (auto-execute countdown) ---
            self._state = AutopilotState.PREVIEWING
            plan_text = self._format_plan_preview(plan)

            self._notify("AUTOPILOT", plan_text)
            if self._config.enable_tts_preview and self._speak_fn:
                # Run TTS in a background thread so synthesis/model-load
                # never blocks the execution countdown.
                threading.Thread(
                    target=self._speak_fn,
                    args=(plan.voice_advice or plan.overall_strategy, False),
                    daemon=True,
                ).start()

            # Auto-execute countdown: executes after delay unless user cancels
            if self._config.confirm_plan:
                # Legacy mode: wait for explicit F1 confirm
                logger.info("Waiting for plan confirmation (F1)...")
                result = self._wait_for_confirmation()
                if result == "abort":
                    self._state = AutopilotState.IDLE
                    self._notify("AUTOPILOT", "Aborted")
                    return False
                if result == "skip":
                    self._state = AutopilotState.IDLE
                    self._actions_skipped += len(plan.actions)
                    self._notify("AUTOPILOT", "Plan skipped")
                    return False
            elif self._config.auto_execute_delay > 0:
                # New default: auto-execute after countdown, F1/F4 cancels
                delay = self._config.auto_execute_delay
                self._notify("AUTOPILOT", f"Executing in {delay:.1f}s... [F1/F4 to cancel]")
                result = self._wait_for_cancel(delay)
                if result == "abort":
                    self._state = AutopilotState.IDLE
                    self._notify("AUTOPILOT", "Aborted")
                    return False
                if result == "cancel":
                    self._state = AutopilotState.IDLE
                    self._actions_skipped += len(plan.actions)
                    self._notify("AUTOPILOT", "Plan cancelled by user")
                    return False
                # result == "execute" → proceed

            # --- PRE-EXECUTION STALENESS RECHECK ---
            # The countdown may have consumed up to 1s. Re-poll game state to
            # make sure the game hasn't moved on during that window.
            try:
                exec_state = self._get_game_state()
                exec_turn = exec_state.get("turn", {})
                if (exec_turn.get("turn_number", 0) != pre_turn_num
                        or exec_turn.get("active_player", 0) != pre_active):
                    logger.warning("STALE at execution time — game moved on during countdown")
                    self._notify("AUTOPILOT", "Plan discarded (game moved during countdown)")
                    self._record_user_takeover(
                        plan, exec_state,
                        reason="plan_went_stale_during_countdown",
                    )
                    self._state = AutopilotState.IDLE
                    return False
                game_state = exec_state  # Use freshest state
            except Exception as e:
                logger.error(f"Pre-execution recheck failed: {e}")

            # --- LEGAL ACTIONS GUARDRAIL ---
            # Reject cast/play actions the LLM hallucinated — if the card
            # isn't in MTGA's legal actions list, it can't be played.
            fresh_legal = self._get_legal_actions(game_state) or []
            if fresh_legal and plan.actions:
                validated = []
                for action in plan.actions:
                    if action.action_type in (ActionType.CAST_SPELL, ActionType.PLAY_LAND):
                        card = (action.card_name or "").lower().strip()
                        # Check if any legal action mentions this card
                        legal_match = any(
                            card and card in la.lower()
                            for la in fresh_legal
                            if la.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                        )
                        if not legal_match and card:
                            logger.warning(
                                f"Rejecting hallucinated action: {action.action_type.value} "
                                f"'{action.card_name}' not in legal actions: {fresh_legal[:5]}"
                            )
                            self._notify("AUTOPILOT", f"Rejected: {action.card_name} (not legal)")
                            continue
                    validated.append(action)
                if len(validated) < len(plan.actions):
                    logger.info(
                        f"Legal actions guardrail: {len(plan.actions)} planned -> "
                        f"{len(validated)} valid"
                    )
                    plan = ActionPlan(
                        actions=validated,
                        overall_strategy=plan.overall_strategy,
                    )

            # --- 3. EXECUTING ---
            self._state = AutopilotState.EXECUTING
            self._gre_bridge_failed_methods = set()

            # Focus MTGA now — no more user input expected (skip if GRE bridge is active)
            if not self._config.dry_run and not self._gre_bridge.connected:
                self._controller.focus_mtga_window()
                time.sleep(0.06)

            for i, action in enumerate(plan.actions):
                if self._abort_event.is_set():
                    self._state = AutopilotState.IDLE
                    return False

                # Some backends still emit an extra "click done" after actions
                # whose handlers already click Done internally.
                if (
                    i > 0
                    and action.action_type == ActionType.CLICK_BUTTON
                    and action.card_name.lower().strip() == "done"
                    and plan.actions[i - 1].action_type
                    in (
                        ActionType.DECLARE_ATTACKERS,
                        ActionType.DECLARE_BLOCKERS,
                        ActionType.SELECT_N,
                        ActionType.ORDER_BLOCKERS,
                    )
                ):
                    logger.info("Skipping redundant Done action after auto-confirming handler")
                    continue

                self._current_action_idx = i

                action_text = f"[{i+1}/{len(plan.actions)}] {action}"
                self._notify("AUTOPILOT", action_text)

                if self._is_action_blocked(action, game_state):
                    self._pause_for_manual("Blocked action repeated in the same priority window", game_state)
                    return False

                # Per-action staleness check: verify game hasn't advanced
                # between multi-step actions (e.g., declare attackers then done)
                if i > 0:
                    try:
                        step_state = self._get_game_state()
                        step_turn = step_state.get("turn", {})
                        if step_turn.get("turn_number", 0) != pre_turn_num:
                            logger.warning(f"STALE mid-execution: turn advanced at action {i+1}")
                            self._notify("AUTOPILOT", "Stopping: game advanced mid-plan")
                            self._state = AutopilotState.IDLE
                            return False
                        game_state = step_state
                    except Exception as e:
                        logger.debug(f"Mid-execution staleness check failed (non-fatal): {e}")

                # Legacy per-action confirmation (only if explicitly enabled)
                if self._config.confirm_each_action:
                    result = self._wait_for_confirmation()
                    if result == "abort":
                        self._state = AutopilotState.IDLE
                        return False
                    if result == "skip":
                        self._actions_skipped += 1
                        continue

                # Snapshot state before action (for verification)
                pre_state = self._get_game_state() if self._config.verify_after_action else None

                # Execute
                click_result = self._execute_action(action, game_state)
                if not click_result.success:
                    logger.warning(f"Action failed: {click_result}")
                    self._notify("AUTOPILOT", f"FAILED: {click_result.error}")
                    # Block this action from being retried in the same priority
                    # window. Without this, an action the bridge can't handle
                    # (e.g. an auto-pick fallback whose name doesn't resolve)
                    # gets re-planned on every backstop tick → infinite loop.
                    self._mark_action_blocked(action, game_state, f"execute failed: {click_result.error}")
                    continue

                self._actions_executed += 1

                # --- 4. VERIFYING ---
                if self._config.verify_after_action and pre_state:
                    self._state = AutopilotState.VERIFYING
                    verified = self._verify_action(action, pre_state)
                    if not verified:
                        logger.warning(f"Action verification failed for: {action}")
                        self._notify("AUTOPILOT", "Verification: state unchanged (may be OK)")
                        self._consecutive_failed_verifications += 1
                        
                        if self._consecutive_failed_verifications >= 3:
                            self._recover_stuck()
                    else:
                        self._consecutive_failed_verifications = 0

                # Delay between actions
                if i < len(plan.actions) - 1:
                    self._controller.wait(self._config.action_delay, "between actions")

            self._state = AutopilotState.IDLE
            self._plans_completed += 1
            plan_had_failures = self._consecutive_failed_verifications > 0
            self._notify("AUTOPILOT", f"Plan complete ({len(plan.actions)} actions)")

            # Reset consecutive failure counter + timeout on success
            if self._consecutive_plan_failures > 0:
                logger.info(
                    f"Autopilot: resetting plan failure counter "
                    f"(was {self._consecutive_plan_failures})"
                )
                self._consecutive_plan_failures = 0
                self._effective_planning_timeout = self._config.planning_timeout

            # --- POST-PLAN: continue turn if we still have priority ---
            # After executing a plan, we may still have priority with legal
            # actions (e.g. played a land, can still cast spells).  Re-poll
            # immediately and re-trigger rather than waiting ~10-20s for the
            # coaching loop to detect a state change.
            try:
                post_plan_state = self._get_game_state()
                post_pending = post_plan_state.get("pending_decision")
                should_continue = False

                if post_pending:
                    # ETB choices, scry, discard, target selection, etc.
                    logger.info(f"Post-plan follow-up decision detected: '{post_pending}'")
                    should_continue = True
                else:
                    # Check if we still have priority with meaningful actions
                    post_turn = post_plan_state.get("turn", {})
                    post_local_seat = None
                    for p in post_plan_state.get("players", []):
                        if p.get("is_local"):
                            post_local_seat = p.get("seat_id")
                    has_priority = (
                        post_turn.get("priority_player") == post_local_seat
                        and post_local_seat is not None
                    )
                    if has_priority:
                        post_legal = self._get_legal_actions(post_plan_state)
                        _PASSTHROUGH = {"pass", "action: activate_mana", "action: floatmana"}
                        post_meaningful = [
                            a for a in post_legal if a.lower() not in _PASSTHROUGH
                        ]
                        if post_meaningful:
                            logger.info(
                                f"Post-plan: still have priority with {len(post_meaningful)} "
                                f"meaningful actions, continuing turn"
                            )
                            should_continue = True

                # Skip continuation if the plan had failed verifications —
                # the game state hasn't actually changed and re-planning
                # will likely repeat the same failed action (e.g. grabbing
                # the wrong card due to hand sort mismatch).
                if plan_had_failures and not post_pending:
                    logger.info(
                        "Post-plan: skipping continuation (verification "
                        "failures — game state may not have changed)"
                    )
                    should_continue = False

                if should_continue and self._continuation_depth < self._MAX_CONTINUATION_DEPTH:
                    # Release lock temporarily so process_trigger can re-acquire
                    self._lock.release()
                    self._continuation_depth += 1
                    try:
                        self.process_trigger(post_plan_state, "decision_required")
                    finally:
                        self._continuation_depth -= 1
                        # Re-acquire for the outer finally block
                        self._lock.acquire()
                elif should_continue:
                    logger.warning(
                        f"Post-plan: skipping continuation (depth {self._continuation_depth} "
                        f">= max {self._MAX_CONTINUATION_DEPTH})"
                    )
            except Exception as e:
                logger.warning(f"Post-plan follow-up handling failed: {e}")

            return True
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass  # Lock was externally released (e.g. toggle off/on)

    def _deterministic_fallback(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: list[str],
        decision_context: Optional[dict[str, Any]],
    ) -> ActionPlan:
        """Build a plan using deterministic heuristics when LLM planning fails.

        Called after multiple consecutive planning failures to ensure the
        autopilot takes *some* action instead of doing nothing.

        Priority:
        1. Declare attackers → attack with all legal attackers
        2. Declare blockers → click Done (no blocks)
        3. Use _pick_preferred_legal_action() + _legal_action_to_action()
        4. Last resort → pass priority
        """
        plan = ActionPlan(trigger=trigger)
        plan.turn_number = game_state.get("turn", {}).get("turn_number", 0)
        ctx = decision_context or game_state.get("decision_context") or {}
        dec_type = ctx.get("type", "")

        if dec_type == "declare_attackers":
            attackers = ctx.get("legal_attackers", [])
            if attackers:
                plan.actions = [
                    GameAction(
                        action_type=ActionType.DECLARE_ATTACKERS,
                        attacker_names=list(attackers),
                        reasoning="deterministic fallback: attack with all",
                    )
                ]
                plan.overall_strategy = "Fallback: attack with all legal attackers"
                logger.info(f"Deterministic fallback: attacking with {attackers}")
                return plan

        if dec_type == "declare_blockers":
            plan.actions = [
                GameAction(
                    action_type=ActionType.CLICK_BUTTON,
                    card_name="done",
                    reasoning="deterministic fallback: no blocks",
                )
            ]
            plan.overall_strategy = "Fallback: no blocks (click Done)"
            logger.info("Deterministic fallback: no blocks")
            return plan

        # Try the planner's static heuristic
        if legal_actions:
            selected = ActionPlanner._pick_preferred_legal_action(legal_actions)
            if selected:
                action = self._planner._legal_action_to_action(selected)
                if action:
                    action.reasoning = "deterministic fallback"
                    plan.actions = [action]
                    plan.overall_strategy = f"Fallback: {selected}"
                    logger.info(f"Deterministic fallback: {selected}")
                    return plan

        # Last resort: pass priority
        plan.actions = [
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning="deterministic fallback: last resort pass",
            )
        ]
        plan.overall_strategy = "Fallback: pass priority (last resort)"
        logger.info("Deterministic fallback: pass priority")
        return plan

    def _handle_afk(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Handle a trigger in AFK mode — auto-pass without LLM.

        AFK mode clicks pass/resolve/done for all priority decisions.
        For mandatory choices (mulligan, scry), picks the "safe default":
        - Mulligan: keep hand
        - Scry: scry to bottom
        - Declare Attackers/Blockers: skip (don't attack/block)
        - Choose Play/Draw: choose play
        - All other decisions: click Done/spacebar
        """
        pending = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")

        # Mandatory decisions that need a specific click
        if pending:
            pending_lower = pending.lower() if isinstance(pending, str) else ""

            if "mulligan" in pending_lower:
                logger.info("AFK: keeping hand (mulligan)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("keep").success

            if "scry" in pending_lower:
                logger.info("AFK: scry to bottom")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("scry_bottom").success

            # New decision types from expanded GRE handling
            if dec_type == "declare_attackers":
                logger.info("AFK: skipping attackers (click Done)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("done").success

            if dec_type == "declare_blockers":
                logger.info("AFK: skipping blockers (click Done)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("done").success

            if dec_type == "choose_starting_player":
                logger.info("AFK: choosing to play")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                # "Play" is typically the first option
                return self._click_fixed("pass").success

            if dec_type in (
                "assign_damage", "order_combat_damage", "pay_costs",
                "search", "distribution", "numeric_input",
                "select_replacement", "casting_time_options",
                "select_counters", "order_triggers",
                "select_n_group", "select_from_groups",
                "search_from_groups", "gather",
            ):
                logger.info(f"AFK: auto-accepting decision '{dec_type}' (click Done)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                result = self._click_fixed("done")
                if result.success:
                    return True
                # Done didn't work, try spacebar
                self._controller.press_key("space", f"AFK: {dec_type} spacebar")
                return True

            # Unknown decision: try Done button, then spacebar
            if pending_lower and "mulligan" not in pending_lower and "scry" not in pending_lower:
                logger.warning(f"AFK: unknown decision '{pending}' - trying Done button")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                result = self._click_fixed("done")
                if result.success:
                    return True
                # Done didn't work, try spacebar
                self._controller.press_key("space", "AFK: unknown decision spacebar")
                return True

        # Everything else: click pass/resolve/done
        logger.info(f"AFK: passing ({trigger})")
        if not self._config.dry_run:
            self._controller.focus_mtga_window()
            time.sleep(0.06)
        return self._exec_pass_priority().success

    def _handle_land_drop(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Handle a trigger in land-drop-only mode.

        Automatically plays one land per turn by dragging it from hand to
        the battlefield. No LLM is used. All other priority passes are
        auto-resolved so the game keeps moving.
        """
        turn = game_state.get("turn", {})
        phase = turn.get("phase", "")
        local_seat = None
        local_player = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                local_player = p

        is_my_turn = turn.get("active_player") == local_seat if local_seat else False
        is_main_phase = "Main" in phase
        stack = game_state.get("stack", [])
        is_stack_empty = len(stack) == 0
        lands_played = local_player.get("lands_played", 0) if local_player else 1
        turn_number = turn.get("turn_number", 0)

        # Check if we can play a land right now
        # Also guard against double-triggers: if we already dragged a land
        # this turn, skip (the server may not have confirmed lands_played yet)
        already_played_this_turn = self._land_drop_last_turn == turn_number
        if is_my_turn and is_main_phase and is_stack_empty and lands_played < 1 and not already_played_this_turn:
            hand = game_state.get("hand", [])
            land_card = None
            for card in hand:
                card_types = card.get("card_types", [])
                type_line = card.get("type_line", "")
                if any("Land" in ct for ct in card_types) or "Land" in type_line:
                    land_card = card
                    break

            if land_card:
                land_name = land_card.get("name", "Land")
                logger.info(f"LAND DROP: playing {land_name}")
                self._notify("LAND_DROP", f"Playing {land_name}")

                coord = self._mapper.get_card_in_hand_coord(
                    land_name, hand, game_state
                )
                if coord:
                    window_rect = self._mapper.window_rect
                    if not window_rect:
                        window_rect = self._mapper.refresh_window()
                    if window_rect:
                        if not self._config.dry_run:
                            self._controller.focus_mtga_window()
                            time.sleep(0.06)

                        from_x, from_y = coord.to_absolute(window_rect)
                        # Drag to center of battlefield (y ≈ 0.50)
                        target = ScreenCoord(0.50, 0.50, f"Battlefield: {land_name}")
                        to_x, to_y = target.to_absolute(window_rect)

                        result = self._controller.drag_card_from_hand(
                            from_x, from_y, to_x, to_y, land_name, window_rect
                        )
                        if result.success:
                            self._actions_executed += 1
                            self._land_drop_last_turn = turn_number
                            logger.info(f"LAND DROP: {land_name} played successfully")
                            return True
                        else:
                            logger.warning(f"LAND DROP: drag failed: {result.error}")
                else:
                    logger.warning(f"LAND DROP: could not map {land_name} in hand")

        # For everything else, auto-pass to keep the game moving
        pending = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")

        if pending:
            pending_lower = pending.lower() if isinstance(pending, str) else ""
            if "mulligan" in pending_lower:
                logger.info("LAND DROP: keeping hand (mulligan)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("keep").success
            if "scry" in pending_lower:
                logger.info("LAND DROP: scry to bottom")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("scry_bottom").success

            # New decision types: auto-pass combat, auto-accept others
            if dec_type in ("declare_attackers", "declare_blockers"):
                logger.info(f"LAND DROP: skipping {dec_type} (click Done)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("done").success

            if dec_type == "choose_starting_player":
                logger.info("LAND DROP: choosing to play")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                return self._click_fixed("pass").success

            if dec_type in (
                "assign_damage", "order_combat_damage", "pay_costs",
                "search", "distribution", "numeric_input",
                "select_replacement", "casting_time_options",
                "select_counters", "order_triggers",
                "select_n_group", "select_from_groups",
                "search_from_groups", "gather",
            ):
                logger.info(f"LAND DROP: auto-accepting decision '{dec_type}' (click Done)")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                result = self._click_fixed("done")
                if result.success:
                    return True
                self._controller.press_key("space", f"LAND DROP: {dec_type} spacebar")
                return True

            # Unknown decision: try Done button, then spacebar
            if pending_lower and "mulligan" not in pending_lower and "scry" not in pending_lower:
                logger.warning(f"LAND DROP: unknown decision '{pending}' - trying Done button")
                if not self._config.dry_run:
                    self._controller.focus_mtga_window()
                    time.sleep(0.06)
                result = self._click_fixed("done")
                if result.success:
                    return True
                self._controller.press_key("space", "LAND DROP: unknown decision spacebar")
                return True

        logger.info(f"LAND DROP: passing ({trigger})")
        if not self._config.dry_run:
            self._controller.focus_mtga_window()
            time.sleep(0.06)
        return self._exec_pass_priority().success

    def _get_legal_actions(self, game_state: dict[str, Any]) -> list[str]:
        """Get legal actions from the rules engine."""
        try:
            from arenamcp.rules_engine import RulesEngine
            return RulesEngine.get_legal_actions(game_state)
        except Exception as e:
            logger.error(f"Failed to get legal actions: {e}")
            return []

    def _format_plan_preview(self, plan: ActionPlan) -> str:
        """Format a plan for human-readable preview."""
        lines = [f"PLAN: {plan.overall_strategy}"]
        for i, action in enumerate(plan.actions, 1):
            lines.append(f"  {i}. {action}")
            if action.reasoning:
                lines.append(f"     ({action.reasoning})")
        delay = self._config.auto_execute_delay
        if delay > 0 and not self._config.confirm_plan:
            lines.append(f"[Auto-executing in {delay:.0f}s | F1/F4=cancel | F11=abort]")
        else:
            lines.append("[F1=confirm | F4=skip | F11=abort]")
        return "\n".join(lines)

    def _notify(self, label: str, text: str) -> None:
        """Send notification to UI."""
        logger.info(f"[{label}] {text}")
        if self._ui_advice_fn:
            try:
                self._ui_advice_fn(text, label)
            except Exception as e:
                logger.debug(f"UI notification callback failed: {e}")

    def _report_fallback_bug(
        self,
        action: GameAction,
        game_state: dict[str, Any],
        reason_tag: str,
    ) -> None:
        """Record a bridge-fallback event for end-of-match telemetry.

        Events are buffered during the match; at match end we sample up to
        `_max_fallback_bugs_per_match` at random and dispatch those to the
        bug_report callback. The rest are discarded, giving representative
        coverage without flooding GitHub with duplicates from stuck loops.
        """
        if self._bug_report_fn is None:
            return

        gre_ref = getattr(action, "gre_action_ref", None)
        ref_info = None
        if gre_ref is not None:
            try:
                ref_info = gre_ref.to_dict() if hasattr(gre_ref, "to_dict") else {
                    "action_type": getattr(gre_ref, "action_type", ""),
                    "grp_id": getattr(gre_ref, "grp_id", 0),
                    "instance_id": getattr(gre_ref, "instance_id", 0),
                }
            except Exception:
                ref_info = None

        bridge_info = {
            "connected": getattr(self._gre_bridge, "connected", False),
            "failed_methods": sorted(self._gre_bridge_failed_methods),
        }

        extra = {
            "auto_fallback_bug": {
                "reason_tag": reason_tag,
                "action_type": action.action_type.value,
                "card_name": action.card_name or "",
                "target_names": list(action.target_names or []),
                "attacker_names": list(action.attacker_names or []),
                "blocker_assignments": dict(action.blocker_assignments or {}),
                "select_card_names": list(action.select_card_names or []),
                "modal_index": action.modal_index,
                "numeric_value": action.numeric_value,
                "gre_action_ref": ref_info,
                "bridge": bridge_info,
                "bridge_request_type": game_state.get("_bridge_request_type"),
                "bridge_request_class": game_state.get("_bridge_request_class"),
                "decision_context": game_state.get("decision_context"),
                "turn": (game_state.get("turn") or {}).get("turn_number"),
                "phase": (game_state.get("turn") or {}).get("phase"),
                "timestamp": time.time(),
            }
        }
        reason = (
            f"auto: bridge fallback ({reason_tag}) on "
            f"{action.action_type.value} {action.card_name or ''}".strip()
        )
        # Buffer — the match-end flush will pick winners.
        self._pending_fallback_bugs.append((reason, extra))

    def _record_user_takeover(
        self,
        plan: Any,
        game_state: dict[str, Any],
        reason: str,
    ) -> None:
        """Record a user-takeover event for end-of-match telemetry.

        When autopilot's plan goes stale because the game state advanced
        before we could execute (the user likely acted manually, OR the
        game auto-resolved), we buffer a bug report. If autopilot was
        supposed to handle this but the user had to step in, we want to
        know what autopilot was going to propose so we can improve it.
        """
        if self._bug_report_fn is None:
            return

        actions = getattr(plan, "actions", None) or []
        first = actions[0] if actions else None
        action_type = getattr(first, "action_type", None)
        action_type_str = action_type.value if action_type else "?"
        card_name = getattr(first, "card_name", "") or ""

        extra = {
            "auto_user_takeover": {
                "reason_tag": reason,
                "planned_action": action_type_str,
                "planned_card": card_name,
                "planned_strategy": getattr(plan, "overall_strategy", ""),
                "planned_voice_advice": getattr(plan, "voice_advice", ""),
                "num_planned_actions": len(actions),
                "bridge_connected": getattr(self._gre_bridge, "connected", False),
                "bridge_request_type": game_state.get("_bridge_request_type"),
                "bridge_request_class": game_state.get("_bridge_request_class"),
                "decision_context": game_state.get("decision_context"),
                "turn": (game_state.get("turn") or {}).get("turn_number"),
                "phase": (game_state.get("turn") or {}).get("phase"),
                "timestamp": time.time(),
            }
        }
        reason_str = (
            f"auto: user took over from autopilot ({reason}) — "
            f"planned {action_type_str} {card_name}".strip()
        )
        self._pending_fallback_bugs.append((reason_str, extra))

    def flush_fallback_bugs_for_match(self) -> int:
        """Dispatch up to N sampled fallback bugs from the current match.

        Called at match end by the standalone coach. Clears the buffer
        whether or not events are dispatched. Returns the number sent.
        """
        buf = self._pending_fallback_bugs
        self._pending_fallback_bugs = []
        if not buf or self._bug_report_fn is None:
            return 0

        import random
        cap = max(1, int(self._max_fallback_bugs_per_match))
        if len(buf) <= cap:
            picked = list(buf)
        else:
            picked = random.sample(buf, cap)

        logger.info(
            f"Flushing {len(picked)}/{len(buf)} fallback bug(s) "
            f"from this match (max {cap})"
        )
        for reason, extra in picked:
            try:
                threading.Thread(
                    target=self._bug_report_fn,
                    args=(reason, extra),
                    daemon=True,
                ).start()
            except Exception as e:
                logger.debug(f"fallback-bug dispatch failed: {e}")
        return len(picked)

    def _get_vision_coord(self, card_name: str, zone: Optional[str] = None) -> Optional[ScreenCoord]:
        """Capture screenshot and use vision to find a card.

        If VisionMapper is active, routes through its tiered lookup
        (cache → local VLM → cloud VLM). Otherwise falls back to the
        legacy single-shot cloud vision call.
        """
        try:
            png_bytes = self._capture_screenshot()
            if not png_bytes:
                return None

            # VisionMapper path: uses tiered cache → local VLM → cloud VLM
            if self._has_vision_scan and hasattr(self._mapper, 'get_element_coord'):
                return self._mapper.get_element_coord(
                    card_name, zone=zone, screenshot_bytes=png_bytes
                )

            # Legacy path: single-shot cloud vision call
            backend = self._planner._backend
            return self._mapper.get_card_coord_via_vision(card_name, png_bytes, backend)
        except Exception as e:
            logger.error(f"Failed to get vision coord: {e}")
            return None

    def _recover_stuck(self) -> None:
        """Attempt to recover from a stuck state (UI prompts, dialogs, etc)."""
        self._notify("AUTOPILOT", "STUCK DETECTED: Attempting recovery...")

        # 1. Re-poll state to see if there's a pending decision we can re-plan for
        try:
            fresh_state = self._get_game_state()
            pending = fresh_state.get("pending_decision")
            if pending and pending != "Action Required":
                logger.info(f"Stuck recovery: found pending decision '{pending}', re-planning")
                self._notify("AUTOPILOT", f"Re-planning for: {pending}")
                legal = self._get_legal_actions(fresh_state)
                plan = self._planner.plan_actions(fresh_state, "decision_required", legal)
                if plan.actions:
                    for action in plan.actions:
                        self._execute_action(action, fresh_state)
                        time.sleep(self._config.action_delay)
                    self._consecutive_failed_verifications = 0
                    return
        except Exception as e:
            logger.error(f"Stuck recovery re-plan failed: {e}")

        # 2. Try common dismissal actions (NOT Escape — it opens MTGA menu)
        logger.warning("Stuck recovery: clicking Done button and pressing Spacebar")
        self._controller.focus_mtga_window()
        time.sleep(0.2)
        self._click_fixed("done")  # Done/Submit button
        time.sleep(0.5)
        self._controller.press_key("space", "Confirming priority")

        # 3. Vision analysis of the stuck state
        logger.info("Stuck recovery: Analyzing screen via vision")
        coord = self._get_vision_coord("Blocking UI Prompt")
        if coord:
            logger.info(f"Vision suggests stuck UI element at {coord}")
            abs_x, abs_y = coord.to_absolute(self._mapper.window_rect)
            self._controller.click(abs_x, abs_y, "Dismissing via vision")

        self._consecutive_failed_verifications = 0

    # --- Action Execution Handlers ---

    def _try_gre_bridge(
        self,
        action: GameAction,
        game_state: dict[str, Any],
    ) -> Optional[ClickResult]:
        """Try to execute an action via the GRE bridge (direct submission).

        Returns a ClickResult if the bridge handled it, or None to fall
        through to mouse-click execution.
        """
        if game_state.get("game_engine_busy"):
            return None

        if not self._gre_bridge.connect():
            return None

        method = action.action_type.value
        if method in self._gre_bridge_failed_methods:
            return None

        # DECLARE ATTACKERS and DECLARE BLOCKERS bypass the bridge-idle check
        # because their dedicated methods query get_pending_actions() directly
        # and will fail gracefully if no request is actually pending.
        # The bridge-idle check uses game_state metadata that may not be
        # populated at the moment this method is called, causing false skips.
        if action.action_type == ActionType.DECLARE_ATTACKERS:
            return self._try_bridge_declare_attackers(action)

        if action.action_type == ActionType.DECLARE_BLOCKERS:
            return self._try_gre_bridge_blockers(action)

        if game_state.get("_bridge_connected") and not (
            game_state.get("_bridge_request_type")
            or game_state.get("_bridge_request_class")
            or game_state.get("_bridge_has_pending")
        ):
            logger.info("GRE bridge execution skipped: bridge is connected but reports no pending window")
            return None

        gre_ref = getattr(action, 'gre_action_ref', None)

        # PASS / RESOLVE — use bridge submit_pass
        if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON):
            if self._gre_bridge.submit_pass():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"{action.action_type.value}: submitted via GRE bridge (pass)"
                )
                return ClickResult(True, 0, 0, "pass", "GRE bridge")
            logger.info("GRE bridge pass failed, falling back to mouse click")
            self._gre_bridge_failed_methods.add(method)
            return None

        # MULLIGAN — submit keep/mulligan via bridge
        if action.action_type in (ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL):
            keep = action.action_type == ActionType.MULLIGAN_KEEP
            if self._gre_bridge.submit_mulligan(keep):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"mulligan: {'keep' if keep else 'mulligan'} via GRE bridge"
                )
                return ClickResult(True, 0, 0, "mulligan", "GRE bridge")
            logger.info("GRE bridge mulligan failed, falling back to clicks")
            self._gre_bridge_failed_methods.add(method)
            return None

        # CHOOSE STARTING PLAYER — submit play/draw via bridge
        if action.action_type == ActionType.CHOOSE_STARTING_PLAYER:
            local_seat = None
            opp_seat = None
            for p in game_state.get("players", []):
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
                else:
                    opp_seat = p.get("seat_id")
            # play_or_draw field from LLM: "play" means we go first (our seat)
            seat = local_seat if getattr(action, 'play_or_draw', 'play') == 'play' else opp_seat
            if seat and self._gre_bridge.submit_choose_starting_player(seat):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"choose_starting_player: seat {seat} via GRE bridge"
                )
                return ClickResult(True, 0, 0, "choose_starting_player", "GRE bridge")
            logger.info("GRE bridge choose_starting_player failed, falling back to clicks")
            self._gre_bridge_failed_methods.add(method)
            return None

        # SELECT TARGET — submit via bridge if target instance IDs are resolvable
        if action.action_type == ActionType.SELECT_TARGET:
            return self._try_gre_bridge_select_target(action)

        # SELECT_N / SEARCH_LIBRARY / SELECT_COUNTERS — submit via bridge to
        # avoid the mouse-click fallback which clicks by list index and
        # frequently misses the actual option positions (causing stuck loops
        # on things like Lluwen's ETB search).
        if action.action_type in (
            ActionType.SELECT_N,
            ActionType.SEARCH_LIBRARY,
            ActionType.SELECT_COUNTERS,
        ):
            # Scry / surveil / similar library-top ordering is a GroupRequest,
            # not a SelectN. Route it through submit_group so the client sends
            # the proper GroupResp with top/bottom zones populated.
            bridge_req_type = (
                game_state.get("_bridge_request_type")
                or game_state.get("_bridge_request_class")
                or ""
            )
            if "Group" in str(bridge_req_type):
                result = self._try_gre_bridge_scry(action, game_state)
                if result is not None:
                    return result

            result = self._try_gre_bridge_select_n(action, game_state)
            if result is not None:
                return result

        # MODAL CHOICE / CASTING OPTIONS — submit via bridge by matching
        # CastingTimeOption entries (actionType="CastingTimeOption").
        # The generic type-match path can't handle these because the bridge
        # uses "CastingTimeOption" not "ActionType_Cast" etc.
        if action.action_type in (ActionType.MODAL_CHOICE, ActionType.CASTING_OPTIONS):
            result = self._try_bridge_casting_time_option(action)
            if result:
                return result

        # For actions with a GRE ref, match by identity fields
        if gre_ref is not None:
            action_type = gre_ref.action_type if hasattr(gre_ref, 'action_type') else ""
            grp_id = gre_ref.grp_id if hasattr(gre_ref, 'grp_id') else 0
            instance_id = gre_ref.instance_id if hasattr(gre_ref, 'instance_id') else 0
            ability_grp_id = gre_ref.ability_grp_id if hasattr(gre_ref, 'ability_grp_id') else 0

            if self._gre_bridge.submit_action_by_match(
                action_type=action_type,
                grp_id=grp_id,
                instance_id=instance_id,
                ability_grp_id=ability_grp_id,
                auto_pass=self._config.auto_pass_priority,
            ):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"{action.action_type.value}: '{action.card_name}' submitted via GRE bridge"
                )
                return ClickResult(True, 0, 0, action.card_name or str(action), "GRE bridge")

            logger.info(f"GRE bridge match failed for {action.action_type.value}, falling back to mouse")
            self._gre_bridge_failed_methods.add(method)
            return None

        # No GRE ref but bridge is connected — try matching by game action type
        from arenamcp.gre_action_matcher import ACTION_TYPE_MAP
        gre_type = ACTION_TYPE_MAP.get(action.action_type)
        if gre_type:
            # Use preloaded bridge actions if available (from bridge trigger detection),
            # otherwise query the bridge fresh
            bridge_actions = None
            if self._bridge_preloaded_actions:
                bridge_actions = self._bridge_preloaded_actions
            else:
                pending = self._gre_bridge.get_pending_actions()
                if pending and pending.get("has_pending") and pending.get("actions"):
                    bridge_actions = pending["actions"]
            if bridge_actions:
                # Find matching action by GRE type AND card identity.
                # Without card name verification, the first type match wins —
                # which submits the wrong card when multiple casts are legal
                # (e.g. submitting Michelangelo instead of Emerald Medallion).
                best_idx = None
                for idx, ba in enumerate(bridge_actions):
                    ba_type = ba.get("actionType", "")
                    # Normalize comparison
                    if not (ba_type == gre_type or f"ActionType_{ba_type}" == gre_type or ba_type == gre_type.replace("ActionType_", "")):
                        continue
                    # Verify card identity via grpId → card name lookup
                    if action.card_name:
                        ba_grp_id = ba.get("grpId", 0)
                        if ba_grp_id:
                            try:
                                from arenamcp import server
                                card_info = server.get_card_info(ba_grp_id)
                                ba_name = card_info.get("name", "")
                            except Exception:
                                ba_name = ""
                            if ba_name and ba_name.lower() != action.card_name.lower():
                                continue  # Wrong card — skip
                    best_idx = idx
                    break

                if best_idx is not None:
                    if self._gre_bridge.submit_action_by_index(
                        best_idx, auto_pass=self._config.auto_pass_priority
                    ):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: '{action.card_name}' submitted via GRE bridge (type+name match)"
                        )
                        return ClickResult(True, 0, 0, action.card_name or str(action), "GRE bridge")
                    else:
                        logger.warning(
                            f"GRE bridge type+name match found idx={best_idx} for "
                            f"'{action.card_name}' but submit_action_by_index failed"
                        )
                elif action.card_name:
                    logger.warning(
                        f"GRE bridge type match: no action matched card name "
                        f"'{action.card_name}' among {len(bridge_actions)} bridge actions"
                    )

        return None

    def _try_bridge_casting_time_option(self, action: GameAction) -> Optional[ClickResult]:
        """Submit a casting-time option (modal choice, done, kicker, etc.) via GRE bridge.

        Bridge actions for CastingTimeOptionRequest have actionType="CastingTimeOption"
        with a choiceKind field ("modal", "done", "choose_or_cost", etc.) and an
        optionIndex for modals. We match the LLM's modal_index to the bridge's
        optionIndex to pick the right entry.
        """
        bridge_actions = None
        if self._bridge_preloaded_actions:
            bridge_actions = self._bridge_preloaded_actions
        else:
            pending = self._gre_bridge.get_pending_actions()
            if pending and pending.get("has_pending") and pending.get("actions"):
                bridge_actions = pending["actions"]

        if not bridge_actions:
            return None

        # Filter to CastingTimeOption entries
        casting_entries = [
            (idx, ba) for idx, ba in enumerate(bridge_actions)
            if ba.get("actionType") == "CastingTimeOption"
        ]

        if not casting_entries:
            return None

        # For modal_choice: match by optionIndex (from LLM's modal_index field)
        modal_index = getattr(action, 'modal_index', 0) or 0

        # For casting_options (the "done/confirm" step), just pick the first
        # non-modal entry (usually "done")
        if action.action_type == ActionType.CASTING_OPTIONS:
            # Prefer "done" entries, then fall back to first entry
            for idx, ba in casting_entries:
                if ba.get("choiceKind") == "done":
                    if self._gre_bridge.submit_action_by_index(
                        idx, auto_pass=self._config.auto_pass_priority
                    ):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"casting_options: '{action.card_name}' done via GRE bridge"
                        )
                        return ClickResult(True, 0, 0, action.card_name or "casting_option", "GRE bridge")
            # No "done" entry — fall through to mouse
            return None

        # modal_choice: find the entry with matching optionIndex
        for idx, ba in casting_entries:
            if ba.get("choiceKind") == "modal" and ba.get("optionIndex", -1) == modal_index:
                if self._gre_bridge.submit_action_by_index(
                    idx, auto_pass=self._config.auto_pass_priority
                ):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"modal_choice: '{action.card_name}' option {modal_index} via GRE bridge"
                    )
                    return ClickResult(True, 0, 0, action.card_name or "modal", "GRE bridge")
                else:
                    logger.warning(
                        f"GRE bridge modal submit failed for '{action.card_name}' option {modal_index}"
                    )
                    return None

        # optionIndex not found — log and fall through to mouse
        available = [(ba.get("choiceKind"), ba.get("optionIndex")) for _, ba in casting_entries]
        logger.warning(
            f"GRE bridge modal: no entry with optionIndex={modal_index} "
            f"among {len(casting_entries)} entries: {available}"
        )
        return None

    def _try_bridge_declare_attackers(self, action: GameAction) -> Optional[ClickResult]:
        """Submit attacker declarations via GRE bridge (two-step NPE handler pattern).

        Step 1: UpdateAttacker — sets SelectedDamageRecipient on each attacker
        Step 2: SubmitAttackers — finalizes the declaration

        Returns ClickResult if bridge handled it, None to fall through to clicks.
        """
        if not self._gre_bridge.connect():
            return None

        # Verify the bridge has a DeclareAttackers request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class", "")
        if "DeclareAttacker" not in req_class:
            logger.info(f"Bridge declare_attackers: pending is {req_class}, not DeclareAttacker")
            return None

        # Build name→instanceId map from decision context
        game_state = self._get_game_state()
        attacker_id_map = self._build_attacker_id_map(game_state)
        battlefield = game_state.get("battlefield", [])
        local_seat = next(
            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
            None,
        )

        # Resolve attacker names to instance IDs
        attacker_entries = []
        for name in action.attacker_names:
            iid = attacker_id_map.get(name)
            if iid is None:
                iid = self._find_instance_id(name, battlefield, local_seat)
            if iid is not None:
                attacker_entries.append({"attackerInstanceId": iid})
            else:
                logger.warning(f"Bridge declare_attackers: can't resolve '{name}' to instance ID")

        if not attacker_entries:
            logger.warning("Bridge declare_attackers: no attacker IDs resolved, falling back to clicks")
            return None

        # Step 1: UpdateAttacker (declare attackers with damage recipients)
        resp = self._gre_bridge.submit_attackers_raw(attacker_entries)
        if not resp or not resp.get("ok"):
            logger.warning(f"Bridge declare_attackers step 1 failed: {resp}")
            return None

        if resp.get("needs_finalize"):
            # Step 2: Wait for GRE to process, then finalize with SubmitAttackers
            time.sleep(0.4)
            resp2 = self._gre_bridge.submit_attackers_raw([])
            if not resp2 or not resp2.get("ok"):
                logger.warning(f"Bridge declare_attackers step 2 (finalize) failed: {resp2}")
                # Step 1 succeeded, so attackers are declared even if finalize fails
                # The game may auto-advance or we can retry
            else:
                logger.info("Bridge declare_attackers: finalized successfully")

        names_str = ", ".join(action.attacker_names)
        self._log_execution_path(
            ExecutionPath.GRE_AWARE,
            f"declare_attackers: [{names_str}] via GRE bridge"
        )
        return ClickResult(True, 0, 0, "attackers", "GRE bridge")

    def _try_gre_bridge_blockers(self, action: GameAction) -> Optional[ClickResult]:
        """Submit blocker assignments via the GRE bridge.

        Maps card names in action.blocker_assignments to instance IDs
        from the current game state's decision context.
        """
        # Verify the bridge actually has a DeclareBlockers request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            logger.info("GRE bridge blockers: no pending interaction, falling back")
            return None
        req_class = pending.get("request_class", "")
        if "DeclareBlockers" not in req_class:
            logger.info(f"GRE bridge blockers: pending is {req_class}, not DeclareBlockers, falling back")
            return None

        game_state = self._get_game_state()
        blocker_id_map = self._build_blocker_id_map(game_state)
        battlefield = game_state.get("battlefield", [])
        opp_seat = None
        for p in game_state.get("players", []):
            if not p.get("is_local"):
                opp_seat = p.get("seat_id")

        assignments = []
        for blocker_name, attacker_name in action.blocker_assignments.items():
            blocker_id = blocker_id_map.get(blocker_name)
            if blocker_id is None:
                blocker_id = self._find_instance_id(
                    blocker_name, battlefield,
                    next((p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")), None)
                )
            attacker_id = self._find_instance_id(attacker_name, battlefield, opp_seat)

            if blocker_id is None or attacker_id is None:
                logger.warning(
                    f"GRE bridge blockers: can't resolve IDs for "
                    f"{blocker_name}({blocker_id}) -> {attacker_name}({attacker_id}), "
                    "falling back to clicks"
                )
                return None

            assignments.append({
                "blockerInstanceId": blocker_id,
                "attackerInstanceIds": [attacker_id],
            })

        if self._gre_bridge.submit_blockers(assignments):
            desc = ", ".join(f"{b}->{a}" for b, a in action.blocker_assignments.items())
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"declare_blockers: {desc} submitted via GRE bridge"
            )
            return ClickResult(True, 0, 0, "declare_blockers", "GRE bridge")

        logger.info("GRE bridge submit_blockers failed, falling back to clicks")
        self._gre_bridge_failed_methods.add("declare_blockers")
        return None

    def _try_gre_bridge_attackers(self, action: GameAction) -> Optional[ClickResult]:
        """Submit attacker declarations via the GRE bridge.

        Maps card names in action.attacker_names to instance IDs and
        targets the opponent's face by default.
        """
        # Verify the bridge actually has a DeclareAttacker request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            logger.info("GRE bridge attackers: no pending interaction, falling back")
            return None
        req_class = pending.get("request_class", "")
        if "DeclareAttacker" not in req_class:
            logger.info(f"GRE bridge attackers: pending is {req_class}, not DeclareAttacker, falling back")
            return None

        game_state = self._get_game_state()
        attacker_id_map = self._build_attacker_id_map(game_state)
        battlefield = game_state.get("battlefield", [])
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        attacker_list = []
        for name in action.attacker_names:
            instance_id = attacker_id_map.get(name)
            if instance_id is None:
                instance_id = self._find_instance_id(name, battlefield, local_seat)
            if instance_id is None:
                logger.warning(
                    f"GRE bridge attackers: can't resolve ID for '{name}', "
                    "falling back to clicks"
                )
                return None

            attacker_list.append({
                "attackerInstanceId": instance_id,
                "damageRecipient": {
                    "type": "DamageRecType_Player",
                    "playerSystemSeatId": opp_seat or 0,
                },
            })

        if self._gre_bridge.submit_attackers(attacker_list):
            names = ", ".join(action.attacker_names)
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"declare_attackers: {names} submitted via GRE bridge"
            )
            return ClickResult(True, 0, 0, "declare_attackers", "GRE bridge")

        logger.info("GRE bridge submit_attackers failed, falling back to clicks")
        self._gre_bridge_failed_methods.add("declare_attackers")
        return None

    def _try_gre_bridge_select_target(self, action: GameAction) -> Optional[ClickResult]:
        """Submit target selection via bridge.

        Uses submit_targets (SelectTargetsRequest) or submit_selection
        (SelectNRequest) depending on the pending request type.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_class = pending.get("request_class", "")
        game_state = self._get_game_state()
        battlefield = game_state.get("battlefield", [])

        # Resolve target name to instance ID
        target_names = action.target_names or ([action.card_name] if action.card_name else [])
        target_id = None
        for name in target_names:
            for card in battlefield:
                if card.get("name", "").lower() == name.lower():
                    iid = card.get("instance_id")
                    if iid:
                        target_id = iid
                        break
            if target_id:
                break

        if target_id is None:
            logger.info(f"GRE bridge select_target: can't resolve ID for {target_names}, falling back")
            return None

        # Use the right bridge method based on request type
        success = False
        if "SelectTargets" in req_class:
            success = self._gre_bridge.submit_targets(target_id)
        else:
            success = self._gre_bridge.submit_selection([target_id])

        if success:
            names = ", ".join(target_names)
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"select_target: {names} (id={target_id}) via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_target", "GRE bridge")

        logger.info("GRE bridge select_target failed, falling back to clicks")
        self._gre_bridge_failed_methods.add("select_target")
        return None

    def _try_gre_bridge_select_n(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit SelectN / Search / SelectCounters via the GRE bridge.

        The LLM gives us `select_card_names`; we resolve those names to
        grp_ids by scanning the game state's known zones (library peek,
        hand, battlefield, graveyard, exile, stack). If we can't find a
        specific grp_id match, we submit an empty list which triggers
        `SubmitArbitrary()` on the plugin side — the game will pick
        automatically rather than leave the selection hanging.

        This path exists specifically to avoid the mouse-click fallback
        (`_exec_select_n`) which clicks by list index and often misses
        the actual option positions, causing the autopilot to loop.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_type = str(pending.get("request_type") or "")
        req_class = str(pending.get("request_class") or "")

        # Only handle SelectN and Search variants; other request types
        # need dedicated paths.
        is_select_n = (
            "SelectN" in req_class or "Search" in req_class
            or req_type in ("SelectN", "Search")
        )
        if not is_select_n:
            return None

        desired_names = [
            (n or "").lower().strip()
            for n in (action.select_card_names or [])
            if n and (n or "").strip()
        ]
        if not desired_names and action.card_name:
            desired_names = [action.card_name.lower().strip()]

        # Decide whether the request takes instance IDs or grp IDs. For
        # most library-reveal style selections (Lluwen, Scry, Surveil,
        # mill-then-pick) the IdType is InstanceId — two copies of the
        # same card have different instance IDs. Submitting grp_ids in
        # that case silently no-ops and the game keeps asking.
        decision_context = game_state.get("decision_context") or {}
        id_type = str(decision_context.get("id_type") or "").strip()
        option_ids = decision_context.get("option_ids") or []
        try:
            option_ids = [int(x) for x in option_ids]
        except (TypeError, ValueError):
            option_ids = []
        wants_instance_ids = (
            "InstanceId" in id_type
            or "instance" in id_type.lower()
            # Heuristic fallback: if the request advertises a short list
            # of specific IDs AND those IDs resolve to battlefield/library
            # game objects, they're instance IDs.
            or (
                len(option_ids) > 0
                and len(option_ids) <= 20
                and all(
                    any(
                        int(c.get("instance_id") or 0) == oid
                        for c in (
                            game_state.get("battlefield", [])
                            + game_state.get("library_top_revealed", [])
                            + game_state.get("hand", [])
                            + game_state.get("graveyard", [])
                            + game_state.get("stack", [])
                            + game_state.get("exile", [])
                        )
                        if isinstance(c, dict)
                    )
                    for oid in option_ids[:5]
                )
            )
        )

        matched_ids: list[int] = []
        zone_keys = (
            "library", "library_top_revealed", "hand", "battlefield",
            "battlefield_player", "battlefield_opponent",
            "graveyard", "graveyard_player", "graveyard_opponent",
            "exile", "stack",
        )

        if wants_instance_ids:
            # Resolve desired_names against visible cards and submit their
            # instance_ids — restricted to option_ids when available.
            option_id_set = set(option_ids)
            for zone_key in zone_keys:
                zone = game_state.get(zone_key)
                if not isinstance(zone, list):
                    continue
                for card in zone:
                    if not isinstance(card, dict):
                        continue
                    name = str(card.get("name") or "").lower().strip()
                    iid = int(card.get("instance_id") or 0)
                    if not (name and iid):
                        continue
                    if option_id_set and iid not in option_id_set:
                        continue
                    for want in desired_names:
                        if want and (want == name or want in name or name in want):
                            if iid not in matched_ids:
                                matched_ids.append(iid)
                            break
                    if len(matched_ids) >= max(1, int(decision_context.get("count") or 1)):
                        break
                if len(matched_ids) >= max(1, int(decision_context.get("count") or 1)):
                    break

        if not matched_ids:
            # Collect candidate cards by grp_id from every visible zone
            # (falls back to this path when IdType is grp-based or we
            # couldn't resolve by instance).
            for zone_key in zone_keys:
                zone = game_state.get(zone_key)
                if not isinstance(zone, list):
                    continue
                for card in zone:
                    if not isinstance(card, dict):
                        continue
                    name = str(card.get("name") or "").lower().strip()
                    grp = card.get("grp_id") or 0
                    if not (name and grp):
                        continue
                    for want in desired_names:
                        if want and (want == name or want in name or name in want):
                            if int(grp) not in matched_ids:
                                matched_ids.append(int(grp))
                            break

            # Fallback: some SelectN targets are library-top reveals (Lluwen ETB,
            # Cultivate, etc.) that don't appear in hand/battlefield/graveyard.
            # Resolve by card name lookup against the card DB so we can still
            # submit the right grp_id.
            if not matched_ids and desired_names:
                try:
                    from arenamcp.card_db import get_card_database
                    card_db = get_card_database()
                    for want in desired_names:
                        card = card_db.get_card_by_name(want)
                        if card and getattr(card, "arena_id", 0):
                            grp = int(card.arena_id)
                            if grp not in matched_ids:
                                matched_ids.append(grp)
                    if matched_ids:
                        logger.info(
                            f"select_n: resolved {desired_names} via card DB -> {matched_ids}"
                        )
                except Exception as e:
                    logger.debug(f"select_n card DB lookup failed: {e}")

        # Submit — empty list → SubmitArbitrary (safe fallback when we can't
        # resolve a specific option)
        success = self._gre_bridge.submit_selection(matched_ids)
        if success:
            id_kind = "instance_ids" if wants_instance_ids else "grp_ids"
            method = f"{len(matched_ids)} {id_kind}" if matched_ids else "arbitrary"
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"select_n: {method} (req={req_class or req_type}) via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_n", "GRE bridge")

        logger.info("GRE bridge select_n failed, falling back to clicks")
        self._gre_bridge_failed_methods.add("select_n")
        return None

    def _try_gre_bridge_scry(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit a scry / surveil-style GroupRequest via the bridge.

        The client represents scry as a `GroupRequest` over the cards being
        scryed, expecting two Groups in the response (top and bottom of the
        library). The LLM gives us:
          - `select_card_names`: the card(s) to keep on top
          - `scry_position`: "top" or "bottom" — applied to all revealed
             cards when no specific names are given

        Cards not named in `select_card_names` go to the other group.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_class = str(pending.get("request_class") or "")
        if "GroupRequest" not in req_class and "Group" not in str(pending.get("request_type") or ""):
            return None

        # Extract the revealed instance IDs the request is asking us to order.
        request_payload = pending.get("request_payload") or {}
        instance_ids_raw = (
            request_payload.get("instanceIds")
            or (pending.get("decision_context") or {}).get("instanceIds")
            or []
        )
        instance_ids: list[int] = []
        for v in instance_ids_raw:
            try:
                instance_ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if not instance_ids:
            logger.info("scry: GroupRequest has no instanceIds; cannot split top/bottom")
            return None

        # Map the LLM's chosen names to instance IDs via the stack / library
        # peek in game state.
        desired_names = [
            (n or "").lower().strip()
            for n in (action.select_card_names or [])
            if n and (n or "").strip()
        ]
        name_to_iid: dict[str, list[int]] = {}
        for zone_key in ("library_top_revealed", "stack", "scry_cards", "revealed"):
            zone = game_state.get(zone_key)
            if not isinstance(zone, list):
                continue
            for card in zone:
                if not isinstance(card, dict):
                    continue
                name = str(card.get("name") or "").lower().strip()
                iid = card.get("instance_id") or card.get("instanceId") or 0
                if name and iid:
                    name_to_iid.setdefault(name, []).append(int(iid))

        top_ids: list[int] = []
        if desired_names:
            for want in desired_names:
                for name, iids in name_to_iid.items():
                    if want and (want == name or want in name or name in want):
                        for iid in iids:
                            if iid in instance_ids and iid not in top_ids:
                                top_ids.append(iid)

        # If no specific names resolved, fall back to scry_position intent.
        pos = (action.scry_position or "").lower()
        if not top_ids and not desired_names:
            if pos == "top":
                top_ids = list(instance_ids)
            # pos == "bottom" or empty: leave top empty (all go bottom)

        bottom_ids = [iid for iid in instance_ids if iid not in top_ids]

        groups = [
            {"ids": top_ids, "zone": "Library", "sub_zone": "Top"},
            {"ids": bottom_ids, "zone": "Library", "sub_zone": "Bottom"},
        ]
        success = self._gre_bridge.submit_group(groups)
        if success:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"scry: top={len(top_ids)} bottom={len(bottom_ids)} via GRE bridge"
            )
            return ClickResult(True, 0, 0, "scry", "GRE bridge")

        logger.info("GRE bridge scry failed, falling back to clicks")
        self._gre_bridge_failed_methods.add("scry")
        return None

    def _execute_action(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Route an action to the appropriate execution handler.

        Tries the GRE bridge first for direct submission (no mouse clicks).
        Falls back to screen-mapped mouse/keyboard input if the bridge is
        unavailable or fails.

        Args:
            action: The GameAction to execute.
            game_state: Current game state for context.

        Returns:
            ClickResult from the execution.
        """
        # Try GRE bridge first (direct action submission, no mouse needed)
        if not self._config.dry_run:
            gre_result = self._try_gre_bridge(action, game_state)
            if gre_result is not None:
                return gre_result

        # The bridge couldn't submit this action. Whether we fall back to
        # mouse or refuse, this counts as a "bridge miss" bug — auto-file
        # a report so we collect telemetry on what action types need
        # bridge coverage next.
        bridge_connected = (
            self._gre_bridge is not None
            and getattr(self._gre_bridge, "connected", False)
        )
        if bridge_connected and not self._config.dry_run:
            self._report_fallback_bug(
                action, game_state,
                reason_tag=(
                    "bridge_only_suppressed"
                    if self._config.bridge_only_when_connected
                    else "falling_back_to_mouse"
                ),
            )

        # Refuse mouse fallback when the bridge IS connected but couldn't
        # handle this specific action (e.g. a CastingTimeOption flavor we
        # don't support yet, like Offspring). Falling through to mouse
        # clicks steals the cursor and is rarely what the user wants —
        # surface the failure and let the user take over manually.
        if (
            self._config.bridge_only_when_connected
            and not self._config.dry_run
            and bridge_connected
        ):
            msg = (
                f"Bridge couldn't handle {action.action_type.value} "
                f"({action.card_name or '?'}) — mouse fallback suppressed. "
                "Take this action manually."
            )
            logger.warning(msg)
            self._notify("AUTOPILOT", f"MANUAL REQUIRED: {msg}")
            return ClickResult(False, 0, 0, action.action_type.value, "bridge_only")

        # Bridge unavailable or disabled — fall back to mouse-click execution.
        gre_ref = getattr(action, 'gre_action_ref', None)
        if gre_ref is not None:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"{action.action_type.value}: {action.card_name or action} (gre_ref={gre_ref}, bridge unavailable)"
            )

        handlers = {
            ActionType.PASS_PRIORITY: self._exec_pass_priority,
            ActionType.RESOLVE: self._exec_resolve,
            ActionType.CLICK_BUTTON: lambda: self._exec_click_button(action),
            ActionType.PLAY_LAND: lambda: self._exec_play_card(action, game_state),
            ActionType.CAST_SPELL: lambda: self._exec_play_card(action, game_state),
            ActionType.ACTIVATE_ABILITY: lambda: self._exec_activate_ability(action, game_state),
            ActionType.DECLARE_ATTACKERS: lambda: self._exec_declare_attackers(action, game_state),
            ActionType.DECLARE_BLOCKERS: lambda: self._exec_declare_blockers(action, game_state),
            ActionType.SELECT_TARGET: lambda: self._exec_select_target(action, game_state),
            ActionType.SELECT_N: lambda: self._exec_select_n(action, game_state),
            ActionType.MODAL_CHOICE: lambda: self._exec_modal_choice(action, game_state),
            ActionType.MULLIGAN_KEEP: lambda: self._exec_mulligan(keep=True),
            ActionType.MULLIGAN_MULL: lambda: self._exec_mulligan(keep=False),
            ActionType.DRAFT_PICK: lambda: self._exec_draft_pick(action, game_state),
            ActionType.ORDER_BLOCKERS: lambda: self._exec_order_blockers(action, game_state),
            # New decision types — most resolve via Done/pass after LLM selection
            ActionType.ASSIGN_DAMAGE: lambda: self._exec_done_action("assign_damage"),
            ActionType.ORDER_COMBAT_DAMAGE: lambda: self._exec_done_action("order_combat_damage"),
            ActionType.PAY_COSTS: lambda: self._exec_pay_costs(action, game_state),
            ActionType.SEARCH_LIBRARY: lambda: self._exec_select_n(action, game_state),
            ActionType.DISTRIBUTE: lambda: self._exec_done_action("distribute"),
            ActionType.NUMERIC_INPUT: lambda: self._exec_done_action("numeric_input"),
            ActionType.CHOOSE_STARTING_PLAYER: lambda: self._exec_choose_play_draw(action),
            ActionType.SELECT_REPLACEMENT: lambda: self._exec_done_action("select_replacement"),
            ActionType.SELECT_COUNTERS: lambda: self._exec_select_n(action, game_state),
            ActionType.CASTING_OPTIONS: lambda: self._exec_modal_choice(action, game_state),
            ActionType.ORDER_TRIGGERS: lambda: self._exec_done_action("order_triggers"),
        }

        handler = handlers.get(action.action_type)
        if handler:
            result = handler()
            if result.success:
                return result
            # Click handler failed — only allow auto_respond for safe cases.
            logger.warning(
                f"Action handler failed for {action.action_type.value}: {result.error}. "
                "Evaluating safe fallback."
            )
        else:
            result = ClickResult(False, 0, 0, str(action), f"No handler for {action.action_type}")

        if (
            not self._config.dry_run
            and self._should_allow_auto_respond(game_state, action)
            and (self._gre_bridge.connected or self._gre_bridge.connect())
        ):
            if self._gre_bridge.auto_respond():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"auto_respond fallback: {action.action_type.value} '{action.card_name}'"
                )
                # Log diagnostic for future fix
                game_state_summary = {
                    "action_type": action.action_type.value,
                    "card_name": action.card_name,
                    "target_names": action.target_names,
                    "attacker_names": action.attacker_names,
                    "blocker_assignments": action.blocker_assignments,
                    "pending_decision": game_state.get("pending_decision"),
                    "bridge_request": game_state.get("_bridge_request_type"),
                    "bridge_class": game_state.get("_bridge_request_class"),
                    "legal_actions": game_state.get("legal_actions", [])[:5],
                }
                logger.warning(
                    f"AUTO_RESPOND_FALLBACK: {game_state_summary} — "
                    "this action type needs a proper bridge handler"
                )
                return ClickResult(True, 0, 0, action.card_name or str(action), "auto_respond fallback")

        if self._is_critical_decision_state(game_state, action):
            self._pause_for_manual(f"No safe automatic fallback for {action.action_type.value}", game_state)
            return ClickResult(False, 0, 0, action.card_name or str(action), "manual required")

        return result

    def _click_fixed(self, name: str) -> ClickResult:
        """Click a fixed-position button by name."""
        coord = self._mapper.get_button_coord(name)
        if not coord:
            return ClickResult(False, 0, 0, name, f"Unknown button: {name}")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, coord.description, window_rect)

    def _exec_pass_priority(self) -> ClickResult:
        """Click the pass/resolve button."""
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, "pass_priority: fixed button")
        return self._click_fixed("pass")

    def _exec_resolve(self) -> ClickResult:
        """Click the resolve button."""
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, "resolve: fixed button")
        return self._click_fixed("resolve")

    def _exec_click_button(self, action: GameAction) -> ClickResult:
        """Click a named button."""
        button_name = action.card_name.lower().replace(" ", "_")
        # Optional-action dialogs (e.g. commander-to-command-zone prompt) are
        # answered via the GRE bridge's submit_optional, not by clicking at a
        # fixed coordinate — the dialog buttons have no deterministic location.
        if button_name in ("accept", "allow", "yes", "decline", "cancel", "no"):
            if self._gre_bridge.connected or self._gre_bridge.connect():
                accept = button_name in ("accept", "allow", "yes")
                if self._gre_bridge.submit_optional(accept):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"optional: submit_optional(accept={accept}) via GRE bridge",
                    )
                    return ClickResult(True, 0, 0, button_name, "GRE bridge")
            logger.warning(
                "optional %s could not be submitted via GRE bridge — no click fallback",
                button_name,
            )
            return ClickResult(False, 0, 0, button_name, "submit_optional failed")
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, f"click_button: {button_name} (fixed coords)")
        # Fallback for common MTGA action buttons that might be named differently by the LLM
        if button_name in ("next", "attack", "all_attack", "done", "no_attacks", "no_blocks"):
            return self._click_fixed("pass") # They all share the same spot
        return self._click_fixed(button_name)

    def _exec_play_card(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Play a card from hand (land or spell).

        Lands are dragged from hand to the battlefield land row (y ≈ 0.75)
        because MTGA requires a drag gesture to play them. Spells are
        clicked normally (MTGA auto-casts on click).

        Coordinate resolution priority:
        1. Deterministic arc-based hand geometry
        2. Vision fallback (only if deterministic fails and vision is enabled)
        """
        hand = game_state.get("hand", [])
        hand_names = [c.get("name", "???") for c in hand]
        logger.info(
            f"_exec_play_card: looking for '{action.card_name}' in hand "
            f"({len(hand)} cards): {hand_names}"
        )
        coord = self._mapper.get_card_in_hand_coord(action.card_name, hand, game_state)

        if coord:
            self._log_execution_path(
                ExecutionPath.DETERMINISTIC_GEOMETRY,
                f"play_card: '{action.card_name}' found via arc-based hand lookup"
            )
        else:
            # Vision fallback — only if deterministic fails
            if self._config.enable_vision_fallback and not (
                self._config.prefer_deterministic and getattr(action, 'gre_action_ref', None) is not None
            ):
                logger.info(f"Trying vision fallback for '{action.card_name}'")
                coord = self._get_vision_coord(action.card_name, zone="hand")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"play_card: '{action.card_name}' found via vision"
                    )

            if not coord:
                return ClickResult(False, 0, 0, action.card_name, "Card not found in hand (Heuristic & Vision failed)")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)

        # Lands and Spells: drag from hand to battlefield center
        if action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
            target = ScreenCoord(0.50, 0.50, f"Battlefield: {action.card_name}")
            to_x, to_y = target.to_absolute(window_rect)
            return self._controller.drag_card_from_hand(
                abs_x, abs_y, to_x, to_y, action.card_name, window_rect
            )

        # Abilities/Other: click to cast
        return self._controller.click_card_in_hand(
            abs_x, abs_y, action.card_name, window_rect
        )

    def _exec_activate_ability(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Click a permanent on the battlefield to activate its ability.

        Coordinate resolution priority:
        1. Deterministic heuristic (permanent grid position)
        2. Vision fallback (only if deterministic fails and vision is enabled)
        """
        battlefield = game_state.get("battlefield", [])
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")

        if not local_seat:
            return ClickResult(False, 0, 0, action.card_name, "Local seat not found")

        # Try instance_id lookup if available from GRE context
        instance_id = self._find_instance_id(action.card_name, battlefield, local_seat)
        coord = self._mapper.get_permanent_coord(
            action.card_name, instance_id, battlefield, local_seat, local_seat
        )

        if coord:
            self._log_execution_path(
                ExecutionPath.DETERMINISTIC_GEOMETRY,
                f"activate_ability: '{action.card_name}' found via heuristic lookup"
            )
        else:
            # Vision fallback — only if deterministic fails
            if self._config.enable_vision_fallback and not (
                self._config.prefer_deterministic and getattr(action, 'gre_action_ref', None) is not None
            ):
                logger.info(f"Trying vision fallback for board permanent '{action.card_name}'")
                coord = self._get_vision_coord(action.card_name, zone="battlefield_yours")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"activate_ability: '{action.card_name}' found via vision"
                    )

            if not coord:
                return ClickResult(False, 0, 0, action.card_name, "Permanent not found on battlefield (Heuristic & Vision failed)")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, f"Activate: {action.card_name}", window_rect)

    def _exec_declare_attackers(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Click each attacking creature, then click Done.

        When instance_ids are available from the decision context, uses them
        for more reliable coordinate lookup.
        """
        # Log GRE action reference if present
        gre_ref = getattr(action, 'gre_action_ref', None)
        if gre_ref is not None:
            logger.info(f"declare_attackers: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        battlefield = game_state.get("battlefield", [])
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")

        if not local_seat:
            return ClickResult(False, 0, 0, "attackers", "Local seat not found")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "attackers", "MTGA window not found")

        # Build name -> instance_id mapping from decision context if available
        attacker_id_map = self._build_attacker_id_map(game_state)

        last_result = ClickResult(True, 0, 0, "attackers")

        for attacker_name in action.attacker_names:
            # Prefer instance_id lookup from decision context
            instance_id = attacker_id_map.get(attacker_name)
            if instance_id is None:
                # Fallback: search battlefield for matching name
                instance_id = self._find_instance_id(attacker_name, battlefield, local_seat)

            coord = self._mapper.get_permanent_coord(
                attacker_name, instance_id, battlefield, local_seat, local_seat
            )
            if coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_attackers: '{attacker_name}' (instance_id={instance_id})"
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                result = self._controller.click(
                    abs_x, abs_y, f"Attack: {attacker_name}", window_rect
                )
                if not result.success:
                    logger.warning(f"Failed to click attacker {attacker_name}")
                last_result = result
            else:
                # Vision fallback for attackers
                if self._config.enable_vision_fallback:
                    coord = self._get_vision_coord(attacker_name, zone="battlefield_yours")
                    if coord:
                        self._log_execution_path(
                            ExecutionPath.VISION_FALLBACK,
                            f"declare_attackers: '{attacker_name}' found via vision"
                        )
                        abs_x, abs_y = coord.to_absolute(window_rect)
                        result = self._controller.click(
                            abs_x, abs_y, f"Attack: {attacker_name}", window_rect
                        )
                        last_result = result
                    else:
                        logger.warning(f"Failed to find attacker {attacker_name} (heuristic & vision)")
                else:
                    logger.warning(f"Failed to find attacker {attacker_name} (heuristic only, vision disabled)")
            self._controller.wait(self._config.action_delay, "between attacker clicks")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_declare_blockers(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Click blocker, then click attacker it should block, then Done.

        When instance_ids are available from the decision context, uses them
        for more reliable coordinate lookup.
        """
        # Log GRE action reference if present
        gre_ref = getattr(action, 'gre_action_ref', None)
        if gre_ref is not None:
            logger.info(f"declare_blockers: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        battlefield = game_state.get("battlefield", [])
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        if not local_seat or not opp_seat:
            return ClickResult(False, 0, 0, "blockers", "Seat info not found")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "blockers", "MTGA window not found")

        # Build name -> instance_id mapping from decision context if available
        blocker_id_map = self._build_blocker_id_map(game_state)

        last_result = ClickResult(True, 0, 0, "blockers")

        for blocker_name, attacker_name in action.blocker_assignments.items():
            # Click the blocker (our creature) — prefer instance_id lookup
            blocker_instance_id = blocker_id_map.get(blocker_name)
            if blocker_instance_id is None:
                blocker_instance_id = self._find_instance_id(blocker_name, battlefield, local_seat)

            blocker_coord = self._mapper.get_permanent_coord(
                blocker_name, blocker_instance_id, battlefield, local_seat, local_seat
            )
            blocker_found = False
            if blocker_coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_blockers: blocker '{blocker_name}' (instance_id={blocker_instance_id})"
                )
                bx, by = blocker_coord.to_absolute(window_rect)
                self._controller.click(bx, by, f"Blocker: {blocker_name}", window_rect)
                self._controller.wait(0.2, "blocker selected")
                blocker_found = True
            elif self._config.enable_vision_fallback:
                coord = self._get_vision_coord(blocker_name, zone="battlefield_yours")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"declare_blockers: blocker '{blocker_name}' found via vision"
                    )
                    bx, by = coord.to_absolute(window_rect)
                    self._controller.click(bx, by, f"Blocker: {blocker_name}", window_rect)
                    self._controller.wait(0.2, "blocker selected")
                    blocker_found = True

            if not blocker_found:
                logger.warning(
                    f"Could not locate blocker '{blocker_name}' "
                    f"(instance_id={blocker_instance_id}) — aborting block assignment"
                )
                return ClickResult(False, 0, 0, "blockers", f"Blocker '{blocker_name}' not found")

            # Click the attacker (opponent's creature) — use instance_id if available
            attacker_instance_id = self._find_instance_id(attacker_name, battlefield, opp_seat)
            attacker_coord = self._mapper.get_permanent_coord(
                attacker_name, attacker_instance_id, battlefield, opp_seat, local_seat
            )
            attacker_found = False
            if attacker_coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_blockers: attacker '{attacker_name}' (instance_id={attacker_instance_id})"
                )
                ax, ay = attacker_coord.to_absolute(window_rect)
                result = self._controller.click(
                    ax, ay, f"Block {attacker_name} with {blocker_name}", window_rect
                )
                last_result = result
                attacker_found = True
            elif self._config.enable_vision_fallback:
                coord = self._get_vision_coord(attacker_name, zone="battlefield_opponent")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"declare_blockers: attacker '{attacker_name}' found via vision"
                    )
                    ax, ay = coord.to_absolute(window_rect)
                    result = self._controller.click(
                        ax, ay, f"Block {attacker_name} with {blocker_name}", window_rect
                    )
                    last_result = result
                    attacker_found = True

            if not attacker_found:
                logger.warning(
                    f"Could not locate attacker '{attacker_name}' "
                    f"(instance_id={attacker_instance_id}) — aborting block assignment"
                )
                return ClickResult(False, 0, 0, "blockers", f"Attacker '{attacker_name}' not found")
            self._controller.wait(self._config.action_delay, "between block assignments")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_select_target(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Click on a target permanent or player.

        Coordinate resolution priority:
        1. Instance_id-based deterministic lookup (if available from GRE context)
        2. Name-based deterministic heuristic lookup
        3. Vision fallback (if both above fail)
        """
        if not action.target_names:
            return ClickResult(False, 0, 0, "target", "No target specified")

        # Log GRE action reference if present
        gre_ref = getattr(action, 'gre_action_ref', None)
        if gre_ref is not None:
            logger.info(f"select_target: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        target_name = action.target_names[0]
        battlefield = game_state.get("battlefield", [])

        # Try to find target on battlefield
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, target_name, "MTGA window not found")

        # Search both sides of the battlefield, using instance_id when available
        for owner in self._get_target_owner_order(game_state, local_seat, opp_seat):
            if owner is None:
                continue
            instance_id = self._find_instance_id(target_name, battlefield, owner)
            coord = self._mapper.get_permanent_coord(
                target_name, instance_id, battlefield, owner, local_seat
            )
            if coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"select_target: '{target_name}' (owner={owner}, instance_id={instance_id})"
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(
                    abs_x, abs_y, f"Target: {target_name}", window_rect
                )

        # Vision fallback for targets
        if self._config.enable_vision_fallback:
            coord = self._get_vision_coord(target_name, zone="battlefield")
            if coord:
                self._log_execution_path(
                    ExecutionPath.VISION_FALLBACK,
                    f"select_target: '{target_name}' found via vision"
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(
                    abs_x, abs_y, f"Target: {target_name}", window_rect
                )

        return ClickResult(False, 0, 0, target_name, "Target not found on battlefield")

    def _exec_select_n(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Handle scry or multi-select UI (select N cards)."""
        # Scry: top or bottom
        if action.scry_position:
            button = "scry_top" if action.scry_position == "top" else "scry_bottom"
            return self._click_fixed(button)

        # Multi-select: click each card then Done
        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "select_n", "MTGA window not found")

        last_result = ClickResult(True, 0, 0, "select_n")

        for i, card_name in enumerate(action.select_card_names):
            coord = self._mapper.get_option_coord(
                i, len(action.select_card_names), "select"
            )
            if coord:
                abs_x, abs_y = coord.to_absolute(window_rect)
                result = self._controller.click(
                    abs_x, abs_y, f"Select: {card_name}", window_rect
                )
                last_result = result
                self._controller.wait(0.2, "between selections")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_modal_choice(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Click a modal choice option."""
        # Determine total options from decision context
        decision = game_state.get("decision_context", {})
        total_options = decision.get("total_options", 2)

        coord = self._mapper.get_option_coord(
            action.modal_index, total_options, "modal"
        )
        if not coord:
            return ClickResult(False, 0, 0, "modal", "Cannot determine option position")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "modal", "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(
            abs_x, abs_y, f"Modal option {action.modal_index}", window_rect
        )

    def _exec_mulligan(self, keep: bool) -> ClickResult:
        """Click Keep or Mulligan button."""
        choice = "keep" if keep else "mulligan"
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, f"mulligan: {choice} (fixed coords)")
        return self._click_fixed(choice)

    def _exec_draft_pick(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Double-click a draft card to pick it."""
        # Try positional first, then vision fallback
        # For draft, we need pack info
        pack = game_state.get("draft_pack", {})
        cards = pack.get("cards", [])
        pack_size = len(cards)

        # Find card index
        card_idx = None
        for i, card in enumerate(cards):
            if card.get("name", "").lower() == action.card_name.lower():
                card_idx = i
                break

        if card_idx is None:
            return ClickResult(False, 0, 0, action.card_name, "Card not found in draft pack")

        coord = self._mapper.get_draft_card_coord(card_idx, pack_size)
        if not coord:
            return ClickResult(False, 0, 0, action.card_name, "Cannot calculate draft position")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.double_click(
            abs_x, abs_y, f"Draft pick: {action.card_name}", window_rect
        )

    def _exec_order_blockers(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Order blockers by dragging (rarely needed)."""
        # Blocker ordering uses drag to reorder. For now, just click Done
        # since MTGA defaults to a reasonable order.
        logger.info("Blocker ordering: using default order (click Done)")
        return self._click_fixed("done")

    def _exec_done_action(self, decision_name: str) -> ClickResult:
        """Generic handler for decisions that just need a Done click after MTGA auto-selects."""
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, f"done_action: {decision_name} (fixed coords)")
        logger.info(f"{decision_name}: accepting default / clicking Done")
        result = self._click_fixed("done")
        if not result.success:
            # Fallback: try spacebar
            self._controller.press_key("space", f"{decision_name}: spacebar fallback")
            return ClickResult(True, 0, 0, decision_name, "spacebar fallback")
        return result

    @staticmethod
    def _parse_pay_cost_requirements(decision_context: dict[str, Any]) -> dict[str, int]:
        """Return normalized mana requirements for a Pay Costs decision."""
        requirements = {
            "generic": 0,
            "W": 0,
            "U": 0,
            "B": 0,
            "R": 0,
            "G": 0,
            "C": 0,
            "Any": 0,
        }

        raw = decision_context.get("mana_requirements")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in requirements:
                    try:
                        requirements[key] = int(value)
                    except (TypeError, ValueError):
                        continue
            if any(requirements.values()):
                return requirements

        mana_cost = str(decision_context.get("mana_cost", "") or "")
        if not mana_cost:
            return requirements

        token_map = {
            "manacolor_white": "W",
            "manacolor_blue": "U",
            "manacolor_black": "B",
            "manacolor_red": "R",
            "manacolor_green": "G",
            "manacolor_colorless": "C",
            "manacolor_any": "Any",
            "manacolor_generic": "generic",
            "generic": "generic",
            "w": "W",
            "u": "U",
            "b": "B",
            "r": "R",
            "g": "G",
            "c": "C",
            "any": "Any",
        }
        for count_str, token in re.findall(r"(\d+)x([^,]+)", mana_cost):
            mapped = token_map.get(token.strip().lower())
            if not mapped:
                continue
            requirements[mapped] += int(count_str)

        return requirements

    @staticmethod
    def _infer_mana_source_colors(card: dict[str, Any]) -> set[str]:
        """Infer which colors a permanent can produce when tapped for mana."""
        colors: set[str] = set()
        color_map = {
            "1": "W",
            "2": "U",
            "3": "B",
            "4": "R",
            "5": "G",
            "6": "C",
            "manacolor_white": "W",
            "manacolor_blue": "U",
            "manacolor_black": "B",
            "manacolor_red": "R",
            "manacolor_green": "G",
            "manacolor_colorless": "C",
            "manacolor_any": "Any",
            "w": "W",
            "u": "U",
            "b": "B",
            "r": "R",
            "g": "G",
            "c": "C",
            "any": "Any",
        }

        for raw in card.get("color_production", []) or []:
            mapped = color_map.get(str(raw).strip().lower())
            if mapped:
                colors.add(mapped)

        name = str(card.get("name", "") or "")
        type_line = str(card.get("type_line", "") or "").lower()
        oracle = str(card.get("oracle_text", "") or "")
        oracle_lower = oracle.lower()

        if "plains" in name.lower() or "plains" in type_line:
            colors.add("W")
        if "island" in name.lower() or "island" in type_line:
            colors.add("U")
        if "swamp" in name.lower() or "swamp" in type_line:
            colors.add("B")
        if "mountain" in name.lower() or "mountain" in type_line:
            colors.add("R")
        if "forest" in name.lower() or "forest" in type_line:
            colors.add("G")
        if re.search(r"\{o?W\}", oracle):
            colors.add("W")
        if re.search(r"\{o?U\}", oracle):
            colors.add("U")
        if re.search(r"\{o?B\}", oracle):
            colors.add("B")
        if re.search(r"\{o?R\}", oracle):
            colors.add("R")
        if re.search(r"\{o?G\}", oracle):
            colors.add("G")
        if re.search(r"\{o?C\}", oracle):
            colors.add("C")
        if "any color" in oracle_lower:
            colors.add("Any")

        return colors

    @staticmethod
    def _select_pay_cost_sources(
        game_state: dict[str, Any],
        decision_context: dict[str, Any],
        local_seat: int,
    ) -> list[dict[str, Any]]:
        """Choose mana sources to tap for a Pay Costs decision."""
        battlefield = game_state.get("battlefield", [])
        by_instance = {
            card.get("instance_id"): card
            for card in battlefield
            if card.get("instance_id") is not None
        }

        autotap = decision_context.get("autotap_solution") or {}
        lands_to_tap = autotap.get("lands_to_tap") if isinstance(autotap, dict) else None
        if isinstance(lands_to_tap, list) and lands_to_tap:
            selected = []
            for tap in lands_to_tap:
                instance_id = tap.get("instanceId") if isinstance(tap, dict) else None
                card = by_instance.get(instance_id)
                if (
                    card
                    and card.get("controller_seat_id") == local_seat
                    and not card.get("is_tapped")
                ):
                    selected.append(card)
            if selected:
                return selected

        requirements = AutopilotEngine._parse_pay_cost_requirements(decision_context)
        if not any(requirements.values()):
            return []

        turn_num = game_state.get("turn", {}).get("turn_number", 0)
        candidates: list[dict[str, Any]] = []
        for card in battlefield:
            if card.get("controller_seat_id") != local_seat or card.get("is_tapped"):
                continue

            type_line = str(card.get("type_line", "") or "").lower()
            oracle = str(card.get("oracle_text", "") or "")
            is_land = "land" in type_line
            is_creature = "creature" in type_line
            has_mana_ability = bool(re.search(r"\{(?:o)?t\}.*add\s+(\{|one |two |three )", oracle, re.I))
            entered = card.get("turn_entered_battlefield", -1)
            has_haste = "haste" in oracle.lower()
            is_sick = is_creature and entered == turn_num and not has_haste

            if not (is_land or (has_mana_ability and not is_sick)):
                continue

            colors = AutopilotEngine._infer_mana_source_colors(card)
            flexibility = len([color for color in colors if color != "Any"]) or 99
            candidates.append(
                {
                    "card": card,
                    "colors": colors,
                    "flexibility": flexibility,
                }
            )

        selected: list[dict[str, Any]] = []

        def pick_candidate(color: Optional[str] = None) -> Optional[dict[str, Any]]:
            pool = candidates
            if color is not None:
                pool = [
                    candidate
                    for candidate in candidates
                    if color in candidate["colors"] or "Any" in candidate["colors"]
                ]
            if not pool:
                return None
            if color is None:
                pool = sorted(
                    pool,
                    key=lambda candidate: (
                        candidate["flexibility"],
                        candidate["card"].get("name", ""),
                        candidate["card"].get("instance_id", 0),
                    ),
                )
            else:
                pool = sorted(
                    pool,
                    key=lambda candidate: (
                        0 if color in candidate["colors"] and "Any" not in candidate["colors"] else 1,
                        candidate["flexibility"],
                        candidate["card"].get("name", ""),
                        candidate["card"].get("instance_id", 0),
                    ),
                )
            chosen = pool[0]
            candidates.remove(chosen)
            selected.append(chosen)
            return chosen

        for color in ("W", "U", "B", "R", "G", "C"):
            for _ in range(requirements.get(color, 0)):
                if pick_candidate(color) is None:
                    return [candidate["card"] for candidate in selected]

        for _ in range(requirements.get("generic", 0) + requirements.get("Any", 0)):
            if pick_candidate() is None:
                break

        return [candidate["card"] for candidate in selected]

    def _click_battlefield_card(
        self,
        card: dict[str, Any],
        battlefield: list[dict[str, Any]],
        local_seat: int,
        description: str,
    ) -> ClickResult:
        """Click a permanent on the battlefield by instance ID when possible."""
        card_name = str(card.get("name", "") or description)
        instance_id = card.get("instance_id")
        owner_seat = card.get("owner_seat_id", local_seat)
        coord = self._mapper.get_permanent_coord(
            card_name,
            instance_id,
            battlefield,
            owner_seat,
            local_seat,
        )

        if coord is None and self._config.enable_vision_fallback:
            coord = self._get_vision_coord(card_name, zone="battlefield_yours")

        if coord is None:
            return ClickResult(False, 0, 0, description, f"Permanent not found: {card_name}")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, description, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, description, window_rect)

    def _exec_pay_costs(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult:
        """Resolve Pay Costs by tapping mana sources instead of blind Done clicks."""
        decision_context = game_state.get("decision_context") or {}
        if decision_context.get("type") != "pay_costs":
            logger.info("pay_costs: no pay-costs context, falling back to Done")
            return self._exec_done_action("pay_costs")

        local_seat = None
        for player in game_state.get("players", []):
            if player.get("is_local"):
                local_seat = player.get("seat_id")
                break
        if local_seat is None:
            return ClickResult(False, 0, 0, "pay_costs", "Local seat not found")

        battlefield = game_state.get("battlefield", [])
        sources = self._select_pay_cost_sources(game_state, decision_context, local_seat)
        if not sources:
            if decision_context.get("has_autotap"):
                logger.info("pay_costs: no explicit tap targets, confirming autotap/default")
            else:
                logger.warning("pay_costs: no mana sources resolved, falling back to Done")
            return self._exec_done_action("pay_costs")

        descriptions = [str(source.get("name", source.get("instance_id", "?"))) for source in sources]
        self._log_execution_path(
            ExecutionPath.DETERMINISTIC_GEOMETRY,
            f"pay_costs: tapping {', '.join(descriptions)}",
        )
        logger.info("pay_costs: tapping mana sources %s", descriptions)

        last_result: Optional[ClickResult] = None
        for source in sources:
            source_name = str(source.get("name", source.get("instance_id", "?")))
            result = self._click_battlefield_card(
                source,
                battlefield,
                local_seat,
                f"Mana source: {source_name}",
            )
            if not result.success:
                return result
            last_result = result
            time.sleep(0.08)

        if last_result is None:
            return ClickResult(False, 0, 0, "pay_costs", "No mana sources tapped")

        return last_result

    def _exec_choose_play_draw(self, action: GameAction) -> ClickResult:
        """Handle choose starting player (play or draw)."""
        choice = action.play_or_draw.lower() if action.play_or_draw else "play"
        logger.info(f"Choosing to {choice}")
        # In MTGA, "Play" is the first option button, "Draw" is second
        # Both typically resolve via the pass/done area or modal options
        if choice == "draw":
            # Try clicking the second option
            coord = self._mapper.get_option_coord(1, 2, "modal")
            if coord:
                window_rect = self._mapper.window_rect
                if not window_rect:
                    window_rect = self._mapper.refresh_window()
                if window_rect:
                    abs_x, abs_y = coord.to_absolute(window_rect)
                    return self._controller.click(abs_x, abs_y, "Choose: Draw", window_rect)
        # Default: "Play" = first option
        coord = self._mapper.get_option_coord(0, 2, "modal")
        if coord:
            window_rect = self._mapper.window_rect
            if not window_rect:
                window_rect = self._mapper.refresh_window()
            if window_rect:
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(abs_x, abs_y, "Choose: Play", window_rect)
        # Last fallback
        return self._click_fixed("pass")

    # --- Instance ID Helpers ---

    @staticmethod
    def _get_target_owner_order(
        game_state: dict[str, Any],
        local_seat: Optional[int],
        opp_seat: Optional[int],
    ) -> list[int]:
        """Prefer the correct battlefield side for target selection."""
        decision = game_state.get("decision_context") or {}
        source_oracle = str(
            decision.get("source_oracle_text")
            or decision.get("source_card_oracle_text")
            or ""
        )
        if source_oracle:
            try:
                from arenamcp.rules_engine import RulesEngine

                req = RulesEngine._infer_target_requirements(source_oracle)
                if req.get("must_control") == "you":
                    return [seat for seat in (local_seat, opp_seat) if seat is not None]
                if req.get("must_control") == "opponent":
                    return [seat for seat in (opp_seat, local_seat) if seat is not None]
            except Exception as exc:
                logger.debug(f"target owner preference inference failed: {exc}")

        return [seat for seat in (opp_seat, local_seat) if seat is not None]

    def _find_instance_id(
        self, card_name: str, battlefield: list[dict[str, Any]], owner_seat: int
    ) -> Optional[int]:
        """Find the instance_id of a card on the battlefield by name and owner.

        Searches battlefield entries for a card matching the given name and
        owner seat, returning its instance_id for more reliable coordinate
        lookup.

        Args:
            card_name: Card name to search for.
            battlefield: List of battlefield card dicts.
            owner_seat: Owner seat_id to filter by.

        Returns:
            instance_id if found, None otherwise.
        """
        match = re.match(r"^(.*?)(?:\s+#(\d+))?$", card_name.strip())
        base_name = (match.group(1) if match else card_name).strip().lower()
        ordinal = int(match.group(2)) if match and match.group(2) else 1

        matches = [
            card for card in battlefield
            if card.get("owner_seat_id") == owner_seat
            and card.get("name", "").strip().lower() == base_name
        ]
        if not matches:
            return None

        matches.sort(key=lambda card: int(card.get("instance_id", 0) or 0))
        index = max(0, min(ordinal - 1, len(matches) - 1))
        return matches[index].get("instance_id")

    def _build_attacker_id_map(self, game_state: dict[str, Any]) -> dict[str, int]:
        """Build a name -> instance_id map from the attacker decision context.

        Uses legal_attacker_ids from the decision context (if available) paired
        with legal_attackers names to create a reliable mapping.

        Returns:
            Dict mapping card name -> instance_id.
        """
        decision = game_state.get("decision_context") or {}
        if decision.get("type") != "declare_attackers":
            return {}

        names = decision.get("legal_attackers", [])
        ids = decision.get("legal_attacker_ids", [])
        if len(names) != len(ids) or not ids:
            return {}

        return dict(zip(names, ids))

    def _build_blocker_id_map(self, game_state: dict[str, Any]) -> dict[str, int]:
        """Build a name -> instance_id map from the blocker decision context.

        Uses legal_blocker_ids from the decision context (if available) paired
        with legal_blockers names to create a reliable mapping.

        Returns:
            Dict mapping card name -> instance_id.
        """
        decision = game_state.get("decision_context") or {}
        if decision.get("type") != "declare_blockers":
            return {}

        names = decision.get("legal_blockers", [])
        ids = decision.get("legal_blocker_ids", [])
        if len(names) != len(ids) or not ids:
            return {}

        return dict(zip(names, ids))

    # --- State Verification ---

    def _verify_action(
        self, action: GameAction, pre_state: dict[str, Any]
    ) -> bool:
        """Verify that an action caused the expected state change.

        Polls game state for up to verification_timeout seconds.

        Args:
            action: The action that was executed.
            pre_state: Game state snapshot from before the action.

        Returns:
            True if state changed as expected.
        """
        # Initial delay to give MTGA time to process the click and update logs
        time.sleep(self._config.post_action_delay)

        deadline = time.time() + self._config.verification_timeout
        poll_interval = 0.15

        card_name = action.card_name.lower() if action.card_name else ""
        pre_pending = pre_state.get("pending_decision")
        pre_bridge_state_id = int(pre_state.get("_bridge_game_state_id", 0) or 0)
        bridge_state_authoritative = pre_bridge_state_id > 0
        last_post_state: Optional[dict[str, Any]] = None

        while time.time() < deadline:
            try:
                post_state = self._get_game_state()
                last_post_state = post_state

                post_bridge_state_id = int(post_state.get("_bridge_game_state_id", 0) or 0)
                if post_bridge_state_id and pre_bridge_state_id and post_bridge_state_id != pre_bridge_state_id:
                    logger.info(
                        "Action verified: bridge game_state_id advanced (%s -> %s)",
                        pre_bridge_state_id,
                        post_bridge_state_id,
                    )
                    return True

                if bridge_state_authoritative:
                    time.sleep(poll_interval)
                    continue

                # 0. New pending decision appeared (ETB choices, mana payments, etc.)
                # This means the action was processed and MTGA is waiting for a
                # follow-up choice (e.g. shock land "Pay 2 life?", scry, etc.)
                post_pending = post_state.get("pending_decision")
                if post_pending != pre_pending:
                    if post_pending:
                        logger.info(f"Action verified: pending decision changed to '{post_pending}'")
                    else:
                        logger.info("Action verified: pending decision cleared")
                    return True

                # 1. Global state changes (Turn, Phase, Priority)
                pre_turn = pre_state.get("turn", {})
                post_turn = post_state.get("turn", {})

                if (
                    post_turn.get("phase") != pre_turn.get("phase")
                    or post_turn.get("step") != pre_turn.get("step")
                    or post_turn.get("priority_player") != pre_turn.get("priority_player")
                    or post_turn.get("turn_number") != pre_turn.get("turn_number")
                ):
                    logger.info(f"Action verified: global state changed ({pre_turn.get('phase')} -> {post_turn.get('phase')})")
                    return True

                # 2. Specific Action Verification
                if action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
                    # Card should no longer be in hand, or should be on stack/battlefield/GY
                    pre_hand = [c.get("instance_id") for c in pre_state.get("hand", [])]
                    post_hand = [c.get("instance_id") for c in post_state.get("hand", [])]
                    
                    if len(post_hand) < len(pre_hand):
                        logger.info(f"Action verified: card '{action.card_name}' left hand")
                        return True
                    
                    # Check if card appeared on battlefield
                    post_bf = [c.get("name", "").lower() for c in post_state.get("battlefield", [])]
                    if any(card_name in name for name in post_bf):
                        # This is a bit weak if the card was already there, but better than nothing
                        # Ideally we'd track instance_id movement
                        pass

                if action.action_type == ActionType.PAY_COSTS:
                    pre_local = next((p.get("seat_id") for p in pre_state.get("players", []) if p.get("is_local")), None)
                    post_local = next((p.get("seat_id") for p in post_state.get("players", []) if p.get("is_local")), None)
                    if pre_local is not None and post_local == pre_local:
                        pre_tapped = sum(
                            1
                            for card in pre_state.get("battlefield", [])
                            if card.get("controller_seat_id") == pre_local and card.get("is_tapped")
                        )
                        post_tapped = sum(
                            1
                            for card in post_state.get("battlefield", [])
                            if card.get("controller_seat_id") == post_local and card.get("is_tapped")
                        )
                        if post_tapped > pre_tapped:
                            logger.info("Action verified: mana sources tapped")
                            return True

                if action.action_type == ActionType.DECLARE_ATTACKERS:
                    # Check if any creatures are now attacking that weren't before
                    pre_atk = sum(1 for c in pre_state.get("battlefield", []) if c.get("is_attacking"))
                    post_atk = sum(1 for c in post_state.get("battlefield", []) if c.get("is_attacking"))
                    if post_atk > pre_atk or (post_atk == 0 and pre_atk > 0): # attacking finished
                        logger.info("Action verified: attackers declared")
                        return True

                # 3. Generic fallback: did ANYTHING change?
                # Hand size changed
                if len(post_state.get("hand", [])) != len(pre_state.get("hand", [])):
                    logger.info("Action verified: hand size changed")
                    return True

                # Battlefield count changed
                if len(post_state.get("battlefield", [])) != len(pre_state.get("battlefield", [])):
                    logger.info("Action verified: battlefield count changed")
                    return True
                
                # Stack size changed
                if len(post_state.get("stack", [])) != len(pre_state.get("stack", [])):
                    logger.info("Action verified: stack changed")
                    return True

            except Exception as e:
                logger.error(f"Verification poll error: {e}")

            time.sleep(poll_interval)

        if last_post_state and last_post_state.get("game_engine_busy"):
            logger.warning(
                "Action verification timed out while the engine was still busy; not blocking action yet"
            )
            return False

        post_bridge_state_id = int((last_post_state or {}).get("_bridge_game_state_id", 0) or 0)
        if pre_bridge_state_id and post_bridge_state_id == pre_bridge_state_id:
            self._mark_action_blocked(
                action,
                pre_state,
                f"bridge game_state_id stayed at {pre_bridge_state_id}",
            )

        logger.warning(f"Action verification timed out after {self._config.verification_timeout}s")
        return False
