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
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from PIL import ImageGrab

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.gre_bridge import (
    GREBridge,
    UNMAPPED_INTERACTION_TYPE,
    _ACTIONS_AVAILABLE_BRIDGE_REQUESTS,
    enrich_snapshot_from_pending_response,
    get_bridge,
)
from arenamcp.input_controller import ClickResult, InputController
from arenamcp.screen_mapper import ScreenCoord, ScreenMapper

logger = logging.getLogger(__name__)


def _match_target_in_battlefield(
    target_names: list[str],
    battlefield: list[dict[str, Any]],
    eligible: Any,
) -> tuple[Optional[int], Optional[str]]:
    """Resolve a target-name hint to a battlefield instance_id.

    Tries exact (case-insensitive) match first, then substring match
    against either direction (helps when the LLM truncates "Wonderweave
    Aerialist" to "Aerialist"), then token-overlap as a last resort.
    `eligible(card)` filters the battlefield to only cards the bridge
    has flagged as legal targets.
    """
    if not target_names:
        return None, None

    candidates = [c for c in (battlefield or []) if eligible(c)]

    def _name(card: dict[str, Any]) -> str:
        return str(card.get("name") or "").strip()

    def _iid(card: dict[str, Any]) -> Optional[int]:
        try:
            v = int(card.get("instance_id") or 0)
        except (TypeError, ValueError):
            return None
        return v or None

    for name in target_names:
        want = (name or "").strip().lower()
        if not want:
            continue
        for card in candidates:
            if _name(card).lower() == want:
                iid = _iid(card)
                if iid:
                    return iid, _name(card)

    # Substring match (either direction). Picks the longest matching
    # candidate name to prefer "Wonderweave Aerialist" over "Aerialist".
    for name in target_names:
        want = (name or "").strip().lower()
        if len(want) < 3:
            continue
        matches: list[tuple[int, str, int]] = []
        for card in candidates:
            cn = _name(card).lower()
            iid = _iid(card)
            if iid and cn and (want in cn or cn in want):
                matches.append((iid, _name(card), len(cn)))
        if matches:
            matches.sort(key=lambda x: -x[2])
            return matches[0][0], matches[0][1]

    # Token-overlap fallback (≥2 shared tokens, ignoring short stopwords).
    STOP = {"the", "of", "and", "a", "an", "in", "to", "on"}
    for name in target_names:
        tokens = {
            t.strip(",.:;\"'()[]").lower()
            for t in (name or "").split()
            if t and t.lower() not in STOP
        }
        tokens = {t for t in tokens if len(t) >= 3}
        if not tokens:
            continue
        best: Optional[tuple[int, int, str]] = None
        for card in candidates:
            cn_tokens = {
                t.strip(",.:;\"'()[]").lower()
                for t in _name(card).split()
                if t and t.lower() not in STOP
            }
            cn_tokens = {t for t in cn_tokens if len(t) >= 3}
            overlap = len(tokens & cn_tokens)
            iid = _iid(card)
            if iid and overlap >= 2 and (best is None or overlap > best[1]):
                best = (iid, overlap, _name(card))
        if best:
            return best[0], best[2]

    return None, None


_PLANNER_CARD_NAME_PREFIXES = (
    "ability:",
    "activate ability:",
    "activate:",
    "play land:",
    "cast:",
    "cast spell:",
)


