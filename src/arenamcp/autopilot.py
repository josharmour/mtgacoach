"""Autopilot Mode - Core Orchestration Engine.

Ties ActionPlanner + ScreenMapper + InputController together with
human-in-the-loop confirmation gates (spacebar to confirm, escape to skip).

The autopilot layers onto the existing coaching loop without replacing it:

    GameState polling → Triggers → ActionPlanner.plan_actions() → Preview
    → [SPACEBAR confirm] → InputController.execute() → Verify state → Loop
"""

import logging
import threading
import time
import io
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from PIL import ImageGrab

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.gre_bridge import GREBridge, GREBridgeError, get_bridge
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
    verification_timeout: float = 1.5
    action_delay: float = 0.25
    post_action_delay: float = 0.4  # Delay after action to allow GRE to update
    planning_timeout: float = 8.0
    enable_vision_fallback: bool = True
    prefer_deterministic: bool = True  # When True, skip VLM for actions that have deterministic coordinates
    enable_tts_preview: bool = True
    dry_run: bool = False
    afk_mode: bool = False  # When True, auto-pass everything without LLM
    land_drop_mode: bool = False  # When True, auto-play one land per turn (no LLM)


class AutopilotEngine:
    """Core autopilot orchestration engine.

    Coordinates action planning, screen mapping, input control, and
    human confirmation to execute MTGA actions automatically.
    """

    _MAX_CONTINUATION_DEPTH: int = 5

    def __init__(
        self,
        planner: ActionPlanner,
        mapper: ScreenMapper,
        controller: InputController,
        get_game_state: Callable[[], dict[str, Any]],
        config: Optional[AutopilotConfig] = None,
        speak_fn: Optional[Callable[[str, bool], None]] = None,
        ui_advice_fn: Optional[Callable[[str, str], None]] = None,
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
        """
        self._planner = planner
        self._mapper = mapper
        self._controller = controller
        self._get_game_state = get_game_state
        self._config = config or AutopilotConfig()
        self._speak_fn = speak_fn
        self._ui_advice_fn = ui_advice_fn

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
        self._gre_bridge_failed_this_plan: bool = False

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

        return info

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
        if not self._lock.acquire(blocking=False):
            logger.debug(f"Autopilot: already processing a trigger, skipping {trigger}")
            return False

        try:
            if self._abort_event.is_set():
                self._state = AutopilotState.IDLE
                return False

            self._clear_events()

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
            turn = game_state.get("turn", {})
            local_seat = None
            for p in game_state.get("players", []):
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
            is_my_turn = turn.get("active_player") == local_seat if local_seat else False

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
                    self._state = AutopilotState.IDLE
                    return False

            # --- STALENESS CHECK ---
            # Re-poll game state after planning (LLM call may take 5-15s).
            # If the game has moved on (different turn, phase, or active player),
            # discard the stale plan instead of executing outdated actions.
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
                    # Lenient phase check: allow Main1 -> Main2 or Combat steps as long as it's still
                    # the same turn and player. BUT if a sorcery/land action was planned and we are
                    # now in Combat, it's stale.
                    is_sorcery_play = any(a.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL) for a in plan.actions)
                    now_combat = "Combat" in fresh_turn.get("phase", "")

                    if is_sorcery_play and now_combat:
                        logger.warning(f"STALE: phase changed {pre_phase} → {fresh_turn.get('phase')} (sorcery plan in combat)")
                        stale = True
                    else:
                        # For other changes (Main1->Main2, or combat step changes), we can try to proceed
                        # but we should update the game_state so coordinates are fresh.
                        logger.info(f"Phase changed {pre_phase} → {fresh_turn.get('phase')}, proceeding with caution")

                if stale:
                    self._notify("AUTOPILOT", "Plan discarded (game moved on)")
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
                self._speak_fn(f"Plan: {plan.overall_strategy}", False)

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
                    self._state = AutopilotState.IDLE
                    return False
                game_state = exec_state  # Use freshest state
            except Exception as e:
                logger.error(f"Pre-execution recheck failed: {e}")

            # --- 3. EXECUTING ---
            self._state = AutopilotState.EXECUTING
            self._gre_bridge_failed_this_plan = False

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
            self._lock.release()

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

    def _try_gre_bridge(self, action: GameAction) -> Optional[ClickResult]:
        """Try to execute an action via the GRE bridge (direct submission).

        Returns a ClickResult if the bridge handled it, or None to fall
        through to mouse-click execution.
        """
        if self._gre_bridge_failed_this_plan:
            return None

        if not self._gre_bridge.connect():
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
            self._gre_bridge_failed_this_plan = True
            return None

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
            self._gre_bridge_failed_this_plan = True
            return None

        # No GRE ref but bridge is connected — try matching by game action type
        from arenamcp.gre_action_matcher import ACTION_TYPE_MAP
        gre_type = ACTION_TYPE_MAP.get(action.action_type)
        if gre_type:
            # Try to find matching action by type + card name via pending actions
            pending = self._gre_bridge.get_pending_actions()
            if pending and pending.get("has_pending") and pending.get("actions"):
                bridge_actions = pending["actions"]
                # Find matching action by GRE type and grpId
                for idx, ba in enumerate(bridge_actions):
                    ba_type = ba.get("actionType", "")
                    # Normalize comparison
                    if ba_type == gre_type or f"ActionType_{ba_type}" == gre_type or ba_type == gre_type.replace("ActionType_", ""):
                        # For named actions, try to verify card identity via game_objects
                        if self._gre_bridge.submit_action_by_index(
                            idx, auto_pass=self._config.auto_pass_priority
                        ):
                            self._log_execution_path(
                                ExecutionPath.GRE_AWARE,
                                f"{action.action_type.value}: '{action.card_name}' submitted via GRE bridge (type match)"
                            )
                            return ClickResult(True, 0, 0, action.card_name or str(action), "GRE bridge")

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
            gre_result = self._try_gre_bridge(action)
            if gre_result is not None:
                return gre_result

        # GRE bridge not available — fall back to mouse-click execution
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
            ActionType.PAY_COSTS: lambda: self._exec_done_action("pay_costs"),
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
        if not handler:
            return ClickResult(False, 0, 0, str(action), f"No handler for {action.action_type}")

        return handler()

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
            if blocker_coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_blockers: blocker '{blocker_name}' (instance_id={blocker_instance_id})"
                )
                bx, by = blocker_coord.to_absolute(window_rect)
                self._controller.click(bx, by, f"Blocker: {blocker_name}", window_rect)
                self._controller.wait(0.2, "blocker selected")
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

            # Click the attacker (opponent's creature) — use instance_id if available
            attacker_instance_id = self._find_instance_id(attacker_name, battlefield, opp_seat)
            attacker_coord = self._mapper.get_permanent_coord(
                attacker_name, attacker_instance_id, battlefield, opp_seat, local_seat
            )
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
        for owner in [opp_seat, local_seat]:
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
        card_lower = card_name.lower()
        for card in battlefield:
            if card.get("owner_seat_id") != owner_seat:
                continue
            name = card.get("name", "")
            if name.lower() == card_lower:
                return card.get("instance_id")
        return None

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

        while time.time() < deadline:
            try:
                post_state = self._get_game_state()

                # 0. New pending decision appeared (ETB choices, mana payments, etc.)
                # This means the action was processed and MTGA is waiting for a
                # follow-up choice (e.g. shock land "Pay 2 life?", scry, etc.)
                post_pending = post_state.get("pending_decision")
                if post_pending and post_pending != pre_pending:
                    logger.info(f"Action verified: new pending decision '{post_pending}' (follow-up choice)")
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

        logger.warning(f"Action verification timed out after {self._config.verification_timeout}s")
        return False