def _normalize_planner_card_name(name: str) -> str:
    """Strip leading legal-action-string labels the LLM sometimes leaves on.

    The legal_actions strings the planner reads are formatted like
    "Activate Ability: Promising Vein" / "Cast Lightning Bolt", and the
    schema instructs the LLM to put just the card name in `card_name`.
    Models occasionally keep the label prefix anyway. The bridge match
    path does case-insensitive equality against the bridge's resolved
    card name ("Promising Vein"), so an unstripped prefix silently
    breaks every type+name match for that ability.

    Strips one matching prefix, case-insensitively. Idempotent on
    already-clean names.
    """
    if not name:
        return name
    stripped = name.strip()
    lo = stripped.lower()
    for prefix in _PLANNER_CARD_NAME_PREFIXES:
        if lo.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


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
    auto_execute_delay: float = 0.0  # Execute immediately by default; nonzero restores the cancel countdown
    auto_pass_priority: bool = True
    auto_resolve: bool = True
    verify_after_action: bool = True
    verification_timeout: float = 2.5
    action_delay: float = 0.25
    post_action_delay: float = 0.4  # Delay after action to allow GRE to update
    # Bumped 8.0 → 12.0 after the planner-prompt slim (~74% input-token cut)
    # cut typical call latency to ~1-1.5s. The extra headroom absorbs Azure
    # tail spikes (we've seen 6+s outliers) without forcing the retry cascade
    # that wastes a full call's worth of latency before recovery.
    planning_timeout: float = 30.0
    enable_vision_fallback: bool = True
    prefer_deterministic: bool = True  # When True, skip VLM for actions that have deterministic coordinates
    enable_tts_preview: bool = True
    dry_run: bool = False
    afk_mode: bool = False  # When True, auto-pass everything without LLM
    land_drop_mode: bool = False  # When True, auto-play one land per turn (no LLM)
    # When True, the planner deterministically plays a land first if the
    # active player has 0 lands played this turn and a Play Land action is
    # legal. Skips the LLM entirely for that priority window. Set False for
    # landfall-synergy decks where casting a trigger source before the land
    # is correct (Lotus Cobra, Felidar Retreat, etc.).
    land_drop_first: bool = True
    # Legacy name, current behavior: keep autopilot bridge-only and refuse
    # to simulate actions with mouse clicks. Actions the bridge cannot
    # submit are surfaced as MANUAL REQUIRED and auto-filed as bridge bugs.
    bridge_only_when_connected: bool = True
    # When the bridge is the only execution path and it's disconnected,
    # wait up to this long for the plugin to reconnect before declaring
    # MANUAL REQUIRED. The plugin's reconnect loop retries every 0.2-2s,
    # so a transient drop (scene transition, Python restart) recovers well
    # inside this window. 0 disables the wait.
    bridge_reconnect_wait: float = 4.0
    # After a wait expires without a connection, skip further waits for
    # this long so a dead plugin doesn't add seconds to every action.
    bridge_reconnect_wait_cooldown: float = 20.0


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
        ui_turn_plan_fn: Optional[Callable[[Optional[dict[str, Any]]], None]] = None,
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
            ui_turn_plan_fn: Optional UI callback (payload-or-None) for the
                static turn-plan panel. Receives the serialized turn plan
                whenever progress advances or the plan is invalidated; None
                payload means "hide the panel". Wholesale-replace; no append.
        """
        self._planner = planner
        self._mapper = mapper
        self._controller = controller
        self._game_state_fn = get_game_state
        self._config = config or AutopilotConfig()
        self._speak_fn = speak_fn
        self._ui_advice_fn = ui_advice_fn
        self._bug_report_fn = bug_report_fn
        self._ui_turn_plan_fn = ui_turn_plan_fn
        # Optional callback to record autopilot-driven decisions into the
        # app's advice_history. Set by standalone after construction.
        self._advice_recorder: Optional[Any] = None
        # Optional TrajectoryRecorder for real-match data collection. When set
        # (by play_real_matches), each planning decision is logged in the
        # self-play JSONL format. None by default => zero overhead.
        self._trajectory_recorder: Optional[Any] = None
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
        # Track which thread owns _lock so toggle_autopilot can distinguish
        # a stuck lock (owner thread dead/gone) from a live one before
        # force-releasing. Force-releasing a live owner's lock corrupts state.
        self._lock_owner_thread_id: Optional[int] = None

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
        # Last time a bridge-reconnect wait expired without the plugin
        # showing up — used to avoid stacking multi-second waits on every
        # action of every plan while the plugin is genuinely gone.
        self._last_bridge_wait_failed_at: float = 0.0

        # Cross-window livelock protection (live 2026-06-09: a cast that
        # can't complete — unpayable cost, rejected targeting — gets rolled
        # back, re-planned, and re-cast across NEW windows, so per-window
        # guards never trip; the cycle ran at machine speed and locked the
        # user out of the UI).
        self._cast_rollback_counts: dict[tuple[int, str], int] = {}
        self._last_cast_submitted: Optional[tuple[int, str]] = None
        self._max_seen_turn: int = 0
        self._window_first_seen_at: float = 0.0
        self._given_up_window_sig: Optional[tuple[Any, ...]] = None
        self._recent_submission_times: deque = deque(maxlen=32)
        # Per-request submission FSM (fable Phase C) — content-addressed
        # request identity, one in-flight submission per request.
        from arenamcp.request_tracker import RequestTracker
        self._request_tracker = RequestTracker()
        self._runaway_tripped_turn: Optional[int] = None
        self._escape_budget_turn: int = -1
        self._escape_count_this_turn: int = 0
        self._bridge_preloaded_actions: Optional[list[dict[str, Any]]] = None

        # Persistent strategic GAME PLAN layer (win conditions + path), reformed
        # only on material board changes and threaded into the planner's prompt
        # so the autopilot develops toward a win instead of reacting per-window.
        try:
            from arenamcp.game_plan import GamePlanManager

            self._game_plan_mgr: Optional[Any] = GamePlanManager(
                self._planner._backend
            )
        except Exception as e:  # never block construction on the strategic layer
            logger.debug("GamePlanManager unavailable: %s", e)
            self._game_plan_mgr = None

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
        # Bug-report dedup for repeated failures in the same priority window.
        self._reported_bridge_bug_keys: set[tuple[Any, ...]] = set()
        self._reported_bridge_bug_window_sig: Optional[tuple[Any, ...]] = None
        # Persistent failure counter (#231): _blocked_action_keys gets cleared
        # whenever _bridge_game_state_id ticks, which lets a perpetually failing
        # action (e.g. SelectTargets from Optimistic Scavenger that the bridge
        # has no handler for) retry forever as long as MTGA re-issues the same
        # logical decision with a new gameStateId. Track consecutive failures
        # by action key here so we can escalate to a "hard block" that survives
        # priority-window resets.
        self._persistent_failure_counts: dict[tuple[Any, ...], int] = {}
        self._HARD_BLOCK_FAILURE_THRESHOLD = 5
        # Universal loop-breaker: count how many times we've processed the SAME
        # interactive window without it clearing. Some interactive submits
        # ("Choose a color" SelectN, X-value, target picks) report success to
        # the bridge but the GRE silently rejects them and re-presents the same
        # window, so the failure counter above never trips and the harness
        # re-fires forever. After _AUTO_RESPOND_LOOP_THRESHOLD no-progress
        # repeats we escalate to the GRE's own auto_respond() — it always picks
        # a legal default, so the game advances unattended even on a request
        # type we don't have an explicit handler for.
        self._window_repeat_sig: Optional[tuple[Any, ...]] = None
        self._window_repeat_count: int = 0
        self._auto_respond_escaped_sig: Optional[tuple[Any, ...]] = None
        self._AUTO_RESPOND_LOOP_THRESHOLD = 3
        # Spoken game-plan announcement dedup (speak each new plan once).
        self._last_announced_plan: str = ""

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
        """Capture MTGA window as PNG bytes for VLM analysis.

        Uses PrintWindow for DirectX/Unity windows; ImageGrab (GDI BitBlt)
        returns black frames on many systems for MTGA.
        """
        try:
            from arenamcp.screen_capture import capture_mtga_png
            from arenamcp.input_controller import find_mtga_hwnd

            window_rect = self._mapper.window_rect
            if not window_rect:
                window_rect = self._mapper.refresh_window()
            bbox = None
            if window_rect:
                left, top, width, height = window_rect
                bbox = (left, top, left + width, top + height)

            try:
                hwnd = find_mtga_hwnd()
            except Exception:
                hwnd = None

            return capture_mtga_png(hwnd=hwnd, bbox=bbox)
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

    def _announce_game_plan(self) -> None:
        """Speak the current game plan aloud when it changes (TTS).

        Lets the operator hear what the autopilot is thinking strategically.
        Fires only when a speak function is wired (the desktop coach and the
        opt-in harness path) and only once per distinct plan. Always
        non-blocking and best-effort — never affects play.
        """
        if self._speak_fn is None or self._game_plan_mgr is None:
            return
        try:
            intro = self._game_plan_mgr.coach_intro()
        except Exception:
            return
        if not intro or intro == self._last_announced_plan:
            return
        self._last_announced_plan = intro
        # Show the plan in the Coach Log + match overlay too (not just speak it).
        # The "PLAN:" prefix marks it strategic so the desktop renders it as
        # visible advice rather than demoting it.
        if self._ui_advice_fn is not None:
            try:
                self._ui_advice_fn(intro, "AUTOPILOT")
            except Exception as e:
                logger.debug("game-plan UI advice failed: %s", e)
        try:
            # speak_fn signature is (text, blocking); announce in the background.
            self._speak_fn(intro, False)
        except TypeError:
            try:
                self._speak_fn(intro)
            except Exception as e:
                logger.debug("game-plan TTS announce failed: %s", e)
        except Exception as e:
            logger.debug("game-plan TTS announce failed: %s", e)

    @staticmethod
    def _is_local_active_turn(game_state: dict[str, Any]) -> bool:
        """True when it's our turn (or seat is unknown — treat as ours)."""
        local_seat = next(
            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
            None,
        )
        if local_seat is None:
            return True
        return (game_state.get("turn", {}) or {}).get("active_player") == local_seat

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
        """Reset blocked-action suppression when the priority window changes.

        Actions that have hit the persistent-failure threshold survive the
        reset — see #231. Without this, an action that the bridge can't
        handle (e.g. a SelectTargets sub-type with no bridge serializer)
        keeps getting retried every time MTGA re-issues the same logical
        decision with a new gameStateId, locking up gameplay.
        """
        sig = self._priority_window_signature(game_state)
        if sig != self._blocked_action_window_sig:
            self._blocked_action_window_sig = sig
            # Preserve hard-blocked actions across the window boundary.
            hard_blocked = {
                key for key in self._blocked_action_keys
                if self._persistent_failure_counts.get(key, 0)
                >= self._HARD_BLOCK_FAILURE_THRESHOLD
            }
            self._blocked_action_keys = hard_blocked

        # Universal loop-breaker bookkeeping: track consecutive repeats of the
        # exact same window so a non-clearing interactive request can be
        # escaped via auto_respond() (see _maybe_escape_stuck_window).
        if sig == self._window_repeat_sig:
            self._window_repeat_count += 1
        else:
            self._window_repeat_sig = sig
            self._window_repeat_count = 0
            self._window_first_seen_at = time.monotonic()
            self._auto_respond_escaped_sig = None
        if sig != self._reported_bridge_bug_window_sig:
            self._reported_bridge_bug_window_sig = sig
            self._reported_bridge_bug_keys.clear()

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
        """Block an action from being retried in the current priority window.

        Also bumps a persistent failure counter (#231). When the counter
        reaches _HARD_BLOCK_FAILURE_THRESHOLD, the block survives priority
        window changes — pause for manual instead of looping forever.
        """
        key = self._action_block_key(action, game_state)
        self._blocked_action_keys.add(key)
        count = self._persistent_failure_counts.get(key, 0) + 1
        self._persistent_failure_counts[key] = count
        if count >= self._HARD_BLOCK_FAILURE_THRESHOLD:
            logger.error(
                "Hard-blocking action after %d consecutive failures: %s (%s)",
                count, action, reason,
            )
            self._pause_for_manual(
                f"Action repeatedly failed ({count}x): {action.action_type.value}"
                f" {action.card_name or ''}".strip(),
                game_state,
            )
        else:
            logger.warning(
                "Blocking action for current window (failure %d/%d): %s (%s)",
                count, self._HARD_BLOCK_FAILURE_THRESHOLD, action, reason,
            )

    def _reset_persistent_failure(self, action: GameAction, game_state: dict[str, Any]) -> None:
        """Clear the persistent-failure counter for an action that just succeeded."""
        key = self._action_block_key(action, game_state)
        self._persistent_failure_counts.pop(key, None)

    def _is_action_blocked(self, action: GameAction, game_state: dict[str, Any]) -> bool:
        """Whether this action already failed to advance the current window."""
        return self._action_block_key(action, game_state) in self._blocked_action_keys

    # Oracle-text keywords that indicate a spell would HARM whatever it
    # targets. If the source spell has one of these and the sole target
    # candidate is a permanent the local player controls, auto-submitting
    # would hand the player's own card to the effect (classic Seam Rip
    # self-destruction bug). Hitting one of these phrases routes the
    # decision back to the LLM, which can cancel the cast or target
    # intentionally. Positive-target spells (auras with "enchant creature
    # you control", buffs with "target creature you control gets"...) do
    # NOT contain these phrases, so they keep the fast-path.
    _HARMFUL_SOURCE_ORACLE_PHRASES = (
        "destroy target",
        "exile target",
        "sacrifice target",  # rare — most "sacrifice" is "sacrifice a X you control"
        "counter target",
        "return target",      # bounce spells
        "opponent sacrifices target",
        "damage to target",   # Shock-style
        "gets -",             # "target creature gets -X/-X"
        "gets −",             # unicode minus variant
        "loses all abilities",
        "loses flying",
    )

    def _pick_single_target_candidate(
        self,
        game_state: dict[str, Any],
    ) -> Optional[int]:
        """Return the sole legal target instance_id if there's exactly one
        AND auto-submit would be a good thing.

        Two gates:
          1. The bridge has to report exactly one legal candidate.
          2. If that candidate is a permanent the local player controls,
             the source spell's oracle text must NOT contain a removal-
             style keyword. This keeps Sheltered-by-Ghosts-on-your-own-
             creature on the fast path, while pausing on Seam Rip when
             the only legal target is your own enchantment.
        Opponent-controlled candidates always pass.
        """
        def _extract_ids(resp: dict[str, Any]) -> list[int]:
            if not resp or not resp.get("has_pending"):
                return []
            cands = resp.get("target_candidates") or []
            ids: list[int] = []
            for c in cands:
                try:
                    iid = int(c.get("targetInstanceId") or 0)
                except (TypeError, ValueError):
                    continue
                if iid and iid not in ids:
                    ids.append(iid)
            return ids

        # Snapshot first; fall back to live bridge poll.
        snap_resp = game_state.get("_bridge_last_poll") or game_state.get("_bridge_trigger")
        ids = _extract_ids(snap_resp) if isinstance(snap_resp, dict) else []
        live_resp = None
        if not ids:
            try:
                if self._gre_bridge.connected or self._gre_bridge.connect():
                    live_resp = self._gre_bridge.get_pending_actions() or {}
                    ids = _extract_ids(live_resp)
            except Exception as e:
                logger.debug(f"_pick_single_target_candidate bridge query failed: {e}")
                return None
        if len(ids) != 1:
            return None

        only_id = ids[0]

        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

        # Look up candidate ownership.
        controller = None
        for card in game_state.get("battlefield", []) or []:
            try:
                iid = int(card.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid == only_id:
                controller = card.get("controller_id") or card.get("owner_seat_id")
                break

        # Opponent-controlled sole target → always safe to auto-submit.
        if local_seat is not None and controller is not None and controller != local_seat:
            return only_id

        # Self-controlled (or unknown controller): only auto-submit when
        # the source spell's oracle text reads as a positive / beneficial
        # targeting effect. Otherwise, pause so the LLM can cancel or
        # target deliberately.
        if self._source_spell_is_harmful_to_target(game_state, snap_resp, live_resp):
            logger.info(
                f"Autopilot: declining auto-submit for target {only_id} — "
                "sole candidate is self-controlled and the source spell "
                "looks removal-shaped. Letting the LLM decide."
            )
            return None

        return only_id

    def _source_spell_is_harmful_to_target(
        self,
        game_state: dict[str, Any],
        snap_resp: Optional[dict[str, Any]],
        live_resp: Optional[dict[str, Any]],
    ) -> bool:
        """Does the spell on the stack read like a removal / hurt effect?

        We find the source card in this order:
          1. decision_context (bridge-supplied sourceId → stack entry)
          2. top of the stack (spell currently resolving targets)
        Then we check its oracle text against known harmful phrases.
        Unknown oracle text => False (err on the side of auto-submit).
        """
        oracle = ""
        name = ""

        stack = game_state.get("stack", []) or []
        ctx = game_state.get("decision_context") or {}
        source_id = None
        for key in ("sourceId", "source_id", "source_instance_id"):
            try:
                v = ctx.get(key)
                if v:
                    source_id = int(v)
                    break
            except (TypeError, ValueError):
                continue

        picked = None
        if source_id:
            for entry in stack:
                try:
                    if int(entry.get("instance_id") or 0) == source_id:
                        picked = entry
                        break
                except (TypeError, ValueError):
                    continue
        if picked is None and stack:
            picked = stack[-1]  # top of stack

        if picked:
            oracle = str(picked.get("oracle_text") or "").lower()
            name = str(picked.get("name") or "")

        # Bridge may include oracle in target_candidates / request payload;
        # use it as a backup.
        for resp in (snap_resp, live_resp):
            if oracle or not resp:
                continue
            rp = (resp.get("request_payload") or {})
            for k in ("sourceOracleText", "oracleText", "oracle_text"):
                if rp.get(k):
                    oracle = str(rp[k]).lower()
                    break

        if not oracle:
            logger.debug(
                f"Autopilot: no oracle text found for source spell "
                f"(name={name!r}); defaulting to auto-submit"
            )
            return False

        for phrase in self._HARMFUL_SOURCE_ORACLE_PHRASES:
            if phrase in oracle:
                logger.info(
                    f"Autopilot: source spell {name!r} oracle contains "
                    f"{phrase!r} — treating as harmful-to-target"
                )
                return True
        return False

    def _record_autopilot_decision(
        self,
        game_state: dict[str, Any],
        trigger: str,
        action_type: str,
        summary: str,
    ) -> None:
        """Emit a synthetic advice-history entry for an autopilot decision.

        Bug reports only show `advice_history`, so autopilot-handled
        triggers (auto-target, auto-pay, auto-confirm, etc.) were
        invisible there. Recording them makes post-match debugging
        actually useful.
        """
        fn = getattr(self, "_advice_recorder", None)
        if not callable(fn):
            return
        try:
            fn(
                advice=f"[autopilot] {action_type}: {summary}",
                trigger=trigger,
                game_state=game_state,
            )
        except Exception as e:
            logger.debug(f"_record_autopilot_decision failed: {e}")

    def _maybe_record_trajectory(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: Optional[list[str]],
        decision_context: Optional[dict[str, Any]],
        plan: Optional[ActionPlan],
        latency_ms: float,
    ) -> None:
        """Record this planning decision to an attached TrajectoryRecorder.

        No-op (and near-zero cost) unless ``self._trajectory_recorder`` is set.
        Fully guarded — never raises into the planning/execution path.
        """
        recorder = getattr(self, "_trajectory_recorder", None)
        if recorder is None:
            return
        try:
            from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT
            prompt_user = self._planner._build_action_prompt(
                game_state, trigger, legal_actions, decision_context
            )
            planned = plan.actions[0] if (plan and plan.actions) else None
            request_type = (
                game_state.get("_bridge_request_type")
                or game_state.get("_bridge_request_class")
                or trigger
            )
            recorder.record_decision(
                game_state=game_state,
                prompt_system=AUTOPILOT_SYSTEM_PROMPT,
                prompt_user=prompt_user,
                planned_action=planned,
                request_type=request_type,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.debug(f"_maybe_record_trajectory failed (ignored): {e}")

    # A cast rolled back this many times in one turn is hidden from the
    # planner for the rest of the turn — it cannot complete and re-trying
    # is the engine of the cast→cancel→re-cast livelock.
    _CAST_ROLLBACK_LIMIT = 2
    # auto_respond escapes allowed per turn. Each new gameStateId makes a
    # new window signature, so the old once-per-window guard allowed an
    # escape every cycle of a cross-window loop — i.e. forever.
    _MAX_ESCAPES_PER_TURN = 2
    # A window must be stuck this long (wall clock) before auto_respond may
    # escape it — the repeat counter alone trips in <1s of trigger spam.
    _ESCAPE_MIN_WINDOW_AGE_S = 12.0

    def _note_cast_rollback(self, why: str) -> None:
        """Record that the most recently submitted cast was rolled back."""
        last = self._last_cast_submitted
        if not last:
            return
        self._cast_rollback_counts[last] = self._cast_rollback_counts.get(last, 0) + 1
        n = self._cast_rollback_counts[last]
        logger.warning(
            f"Cast rollback #{n} for {last[1]!r} (turn {last[0]}): {why}"
        )

    @staticmethod
    def _plain_card_name(text: str) -> str:
        """Strip (P/T) and trailing [TAG]s from a legal-action card name."""
        text = re.sub(r"\s*\([\dxX*+-]+/[\dxX*+-]+\)\s*$", "", text or "").strip()
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text).strip()
        return text

    def _filter_rolled_back_casts(
        self, legal_actions: list[str], game_state: dict[str, Any]
    ) -> list[str]:
        """Hide 'Cast X' from the planner once X was rolled back twice this turn.

        A cast that reached PayCosts/targeting and got cancelled cannot
        complete with the current resources; offering it to the planner
        again just re-arms the livelock (live 2026-06-09).
        """
        if not legal_actions or not self._cast_rollback_counts:
            return legal_actions
        turn = int((game_state.get("turn") or {}).get("turn_number", 0) or 0)
        out: list[str] = []
        for la in legal_actions:
            if la.lower().strip().startswith("cast "):
                name = self._plain_card_name(la.strip()[5:]).lower()
                if (
                    self._cast_rollback_counts.get((turn, name), 0)
                    >= self._CAST_ROLLBACK_LIMIT
                ):
                    logger.info(
                        f"Suppressing legal action {la!r} — cast rolled back "
                        f"{self._CAST_ROLLBACK_LIMIT}+ times this turn"
                    )
                    continue
            out.append(la)
        return out

    def _try_auto_respond_escape(
        self, game_state: Optional[dict[str, Any]], reason: str
    ) -> bool:
        """Escape a stuck interactive request via the GRE's own auto_respond().

        Last-resort, universal unblocker. ``auto_respond()`` invokes the pending
        request's ``AutoRespond()`` on the MTGA side, which picks a legal default
        for ANY request type (color choice, X value, target, modal, ...). It is
        not always the optimal choice, but it always advances the game — which is
        what lets the autopilot finish a match unattended on a request type we
        don't have an explicit handler for. Restricted to interactive
        (non-ActionsAvailable) requests; ActionsAvailable windows pass/play
        through their own paths.
        """
        if self._config.dry_run or self._gre_bridge is None:
            return False
        if not getattr(self._gre_bridge, "connected", False):
            return False
        breq = str((game_state or {}).get("_bridge_request_type") or "")
        bcls = str((game_state or {}).get("_bridge_request_class") or "")
        if not (breq or bcls):
            return False
        # Don't auto_respond an ordinary priority window — those pass/play.
        if (
            breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
        ):
            return False
        # Per-turn escape budget. Window signatures change every
        # gameStateId, so a cross-window loop presents a "new" window each
        # cycle and the once-per-window guard never limits anything —
        # observed live 2026-06-09 as an escape every ~3s, each one
        # cancelling the user's own cast.
        turn = int(((game_state or {}).get("turn") or {}).get("turn_number", 0) or 0)
        if turn != self._escape_budget_turn:
            self._escape_budget_turn = turn
            self._escape_count_this_turn = 0
        if self._escape_count_this_turn >= self._MAX_ESCAPES_PER_TURN:
            logger.warning(
                "auto_respond escape budget exhausted for turn %s — leaving "
                "%s for the user", turn, breq or bcls,
            )
            return False
        try:
            if self._gre_bridge.auto_respond():
                self._escape_count_this_turn += 1
                if any(
                    k in (breq + bcls)
                    for k in ("SelectTargets", "PayCosts", "CastingTimeOption")
                ):
                    # Escaping a casting-flow window rolls back the cast.
                    self._note_cast_rollback(
                        f"auto_respond escape on {breq or bcls}"
                    )
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"auto_respond escape on stuck {breq or bcls} ({reason})",
                )
                # Feed the strategic layer: a plan step we couldn't enact.
                if self._game_plan_mgr is not None:
                    try:
                        self._game_plan_mgr.note_stall(f"{breq or bcls} ({reason})")
                    except Exception:
                        pass
                self._state = AutopilotState.IDLE
                return True
        except Exception as e:
            logger.debug(f"auto_respond escape failed: {e}")
        return False

    def _maybe_escape_stuck_window(self, game_state: dict[str, Any]) -> bool:
        """If the same interactive window has repeated too many times, escape it.

        Handles the case where an interactive submit reports success to the
        bridge but the GRE silently rejects it (wrong id/type) and re-presents
        the same window — the per-action failure counter never trips because
        nothing "failed", so without this the harness re-fires forever (observed
        live as the 'Choose a color' SelectN loop submitting 19 times).
        """
        sig = self._window_repeat_sig
        # Age gate: the repeat counter increments on every trigger ping and
        # several pings land per second for one window, so the count alone
        # said "stuck" within ~0.5s of a cast — the escape then fired BEFORE
        # the real handler got one attempt, and its AutoRespond consumed
        # MTGA's client-side request object while the GRE kept waiting. The
        # game froze on the targeting arrow until a human clicked (live
        # 2026-06-09: Ruthless Negotiation, Withering Torment). Only escape
        # windows that have been stuck for real wall-clock time.
        window_age = time.monotonic() - getattr(self, "_window_first_seen_at", 0.0)
        if (
            self._window_repeat_count >= self._AUTO_RESPOND_LOOP_THRESHOLD
            and window_age >= self._ESCAPE_MIN_WINDOW_AGE_S
            and sig is not None
            and sig != self._auto_respond_escaped_sig
        ):
            if self._try_auto_respond_escape(
                game_state, f"window repeated {self._window_repeat_count}x"
            ):
                self._auto_respond_escaped_sig = sig
                self._window_repeat_count = 0
                return True
        return False

    def _try_submit_plan_advancing_play(
        self, game_state: Optional[dict[str, Any]]
    ) -> bool:
        """Submit a legal plan-advancing play instead of passing it away.

        Last-ditch guard used before the auto-pass fallback: on our own
        ActionsAvailable window, if the bridge offers a land drop or a castable
        spell that the plan wants, submit it by index rather than passing
        priority. This is what stops the autopilot from silently skipping a
        castable creature (e.g. Spellbook Vendor / Veteran Survivor) when the
        planner's chosen action failed to match and we'd otherwise auto-pass.

        Deliberately conservative: only fires for an unambiguous choice — the
        plan's wanted card, the sole legal land drop, or the sole legal cast.
        When several casts are legal and none matches the plan, it declines
        (returns False) and lets the caller pass, since blindly casting a random
        spell is worse than passing.
        """
        if self._config.dry_run or self._gre_bridge is None:
            return False
        if not getattr(self._gre_bridge, "connected", False):
            return False
        if game_state is None or not self._is_local_active_turn(game_state):
            return False
        breq = str(game_state.get("_bridge_request_type") or "")
        bcls = str(game_state.get("_bridge_request_class") or "")
        if not (
            breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
        ):
            return False
        try:
            pending = self._gre_bridge.get_pending_actions()
        except Exception:
            return False
        if not pending or not pending.get("has_pending"):
            return False
        actions = pending.get("actions") or []

        def _norm(a: dict) -> str:
            return str(a.get("actionType", "")).replace("ActionType_", "").lower()

        candidates = [(i, a) for i, a in enumerate(actions) if _norm(a) in ("play", "cast")]
        if not candidates:
            return False

        chosen_idx: Optional[int] = None
        # 1. Prefer the card the plan actually wanted.
        wanted = ""
        if self._current_plan and getattr(self._current_plan, "actions", None):
            first = self._current_plan.actions[0]
            if first.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
                wanted = _normalize_planner_card_name(first.card_name or "").lower()
        if wanted:
            for i, a in candidates:
                grp = a.get("grpId", 0)
                name = ""
                if grp:
                    try:
                        from arenamcp import server
                        name = (server.get_card_info(grp).get("name", "") or "").lower()
                    except Exception:
                        name = ""
                if name and (wanted == name or wanted in name or name in wanted):
                    chosen_idx = i
                    break
        # 2. Else an unambiguous sole land drop, then a sole cast.
        if chosen_idx is None:
            plays = [i for i, a in candidates if _norm(a) == "play"]
            casts = [i for i, a in candidates if _norm(a) == "cast"]
            if len(plays) == 1:
                chosen_idx = plays[0]
            elif len(casts) == 1:
                chosen_idx = casts[0]
        if chosen_idx is None:
            return False
        try:
            if self._gre_bridge.submit_action_by_index(
                chosen_idx, auto_pass=self._config.auto_pass_priority
            ):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"plan-advancing play submitted instead of auto-pass (idx={chosen_idx})",
                )
                return True
        except Exception as e:
            logger.debug(f"plan-advancing submit failed: {e}")
        return False

    def _pause_for_manual(self, reason: str, game_state: Optional[dict[str, Any]] = None) -> None:
        """Pause the autopilot and surface that manual input is required.

        Appends a short bridge-gap hint to the user-facing notification so
        the operator can tell *why* autopilot stopped: a known unhandled
        request type ("Bridge gap: SelectTargets") reads very differently
        from "bridge offline" or "no request pending". Without this hint
        the user just sees "MANUAL REQUIRED: Bridge couldn't handle X" and
        has no signal whether to file a bug, reconnect, or just wait.
        """
        # Never pass away a legal, plan-advancing play. Before the graceful
        # auto-pass below, if this is our own ActionsAvailable window and the
        # bridge offers an unambiguous land drop or castable spell the plan
        # wants, submit it instead of passing. This is what fixes the autopilot
        # silently skipping a castable creature when the planner's action failed
        # to match the bridge.
        if not self._config.dry_run and self._try_submit_plan_advancing_play(game_state):
            self._state = AutopilotState.IDLE
            return

        # Graceful auto-pass: if we're stuck on a normal ActionsAvailable
        # priority window where passing is legal, advance the game by passing
        # instead of halting for manual input. This keeps a match moving when
        # the planner's chosen action can't be submitted (e.g. it wanted a
        # second land it doesn't have, or an aura with no legal target) —
        # passing priority is the correct fallback and prevents a dead-loop.
        # Non-ActionsAvailable interactive requests (Group/SelectN/Search/...)
        # are handled earlier by the safe-default net (passing them is illegal),
        # so we only auto-pass here when the bridge explicitly allows a pass.
        if (
            not self._config.dry_run
            and game_state is not None
            and self._gre_bridge is not None
            and getattr(self._gre_bridge, "connected", False)
            and bool(game_state.get("_bridge_can_pass"))
        ):
            breq = str(game_state.get("_bridge_request_type") or "")
            bcls = str(game_state.get("_bridge_request_class") or "")
            if (
                breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            ):
                try:
                    if self._gre_bridge.submit_pass():
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"auto-pass to advance (could not act: {reason})",
                        )
                        self._state = AutopilotState.IDLE
                        return
                except Exception as e:
                    logger.debug(f"auto-pass fallback failed: {e}")

        # Final universal escape before surfacing manual-required: if there's a
        # pending interactive (non-ActionsAvailable) request we couldn't handle,
        # let the GRE auto-respond with a legal default so the match keeps
        # going. Optimality is secondary to staying hands-free.
        # Age-gated like _maybe_escape_stuck_window: this path fired ~5s into
        # a London-bottoming GroupRequest (live 2026-06-09 19:01) and
        # auto-responded the user's mulligan bottoming before the proper
        # group handler ran. A young window is not stuck — let the normal
        # handlers have it first.
        window_age = time.monotonic() - getattr(self, "_window_first_seen_at", 0.0)
        if (
            not self._config.dry_run
            and window_age >= self._ESCAPE_MIN_WINDOW_AGE_S
            and self._try_auto_respond_escape(
                game_state, f"manual-required fallback: {reason}"
            )
        ):
            return

        # The plan could not be enacted here — tell the strategic layer so a
        # repeatedly-unexecutable plan gets reformed into a different line.
        if self._game_plan_mgr is not None:
            try:
                self._game_plan_mgr.note_stall(reason)
            except Exception:
                pass

        self._state = AutopilotState.PAUSED
        # Stand down for THIS window: the user has been told to act. Without
        # this, the coaching loop's backstop re-forces decision_required
        # every ~2s, each cycle replanning (LLM call) and re-speaking the
        # same advice against a window only the user can resolve (live
        # 2026-06-09: dead SelectTargets window → TTS loop).
        if game_state is not None:
            try:
                self._given_up_window_sig = self._priority_window_signature(game_state)
            except Exception:
                self._given_up_window_sig = None
        hint = self._format_bridge_gap_hint(game_state)
        details = ""
        if game_state:
            details = (
                f" pending={game_state.get('pending_decision')!r}"
                f" bridge={game_state.get('_bridge_request_type') or game_state.get('_bridge_request_class')!r}"
            )
        logger.warning("Autopilot manual required: %s%s", reason, details)
        suffix = f" [{hint}]" if hint else ""
        self._notify("AUTOPILOT", f"MANUAL REQUIRED: {reason}{suffix}")

    def is_window_given_up(self, game_state: dict[str, Any]) -> bool:
        """True if MANUAL REQUIRED was already declared for the current window.

        The coaching loop uses this to stop re-forcing decision_required
        for a window the autopilot has handed to the user. Self-clears as
        soon as the window signature changes (user acted / game advanced).
        """
        sig = getattr(self, "_given_up_window_sig", None)
        if sig is None:
            return False
        try:
            return self._priority_window_signature(game_state) == sig
        except Exception:
            return False

    def _format_bridge_gap_hint(
        self, game_state: Optional[dict[str, Any]]
    ) -> str:
        """Build a short user-facing explanation of why the bridge couldn't act.

        Possible shapes:
          - "Bridge gap: SelectTargetsRequest" — bridge has a pending request
            but no handler for that type yet.
          - "Bridge offline"                   — bridge isn't connected.
          - "No bridge request pending"        — bridge connected but quiet.
          - ""                                 — no game_state available.
        """
        if not game_state:
            return ""

        connected = game_state.get("_bridge_connected")
        if connected is False:
            return "Bridge offline"

        req = (
            game_state.get("_bridge_request_type")
            or game_state.get("_bridge_request_class")
        )
        if not req:
            pending = game_state.get("pending_decision")
            if pending:
                return f"No bridge request pending (pending_decision={pending!r})"
            return "No bridge request pending"

        decision_type = ""
        ctx = game_state.get("decision_context") or {}
        if isinstance(ctx, dict):
            decision_type = str(ctx.get("type") or "")
        if decision_type:
            return f"Bridge gap: {req} (type={decision_type})"
        return f"Bridge gap: {req}"

    def _manual_required_bridge_result(
        self,
        action: GameAction,
        game_state: dict[str, Any],
        reason_tag: str,
        message: str,
    ) -> ClickResult:
        """Report a bridge miss, pause autopilot, and return a failed result."""
        self._report_fallback_bug(action, game_state, reason_tag)
        self._pause_for_manual(message, game_state)
        return ClickResult(False, 0, 0, action.card_name or action.action_type.value, "manual required")

    def _run_bridge_action(self, action: GameAction, game_state: dict[str, Any]) -> bool:
        """Execute a bridge action, or no-op it in dry-run mode."""
        if self._config.dry_run:
            logger.info("[DRY RUN] bridge-only action: %s", action)
            return True
        return self._execute_action(action, game_state).success

    def _is_planner_action_stale_vs_bridge(
        self,
        action: GameAction,
        game_state: dict[str, Any],
    ) -> bool:
        """Detect "planner picked an action the bridge no longer offers".

        Known stale-state shapes:

        0. **Bridge has no pending request at all** — the priority window
           closed between plan-generation and submission. Any planner action
           would just produce ``bridge_submit_failed``; treat as stale so we
           re-plan cleanly instead of filing a noise bug report. Cluster:
           issues #191 #194 (post-resolution race) and the duplicates #192
           #193 (match-boundary takeover).
        1. ``play_land`` / ``cast_spell`` against an ActionsAvailable request
           that has no matching Play/Cast entries — planner saw stale
           ``legal_actions`` (e.g. user already used their land drop). Cluster
           that produced this code path: issues #136 #137 #139 #140.
        2. ``play_land`` / ``cast_spell`` against a non-ActionsAvailable
           request type entirely (SelectN, Search, SelectTargets, PayCosts,
           CastingTimeOption, etc.). A new decision window opened between
           plan-generation and submission; the plan's first step is no
           longer applicable until that window resolves. Cluster: SelectN
           bridge gap from #189 and the rest of the v2.3.0 SelectN reports.
        3. ``declare_attackers`` / ``declare_blockers`` against a non-combat
           request class — rules_engine synthesizes "Declare Attackers: ..."
           into legal_actions during main phase, but the actual GRE pending is
           still ActionsAvailable / SelectN / etc. (window changes during the
           planner's LLM call). Surfacing manual-required is misleading
           because the user can't act on a step that hasn't started yet.
        4. ``select_n`` / ``select_target`` / ``search_library`` /
           ``select_counters`` against a non-selection bridge request. These
           need SelectN / SelectTargets / Search / Group request types.

        For everything else we return False so the normal
        ``bridge_submit_failed`` path still files a bug — those are real
        bridge issues worth investigating.
        """
        bridge_type = str(game_state.get("_bridge_request_type") or "")
        bridge_class = str(game_state.get("_bridge_request_class") or "")

        # Shape 0: bridge connected but no pending request at all. Any submit
        # would hit "no pending window" — the priority window closed. Skip
        # rather than file a bridge_submit_failed bug. Excluded action types:
        # ones that legitimately submit while no GRE request is pending (none
        # currently — every submit path needs a target request).
        if not bridge_type and not bridge_class:
            return True

        if action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
            # Shape 2: bridge has a different request type pending entirely.
            is_actions_available = (
                bridge_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                or bridge_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            )
            if not is_actions_available:
                return True

            # Shape 1: bridge IS ActionsAvailable but doesn't offer the
            # specific Play/Cast the planner picked.
            bridge_actions = game_state.get("_bridge_actions") or []
            if not bridge_actions:
                return False
            target_type = "Play" if action.action_type == ActionType.PLAY_LAND else "Cast"
            for ba in bridge_actions:
                ba_type = (ba.get("actionType") or "")
                if ba_type == target_type or ba_type == f"ActionType_{target_type}":
                    return False
            return True

        if action.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS):
            expected = (
                "DeclareAttacker"
                if action.action_type == ActionType.DECLARE_ATTACKERS
                else "DeclareBlockers"
            )
            if expected in bridge_class or expected in bridge_type:
                return False
            # Bridge doesn't have the combat request the planner targeted —
            # planner's legal_actions snapshot was stale.
            return True

        # Shape #4: selection-family — SelectN / SelectTargets / Search /
        # Group / SelectReplacement etc. all expect a "selection-class"
        # bridge request to be pending. If the bridge has a different
        # request, it's stale — the planner saw a decision that's already
        # been resolved or hasn't started.
        if action.action_type in (
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
            ActionType.SEARCH_LIBRARY,
            ActionType.SELECT_COUNTERS,
            ActionType.SELECT_REPLACEMENT,
        ):
            looks_compatible = any(
                kw in bridge_class or kw in bridge_type
                for kw in ("SelectN", "SelectTarget", "Search", "Group", "SelectReplacement")
            )
            if looks_compatible:
                return False
            # No matching bridge request — race or already-resolved.
            return True

        # Shape 5: pass/resolve against a non-passable window. SubmitPass
        # only exists on ActionsAvailableRequest — if the window changed to
        # PayCosts / CastingTimeOption / a selection request between plan
        # and submit (a cast started resolving, or the user acted manually),
        # "Cannot pass on current interaction" is guaranteed. Stale: the
        # next plan cycle sees the new window. Cluster: bug_20260610_121152
        # (planned pass landed on PayCostsReq while the user manually cast
        # Sapling Nursery).
        if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE):
            is_actions_available = (
                bridge_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                or bridge_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            )
            if not is_actions_available:
                return True

        return False

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

    def _acquire_lock(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire self._lock and record owner thread on success."""
        acquired = self._lock.acquire(blocking=blocking, timeout=timeout)
        if acquired:
            self._lock_owner_thread_id = threading.get_ident()
        return acquired

    def _release_lock(self) -> None:
        """Release self._lock and clear owner thread.

        Safe to call when the lock isn't held by the current thread
        (e.g. after a force-release recovery).
        """
        self._lock_owner_thread_id = None
        try:
            self._lock.release()
        except RuntimeError:
            pass

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
        if not self._acquire_lock(timeout=10.0):
            # Lock held for >10 seconds — force release (previous call is hung)
            logger.warning(f"Autopilot: lock held >10s, force-releasing for {trigger}")
            self._release_lock()
            if not self._acquire_lock(blocking=False):
                logger.error(f"Autopilot: could not acquire lock even after force-release")
                return False

        try:
            if self._abort_event.is_set():
                self._state = AutopilotState.IDLE
                return False

            turn_num = int((game_state.get("turn") or {}).get("turn_number", 0) or 0)
            if turn_num and turn_num < self._max_seen_turn:
                # Turn counter went backwards → new match. Drop per-match
                # livelock memories.
                self._cast_rollback_counts.clear()
                self._last_cast_submitted = None
                self._runaway_tripped_turn = None
                self._request_tracker.reset()
            self._max_seen_turn = max(self._max_seen_turn, turn_num)

            # Runaway protection: once tripped, stand down for the rest of
            # the turn no matter how many triggers fire. Self-clears on the
            # next turn.
            if self._runaway_tripped_turn is not None:
                if turn_num == self._runaway_tripped_turn:
                    logger.info(
                        "Autopilot: runaway protection active (turn %s) — standing down",
                        turn_num,
                    )
                    self._state = AutopilotState.IDLE
                    return False
                self._runaway_tripped_turn = None

            # Given-up window: MANUAL REQUIRED was already declared for this
            # exact window — replanning it would only repeat the same LLM
            # call and TTS line. Stay silent until the window changes.
            if self.is_window_given_up(game_state):
                logger.debug(
                    "Autopilot: window already declared manual-required; "
                    "standing by for the user"
                )
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

            # Universal loop-breaker: if the same interactive window keeps
            # re-presenting despite our submits, escape via auto_respond() so the
            # game advances unattended instead of looping forever.
            if self._maybe_escape_stuck_window(game_state):
                return True

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
                    # Arbiter doctrine (fable-improvements.md item 4): a
                    # connected, idle bridge is authoritative — log-derived
                    # decisions are stale by definition. The old "log has
                    # data; proceeding" branch here planned (and spoke)
                    # against ghost decisions the client had already
                    # consumed (live 2026-06-09 TTS/replan spiral).
                    logger.info(
                        "Autopilot: bridge connected but idle — no decision "
                        "exists (arbiter); dropping trigger '%s'",
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

            # --- TYPED-DECISION PATH (fable Phase B) ---
            # Interactive request families flow as structured options:
            # the planner picks option ids, submission is by id, and no
            # display string is parsed. ActionsAvailable stays on the
            # legacy strategic path until Phase C migrates it.
            typed_handled = self._try_typed_decision_path(game_state, trigger)
            if typed_handled is not None:
                return typed_handled

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

            # Bridge zeroes out request_type/class during Intermission (see
            # gre_bridge._process_bridge_overlay), so the string-prefix check
            # alone misses the common end-of-match case. Use the durable
            # _bridge_in_intermission signal as the primary guard.
            if (
                game_state.get("_bridge_in_intermission")
                or game_state.get("match_ended")
                or pending == "Intermission"
                or bridge_request_type.startswith("Intermission")
                or bridge_request_class.startswith("Intermission")
            ):
                logger.info("Autopilot: ignoring non-actionable intermission request")
                self._state = AutopilotState.IDLE
                return True

            # SelectTargets single-candidate auto-submit. When there's
            # exactly one legal target (common: "Target a creature you
            # control" with only one creature in play), skip the LLM
            # and submit immediately — saves ~4s of latency and avoids
            # the stale-plan race that leaves the request stuck.
            if (
                "SelectTargets" in bridge_request_class
                or bridge_request_type in ("SelectTargets", "SelectTargetsReq")
                or (game_state.get("decision_context") or {}).get("type") == "target_selection"
            ):
                auto_id = self._pick_single_target_candidate(game_state)
                if auto_id is not None:
                    logger.info(
                        f"Autopilot: auto-submitting single-candidate target "
                        f"(instance_id={auto_id})"
                    )
                    if not self._config.dry_run and (
                        self._gre_bridge.connected or self._gre_bridge.connect()
                    ):
                        if self._gre_bridge.submit_targets(auto_id):
                            self._log_execution_path(
                                ExecutionPath.GRE_AWARE,
                                f"auto-submit single target {auto_id}",
                            )
                            self._record_autopilot_decision(
                                game_state,
                                trigger,
                                action_type="select_target",
                                summary=f"auto-selected only legal target (instance_id={auto_id})",
                            )
                            self._state = AutopilotState.IDLE
                            return True
                        logger.warning(
                            f"Autopilot: submit_targets({auto_id}) failed — "
                            "falling through to LLM planning"
                        )

            # Fetch legal actions once for all shortcut checks below
            legal = self._get_legal_actions(game_state)

            # PayCostsRequest — accept autotap if available, otherwise only
            # cancel when we genuinely have no resolvable payment route.
            if (
                bridge_request_type in ("PayCosts", "PayCostsReq", "pay_costs")
                or bridge_request_class in ("PayCostsRequest",)
                or (game_state.get("decision_context") or {}).get("type") == "pay_costs"
            ):
                # User preference (2026-04-30): always click Auto Pay when
                # MTGA offers it — never try to manually decide which lands
                # to tap. submit_auto_tap walks PayCostsRequest's children
                # for the AutoTapActionsRequest and submits its solution
                # (= what the in-game Auto Pay button does).
                logger.info("Autopilot: submitting AutoTap solution for PayCosts")
                if not self._config.dry_run and (
                    self._gre_bridge.connected or self._gre_bridge.connect()
                ):
                    if self._gre_bridge.submit_auto_tap():
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE, "auto_pay via submit_auto_tap"
                        )
                        self._record_autopilot_decision(
                            game_state, trigger,
                            action_type="pay_costs",
                            summary="submitted AutoTap solution via bridge",
                        )
                        return True
                    # No autotap child available — fall back to cancel.
                    logger.info("Autopilot: no AutoTap solution; cancelling PayCostsRequest")
                    if self._gre_bridge.cancel_action():
                        self._log_execution_path(ExecutionPath.GRE_AWARE, "cancel PayCosts")
                        # The cast that opened this PayCosts can't be paid —
                        # remember it so the planner stops re-picking it.
                        self._note_cast_rollback("PayCosts cancelled (no autotap)")
                        return True
                self._record_autopilot_decision(
                    game_state, trigger,
                    action_type="pay_costs",
                    summary="auto-pay attempt failed",
                )
                self._manual_required_bridge_result(
                    GameAction(
                        action_type=ActionType.PAY_COSTS,
                        card_name="auto_pay",
                        reasoning="submit AutoTap via GRE bridge",
                    ),
                    game_state,
                    "bridge_submit_failed",
                    "GRE bridge submit_auto_tap did not advance Pay Costs",
                )
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
                self._record_autopilot_decision(
                    game_state, trigger,
                    action_type="click_button",
                    summary=f"auto-confirmed '{done_action}'",
                )
                if "attacker" in done_action.lower():
                    # Do NOT silently confirm an empty attack when this turn's
                    # plan intended to swing. If an attack was planned and the
                    # bridge is presenting legal attackers, declare them instead
                    # of submitting DeclareAttackersSubmit with nobody attacking.
                    intended_attackers: list[str] = []
                    if self._planner.has_pending_attack_intent():
                        ctx = game_state.get("decision_context") or {}
                        intended_attackers = [
                            str(name)
                            for name in (ctx.get("legal_attackers") or [])
                            if name
                        ]
                        if intended_attackers:
                            logger.info(
                                "Autopilot: attack intended this turn — declaring "
                                f"{intended_attackers} instead of confirming empty"
                            )
                    if not intended_attackers:
                        # No planner intent — ask the combat solver before
                        # submitting an empty attack. Live finding 2026-06-06:
                        # autopilot confirmed "no attackers" every combat even
                        # with safe profitable attacks on board, because the
                        # only attack source was turn-plan intent.
                        solver_names = self._solver_attack_names(game_state)
                        if solver_names:
                            intended_attackers = solver_names
                            logger.info(
                                "Autopilot: combat solver picked attackers "
                                f"{solver_names}; declaring instead of empty confirm"
                            )
                    return self._run_bridge_action(
                        GameAction(
                            action_type=ActionType.DECLARE_ATTACKERS,
                            attacker_names=intended_attackers,
                            reasoning=f"auto-confirmed '{done_action}'",
                        ),
                        game_state,
                    )
                if "blocker" in done_action.lower():
                    return self._run_bridge_action(
                        GameAction(
                            action_type=ActionType.DECLARE_BLOCKERS,
                            blocker_assignments={},
                            reasoning=f"auto-confirmed '{done_action}'",
                        ),
                        game_state,
                    )
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"auto-confirmed '{done_action}'",
                    ),
                    game_state,
                )

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
                        self._manual_required_bridge_result(
                            GameAction(
                                action_type=ActionType.CLICK_BUTTON,
                                card_name="decline",
                                reasoning="auto-decline optional action via GRE bridge",
                            ),
                            game_state,
                            "bridge_submit_failed",
                            "GRE bridge could not decline the optional action",
                        )
                        return False
                    return True

            # "Priority (Pass Only)" means only Pass is legal — auto-pass immediately
            # without LLM planning. MTGA may also auto-pass these, so speed is key.
            if pending == "Priority (Pass Only)":
                logger.info("Autopilot: auto-passing (pass-only priority)")
                self._record_autopilot_decision(
                    game_state, trigger,
                    action_type="pass_priority",
                    summary="pass-only priority, auto-passed",
                )
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.PASS_PRIORITY,
                        reasoning="pass-only priority, auto-passed",
                    ),
                    game_state,
                )

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
                        return self._run_bridge_action(
                            GameAction(
                                action_type=ActionType.PASS_PRIORITY,
                                reasoning="no legal actions; auto-pass via GRE bridge",
                            ),
                            game_state,
                        )

                if self._config.auto_resolve and trigger == "spell_resolved":
                    if not is_my_turn and not can_do_anything:
                        logger.info("Autopilot: auto-resolving (opponent's spell, no responses)")
                        return self._run_bridge_action(
                            GameAction(
                                action_type=ActionType.RESOLVE,
                                reasoning="opponent spell resolved; auto-resolve via GRE bridge",
                            ),
                            game_state,
                        )

                # Auto-pass stack triggers with no instant-speed responses
                if trigger in ("stack_spell_yours", "stack_spell_opponent"):
                    if not can_do_anything:
                        logger.info(f"Autopilot: auto-passing {trigger} (no instant responses)")
                        return self._run_bridge_action(
                            GameAction(
                                action_type=ActionType.PASS_PRIORITY,
                                reasoning=f"{trigger}: auto-pass via GRE bridge",
                            ),
                            game_state,
                        )

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
                        # No usable stashed context — submit via GRE bridge, never mouse.
                        logger.info(f"Autopilot: combat step {step} without decision context — submitting via GRE bridge")
                        if trigger == "combat_attackers":
                            return self._run_bridge_action(
                                GameAction(
                                    action_type=ActionType.DECLARE_ATTACKERS,
                                    attacker_names=[],
                                    reasoning=f"{trigger}: no usable combat context",
                                ),
                                game_state,
                            )
                        return self._run_bridge_action(
                            GameAction(
                                action_type=ActionType.DECLARE_BLOCKERS,
                                blocker_assignments={},
                                reasoning=f"{trigger}: no usable combat context",
                            ),
                            game_state,
                        )

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
            # Bridge state id we'll use to detect "the bridge has processed
            # our submit" after execution. If this id hasn't advanced when we
            # try to re-trigger, the post-plan continuation would race against
            # an in-flight ETB / triggered ability and burn a wasted LLM call
            # against stale legal_actions. Captured here, used at line ~1813.
            pre_bridge_state_id = int(game_state.get("_bridge_game_state_id", 0) or 0)

            legal_actions = self._get_legal_actions(game_state)
            legal_actions = self._filter_rolled_back_casts(legal_actions, game_state)
            decision_context = game_state.get("decision_context")

            logger.info(
                f"Autopilot planning: trigger={trigger}, "
                f"legal_actions={len(legal_actions or [])} "
                f"({legal_actions[:3] if legal_actions else []}{'...' if legal_actions and len(legal_actions) > 3 else ''}), "
                f"decision={decision_context.get('type') if decision_context else None}, "
                f"bridge={game_state.get('_bridge_request_type')}"
            )

            # --- STRATEGIC GAME PLAN: (re)form on material change, then inject ---
            # Refresh the persistent game plan before tactical planning so the
            # per-decision prompt is framed by "how we win this game". The
            # manager only calls the LLM on material board changes (and at most
            # once per turn), so this is cheap on most windows. Gate the
            # (potentially blocking) reform to our own turn — we don't want to
            # burn think-time reforming during the opponent's turn — but always
            # inject whatever plan we have.
            if self._game_plan_mgr is not None:
                try:
                    if self._is_local_active_turn(game_state):
                        self._game_plan_mgr.maybe_reform(game_state)
                    self._planner.set_game_plan(self._game_plan_mgr.plan_text())
                    self._announce_game_plan()
                except Exception as e:
                    logger.debug("game-plan refresh skipped: %s", e)

            _plan_started_at = time.perf_counter()
            plan = self._planner.plan_actions(
                game_state, trigger, legal_actions, decision_context
            )
            # Opt-in trajectory capture for real-match data collection. No-op
            # unless a recorder was attached (engine._trajectory_recorder).
            self._maybe_record_trajectory(
                game_state, trigger, legal_actions, decision_context, plan,
                (time.perf_counter() - _plan_started_at) * 1000.0,
            )

            # Surface any newly-built turn plan to the UI immediately so the
            # static panel populates before the first action lands. Safe to
            # call when there's no active plan — the helper short-circuits.
            self._emit_turn_plan_payload()

            # --- SAFE-DEFAULT NET for non-passable interactive requests ---
            # The planner/fallback can only ever emit pass/resolve for many
            # interactive GRE requests (Group bottoming, SelectN, Search,
            # NumericInput, SelectTargets, ...). Those requests do NOT accept a
            # pass, so submitting one livelocks the GRE and the "blocked action
            # repeated" guard then halts the turn forever (observed live with
            # the London-mulligan bottoming GroupRequest). When the plan can't
            # produce a real submission for such a request, submit a typed safe
            # default via the bridge instead so the game always advances. This
            # never touches the ActionsAvailable priority path.
            if (
                self._is_non_passable_interactive(game_state)
                and self._plan_cannot_legally_submit(plan)
            ):
                net = self._try_interactive_safe_default(game_state, trigger)
                if net is not None:
                    self._consecutive_plan_failures = 0
                    self._state = AutopilotState.IDLE
                    if net:
                        return True
                    self._pause_for_manual(
                        "Safe-default submission failed for non-passable request",
                        game_state,
                    )
                    return False

            if not plan.actions:
                self._consecutive_plan_failures += 1
                logger.warning(
                    f"Autopilot: planner returned no actions "
                    f"(consecutive failures: {self._consecutive_plan_failures})"
                )

                # After 2 failures: escalate timeout (×1.5, cap 45s)
                if self._consecutive_plan_failures >= 2:
                    new_timeout = min(
                        self._effective_planning_timeout * 1.5,
                        45.0,
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
                        has_combat_action = any(a.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS) for a in plan.actions)
                        now_combat = "Combat" in fresh_turn.get("phase", "")

                        # Only discard a sorcery-speed plan that got overtaken by
                        # combat. If the plan ALSO includes a combat action
                        # (declare attackers/blockers), moving into combat is
                        # exactly where we want to be — keep the plan; the
                        # executor stale-skips the now-illegal sorcery steps and
                        # submits the combat action.
                        if is_sorcery_play and now_combat and not has_combat_action:
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

            # Legacy mouse path only: bridge-only mode should not steal focus.
            if (
                not self._config.dry_run
                and not self._config.bridge_only_when_connected
                and not self._gre_bridge.connected
            ):
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
                    # Backstop: never dead-loop on a non-passable interactive
                    # request. If the blocked action is a pass/resolve the GRE
                    # won't accept here, submit a typed safe default instead of
                    # halting the turn forever.
                    if (
                        action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                        and self._is_non_passable_interactive(game_state)
                    ):
                        net = self._try_interactive_safe_default(game_state, trigger)
                        if net:
                            self._state = AutopilotState.IDLE
                            return True
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

                # Stale-skip: bridge has moved on (different request type
                # pending, or in a step the planner's action doesn't apply
                # to). The action wasn't actually submitted — break out of
                # the current plan loop and let the next plan cycle pick
                # against the fresh bridge state. Continuing through later
                # plan steps would just stale-skip them all and leave us
                # with a "Plan complete (N actions)" log line for actions
                # nothing actually happened on.
                if click_result.error and "stale-skip" in click_result.error:
                    logger.info(
                        f"Autopilot: stale-skip detected ({click_result.error}); "
                        "invalidating plan and yielding to next cycle"
                    )
                    try:
                        self._planner.invalidate_turn_plan(
                            "bridge moved past plan step (stale-skip)"
                        )
                        self._notify_turn_plan(None)
                    except Exception as e:
                        logger.debug(f"invalidate_turn_plan on stale-skip failed: {e}")
                    self._state = AutopilotState.IDLE
                    return True

                self._actions_executed += 1
                # Clear the persistent-failure counter for this action key
                # so a future failure starts counting from 0 (#231).
                self._reset_persistent_failure(action, game_state)

                # Livelock bookkeeping: count real submissions (not no-ops)
                # toward runaway protection, and remember the last cast so a
                # later rollback (PayCosts cancel / targeting escape) can be
                # attributed to it.
                result_src = click_result.error or ""
                is_real_submission = not any(
                    k in result_src for k in ("stale-skip", "no-op", "intermission")
                )
                if is_real_submission:
                    now_ts = time.monotonic()
                    self._recent_submission_times.append(now_ts)
                    if (
                        len(self._recent_submission_times) >= 15
                        and now_ts - self._recent_submission_times[-15] <= 10.0
                    ):
                        self._runaway_tripped_turn = pre_turn_num
                        self._pause_for_manual(
                            "Runaway protection: 15+ submissions in 10s — "
                            "autopilot standing down until next turn",
                            game_state,
                        )
                        return False
                    if action.action_type == ActionType.CAST_SPELL and action.card_name:
                        self._last_cast_submitted = (
                            pre_turn_num,
                            action.card_name.strip().lower(),
                        )

                # --- 4. VERIFYING ---
                action_verified = True
                if self._config.verify_after_action and pre_state:
                    self._state = AutopilotState.VERIFYING
                    verified = self._verify_action(action, pre_state)
                    if not verified:
                        action_verified = False
                        logger.warning(f"Action verification failed for: {action}")
                        self._notify("AUTOPILOT", "Verification: state unchanged (may be OK)")
                        self._consecutive_failed_verifications += 1

                        if self._consecutive_failed_verifications >= 3:
                            self._recover_stuck()
                    else:
                        self._consecutive_failed_verifications = 0

                # Advance the multi-step turn plan when an action lands.
                # On mismatch, drop the plan and re-emit so the UI shows
                # a "Replanned: ..." note before the panel hides — the
                # next own-turn LLM call will produce a fresh plan.
                if action_verified:
                    try:
                        advanced = self._planner.advance_turn_plan(action)
                    except Exception as e:
                        logger.debug(f"advance_turn_plan failed: {e}")
                        advanced = False
                    if advanced:
                        self._emit_turn_plan_payload()
                    elif self._planner.get_turn_plan_payload() is not None:
                        # We had a plan, the executed action didn't match,
                        # so divergence — invalidate, emit the cleared
                        # state with the reason, then a None to hide.
                        self._planner.invalidate_turn_plan(
                            "executed action diverged from plan"
                        )
                        self._notify_turn_plan(None)

                # Delay between actions
                if i < len(plan.actions) - 1:
                    self._controller.wait(self._config.action_delay, "between actions")

            # Preserve PAUSED state if _pause_for_manual fired mid-plan
            # (e.g. bridge-mismatch on one action). Overwriting PAUSED with
            # IDLE here is what stranded users with a stale current_plan in
            # the UI while the engine reported idle — see #230.
            if self._state != AutopilotState.PAUSED:
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
                # Wait for the bridge to settle: poll until either
                # _bridge_game_state_id advances past the pre-execute id (meaning
                # the bridge processed our submit and any chained ETB request),
                # or a 500ms deadline passes. Without this gate, the continuation
                # races the bridge and re-plans against still-stale legal_actions —
                # producing a 3-4s wasted LLM call that gets discarded when the
                # bridge finally surfaces the new request (e.g. SearchRequest from
                # a fetchland's triggered ability).
                _settle_deadline = time.time() + 0.5
                post_plan_state = self._get_game_state()
                while (
                    int(post_plan_state.get("_bridge_game_state_id", 0) or 0) <= pre_bridge_state_id
                    and time.time() < _settle_deadline
                ):
                    time.sleep(0.05)
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
                    self._release_lock()
                    self._continuation_depth += 1
                    try:
                        self.process_trigger(post_plan_state, "decision_required")
                    finally:
                        self._continuation_depth -= 1
                        # Re-acquire for the outer finally block
                        self._acquire_lock()
                elif should_continue:
                    logger.warning(
                        f"Post-plan: skipping continuation (depth {self._continuation_depth} "
                        f">= max {self._MAX_CONTINUATION_DEPTH})"
                    )
            except Exception as e:
                logger.warning(f"Post-plan follow-up handling failed: {e}")

            return True
        finally:
            # Invariant: state == IDLE implies no in-flight plan. Multiple
            # IDLE transitions throughout process_trigger don't clear the
            # plan reference individually; enforcing the invariant here
            # keeps get_debug_info() honest and prevents the "stale plan
            # visible while engine reports idle" symptom from #230.
            if self._state == AutopilotState.IDLE:
                self._current_plan = None
                self._current_action_idx = 0
            self._release_lock()

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

        AFK mode submits bridge actions for all priority decisions.
        For mandatory choices (mulligan, scry), picks the "safe default":
        - Mulligan: keep hand
        - Scry: scry to bottom
        - Declare Attackers/Blockers: skip (don't attack/block)
        - Choose Play/Draw: choose play
        - All other decisions: submit the equivalent bridge confirmation
        """
        pending = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")

        # Mandatory decisions that need a specific click
        if pending:
            pending_lower = pending.lower() if isinstance(pending, str) else ""

            if "mulligan" in pending_lower:
                logger.info("AFK: keeping hand (mulligan)")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.MULLIGAN_KEEP,
                        reasoning="AFK safe default: keep hand",
                    ),
                    game_state,
                )

            if "scry" in pending_lower:
                logger.info("AFK: scry to bottom")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.SELECT_N,
                        scry_position="bottom",
                        reasoning="AFK safe default: put scry cards on bottom",
                    ),
                    game_state,
                )

            # New decision types from expanded GRE handling
            if dec_type == "declare_attackers":
                logger.info("AFK: skipping attackers")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.DECLARE_ATTACKERS,
                        attacker_names=[],
                        reasoning="AFK safe default: no attacks",
                    ),
                    game_state,
                )

            if dec_type == "declare_blockers":
                logger.info("AFK: skipping blockers")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.DECLARE_BLOCKERS,
                        blocker_assignments={},
                        reasoning="AFK safe default: no blocks",
                    ),
                    game_state,
                )

            if dec_type == "choose_starting_player":
                logger.info("AFK: choosing to play")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CHOOSE_STARTING_PLAYER,
                        play_or_draw="play",
                        reasoning="AFK safe default: choose play",
                    ),
                    game_state,
                )

            if dec_type in (
                "assign_damage", "order_combat_damage", "pay_costs",
                "search", "distribution", "numeric_input",
                "select_replacement", "casting_time_options",
                "select_counters", "order_triggers",
                "select_n_group", "select_from_groups",
                "search_from_groups", "gather",
            ):
                logger.info(f"AFK: auto-accepting decision '{dec_type}'")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"AFK default confirmation for {dec_type}",
                    ),
                    game_state,
                )

            # Unknown decision: try a bridge-side confirmation, never mouse.
            if pending_lower and "mulligan" not in pending_lower and "scry" not in pending_lower:
                logger.warning(f"AFK: unknown decision '{pending}' - trying bridge confirmation")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"AFK unknown decision fallback for {pending}",
                    ),
                    game_state,
                )

        # Everything else: pass priority via bridge.
        logger.info(f"AFK: passing ({trigger})")
        return self._run_bridge_action(
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning=f"AFK auto-pass for {trigger}",
            ),
            game_state,
        )

    def _handle_land_drop(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Handle a trigger in land-drop-only mode.

        Automatically plays one land per turn through the GRE bridge.
        No LLM is used. All other priority passes are handled through the
        bridge so the game keeps moving without mouse clicks.
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
                if self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.PLAY_LAND,
                        card_name=land_name,
                        reasoning="land-drop mode: play first land via GRE bridge",
                    ),
                    game_state,
                ):
                    self._actions_executed += 1
                    self._land_drop_last_turn = turn_number
                    logger.info(f"LAND DROP: {land_name} played successfully")
                    return True
                logger.warning(f"LAND DROP: bridge play failed for {land_name}")

        # For everything else, auto-pass to keep the game moving
        pending = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")

        if pending:
            pending_lower = pending.lower() if isinstance(pending, str) else ""
            if "mulligan" in pending_lower:
                logger.info("LAND DROP: keeping hand (mulligan)")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.MULLIGAN_KEEP,
                        reasoning="land-drop safe default: keep hand",
                    ),
                    game_state,
                )
            if "scry" in pending_lower:
                logger.info("LAND DROP: scry to bottom")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.SELECT_N,
                        scry_position="bottom",
                        reasoning="land-drop safe default: put scry cards on bottom",
                    ),
                    game_state,
                )

            # New decision types: auto-pass combat, auto-accept others
            if dec_type in ("declare_attackers", "declare_blockers"):
                logger.info(f"LAND DROP: skipping {dec_type}")
                if dec_type == "declare_attackers":
                    return self._run_bridge_action(
                        GameAction(
                            action_type=ActionType.DECLARE_ATTACKERS,
                            attacker_names=[],
                            reasoning=f"land-drop mode default for {dec_type}",
                        ),
                        game_state,
                    )
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.DECLARE_BLOCKERS,
                        blocker_assignments={},
                        reasoning=f"land-drop mode default for {dec_type}",
                    ),
                    game_state,
                )

            if dec_type == "choose_starting_player":
                logger.info("LAND DROP: choosing to play")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CHOOSE_STARTING_PLAYER,
                        play_or_draw="play",
                        reasoning="land-drop mode default: choose play",
                    ),
                    game_state,
                )

            if dec_type in (
                "assign_damage", "order_combat_damage", "pay_costs",
                "search", "distribution", "numeric_input",
                "select_replacement", "casting_time_options",
                "select_counters", "order_triggers",
                "select_n_group", "select_from_groups",
                "search_from_groups", "gather",
            ):
                logger.info(f"LAND DROP: auto-accepting decision '{dec_type}'")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"land-drop mode confirmation for {dec_type}",
                    ),
                    game_state,
                )

            # Unknown decision: try bridge confirmation, never mouse.
            if pending_lower and "mulligan" not in pending_lower and "scry" not in pending_lower:
                logger.warning(f"LAND DROP: unknown decision '{pending}' - trying bridge confirmation")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"land-drop unknown decision fallback for {pending}",
                    ),
                    game_state,
                )

        logger.info(f"LAND DROP: passing ({trigger})")
        return self._run_bridge_action(
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning=f"land-drop mode auto-pass for {trigger}",
            ),
            game_state,
        )

    def _get_legal_actions(self, game_state: dict[str, Any]) -> list[str]:
        """Get legal actions from the rules engine."""
        try:
            from arenamcp.rules_engine import RulesEngine
            return RulesEngine.get_legal_actions(game_state)
        except Exception as e:
            logger.error(f"Failed to get legal actions: {e}")
            return []

    def _format_plan_preview(self, plan: ActionPlan) -> str:
        """Format a plan for human-readable preview.

        Hotkey hints (F1/F4/F11) are intentionally not appended here — the
        advice overlay should show only the advice itself. Hotkeys are
        documented in the desktop UI and remain functional regardless.
        """
        lines = [f"PLAN: {plan.overall_strategy}"]
        for i, action in enumerate(plan.actions, 1):
            lines.append(f"  {i}. {action}")
            if action.reasoning:
                lines.append(f"     ({action.reasoning})")
        return "\n".join(lines)

    def _notify(self, label: str, text: str) -> None:
        """Send notification to UI."""
        logger.info(f"[{label}] {text}")
        if self._ui_advice_fn:
            try:
                self._ui_advice_fn(text, label)
            except Exception as e:
                logger.debug(f"UI notification callback failed: {e}")

    def _notify_turn_plan(self, payload: Optional[dict[str, Any]]) -> None:
        """Forward a turn-plan payload to the UI panel callback.

        ``payload`` may be ``None`` to clear/hide the panel (replan/invalidate).
        """
        if self._ui_turn_plan_fn is None:
            return
        try:
            self._ui_turn_plan_fn(payload)
        except Exception as e:
            logger.debug(f"UI turn-plan callback failed: {e}")

    def _emit_turn_plan_payload(self) -> None:
        """Re-emit the planner's current turn-plan payload (or None)."""
        try:
            payload = self._planner.get_turn_plan_payload()
        except Exception as e:
            logger.debug(f"get_turn_plan_payload failed: {e}")
            return
        self._notify_turn_plan(payload)

    def _report_fallback_bug(
        self,
        action: GameAction,
        game_state: dict[str, Any],
        reason_tag: str,
    ) -> None:
        """Immediately dispatch a deduped bridge-miss bug report."""
        if self._bug_report_fn is None:
            return

        # `planner_action_stale` means the planner picked an action that
        # the bridge has already moved past (e.g. user played their land
        # but the planner saw stale legal_actions). The user is still
        # informed via MANUAL REQUIRED, but this is a self-inflicted state
        # mismatch, not a real bridge issue — don't auto-file a bug for it.
        # See issues #136 #137 #139 #140 (the play_land cluster).
        if reason_tag == "planner_action_stale":
            return

        # Intermission is non-actionable by design. Suppress fallback
        # reports so queued end-of-turn passes don't flood GitHub with
        # duplicates when a match ends (see issues #124-127).
        req_type = (game_state.get("_bridge_request_type") or "")
        req_class = (game_state.get("_bridge_request_class") or "")
        if (
            game_state.get("_bridge_in_intermission")
            or game_state.get("match_ended")
            or req_type.startswith("Intermission")
            or req_class.startswith("Intermission")
        ):
            return

        dedupe_key = (reason_tag,) + self._action_block_key(action, game_state)
        if dedupe_key in self._reported_bridge_bug_keys:
            return
        self._reported_bridge_bug_keys.add(dedupe_key)

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
        try:
            threading.Thread(
                target=self._bug_report_fn,
                args=(reason, extra),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"fallback-bug dispatch failed: {e}")

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
        fresh_state: dict[str, Any] = {}

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

        logger.warning("Stuck recovery: bridge-only mode will not click through the UI")
        self._pause_for_manual(
            "Autopilot is stuck and needs a manual bridge-safe recovery step",
            fresh_state or None,
        )
        self._consecutive_failed_verifications = 0

    # --- Action Execution Handlers ---

    # Interactive families served by the typed-decision path. Mulligan is
    # included; ActionsAvailable intentionally is NOT (legacy strategic
    # planning is still richer there — Phase C migrates it).
    _TYPED_DECISION_FAMILIES = frozenset({"SelectTargets", "SelectN", "Search", "Mulligan", "Group"})

    def _try_typed_decision_path(
        self, game_state: dict[str, Any], trigger: str
    ) -> Optional[bool]:
        """Handle interactive requests via the typed PendingDecision pipeline.

        Returns True/False when the path owned the decision (submitted /
        definitively failed), or None to fall through to the legacy path
        (no bridge, unmapped family, no options).
        """
        if self._config.dry_run:
            return None
        if not (self._gre_bridge.connected or self._gre_bridge.connect()):
            return None
        try:
            poll = self._gre_bridge.get_pending_actions() or {}
        except Exception as e:
            logger.debug(f"typed-decision poll failed: {e}")
            return None

        from arenamcp.decisions import build_pending_decision, submit_option
        from arenamcp.request_tracker import decision_fingerprint

        def _resolve_instance(iid: int) -> str:
            for zone in ("hand", "battlefield"):
                for c in game_state.get(zone) or []:
                    if isinstance(c, dict) and int(c.get("instance_id") or 0) == iid:
                        return str(c.get("name") or "")
            return ""

        decision = build_pending_decision(poll, resolve_instance=_resolve_instance)
        # Feed the tracker the current decision (or None) so any in-flight
        # submission settles as ADVANCED/REJECTED before we act.
        fp = decision_fingerprint(decision) if decision else None
        self._request_tracker.observe(fp)
        if decision is None or decision.request_type not in self._TYPED_DECISION_FAMILIES:
            return None
        assert fp is not None

        if not self._request_tracker.may_submit(fp):
            if self._request_tracker.exhausted(fp):
                # Answered MAX times without the game advancing — a human
                # is needed. Declare once (sets the given-up window) and
                # own the trigger so coaching doesn't replan it either.
                try:
                    from arenamcp.stall_corpus import record_stall
                    record_stall(
                        decision,
                        None,
                        "exhausted",
                        {
                            "turn": (game_state.get("turn") or {}).get("turn_number"),
                            "phase": (game_state.get("turn") or {}).get("phase"),
                            "rejections": self._request_tracker.rejections(fp),
                        },
                    )
                except Exception:
                    pass
                self._pause_for_manual(
                    f"{decision.request_type} not accepted after "
                    f"{self._request_tracker.MAX_SUBMISSIONS_PER_REQUEST} "
                    "submissions",
                    game_state,
                )
                return True
            # A submission is in flight — give it time to settle.
            logger.debug(
                "typed-decision: submission in flight for %s; waiting",
                decision.request_type,
            )
            self._state = AutopilotState.IDLE
            return True

        if decision.request_type == "Group":
            # Only take Group windows when the LLM gives a valid pick — the
            # legacy safe-default has a smarter worst-card bottoming ranking
            # than a blind deterministic fallback, so it keeps that job.
            try:
                llm_ids = self._planner._llm_decision_options(decision, game_state)
            except Exception:
                llm_ids = []
            valid = decision.option_ids()
            option_ids = [o for o in llm_ids if o in valid][: decision.max_select]
            if len(option_ids) != decision.min_select:
                return None  # legacy group-default path handles it
        else:
            option_ids = self._planner.plan_decision_options(decision, game_state)
        if not option_ids:
            return None

        labels = [
            (decision.find(oid).label if decision.find(oid) else oid)
            for oid in option_ids
        ]
        if submit_option(self._gre_bridge, decision, option_ids):
            self._request_tracker.note_submitted(fp)
            try:
                from arenamcp.match_packets import get_current_packet
                packet = get_current_packet()
                if packet:
                    packet.add_decision(decision, option_ids)
            except Exception as e:
                logger.warning(f"MatchPacket: failed to record decision: {e}")
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"typed-decision {decision.request_type}: {', '.join(labels)}",
            )
            self._notify(
                "AUTOPILOT",
                f"{decision.request_type}: {', '.join(labels)}",
            )
            self._actions_executed += 1
            self._state = AutopilotState.IDLE
            return True
        try:
            from arenamcp.stall_corpus import record_stall
            record_stall(
                decision,
                option_ids,
                "submit_failed",
                {"turn": (game_state.get("turn") or {}).get("turn_number")},
            )
        except Exception:
            pass
        logger.info(
            "typed-decision submit failed for %s (%s); falling back to legacy path",
            decision.request_type,
            option_ids,
        )
        return None

    def _wait_for_bridge_reconnect(self) -> bool:
        """Briefly wait for the GRE bridge plugin to reconnect.

        In bridge-only mode the bridge is the sole execution path; when it
        drops (MTGA scene transition, Python server restart) the plugin's
        reconnect loop comes back within ~0.2-2s. Waiting here converts a
        transient drop into a successful submit instead of a per-action
        MANUAL REQUIRED cascade.

        Returns True only if the bridge is connected on exit. After an
        unsuccessful wait, further waits are skipped for
        ``bridge_reconnect_wait_cooldown`` seconds so a genuinely dead
        plugin (e.g. BepInEx not injected) doesn't slow every action.
        """
        if self._config.dry_run or not self._config.bridge_only_when_connected:
            return False
        wait_budget = self._config.bridge_reconnect_wait
        if wait_budget <= 0:
            return False
        if getattr(self._gre_bridge, "connected", False):
            return True
        now = time.monotonic()
        if (
            now - self._last_bridge_wait_failed_at
            < self._config.bridge_reconnect_wait_cooldown
        ):
            return False
        self._notify(
            "AUTOPILOT",
            f"Bridge offline — waiting up to {wait_budget:.0f}s for the "
            "plugin to reconnect...",
        )
        deadline = now + wait_budget
        while time.monotonic() < deadline and not self._abort_event.is_set():
            try:
                if self._gre_bridge.connected or self._gre_bridge.connect():
                    logger.info("Bridge reconnected during wait; retrying via bridge")
                    return True
            except Exception as e:
                logger.debug(f"Bridge reconnect attempt failed: {e}")
            time.sleep(0.25)
        self._last_bridge_wait_failed_at = time.monotonic()
        logger.warning(
            "Bridge still offline after %.1fs wait — the MtgaCoachBridge "
            "plugin isn't connecting. If MTGA is running, BepInEx is likely "
            "not injected. On Linux/Proton, Steam launch options must "
            'include: WINEDLLOVERRIDES="winhttp=n,b" %%command%%',
            wait_budget,
        )
        return False

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
            # Snapshot may be stale — re-poll the bridge before bailing.
            # During chained interaction windows (e.g. Eerie/ETB triggers
            # firing right after a target submission) the snapshot can briefly
            # show no pending while the live bridge already has the next
            # request queued.
            try:
                live = self._gre_bridge.get_pending_actions() or {}
            except Exception:
                live = {}
            if not live.get("has_pending"):
                logger.info("GRE bridge execution skipped: bridge is connected but reports no pending window (live re-poll confirmed)")
                return None
            logger.info(
                "GRE bridge stale-snapshot recovery: live poll shows pending "
                f"{live.get('request_class') or live.get('request_type')!r}; proceeding"
            )

        gre_ref = getattr(action, 'gre_action_ref', None)

        # CLICK_BUTTON on an OptionalActionMessageRequest must go through
        # submit_optional, NOT submit_pass — the latter is rejected by MTGA
        # ("Cannot pass on current interaction"). Issue #161 was filed because
        # this branch lumped CLICK_BUTTON with pass/resolve and surfaced
        # bridge_submit_failed when the LLM tried to accept an ETB trigger.
        if action.action_type == ActionType.CLICK_BUTTON:
            bridge_request_class = (
                game_state.get("_bridge_request_class")
                or game_state.get("_bridge_request_type")
                or ""
            )
            decision_type = (
                (game_state.get("decision_context") or {}).get("type") or ""
            )
            if "Optional" in str(bridge_request_class) or decision_type == "optional_action":
                button_name = (action.card_name or "").lower().strip()
                # The LLM occasionally leaves card_name empty when the prompt
                # is a yes/no benefit (e.g. "Search your library?"). Default
                # to accept when the name doesn't explicitly decline — the
                # earlier auto-decline path already handled the "no
                # meaningful actions" case, so reaching here means the LLM
                # actively chose a side.
                if button_name in ("decline", "cancel", "no", "skip"):
                    accept = False
                else:
                    accept = True
                if self._gre_bridge.submit_optional(accept):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"click_button: submit_optional(accept={accept}) via GRE bridge"
                    )
                    return ClickResult(
                        True, 0, 0, button_name or ("accept" if accept else "decline"), "GRE bridge"
                    )
                logger.info("GRE bridge submit_optional failed; surfacing manual-required to caller")
                self._gre_bridge_failed_methods.add(method)
                return None

        # PASS / RESOLVE / generic CLICK_BUTTON — use bridge submit_pass
        if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON):
            if self._gre_bridge.submit_pass():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"{action.action_type.value}: submitted via GRE bridge (pass)"
                )
                return ClickResult(True, 0, 0, "pass", "GRE bridge")
            logger.info("GRE bridge pass failed; surfacing manual-required to caller")
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
            logger.info("GRE bridge mulligan failed, surfacing manual-required to caller")
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
            logger.info("GRE bridge choose_starting_player failed; surfacing manual-required to caller")
            self._gre_bridge_failed_methods.add(method)
            return None

        # SELECT TARGET — submit via bridge if target instance IDs are resolvable
        if action.action_type == ActionType.SELECT_TARGET:
            return self._try_gre_bridge_select_target(action)

        # ASSIGN_DAMAGE — submit combat damage assignments via bridge.
        if action.action_type == ActionType.ASSIGN_DAMAGE:
            result = self._try_gre_bridge_assign_damage(action, game_state)
            if result is not None:
                return result

        # DISTRIBUTE — submit a distribution decision via bridge.
        if action.action_type == ActionType.DISTRIBUTE:
            result = self._try_gre_bridge_distribute(action, game_state)
            if result is not None:
                return result

        # ORDER_TRIGGERS / ORDER_BLOCKERS — submit ordering via bridge.
        # Routed by inspecting the bridge request class: OrderRequest →
        # submit_order; SelectFromGroupsRequest / GroupRequest → submit_group.
        if action.action_type in (ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.ORDER_COMBAT_DAMAGE):
            result = self._try_gre_bridge_order(action, game_state)
            if result is not None:
                return result

        # SELECT_REPLACEMENT — choose a replacement effect (or decline if optional).
        if action.action_type == ActionType.SELECT_REPLACEMENT:
            result = self._try_gre_bridge_select_replacement(action, game_state)
            if result is not None:
                return result

        # SELECT_N / SEARCH_LIBRARY / SELECT_COUNTERS — submit via bridge.
        # The legacy mouse path (kept only when bridge-only mode is off) clicks
        # by list index and
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

            logger.info(f"GRE bridge match failed for {action.action_type.value}; surfacing manual-required to caller")
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
                # Strip any leading "Ability: " / "Cast: " etc. label the LLM
                # may have left on action.card_name (the legal_actions strings
                # use those labels and models occasionally copy them through).
                wanted_name = _normalize_planner_card_name(action.card_name or "")
                for idx, ba in enumerate(bridge_actions):
                    ba_type = ba.get("actionType", "")
                    # Normalize comparison
                    if not (ba_type == gre_type or f"ActionType_{ba_type}" == gre_type or ba_type == gre_type.replace("ActionType_", "")):
                        continue
                    # Verify card identity via grpId → card name lookup
                    if wanted_name:
                        ba_grp_id = ba.get("grpId", 0)
                        if ba_grp_id:
                            try:
                                from arenamcp import server
                                card_info = server.get_card_info(ba_grp_id)
                                ba_name = card_info.get("name", "")
                            except Exception:
                                ba_name = ""
                            if ba_name:
                                w = wanted_name.lower()
                                c = ba_name.lower()
                                # Allow substring-in-either-direction so split
                                # cards / faces (e.g. "Lightning Bolt //
                                # Shock") and shorthand still match.
                                if not (w == c or w in c or c in w):
                                    continue  # Wrong card — skip
                    best_idx = idx
                    break

                # Sole-candidate fallback: the planner's card_name didn't
                # exact-match any bridge action (truncation, split-card face,
                # grpId→name lookup miss), but if exactly ONE bridge action has
                # the wanted GRE type it is unambiguous — submit it rather than
                # dropping the play, which would let _pause_for_manual auto-pass
                # away a castable creature/land (the Spellbook Vendor /
                # Veteran Survivor skip bug). Mirrors the PLAY/CAST sole-candidate
                # branches in gre_action_matcher.match_action_to_gre.
                if best_idx is None:

                    def _type_eq(t: str) -> bool:
                        return (
                            t == gre_type
                            or f"ActionType_{t}" == gre_type
                            or t == gre_type.replace("ActionType_", "")
                        )

                    type_matches = [
                        idx
                        for idx, ba in enumerate(bridge_actions)
                        if _type_eq(ba.get("actionType", ""))
                    ]
                    if len(type_matches) == 1:
                        best_idx = type_matches[0]
                        logger.info(
                            f"GRE bridge sole-candidate: '{action.card_name}' "
                            f"didn't name-match, submitting the only {gre_type} "
                            f"action (idx={best_idx})"
                        )

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

    def _solver_attack_names(self, game_state: dict[str, Any]) -> list[str]:
        """Deterministic attack pick for auto-confirmed DeclareAttackers.

        Used when a DeclareAttackers window is about to be auto-confirmed
        with no planner attack intent. Returns attacker names only when the
        combat solver's best plan is strictly better than not attacking
        (the solver scores the empty plan too); otherwise [] keeps the
        empty-confirm behavior.
        """
        try:
            from arenamcp.combat_solver import optimal_attacks
        except Exception:
            return []

        ctx = game_state.get("decision_context") or {}
        legal_names = {str(n) for n in (ctx.get("legal_attackers") or []) if n}
        if not legal_names:
            return []

        local_seat = next(
            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
            None,
        )
        if local_seat is None:
            return []

        def _is_creature(c: dict) -> bool:
            tl = (c.get("type_line") or "").lower()
            return "creature" in tl or "CardType_Creature" in (c.get("card_types") or [])

        yours: list[dict] = []
        theirs: list[dict] = []
        for c in game_state.get("battlefield", []) or []:
            if not _is_creature(c):
                continue
            if c.get("controller_seat_id") == local_seat:
                yours.append(c)
            elif c.get("controller_seat_id") is not None:
                theirs.append(c)

        candidates = [c for c in yours if (c.get("name") or "") in legal_names]
        if not candidates:
            return []

        your_life, opp_life = 20, 20
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                your_life = p.get("life_total", 20)
            else:
                opp_life = p.get("life_total", 20)

        opp_blockers = [c for c in theirs if not c.get("is_tapped")]
        remaining_blockers = [
            c for c in yours if c not in candidates and not c.get("is_tapped")
        ]
        try:
            plan = optimal_attacks(
                candidates,
                opp_blockers,
                opp_life,
                your_life,
                theirs,
                remaining_blockers,
            )
        except Exception as e:
            logger.debug(f"combat solver attack fallback failed: {e}")
            return []
        if plan is None or not plan.attacker_names:
            return []
        # Conservative gate: only override the empty confirm when the swing
        # actually accomplishes something (damage through or a favorable
        # material trade). A zero-value attack isn't worth the crackback
        # risk the solver might have underestimated.
        if (
            plan.damage_through <= 0
            and plan.blockers_killed_material <= plan.attackers_lost_material
        ):
            return []
        logger.info(
            f"Combat solver attack fallback: {plan.explanation} "
            f"(score={plan.score:.1f})"
        )
        return [n for n in plan.attacker_names if n in legal_names]

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
            # Empty attacker list = "attack with nobody / Done (confirm attackers)".
            # The plugin treats an empty list as a direct SubmitAttackers() finalize.
            # This is correct when the user has no legal attackers (summoning-sick
            # or no creatures) or when auto-confirm fires after the LLM didn't
            # pick any attackers (action.attacker_names was [] from auto-confirm).
            if action.attacker_names:
                logger.warning(
                    "Bridge declare_attackers: requested attackers "
                    f"{action.attacker_names} could not be resolved, surfacing "
                    "manual-required to caller"
                )
                return None
            logger.info("Bridge declare_attackers: confirming with no attackers (Done)")
            resp = self._gre_bridge.submit_attackers_raw([])
            if not resp or not resp.get("ok"):
                logger.warning(f"Bridge declare_attackers (no-attackers confirm) failed: {resp}")
                return None
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                "declare_attackers: confirmed no attackers via GRE bridge",
            )
            return ClickResult(True, 0, 0, "attackers", "GRE bridge")

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

        Resolves blocker/attacker instance IDs from the bridge's own
        DeclareBlockersRequest payload (`blockers` array with
        `blockerInstanceId` and `attackerInstanceIds`). MTGA renumbers
        instances on zone transitions (e.g. token/clone re-IDs), so the
        gamestate snapshot can hold stale IDs by the time we submit —
        which made the plugin's match against `AllBlockers` silently
        fall through to a no-op `SubmitBlockersReq` and stuck the
        autopilot in a Declare-Blockers loop.
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

        bridge_blockers = pending.get("blockers") or []
        if not bridge_blockers:
            logger.info("GRE bridge blockers: bridge reports empty blockers list, falling back")
            return None

        game_state = self._get_game_state()
        battlefield = game_state.get("battlefield", [])

        def _name_of(iid: int) -> str:
            for c in battlefield:
                try:
                    if int(c.get("instance_id") or 0) == iid:
                        return (c.get("name") or "").lower()
                except (TypeError, ValueError):
                    continue
            return ""

        bridge_by_name: dict[str, dict] = {}
        bridge_id_list: list[int] = []
        for b in bridge_blockers:
            try:
                biid = int(b.get("blockerInstanceId") or 0)
            except (TypeError, ValueError):
                continue
            if not biid:
                continue
            bridge_id_list.append(biid)
            n = _name_of(biid)
            if n:
                bridge_by_name[n] = b

        assignments = []
        for blocker_name, attacker_name in action.blocker_assignments.items():
            bn = (blocker_name or "").lower()
            b_entry = bridge_by_name.get(bn)
            if not b_entry:
                for k, v in bridge_by_name.items():
                    if bn and (bn in k or k in bn):
                        b_entry = v
                        break

            if not b_entry:
                logger.warning(
                    f"GRE bridge blockers: can't find blocker {blocker_name!r} "
                    f"among bridge entries (names: {list(bridge_by_name)}, "
                    f"ids: {bridge_id_list}), surfacing manual-required"
                )
                return None

            try:
                blocker_id = int(b_entry["blockerInstanceId"])
            except (TypeError, ValueError, KeyError):
                logger.warning(f"GRE bridge blockers: bad blockerInstanceId in {b_entry}")
                return None

            an = (attacker_name or "").lower()
            attacker_id: Optional[int] = None
            legal_attackers = b_entry.get("attackerInstanceIds") or []
            for aid in legal_attackers:
                try:
                    aid_i = int(aid)
                except (TypeError, ValueError):
                    continue
                cand_name = _name_of(aid_i)
                if cand_name == an or (an and (an in cand_name or cand_name in an)):
                    attacker_id = aid_i
                    break

            if attacker_id is None and len(legal_attackers) == 1:
                try:
                    attacker_id = int(legal_attackers[0])
                    logger.info(
                        f"GRE bridge blockers: attacker name lookup failed for "
                        f"{attacker_name!r}; using sole legal attacker {attacker_id}"
                    )
                except (TypeError, ValueError):
                    attacker_id = None

            if attacker_id is None:
                logger.warning(
                    f"GRE bridge blockers: can't resolve attacker {attacker_name!r} "
                    f"for blocker {blocker_name!r} (legal attacker ids: "
                    f"{legal_attackers}), surfacing manual-required"
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
                f"declare_blockers: {desc} submitted via GRE bridge (bridge-authoritative ids)"
            )
            return ClickResult(True, 0, 0, "declare_blockers", "GRE bridge")

        logger.info("GRE bridge submit_blockers failed, surfacing manual-required to caller")
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
                    "surfacing manual-required to caller"
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

        logger.info("GRE bridge submit_attackers failed, surfacing manual-required to caller")
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

        # Restrict resolution to the bridge's legal-target set when present,
        # so a fuzzy match can't accidentally pick a creature that isn't a
        # valid choice for this particular spell/ability.
        bridge_candidate_ids: set[int] = set()
        for cand in pending.get("target_candidates") or []:
            try:
                iid = int(cand.get("targetInstanceId") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                bridge_candidate_ids.add(iid)

        def _eligible(card: dict[str, Any]) -> bool:
            if not bridge_candidate_ids:
                return True
            try:
                return int(card.get("instance_id") or 0) in bridge_candidate_ids
            except (TypeError, ValueError):
                return False

        # Resolve target name to instance ID — exact first, then fuzzy.
        target_names = action.target_names or ([action.card_name] if action.card_name else [])
        target_id = None
        matched_name = None
        target_id, matched_name = _match_target_in_battlefield(
            target_names, battlefield, _eligible
        )

        # Bridge-only fallback: opponent permanents may not appear in the local
        # battlefield zone, but the bridge ships their grpId in target_candidates.
        # Build synthetic candidate cards from the bridge list and re-match.
        if target_id is None and pending.get("target_candidates"):
            try:
                from arenamcp import server as _server
            except ImportError:
                _server = None
            synthetic: list[dict[str, Any]] = []
            seen_iids: set[int] = set()
            for cand in pending.get("target_candidates") or []:
                try:
                    iid = int(cand.get("targetInstanceId") or 0)
                    grp = int(cand.get("grpId") or 0)
                except (TypeError, ValueError):
                    continue
                if not iid or iid in seen_iids:
                    continue
                seen_iids.add(iid)
                name = ""
                if grp and _server is not None:
                    try:
                        info = _server.enrich_with_oracle_text(grp)
                        name = str(info.get("name") or "")
                    except Exception:
                        name = ""
                synthetic.append({"instance_id": iid, "name": name, "grp_id": grp})
            if synthetic:
                target_id, matched_name = _match_target_in_battlefield(
                    target_names, synthetic, lambda _c: True
                )

        # Last resort: if the bridge reports exactly one legal candidate,
        # use it even when the name lookup failed. Catches common cases
        # like "Target creature you control" with only one creature.
        if target_id is None and len(bridge_candidate_ids) == 1:
            target_id = next(iter(bridge_candidate_ids))
            matched_name = f"<single-candidate id={target_id}>"
            logger.info(
                f"GRE bridge select_target: name lookup failed for {target_names}; "
                f"using sole bridge candidate {target_id}"
            )

        if target_id is None:
            # Log candidate list so bug reports show what was available.
            cand_summary = []
            seen_log_iids: set[int] = set()
            for card in battlefield:
                if _eligible(card):
                    try:
                        iid_log = int(card.get("instance_id") or 0)
                    except (TypeError, ValueError):
                        iid_log = 0
                    if iid_log:
                        seen_log_iids.add(iid_log)
                    cand_summary.append(
                        f"{card.get('name')!r}#{card.get('instance_id')}"
                    )
            # Also surface bridge-only candidates so bug reports show opponent
            # permanents that aren't in the local battlefield zone.
            try:
                from arenamcp import server as _server_log
            except ImportError:
                _server_log = None
            for cand in pending.get("target_candidates") or []:
                try:
                    iid_b = int(cand.get("targetInstanceId") or 0)
                    grp_b = int(cand.get("grpId") or 0)
                except (TypeError, ValueError):
                    continue
                if not iid_b or iid_b in seen_log_iids:
                    continue
                name_b = ""
                if grp_b and _server_log is not None:
                    try:
                        name_b = str(_server_log.enrich_with_oracle_text(grp_b).get("name") or "")
                    except Exception:
                        name_b = ""
                cand_summary.append(f"{name_b!r}#{iid_b}(bridge)")
            logger.info(
                f"GRE bridge select_target: can't resolve ID for {target_names} "
                f"(candidates: [{', '.join(cand_summary) or 'none'}]), falling back"
            )
            return None

        # Use the right bridge method based on request type
        success = False
        if "SelectTargets" in req_class:
            success = self._gre_bridge.submit_targets(target_id)
        else:
            success = self._gre_bridge.submit_selection([target_id])

        if success:
            display = matched_name or ", ".join(target_names)
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"select_target: {display} (id={target_id}) via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_target", "GRE bridge")

        logger.info("GRE bridge select_target failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("select_target")
        return None

    def _try_gre_bridge_assign_damage(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit combat damage assignments via the GRE bridge.

        The planner's GameAction carries `distribution` as a name → damage
        map per attacker, but for AssignDamage we expect a richer structure:
        the LLM ideally emits `target_names` listing the receivers in
        order with `distribution` keyed by receiver name. When only one
        attacker has damage to assign, we accept the simple form and
        treat `distribution` as receiver_name → damage. Otherwise we
        fall back to the bridge's existing assigner template.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "AssignDamage" not in str(req_class):
            return None

        battlefield = game_state.get("battlefield", []) or []

        bridge_assigners = pending.get("assigners") or []
        if not bridge_assigners:
            # Without bridge-side assigner shape we can't safely synthesize a
            # full AssignDamage submission. Surface manual-required.
            logger.info("GRE bridge assign_damage: bridge did not surface assigners; surfacing manual-required")
            return None

        assigners: list[dict[str, Any]] = []
        dist_map = {k.lower(): v for k, v in (action.distribution or {}).items()}
        for assigner in bridge_assigners:
            try:
                attacker_id = int(assigner.get("instanceId") or 0)
                total = int(assigner.get("totalDamage") or 0)
            except (TypeError, ValueError):
                continue
            if not attacker_id or total <= 0:
                continue
            assignments_in = assigner.get("assignments") or []
            built: list[dict[str, int]] = []
            remaining = total
            # If the LLM gave us a per-receiver distribution, use that;
            # otherwise dump everything onto the first legal receiver
            # (typically the defending player or the only blocker).
            if dist_map:
                for entry in assignments_in:
                    try:
                        receiver_id = int(entry.get("instanceId") or 0)
                    except (TypeError, ValueError):
                        continue
                    rname = ""
                    for c in battlefield:
                        try:
                            if int(c.get("instance_id") or 0) == receiver_id:
                                rname = str(c.get("name") or "").lower()
                                break
                        except (TypeError, ValueError):
                            continue
                    dmg = int(dist_map.get(rname, 0))
                    if dmg <= 0:
                        continue
                    built.append({"instanceId": receiver_id, "damage": min(dmg, remaining)})
                    remaining -= dmg
                    if remaining <= 0:
                        break
            if not built:
                # Default: dump all damage on the first listed assignment
                # (bridge-supplied default order matches MTGA's blocker ordering).
                if assignments_in:
                    try:
                        first_id = int(assignments_in[0].get("instanceId") or 0)
                    except (TypeError, ValueError):
                        first_id = 0
                    if first_id:
                        built.append({"instanceId": first_id, "damage": total})
                        remaining = 0
            # Spill any leftover damage to the last assignment slot.
            if remaining > 0 and built:
                built[-1]["damage"] = int(built[-1]["damage"]) + remaining
            assigners.append({"instanceId": attacker_id, "assignments": built})

        if not assigners:
            logger.info("GRE bridge assign_damage: no assignments built; surfacing manual-required")
            return None

        if self._gre_bridge.submit_assign_damage(assigners):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"assign_damage: {len(assigners)} assigners via GRE bridge"
            )
            return ClickResult(True, 0, 0, "assign_damage", "GRE bridge")
        self._gre_bridge_failed_methods.add("assign_damage")
        return None

    def _try_gre_bridge_distribute(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit a Distribution decision via GRE bridge."""
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "Distribution" not in str(req_class):
            return None

        battlefield = game_state.get("battlefield", []) or []

        # The LLM expresses distribution as name → amount. Resolve names
        # to instance_ids before sending. If we can't resolve everything
        # we surface manual-required so vision/manual can take over.
        distributions: dict[int, int] = {}
        for name, amount in (action.distribution or {}).items():
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            iid, _ = _match_target_in_battlefield([name], battlefield, lambda _c: True)
            if not iid:
                logger.info(f"GRE bridge distribute: can't resolve {name!r}; surfacing manual-required")
                return None
            distributions[int(iid)] = amount

        if not distributions:
            return None

        if self._gre_bridge.submit_distribution(distributions):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"distribute: {len(distributions)} targets via GRE bridge"
            )
            return ClickResult(True, 0, 0, "distribute", "GRE bridge")
        self._gre_bridge_failed_methods.add("distribute")
        return None

    def _try_gre_bridge_order(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit ordering (triggers / blockers / combat damage) via GRE bridge.

        Bridge mapping:
          OrderRequest         → submit_order
          SelectFromGroups     → submit_select_from_groups
          GroupRequest         → submit_group (existing)
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        req_class_str = str(req_class)

        # OrderRequest: submit current order (or LLM-provided ordering if
        # ever reified). Most stack-trigger ordering is "default order is
        # fine"; sending the bridge's current Ids list confirms it.
        if "Order" in req_class_str and "Group" not in req_class_str:
            if self._gre_bridge.submit_order():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    "order: default ordering via GRE bridge"
                )
                return ClickResult(True, 0, 0, "order", "GRE bridge")
            self._gre_bridge_failed_methods.add("order")
            return None

        # SelectFromGroupsRequest: submit a single empty group to accept
        # the bridge's current default. The vast majority of in-game
        # SelectFromGroups prompts (e.g. assignment of triggers to stack
        # spots) are "confirm the default" interactions.
        if "SelectFromGroups" in req_class_str:
            if self._gre_bridge.submit_select_from_groups([]):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    "order: select_from_groups default via GRE bridge"
                )
                return ClickResult(True, 0, 0, "order", "GRE bridge")
            self._gre_bridge_failed_methods.add("order")
            return None

        return None

    def _try_gre_bridge_select_replacement(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> Optional[ClickResult]:
        """Submit a SelectReplacement choice via GRE bridge.

        Uses action.modal_index as the replacement index when set,
        otherwise picks index 0. Honors a 'decline' card_name / no-op
        modal as a decline when the request is optional.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "SelectReplacement" not in str(req_class):
            return None

        button_name = (action.card_name or "").lower().strip()
        if button_name in ("decline", "cancel", "no", "skip"):
            if self._gre_bridge.submit_select_replacement(decline=True):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    "select_replacement: declined via GRE bridge"
                )
                return ClickResult(True, 0, 0, "select_replacement", "GRE bridge")
            self._gre_bridge_failed_methods.add("select_replacement")
            return None

        idx = int(getattr(action, "modal_index", 0) or 0)
        if self._gre_bridge.submit_select_replacement(index=idx):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"select_replacement: index {idx} via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_replacement", "GRE bridge")
        self._gre_bridge_failed_methods.add("select_replacement")
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
        #
        # Prefer the explicit fields from the bridge response when they
        # exist (newer plugin builds — see HandleGetPendingActions for
        # SelectNRequest). Fall back to decision_context for older
        # bridges that only emitted the reflected request_payload.
        decision_context = game_state.get("decision_context") or {}
        explicit_ids = pending.get("select_n_ids")
        if isinstance(explicit_ids, list):
            try:
                option_ids = [int(x) for x in explicit_ids]
            except (TypeError, ValueError):
                option_ids = []
        else:
            option_ids = decision_context.get("option_ids") or []
            try:
                option_ids = [int(x) for x in option_ids]
            except (TypeError, ValueError):
                option_ids = []

        # Explicit flag wins; fall back to id_type string parsing + the
        # battlefield-membership heuristic when the bridge didn't tag it.
        explicit_is_instance = pending.get("select_n_is_instance_id")
        if isinstance(explicit_is_instance, bool):
            wants_instance_ids = explicit_is_instance
        else:
            id_type = str(
                pending.get("select_n_id_type")
                or decision_context.get("id_type")
                or ""
            ).strip()
            wants_instance_ids = (
                "InstanceId" in id_type
                or "instance" in id_type.lower()
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

        # Selection size: prefer explicit min/max from the bridge so the
        # match loop doesn't over- or under-collect when decision_context
        # is empty (e.g. fresh CastingTime sub-decision).
        try:
            select_min = int(pending.get("select_n_min") or 0)
        except (TypeError, ValueError):
            select_min = 0
        try:
            select_max = int(pending.get("select_n_max") or 0)
        except (TypeError, ValueError):
            select_max = 0
        if select_max > 0:
            target_count = select_max
        else:
            try:
                target_count = max(1, int(decision_context.get("count") or 1))
            except (TypeError, ValueError):
                target_count = 1

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
                    if len(matched_ids) >= target_count:
                        break
                if len(matched_ids) >= target_count:
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

        # Mandatory-selection fallback: if we couldn't resolve specific cards
        # but the request REQUIRES at least select_min picks (e.g. end-of-turn
        # discard down to 7), an empty/arbitrary submit is rejected and loops
        # forever. Pick select_min candidate ids so the decision completes.
        if not matched_ids and select_min > 0 and option_ids:
            matched_ids = [int(x) for x in list(option_ids)[:select_min]]
            logger.info(
                f"select_n: mandatory min={select_min}, no pick resolved — "
                f"defaulting to first {len(matched_ids)} candidate(s) {matched_ids}"
            )

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

        logger.info("GRE bridge select_n failed, surfacing manual-required to caller")
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

        logger.info("GRE bridge scry failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("scry")
        return None

    # ------------------------------------------------------------------
    # Safe-default net for non-passable interactive GRE requests
    # ------------------------------------------------------------------
    #
    # Many interactive requests (Group bottoming, SelectN, Search,
    # NumericInput, SelectTargets, ...) do NOT accept a pass. When the
    # planner/fallback can only produce pass/resolve for one of these, the
    # plugin rejects the pass, the "blocked action repeated" guard fires, and
    # the autopilot dead-loops on the opening interaction (observed live with
    # the London-mulligan bottoming GroupRequest). These helpers submit a
    # *legal* typed default via the bridge so the GRE always advances.

    @staticmethod
    def _plan_cannot_legally_submit(plan: Optional[ActionPlan]) -> bool:
        """True when the plan can't produce a real (non-pass) submission."""
        if not plan or not plan.actions:
            return True
        passive = {ActionType.PASS_PRIORITY, ActionType.RESOLVE}
        return all(a.action_type in passive for a in plan.actions)

    def _is_non_passable_interactive(self, game_state: dict[str, Any]) -> bool:
        """True when the pending bridge request is interactive and rejects pass.

        Excludes the ActionsAvailable priority window (the normal
        play-land/cast/attack/pass path) and the dedicated Mulligan keep/mull
        request, both of which have their own correct handling.
        """
        btype = str(game_state.get("_bridge_request_type") or "")
        bclass = str(game_state.get("_bridge_request_class") or "")
        if not btype and not bclass:
            return False
        if "ActionsAvailable" in btype or "ActionsAvailable" in bclass:
            return False
        # The Mulligan keep/mull decision (request type "Mulligan") is its own
        # critical path. Note the post-keep London bottoming step is a separate
        # GroupRequest (type "Group", context "LondonMulligan"), which IS
        # handled by the net below.
        if btype in ("Mulligan", "MulliganReq", "MulliganRequest") or bclass == "MulliganRequest":
            return False
        if game_state.get("_bridge_can_pass"):
            return False
        return True

    def _try_interactive_safe_default(
        self, game_state: dict[str, Any], trigger: str
    ) -> Optional[bool]:
        """Submit a typed safe default for a non-passable interactive request.

        Returns True if a legal default was submitted, False if every attempt
        (including MTGA's own AutoRespond) failed, or None if the request
        isn't actionable here (dry-run, bridge offline, or nothing pending).
        """
        if self._config.dry_run:
            return None
        if not (self._gre_bridge.connected or self._gre_bridge.connect()):
            return None
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        dec_type = self._decision_type(game_state)
        btype = str(game_state.get("_bridge_request_type") or pending.get("request_type") or "")
        bclass = str(game_state.get("_bridge_request_class") or pending.get("request_class") or "")
        label = btype or bclass or dec_type or "interactive"

        def _ok(detail: str) -> bool:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"{label}: safe-default submission ({detail})"
            )
            self._record_autopilot_decision(
                game_state, trigger,
                action_type="safe_default",
                summary=f"{label}: {detail}",
            )
            return True

        # Group: London-mulligan bottoming / scry-surveil / ordering default.
        if "Group" in btype or "Group" in bclass or dec_type == "group_selection":
            res = self._try_gre_bridge_group_default(game_state, pending)
            if res is not None and res.success:
                return _ok("group default")

        # SelectN / Search: submit the resolvable selection, else SubmitArbitrary.
        if (
            dec_type in ("select_n", "selection_generic", "search")
            or "SelectN" in btype or "SelectN" in bclass
            or "Search" in btype or "Search" in bclass
        ):
            res = self._try_gre_bridge_select_n(
                GameAction(action_type=ActionType.SELECT_N), game_state
            )
            if res is not None and res.success:
                return _ok("select_n/search min-or-arbitrary")
            if self._gre_bridge.submit_selection([]):
                return _ok("empty selection (SubmitArbitrary)")

        # NumericInput: min (or first suggested) legal value.
        if dec_type == "numeric_input" or "Numeric" in btype or "Numeric" in bclass:
            value = self._safe_default_numeric(pending)
            if self._gre_bridge.submit_numeric(value):
                return _ok(f"numeric={value}")

        # SelectTargets: first legal candidate.
        if dec_type == "target_selection" or "SelectTargets" in btype or "SelectTargets" in bclass:
            tid = self._first_target_candidate(pending)
            if tid is not None and self._gre_bridge.submit_targets(tid):
                return _ok(f"first target {tid}")

        # SelectReplacement: first replacement.
        if dec_type == "select_replacement" or "SelectReplacement" in btype or "SelectReplacement" in bclass:
            if self._gre_bridge.submit_select_replacement(index=0):
                return _ok("replacement index 0")

        # Ordering / SelectFromGroups: accept the given default order.
        if dec_type in ("order_triggers", "order_combat_damage", "select_from_groups"):
            if self._gre_bridge.submit_order():
                return _ok("default order")
            if self._gre_bridge.submit_select_from_groups([]):
                return _ok("select_from_groups default")

        # Universal fallback: MTGA's own "do the default" for this request.
        if self._gre_bridge.auto_respond():
            return _ok("auto_respond")
        return False

    @staticmethod
    def _safe_default_numeric(pending: dict[str, Any]) -> int:
        """Pick a safe legal value for a NumericInputRequest (suggested|min)."""
        disallowed = set()
        for v in (pending.get("numeric_disallowed") or []):
            try:
                disallowed.add(int(v))
            except (TypeError, ValueError):
                continue
        for v in (pending.get("numeric_suggested") or []):
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv not in disallowed:
                return iv
        try:
            lo = int(pending.get("numeric_min") or 0)
        except (TypeError, ValueError):
            lo = 0
        try:
            hi = int(pending.get("numeric_max") or lo)
        except (TypeError, ValueError):
            hi = lo
        v = lo
        while v in disallowed and v < hi:
            v += 1
        return v

    @staticmethod
    def _first_target_candidate(pending: dict[str, Any]) -> Optional[int]:
        """Return the first legal target instance_id, if any."""
        for c in (pending.get("target_candidates") or []):
            if not isinstance(c, dict):
                continue
            try:
                iid = int(c.get("targetInstanceId") or c.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                return iid
        return None

    def _try_gre_bridge_group_default(
        self,
        game_state: dict[str, Any],
        pending: Optional[dict[str, Any]] = None,
    ) -> Optional[ClickResult]:
        """Submit a safe-default GroupRequest response via the bridge.

        Two cases, matching MTGA's own LondonWorkflow / ScryWorkflow shapes:
          - London mulligan bottoming: put the worst N cards on the bottom of
            the library, keep the rest in hand. N = GroupSpecs[bottom].LowerBound
            (the slot the client requires us to fill).
          - Any other ordering Group (scry / surveil / trigger ordering): accept
            the cards in the order/zones already presented (nothing to bottom).

        Returns a ClickResult (success flag set), or None if not a GroupRequest.
        """
        pending = pending or self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        btype = str(pending.get("request_type") or "")
        bclass = str(pending.get("request_class") or "")
        if "Group" not in btype and "Group" not in bclass:
            return None

        payload = pending.get("request_payload") or {}
        raw_ids = (
            pending.get("group_instance_ids")
            or payload.get("instanceIds")
            or (game_state.get("decision_context") or {}).get("instanceIds")
            or []
        )
        instance_ids: list[int] = []
        for v in raw_ids:
            try:
                instance_ids.append(int(v))
            except (TypeError, ValueError):
                continue

        specs = pending.get("group_specs") or payload.get("groupSpecs") or []
        context = str(pending.get("group_context") or payload.get("context") or "")

        def _spec_bound(spec: dict[str, Any]) -> int:
            for key in ("lowerBound", "upperBound", "lower_bound", "upper_bound"):
                try:
                    b = int(spec.get(key) or 0)
                except (TypeError, ValueError):
                    b = 0
                if b > 0:
                    return b
            return 0

        def _is_bottom_spec(spec: dict[str, Any]) -> bool:
            zone = str(spec.get("zoneType") or spec.get("zone") or "")
            sub = str(spec.get("subZoneType") or spec.get("subZone") or "")
            return "Bottom" in sub or "Library" in zone

        # Determine how many cards must go to the bottom. Prefer the bottom
        # spec's bound (LondonWorkflow reads GroupSpecs[1].LowerBound); fall
        # back to hand_size - 7 for a London mulligan when specs are opaque.
        bottom_count = 0
        for spec in specs:
            if isinstance(spec, dict) and _is_bottom_spec(spec):
                bottom_count += _spec_bound(spec)
        if bottom_count <= 0 and "LondonMulligan" in context:
            bottom_count = max(0, len(instance_ids) - 7)
        bottom_count = max(0, min(bottom_count, len(instance_ids)))

        if not instance_ids or bottom_count <= 0:
            # Nothing to bottom: accept the default order. Put every card in the
            # first (top/keep) group, mirroring the request's first spec zone.
            top_zone, top_sub = "Hand", "Top"
            if specs and isinstance(specs[0], dict):
                top_zone = str(specs[0].get("zoneType") or specs[0].get("zone") or top_zone)
                top_sub = str(specs[0].get("subZoneType") or specs[0].get("subZone") or top_sub)
            groups = [{"ids": list(instance_ids), "zone": top_zone, "sub_zone": top_sub}]
            for spec in specs[1:]:
                z = str(spec.get("zoneType") or spec.get("zone") or "") if isinstance(spec, dict) else ""
                s = str(spec.get("subZoneType") or spec.get("subZone") or "") if isinstance(spec, dict) else ""
                groups.append({"ids": [], "zone": z or None, "sub_zone": s or None})
            ok = self._gre_bridge.submit_group(groups)
            if ok:
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"group: default order ({len(instance_ids)} cards, ctx={context or '?'}) via GRE bridge",
                )
                return ClickResult(True, 0, 0, "group", "GRE bridge")
            self._gre_bridge_failed_methods.add("group")
            return ClickResult(False, 0, 0, "group", "GRE bridge")

        # Bottom the worst N cards; keep the rest in hand. Response shape mirrors
        # MTGA's LondonWorkflow: [Hand/Top keep group, Library/Bottom group].
        worst_first = self._rank_cards_for_bottoming(instance_ids, game_state)
        bottom_ids = worst_first[:bottom_count]
        keep_ids = [iid for iid in instance_ids if iid not in bottom_ids]
        groups = [
            {"ids": keep_ids, "zone": "Hand", "sub_zone": "Top"},
            {"ids": bottom_ids, "zone": "Library", "sub_zone": "Bottom"},
        ]
        ok = self._gre_bridge.submit_group(groups)
        if ok:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"group: bottom {len(bottom_ids)} keep {len(keep_ids)} "
                f"(ctx={context or '?'}) via GRE bridge",
            )
            return ClickResult(True, 0, 0, "group", "GRE bridge")
        self._gre_bridge_failed_methods.add("group")
        return ClickResult(False, 0, 0, "group", "GRE bridge")

    def _rank_cards_for_bottoming(
        self, instance_ids: list[int], game_state: dict[str, Any]
    ) -> list[int]:
        """Order instance_ids worst-keep first (best candidates to bottom).

        Heuristic: bottom excess lands first (keep ~4), then highest-cmc
        spells, keeping a low land+cheap-spell base. Cards with no resolvable
        info fall to the end of their bucket — at minimum we still return a
        valid permutation so a default selection is always available.
        """
        info = {iid: self._lookup_card_for_bottoming(iid, game_state) for iid in instance_ids}
        lands = [iid for iid in instance_ids if info[iid] and info[iid]["is_land"]]
        nonlands = [iid for iid in instance_ids if iid not in lands]
        keep_lands = min(len(lands), 4)
        excess_lands = lands[keep_lands:]
        kept_lands = lands[:keep_lands]
        nonlands_sorted = sorted(
            nonlands,
            key=lambda i: -(info[i]["cmc"] if info[i] else 0),
        )
        # Worst first: extra lands, then most expensive spells, then the cheap
        # spells + the lands we'd rather keep (least likely to be bottomed).
        return excess_lands + nonlands_sorted + kept_lands

    def _lookup_card_for_bottoming(
        self, instance_id: int, game_state: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Resolve an instance_id to {name, is_land, cmc} from visible zones."""
        for zone_key in ("hand", "library_top_revealed", "stack"):
            for c in (game_state.get(zone_key) or []):
                if not isinstance(c, dict):
                    continue
                try:
                    iid = int(c.get("instance_id") or 0)
                except (TypeError, ValueError):
                    continue
                if iid != instance_id:
                    continue
                type_line = str(c.get("type_line") or "")
                mana_cost = str(c.get("mana_cost") or "")
                return {
                    "name": str(c.get("name") or ""),
                    "is_land": "land" in type_line.lower(),
                    "cmc": self._parse_mana_value(mana_cost),
                }
        return None

    @staticmethod
    def _parse_mana_value(mana_cost: str) -> int:
        """Convert a mana-cost string like '{2}{G}{G}' to a CMC integer."""
        if not mana_cost:
            return 0
        total = 0
        for sym in re.findall(r"\{([^}]+)\}", mana_cost):
            s = sym.strip().upper()
            if s.isdigit():
                total += int(s)
            elif s in ("X", "Y", "Z"):
                total += 0
            else:
                total += 1
        return total

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
        # Match just ended — the bridge is in Intermission and no action
        # (including pass_priority) is legal. Queued actions from the last
        # priority window would otherwise fail against IntermissionRequest
        # and produce duplicate `bridge_only_suppressed` fallback bug
        # reports (see issues #124-127). Treat it as a silent no-op.
        if (
            game_state.get("_bridge_in_intermission")
            or game_state.get("match_ended")
        ):
            logger.info(
                f"Autopilot: skipping {action.action_type.value} — "
                "bridge in intermission"
            )
            return ClickResult(True, 0, 0, action.action_type.value, "intermission_noop")

        # Try GRE bridge first (direct action submission, no mouse needed)
        if not self._config.dry_run:
            gre_result = self._try_gre_bridge(action, game_state)
            if (
                gre_result is None
                and not getattr(self._gre_bridge, "connected", False)
                and self._wait_for_bridge_reconnect()
            ):
                # Bridge came back mid-window — retry the submission instead
                # of cascading into MANUAL REQUIRED (live failure 2026-06-07:
                # every action in a match died "Bridge offline" because the
                # executor never gave the plugin's reconnect loop a chance).
                gre_result = self._try_gre_bridge(action, game_state)
            if gre_result is not None:
                return gre_result

        bridge_connected = (
            self._gre_bridge is not None
            and getattr(self._gre_bridge, "connected", False)
        )
        if (
            self._config.bridge_only_when_connected
            and not self._config.dry_run
        ):
            if bridge_connected:
                # Distinguish "planner picked an action the bridge has already
                # moved past" (e.g. user already played a land this turn, but
                # the planner saw stale legal_actions) from a real bridge
                # failure. We still surface MANUAL REQUIRED in both cases —
                # the user needs to take over — but the stale-state path
                # is self-inflicted and shouldn't auto-file a bug report
                # (see issues #136 #137 #139 #140 — the cluster of
                # `bridge_submit_failed` for play_land where the bridge
                # simply has no Play action because lands_played != 0).
                if self._is_planner_action_stale_vs_bridge(action, game_state):
                    # Silent-skip cases — the bridge will surface the right
                    # request shortly and the next plan cycle will pick
                    # correctly. Pausing for manual input here is wrong:
                    # the user can't act on a step that hasn't started yet
                    # (combat) or one that's been displaced by an
                    # in-resolution decision window (SelectN/Search/etc).
                    bridge_type = str(game_state.get("_bridge_request_type") or "")
                    bridge_class = str(game_state.get("_bridge_request_class") or "")
                    bridge_is_actions_available = (
                        bridge_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                        or bridge_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                    )
                    bridge_has_other_request = bool(
                        (bridge_type or bridge_class) and not bridge_is_actions_available
                    )

                    is_combat_stale = action.action_type in (
                        ActionType.DECLARE_ATTACKERS,
                        ActionType.DECLARE_BLOCKERS,
                    )
                    is_displaced_main_action = (
                        action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL)
                        and bridge_has_other_request
                    )
                    is_displaced_select = (
                        action.action_type
                        in (
                            ActionType.SELECT_N,
                            ActionType.SEARCH_LIBRARY,
                            ActionType.SELECT_COUNTERS,
                        )
                    )
                    is_displaced_pass = (
                        action.action_type
                        in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                        and bridge_has_other_request
                    )

                    # Combat livelock fix: when we want to declare attackers/
                    # blockers but the bridge is still at a precombat
                    # ActionsAvailableRequest, a no-op skip livelocks — the next
                    # plan re-issues the same combat action and we never advance.
                    # Pass priority to move the game into the combat step, where
                    # the bridge presents the DeclareAttacker/Blocker request and
                    # we can actually attack/block.
                    if (
                        is_combat_stale
                        and bridge_is_actions_available
                        and bool(game_state.get("_bridge_can_pass"))
                        and self._gre_bridge is not None
                    ):
                        try:
                            if self._gre_bridge.submit_pass():
                                self._log_execution_path(
                                    ExecutionPath.GRE_AWARE,
                                    f"{action.action_type.value}: not in combat "
                                    "step — passing priority to advance toward "
                                    "combat",
                                )
                                return ClickResult(
                                    True, 0, 0, "pass_priority",
                                    "GRE bridge (advance-to-combat)",
                                )
                        except Exception as e:
                            logger.debug(f"advance-to-combat pass failed: {e}")

                    if (
                        is_combat_stale
                        or is_displaced_main_action
                        or is_displaced_select
                        or is_displaced_pass
                    ):
                        if is_combat_stale:
                            reason = "bridge not in combat step yet"
                        elif is_displaced_main_action:
                            reason = f"bridge moved to {bridge_type or bridge_class}"
                        elif is_displaced_pass:
                            reason = (
                                f"window is now {bridge_type or bridge_class} — "
                                "pass not applicable"
                            )
                        else:
                            reason = (
                                f"bridge has no SelectN/Search pending "
                                f"(now: {bridge_type or bridge_class or 'nothing'})"
                            )
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: {reason} — skipping (will re-plan)",
                        )
                        return ClickResult(
                            True, 0, 0, action.action_type.value, "GRE bridge (stale-skip)"
                        )

                    # Real Shape 1: bridge IS ActionsAvailable but doesn't
                    # offer the specific Play/Cast we wanted. The user
                    # genuinely needs to take over (e.g. they already played
                    # their land for the turn).
                    msg = (
                        f"Game advanced past {action.action_type.value} "
                        f"({action.card_name or '?'}) — bridge no longer "
                        "offers this action. Take it manually if still needed."
                    )
                    return self._manual_required_bridge_result(
                        action,
                        game_state,
                        "planner_action_stale",
                        msg,
                    )

                # Pass/resolve failures are overwhelmingly races: the window
                # we planned against closed or was replaced while the LLM was
                # thinking. Classify against a LIVE poll, not the planning
                # snapshot — the snapshot routinely still says
                # ActionsAvailable when the bridge has already moved on
                # (observed live 2026-06-10: repeated "failure N/5:
                # pass_priority" during opponent-turn window churn).
                if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE):
                    try:
                        live = self._gre_bridge.get_pending_actions() or {}
                    except Exception:
                        live = {}
                    live_type = str(live.get("request_type") or "")
                    live_class = str(live.get("request_class") or "")
                    if not live.get("has_pending"):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: window already closed "
                            "(live poll) — no-op",
                        )
                        return ClickResult(
                            True, 0, 0, action.action_type.value,
                            "GRE bridge (no-op, window closed)",
                        )
                    if not (
                        live_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                        or live_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                    ):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: window is now "
                            f"{live_class or live_type} — skipping (will re-plan)",
                        )
                        return ClickResult(
                            True, 0, 0, action.action_type.value,
                            "GRE bridge (stale-skip)",
                        )
                    # Live window IS ActionsAvailable and pass still failed —
                    # fall through to the genuine manual-required path.

                # Pattern A: pass_priority + nothing pending = no-op success.
                # MTGA already cleared the priority window we wanted to pass on
                # (race between plan execution and decision clearing). Logging
                # a "manual required" here is misleading — there's literally
                # nothing for the user to do. Treat as benign success.
                bridge_pending_anything = bool(
                    game_state.get("_bridge_request_type")
                    or game_state.get("_bridge_request_class")
                    or game_state.get("_bridge_has_pending")
                )
                if (
                    action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                    and not bridge_pending_anything
                ):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"{action.action_type.value}: bridge has nothing pending — no-op",
                    )
                    return ClickResult(True, 0, 0, action.action_type.value, "GRE bridge (no-op)")

                # Pattern B: select_target with no card_name. If the bridge
                # has SelectTargets pending and exactly one legal candidate,
                # submit it as a safety net (the planner failed to specify
                # which target, but there's only one valid choice). Otherwise
                # fall through to manual required — multi-candidate selection
                # without a name is a real planner gap that needs a human.
                if (
                    action.action_type == ActionType.SELECT_TARGET
                    and not (action.card_name or "").strip()
                ):
                    bridge_class = str(game_state.get("_bridge_request_class") or "")
                    bridge_type = str(game_state.get("_bridge_request_type") or "")
                    if "SelectTargets" in bridge_class or "SelectTargets" in bridge_type:
                        only_id = self._pick_single_target_candidate(game_state)
                        if only_id is not None and self._gre_bridge.submit_targets(only_id):
                            self._log_execution_path(
                                ExecutionPath.GRE_AWARE,
                                f"select_target (no name): auto-picked sole candidate {only_id}",
                            )
                            return ClickResult(True, 0, 0, "select_target", "GRE bridge")

                msg = (
                    f"Bridge couldn't handle {action.action_type.value} "
                    f"({action.card_name or '?'}) — take this action manually."
                )
                return self._manual_required_bridge_result(
                    action,
                    game_state,
                    "bridge_submit_failed",
                    msg,
                )

            msg = (
                f"GRE bridge is unavailable for {action.action_type.value} "
                f"({action.card_name or '?'}) — take this action manually."
            )
            return self._manual_required_bridge_result(
                action,
                game_state,
                "bridge_unavailable",
                msg,
            )

        # Legacy mouse fallback path retained only when bridge-only mode is off.
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

                if action.action_type == ActionType.DECLARE_BLOCKERS:
                    # Blocks don't change zone or phase — combat damage is the
                    # same step as Declare Blockers. The reliable signal is
                    # the bridge's pending request moving off DeclareBlockers
                    # (next step is usually ActionsAvailable for second main /
                    # combat damage triggers, or the phase advancing).
                    pre_bridge_class = (pre_state.get("_bridge_request_class") or "")
                    post_bridge_class = (post_state.get("_bridge_request_class") or "")
                    if (
                        "DeclareBlockers" in pre_bridge_class
                        and "DeclareBlockers" not in post_bridge_class
                    ):
                        logger.info(
                            f"Action verified: bridge moved off DeclareBlockers "
                            f"({pre_bridge_class!r} -> {post_bridge_class!r})"
                        )
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
