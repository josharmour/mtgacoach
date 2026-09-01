"""Autopilot Mode - Core Orchestration Engine.

Ties ActionPlanner + ScreenMapper + InputController together with
human-in-the-loop confirmation gates (spacebar to confirm, escape to skip).

The autopilot layers onto the existing coaching loop without replacing it:

    GameState polling → Triggers → ActionPlanner.plan_actions() → Preview
    → [SPACEBAR confirm] → InputController.execute() → Verify state → Loop
"""

import contextlib
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.autopilot_exec import _ActionExecMixin
from arenamcp.autopilot_modes import _AutopilotModesMixin
from arenamcp.autopilot_telemetry import _AutopilotTelemetryMixin
from arenamcp.autopilot_models import AutopilotConfig, AutopilotState, ExecutionPath
from arenamcp.autopilot_targets import _normalize_planner_card_name
from arenamcp.gre_bridge import (
    _ACTIONS_AVAILABLE_BRIDGE_REQUESTS,
    UNMAPPED_INTERACTION_TYPE,
    GREBridge,
    enrich_snapshot_from_pending_response,
    get_bridge,
)
from arenamcp.input_controller import ClickResult, InputController
from arenamcp.screen_mapper import ScreenCoord, ScreenMapper

logger = logging.getLogger(__name__)


class AutopilotEngine(
    _BridgeSubmitMixin,
    _ActionExecMixin,
    _AutopilotTelemetryMixin,
    _AutopilotModesMixin,
):
    """Core autopilot orchestration engine.

    Coordinates action planning, screen mapping, input control, and
    human confirmation to execute MTGA actions automatically.
    """

    _MAX_CONTINUATION_DEPTH: int = 5
    _CRITICAL_DECISION_TYPES: frozenset[str] = frozenset(
        {
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
        }
    )

    def __init__(
        self,
        planner: ActionPlanner,
        mapper: ScreenMapper,
        controller: InputController,
        get_game_state: Callable[[], dict[str, Any]],
        config: AutopilotConfig | None = None,
        speak_fn: Callable[[str, bool], None] | None = None,
        ui_advice_fn: Callable[[str, str], None] | None = None,
        bug_report_fn: Callable[[str, dict], None] | None = None,
        ui_turn_plan_fn: Callable[[dict[str, Any] | None], None] | None = None,
        ui_game_plan_fn: Callable[[dict[str, Any] | None], None] | None = None,
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
            ui_game_plan_fn: Optional UI callback (payload-or-None) for the
                strategic game-plan card. Receives GamePlan.as_payload()
                whenever the persistent plan changes. Wholesale-replace.
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
        self._ui_game_plan_fn = ui_game_plan_fn
        self._last_emitted_game_plan: dict[str, Any] | None = None
        # Optional callback to record autopilot-driven decisions into the
        # app's advice_history. Set by standalone after construction.
        self._advice_recorder: Any | None = None
        # Optional TrajectoryRecorder for real-match data collection. When set
        # (by play_real_matches), each planning decision is logged in the
        # self-play JSONL format. None by default => zero overhead.
        self._trajectory_recorder: Any | None = None
        # Buffer of fallback bug events collected during the current match.
        # On match end, we sample up to `_max_fallback_bugs_per_match` at
        # random and dispatch those. Rest are discarded — goal is
        # representative telemetry without spam.
        self._pending_fallback_bugs: list[tuple[str, dict]] = []
        # P1-8: recent takeover records (by-reference extras) awaiting
        # possible self_recovered_replan reclassification.
        self._recent_takeovers: list[dict[str, Any]] = []
        # P1-4: manual-play detection state.
        self._last_seen_own_stack: set[str] = set()
        self._manual_play_cooldown_until: float = 0.0
        self._recent_bot_submissions: list[tuple[float, str]] = []
        self._max_fallback_bugs_per_match: int = 5

        # State
        self._state = AutopilotState.IDLE
        self._current_plan = None
        self._current_action_idx = 0
        self._lock = threading.Lock()
        # Track which thread owns _lock so toggle_autopilot can distinguish
        # a stuck lock (owner thread dead/gone) from a live one before
        # force-releasing. Force-releasing a live owner's lock corrupts state.
        self._lock_owner_thread_id: int | None = None

        # Confirmation events
        self._confirm_event = threading.Event()
        self._skip_event = threading.Event()
        self._abort_event = threading.Event()
        # R2: game-plan reform runs off the critical path; this guards
        # against stacking concurrent reform threads.
        self._game_plan_reform_inflight = threading.Event()
        # P2-3: (window signature, advice text, ts) of the last computed
        # plan, for coach fall-through reuse.
        self._last_plan_advice: tuple[Any, str, float] | None = None

        # Statistics
        self._actions_executed = 0
        # Wall-clock of the last successful execution; used to tell "user
        # took over" apart from "our own previous plan already advanced the
        # state" when a redundant overlapping plan comes back stale.
        self._last_exec_success_ts: float = 0.0
        self._actions_skipped = 0
        self._plans_completed = 0
        self._consecutive_failed_verifications = 0

        # Land-drop dedup: track last turn we played a land to prevent
        # double-triggers when game state hasn't updated yet
        self._land_drop_last_turn: int = -1

        # Vision scan: track if mapper supports layout scanning
        self._has_vision_scan = hasattr(self._mapper, "scan_layout")

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
        # Game-wide rollback totals by card name: a cast that keeps rolling
        # back on DIFFERENT turns is the same livelock stretched out (live
        # 2026-07-01: Patriar's Humiliation wedged at targeting, rolled back
        # on the timer, and was re-picked the next turn — the per-turn key
        # above reset each time).
        self._cast_rollback_totals: dict[str, int] = {}
        self._last_cast_submitted: tuple[int, str] | None = None
        self._last_cast_submitted_ts: float = 0.0
        self._max_seen_turn: int = 0
        self._window_first_seen_at: float = 0.0
        self._given_up_window_sig: tuple[Any, ...] | None = None
        self._recent_submission_times: deque = deque(maxlen=32)
        # Per-request submission FSM (fable Phase C) — content-addressed
        # request identity, one in-flight submission per request.
        from arenamcp.request_tracker import RequestTracker

        self._request_tracker = RequestTracker()
        self._runaway_tripped_turn: int | None = None
        self._escape_budget_turn: int = -1
        self._escape_count_this_turn: int = 0
        self._bridge_preloaded_actions: list[dict[str, Any]] | None = None

        # Persistent strategic GAME PLAN layer (win conditions + path), reformed
        # only on material board changes and threaded into the planner's prompt
        # so the autopilot develops toward a win instead of reacting per-window.
        try:
            from arenamcp.game_plan import GamePlanManager

            self._game_plan_mgr: Any | None = GamePlanManager(self._planner._backend)
        except Exception as e:  # never block construction on the strategic layer
            logger.debug("GamePlanManager unavailable: %s", e)
            self._game_plan_mgr = None

        # Execution path tracking
        self._path_stats: dict[str, int] = {}

        # Consecutive planning failure tracking (timeout/empty plan escalation)
        self._consecutive_plan_failures: int = 0
        self._effective_planning_timeout: float = self._config.planning_timeout

        # Stashed combat decision context (survives across triggers)
        self._last_combat_context: dict[str, Any] | None = None
        self._last_combat_context_time: float = 0.0
        self._last_combat_context_turn: int = -1

        # Post-plan continuation depth (prevents runaway recursion)
        self._continuation_depth: int = 0

        # Retry suppression for actions that failed to advance the GRE state
        self._blocked_action_keys: set[tuple[Any, ...]] = set()
        self._blocked_action_window_sig: tuple[Any, ...] | None = None
        # Bug-report dedup for repeated failures in the same priority window.
        self._reported_bridge_bug_keys: set[tuple[Any, ...]] = set()
        self._reported_bridge_bug_window_sig: tuple[Any, ...] | None = None
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
        self._window_repeat_sig: tuple[Any, ...] | None = None
        self._window_repeat_count: int = 0
        self._auto_respond_escaped_sig: tuple[Any, ...] | None = None
        self._AUTO_RESPOND_LOOP_THRESHOLD = 3
        # Spoken game-plan announcement dedup (speak each new plan once).
        self._last_announced_plan: str = ""

    def _capture_screenshot(self) -> bytes | None:
        """Capture MTGA window as PNG bytes for VLM analysis.

        Uses PrintWindow for DirectX/Unity windows; ImageGrab (GDI BitBlt)
        returns black frames on many systems for MTGA.
        """
        try:
            from arenamcp.input_controller import find_mtga_hwnd
            from arenamcp.screen_capture import capture_mtga_png

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
            logger.info(
                f"Vision scan completed: {elapsed_ms:.0f}ms, {self._mapper.cache_size} elements cached"
            )
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
    def current_plan(self) -> ActionPlan | None:
        """Currently active action plan."""
        return self._current_plan

    @property
    def _announce_game_plan(self) -> None:
        """Speak the current game plan aloud when it changes (TTS).

        Lets the operator hear what the autopilot is thinking strategically.
        Fires only when a speak function is wired (the desktop coach and the
        opt-in harness path) and only once per distinct plan. Always
        non-blocking and best-effort — never affects play.
        """
        if self._game_plan_mgr is None:
            return
        # Structured plan → UI strategy card. Independent of TTS wiring and
        # deduped on payload so the card only repaints when the plan changes.
        if self._ui_game_plan_fn is not None:
            try:
                plan = self._game_plan_mgr.current
                payload = dict(plan.as_payload(), source="autopilot") if plan else {}
                if payload != self._last_emitted_game_plan:
                    self._last_emitted_game_plan = payload
                    self._ui_game_plan_fn(payload)
            except Exception as e:
                logger.debug("game-plan UI payload failed: %s", e)
        if self._speak_fn is None:
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

    # --- R1: decision-window identity ------------------------------------
    # The log-parsed turn counter lags the bridge at turn boundaries, so
    # turn/phase staleness checks discard plans whose window is still open
    # (Arcane Signet discarded as "turn advanced 5→6" then re-planned and
    # cast on the SAME window 11s later — 2026-07-05, ~4 wasted LLM calls).
    # The bridge request identity is stable for the lifetime of one window.

    @staticmethod
    def _normalize_request_type(rtype: Any) -> str:
        s = str(rtype or "")
        for suffix in ("Request", "Req"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s

    def _snapshot_window_identity(self, game_state: dict[str, Any]) -> tuple[Any, ...] | None:
        """Window identity from a bridge-overlaid snapshot, or None."""
        gsid = int(game_state.get("_bridge_game_state_id", 0) or 0)
        rtype = self._normalize_request_type(
            game_state.get("_bridge_request_type") or game_state.get("_bridge_request_class")
        )
        if not gsid or not rtype:
            return None
        actions = game_state.get("_bridge_actions")
        n = len(actions) if isinstance(actions, list) else -1
        return (gsid, rtype, n)

    # P1-4: how long autopilot stays advise-only after spotting a manual play.
    _MANUAL_PLAY_COOLDOWN_S = 20.0
    # Bot submissions within this window explain an own stack object.
    _MANUAL_PLAY_BOT_WINDOW_S = 15.0

    def _detect_manual_play(self, game_state: dict[str, Any]) -> bool:
        """True when a NEW own-controlled stack object wasn't bot-submitted.

        Tracks the set of own stack names between polls; a new one that no
        recent bot submission explains means the user is casting manually —
        the autopilot enters an advise-only cooldown and says so instead of
        fighting the user's plays (P1-4, live 2026-07-06 01:02).
        """
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            return False
        own_stack = {
            str(e.get("name") or "").strip().lower()
            for e in (game_state.get("stack") or [])
            if isinstance(e, dict)
            and (e.get("controller_seat_id") or e.get("owner_seat_id")) == local_seat
            and e.get("name")
        }
        new_names = own_stack - self._last_seen_own_stack
        self._last_seen_own_stack = own_stack
        if not new_names:
            return False
        now = time.monotonic()
        recent_bot_names = {
            name for ts, name in self._recent_bot_submissions if now - ts <= self._MANUAL_PLAY_BOT_WINDOW_S
        }
        unexplained = {n for n in new_names if n not in recent_bot_names}
        if not unexplained:
            return False
        already_cooling = time.time() < self._manual_play_cooldown_until
        self._manual_play_cooldown_until = time.time() + self._MANUAL_PLAY_COOLDOWN_S
        if not already_cooling:
            logger.info(
                f"Autopilot: manual play detected ({sorted(unexplained)}) — "
                f"advise-only for {self._MANUAL_PLAY_COOLDOWN_S:.0f}s"
            )
            self._notify(
                "AUTOPILOT",
                "You're playing — autopilot standing by (advice only)",
            )
        return True

    def get_reusable_advice(self, game_state: dict[str, Any]) -> str | None:
        """Advice from the plan just computed for this same decision window.

        The coach fall-through used to re-run plan_actions on the identical
        state (8 duplicate calls / ~58s on 2026-07-05) — P2-3. Returns None
        when the window changed or the plan is older than 20s.
        """
        entry = self._last_plan_advice
        if not entry:
            return None
        sig, advice, ts = entry
        if not advice or time.time() - ts > 20.0:
            return None
        if sig != self._priority_window_signature(game_state):
            return None
        return advice

    def _live_pending_request_is(self, expected_norm: str) -> bool | None:
        """Live-verify the pending bridge request family.

        Returns True/False when a live poll answers, None when it cannot
        (bridge offline / poll error) — callers keep their snapshot-based
        behavior on None rather than guessing.
        """
        if not (self._gre_bridge.connected or self._gre_bridge.connect()):
            return None
        try:
            live = self._gre_bridge.get_pending_actions() or {}
        except Exception:
            return None
        if not live.get("has_pending"):
            return False
        return (
            self._normalize_request_type(live.get("request_type") or live.get("request_class"))
            == expected_norm
        )

    def _live_window_identity(self) -> tuple[Any, ...] | None:
        """Window identity from a live bridge poll, or None when idle/offline."""
        if not (self._gre_bridge.connected or self._gre_bridge.connect()):
            return None
        try:
            poll = self._gre_bridge.get_pending_actions() or {}
        except Exception:
            return None
        if not poll.get("has_pending"):
            return None
        gsid = int(poll.get("game_state_id") or 0)
        rtype = self._normalize_request_type(poll.get("request_type") or poll.get("request_class"))
        if not gsid or not rtype:
            return None
        actions = poll.get("actions")
        n = len(actions) if isinstance(actions, list) else -1
        return (gsid, rtype, n)

    @staticmethod
    def _window_identities_match(pre: tuple[Any, ...] | None, fresh: tuple[Any, ...] | None) -> bool:
        """True only when both identities are known and denote the same window.

        Action counts of -1 (unknown) compare as wildcards — older plugin
        builds omit the action list on some request families.
        """
        if not pre or not fresh:
            return False
        if pre[0] != fresh[0] or pre[1] != fresh[1]:
            return False
        return pre[2] == fresh[2] or pre[2] == -1 or fresh[2] == -1

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
                key
                for key in self._blocked_action_keys
                if self._persistent_failure_counts.get(key, 0) >= self._HARD_BLOCK_FAILURE_THRESHOLD
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
            sorted(
                (blocker.lower(), attacker.lower())
                for blocker, attacker in (action.blocker_assignments or {}).items()
            )
        )
        selection_names = tuple(sorted(name.lower() for name in (action.select_card_names or [])))
        distribution = tuple(
            sorted((name.lower(), amount) for name, amount in (action.distribution or {}).items())
        )

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
                count,
                action,
                reason,
            )
            self._pause_for_manual(
                f"Action repeatedly failed ({count}x): {action.action_type.value}"
                f" {action.card_name or ''}".strip(),
                game_state,
            )
        else:
            logger.warning(
                "Blocking action for current window (failure %d/%d): %s (%s)",
                count,
                self._HARD_BLOCK_FAILURE_THRESHOLD,
                action,
                reason,
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
        "return target",  # bounce spells
        "opponent sacrifices target",
        "damage to target",  # Shock-style
        "gets -",  # "target creature gets -X/-X"
        "gets −",  # unicode minus variant
        "loses all abilities",
        "loses flying",
    )

    def _pick_single_target_candidate(
        self,
        game_state: dict[str, Any],
    ) -> int | None:
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
                # gamestate emits controller_seat_id (never controller_id).
                controller = card.get("controller_seat_id") or card.get("owner_seat_id")
                break

        # Opponent-controlled sole target: only auto-submit when
        # the source spell is harmful to the opponent's permanent.
        # When the opponent is casting a buff on their own thing, we
        # should not confirm it for them.
        if local_seat is not None and controller is not None and controller != local_seat:
            if self._source_spell_is_harmful_to_target(game_state, snap_resp, live_resp):
                return only_id
            logger.info(
                f"Autopilot: declining auto-submit for target {only_id} — "
                "sole candidate is opponent-controlled but the source spell "
                "looks beneficial to the opponent. Letting the LLM decide."
            )
            return None

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

    def _resolve_decision_source(
        self,
        game_state: dict[str, Any],
        snap_resp: dict[str, Any] | None = None,
        live_resp: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Resolve (name, lowercased oracle text) of the decision's source.

        Order: decision_context sourceId → matching stack entry, else top of
        stack, else oracle from the bridge request payload. Empty strings
        when nothing resolves.
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
            rp = resp.get("request_payload") or {}
            for k in ("sourceOracleText", "oracleText", "oracle_text"):
                if rp.get(k):
                    oracle = str(rp[k]).lower()
                    break

        return name, oracle

    # A cast/activation the autopilot submitted within this window opens a
    # PayCosts that is part of the normal casting flow — always auto-pay it.
    _OPTIONAL_COST_OWN_ACTION_WINDOW_S = 10.0

    def _source_spell_is_harmful_to_target(
        self,
        game_state: dict[str, Any],
        snap_resp: dict[str, Any] | None,
        live_resp: dict[str, Any] | None,
    ) -> bool:
        """Does the spell on the stack read like a removal / hurt effect?

        We find the source card in this order:
          1. decision_context (bridge-supplied sourceId → stack entry)
          2. top of the stack (spell currently resolving targets)
        Then we check its oracle text against known harmful phrases.
        Unknown oracle text => False (err on the side of auto-submit).
        """
        name, oracle = self._resolve_decision_source(game_state, snap_resp, live_resp)

        if not oracle:
            logger.debug(
                f"Autopilot: no oracle text found for source spell (name={name!r}); defaulting to auto-submit"
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

    # A cast rolled back this many times in one turn is hidden from the
    # planner for the rest of the turn — it cannot complete and re-trying
    # is the engine of the cast→cancel→re-cast livelock.
    _CAST_ROLLBACK_LIMIT = 2
    # Across turns: the same cast wedging on different turns is the same
    # livelock; after this many total rollbacks the cast is off the menu
    # for the rest of the game.
    _CAST_ROLLBACK_GAME_LIMIT = 3
    # auto_respond escapes allowed per turn. Each new gameStateId makes a
    # new window signature, so the old once-per-window guard allowed an
    # escape every cycle of a cross-window loop — i.e. forever.
    _MAX_ESCAPES_PER_TURN = 2
    # A window must be stuck this long (wall clock) before auto_respond may
    # escape it — the repeat counter alone trips in <1s of trigger spam.
    _ESCAPE_MIN_WINDOW_AGE_S = 12.0

    # A cast submission older than this cannot be the thing that just rolled
    # back — 2026-07-05 a PayCosts cancel of an ability activation was blamed
    # on a Rampant Growth cast submitted 2 minutes earlier, charging strikes
    # toward the innocent card's game-wide suppression limit.
    _CAST_ROLLBACK_ATTRIBUTION_MAX_AGE_S = 10.0

    def _note_cast_rollback(self, why: str) -> None:
        """Record that the most recently submitted cast was rolled back."""
        last = self._last_cast_submitted
        if not last:
            return
        age = time.monotonic() - self._last_cast_submitted_ts
        if age > self._CAST_ROLLBACK_ATTRIBUTION_MAX_AGE_S:
            logger.info(
                f"Ignoring rollback attribution to {last[1]!r} — submission is "
                f"{age:.0f}s old, the rollback belongs to something newer: {why}"
            )
            self._last_cast_submitted = None
            return
        self._cast_rollback_counts[last] = self._cast_rollback_counts.get(last, 0) + 1
        self._cast_rollback_totals[last[1]] = self._cast_rollback_totals.get(last[1], 0) + 1
        n = self._cast_rollback_counts[last]
        logger.warning(
            f"Cast rollback #{n} for {last[1]!r} (turn {last[0]}, "
            f"game total {self._cast_rollback_totals[last[1]]}): {why}"
        )
        # One submission = at most one rollback; clear so a later detection
        # pass can't double-count the same wedge.
        self._last_cast_submitted = None

    @staticmethod
    def _plain_card_name(text: str) -> str:
        """Strip (P/T) and trailing [TAG]s from a legal-action card name."""
        text = re.sub(r"\s*\([\dxX*+-]+/[\dxX*+-]+\)\s*$", "", text or "").strip()
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text).strip()
        return text

    def _filter_rolled_back_casts(self, legal_actions: list[str], game_state: dict[str, Any]) -> list[str]:
        """Hide 'Cast X' from the planner once X was rolled back twice this turn.

        A cast that reached PayCosts/targeting and got cancelled cannot
        complete with the current resources; offering it to the planner
        again just re-arms the livelock (live 2026-06-09).
        """
        if not legal_actions or not (self._cast_rollback_counts or self._cast_rollback_totals):
            return legal_actions
        turn = int((game_state.get("turn") or {}).get("turn_number", 0) or 0)
        out: list[str] = []
        for la in legal_actions:
            la_lower = la.lower().strip()
            # P0-6: ability activations wedge through PayCosts the same way
            # casts do — suppress both once they hit the rollback limits.
            name = None
            if la_lower.startswith("cast "):
                name = self._plain_card_name(la.strip()[5:]).lower()
            elif la_lower.startswith("activate ability: "):
                name = self._plain_card_name(la.strip()[len("activate ability: ") :]).lower()
            if name:
                if self._cast_rollback_counts.get((turn, name), 0) >= self._CAST_ROLLBACK_LIMIT:
                    logger.info(
                        f"Suppressing legal action {la!r} — rolled back "
                        f"{self._CAST_ROLLBACK_LIMIT}+ times this turn"
                    )
                    continue
                if self._cast_rollback_totals.get(name, 0) >= self._CAST_ROLLBACK_GAME_LIMIT:
                    # #40: never game-lock the commander — it's the deck's
                    # centerpiece and its PayCosts failures are usually the
                    # late-autotap-child bridge gap, not unpayability. The
                    # per-turn limit above still breaks live loops.
                    command_names = {
                        str(c.get("name") or "").strip().lower() for c in game_state.get("command", []) or []
                    }
                    if name in command_names:
                        logger.info(
                            f"Not game-suppressing {la!r} — command-zone card (per-turn limit still applies)"
                        )
                    else:
                        logger.info(
                            f"Suppressing legal action {la!r} — rolled back "
                            f"{self._CAST_ROLLBACK_GAME_LIMIT}+ times this game"
                        )
                        continue
            out.append(la)
        return out

    def _try_auto_respond_escape(self, game_state: dict[str, Any] | None, reason: str) -> bool:
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
        if breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS:
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
                "auto_respond escape budget exhausted for turn %s — leaving %s for the user",
                turn,
                breq or bcls,
            )
            return False
        try:
            if self._gre_bridge.auto_respond():
                self._escape_count_this_turn += 1
                if any(k in (breq + bcls) for k in ("SelectTargets", "PayCosts", "CastingTimeOption")):
                    # Escaping a casting-flow window rolls back the cast.
                    self._note_cast_rollback(f"auto_respond escape on {breq or bcls}")
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"auto_respond escape on stuck {breq or bcls} ({reason})",
                )
                # Feed the strategic layer: a plan step we couldn't enact.
                if self._game_plan_mgr is not None:
                    with contextlib.suppress(Exception):
                        self._game_plan_mgr.note_stall(f"{breq or bcls} ({reason})")
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
        ) and self._try_auto_respond_escape(game_state, f"window repeated {self._window_repeat_count}x"):
            self._auto_respond_escaped_sig = sig
            self._window_repeat_count = 0
            return True
        return False

    def _try_submit_plan_advancing_play(self, game_state: dict[str, Any] | None) -> bool:
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
        if not (breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS):
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

        chosen_idx: int | None = None
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
            if self._gre_bridge.submit_action_by_index(chosen_idx, auto_pass=self._config.auto_pass_priority):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"plan-advancing play submitted instead of auto-pass (idx={chosen_idx})",
                )
                return True
        except Exception as e:
            logger.debug(f"plan-advancing submit failed: {e}")
        return False

    def _pause_for_manual(self, reason: str, game_state: dict[str, Any] | None = None) -> None:
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
            if breq in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS or bcls in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS:
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
            and self._try_auto_respond_escape(game_state, f"manual-required fallback: {reason}")
        ):
            return

        # The plan could not be enacted here — tell the strategic layer so a
        # repeatedly-unexecutable plan gets reformed into a different line.
        if self._game_plan_mgr is not None:
            with contextlib.suppress(Exception):
                self._game_plan_mgr.note_stall(reason)

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

    def _format_bridge_gap_hint(self, game_state: dict[str, Any] | None) -> str:
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

        req = game_state.get("_bridge_request_type") or game_state.get("_bridge_request_class")
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

        if action.action_type in (
            ActionType.PLAY_LAND,
            ActionType.CAST_SPELL,
            # Shape 2a (2026-07-29): ACTIVATE_ABILITY — same shape: planner
            # picked an ability activation but the bridge has a different
            # request type pending (SelectTargets / Search / PayCosts / etc.).
            # In the July 2-6 cluster (#392 Mutagen, #407 Utter Insignificance)
            # the planner kept picking activate_ability against stale game
            # state that was no longer offering that ability. Treat as stale
            # so the system re-plans cleanly instead of filing a
            # bridge_submit_failed noise bug.
            ActionType.ACTIVATE_ABILITY,
        ):
            # Shape 2: bridge has a different request type pending entirely.
            is_actions_available = (
                bridge_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                or bridge_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            )
            if not is_actions_available:
                return True

            # Shape 1: bridge IS ActionsAvailable but doesn't offer the
            # specific Play/Cast the planner picked.
            # ACTIVATE_ABILITY skips Shape 1 — the bridge doesn't enumerate
            # activations as top-level actionType entries in ActionsAvailable
            # (they're card-contextual). Let the normal execute path decide.
            if action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
                bridge_actions = game_state.get("_bridge_actions") or []
                if not bridge_actions:
                    return False
                target_type = "Play" if action.action_type == ActionType.PLAY_LAND else "Cast"
                for ba in bridge_actions:
                    ba_type = ba.get("actionType") or ""
                    if ba_type == target_type or ba_type == f"ActionType_{target_type}":
                        return False
                return True

        if action.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS):
            expected = (
                "DeclareAttacker" if action.action_type == ActionType.DECLARE_ATTACKERS else "DeclareBlockers"
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
        action: GameAction | None = None,
    ) -> bool:
        """Whether the current state should never fall back to auto_respond."""
        if self._decision_type(game_state) in self._CRITICAL_DECISION_TYPES:
            return True

        return bool(
            action
            and action.action_type
            in {
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
            }
        )

    def _should_allow_auto_respond(
        self,
        game_state: dict[str, Any],
        action: GameAction | None = None,
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

    def force_stop(self) -> None:
        """Panic button: abort in-flight work and drop all queued intent.

        Wired to the UI's Force Stop control for autopilot spirals
        (repeated cast/target loops). The caller also disables autopilot;
        this clears engine-side momentum — current plan, the planner's
        locked turn memo/intent, and the per-request submission FSM — so
        nothing resumes or re-locks the same doomed plan when autopilot
        is re-enabled.
        """
        logger.warning("Autopilot FORCE STOP requested")
        self._abort_event.set()
        self._confirm_event.set()
        self._skip_event.set()
        self._current_plan = None
        self._state = AutopilotState.IDLE
        with contextlib.suppress(Exception):
            self._request_tracker.reset()
        planner = getattr(self, "_planner", None)
        if planner is not None:
            planner._turn_memo = None
            planner._turn_intent = None

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
        with contextlib.suppress(RuntimeError):
            self._lock.release()

    def _wait_for_cancel(self, timeout: float | None = None) -> str:
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
                logger.error("Autopilot: could not acquire lock even after force-release")
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
                self._cast_rollback_totals.clear()
                self._last_cast_submitted = None
                self._runaway_tripped_turn = None
                self._request_tracker.reset()
            self._max_seen_turn = max(self._max_seen_turn, turn_num)

            # P1-4: the user casting manually while autopilot runs. On
            # 2026-07-06 01:02 the bot fought the user's manual plays for
            # ~50s (activated the user's aura 3x, target attempts failing
            # "Pending is null"). When an own-controlled stack object
            # appears that WE didn't submit, stand down to advise-only for
            # a cooldown instead of racing the user.
            if self._detect_manual_play(game_state):
                return False  # fall through to coaching (advise-only)
            if time.time() < self._manual_play_cooldown_until:
                logger.info("Autopilot: user is playing — standing by (advise-only)")
                return False

            # Silent-rollback detection: we submitted a cast, and the SAME
            # cast is being offered again in a later priority window while
            # nothing of ours sits on the stack. The 2026-07-01 wedges rolled
            # back on MTGA's action timer with no cancel/escape event on our
            # side, so only this re-offer signature reveals them. The 5s
            # floor keeps stale snapshots from the submission window itself
            # from counting.
            last_cast = self._last_cast_submitted
            if last_cast is not None and time.monotonic() - self._last_cast_submitted_ts > 5.0:
                offered = {
                    self._plain_card_name(a.strip()[5:]).lower()
                    for a in self._get_legal_actions(game_state)
                    if a.lower().strip().startswith("cast ")
                }
                if last_cast[1] in offered:
                    stack_owned = any(
                        isinstance(c, dict) and str(c.get("name") or "").strip().lower() == last_cast[1]
                        for c in (game_state.get("stack") or [])
                    )
                    if not stack_owned:
                        self._note_cast_rollback("cast re-offered in a later window (timer rollback)")

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
                logger.debug("Autopilot: window already declared manual-required; standing by for the user")
                self._state = AutopilotState.IDLE
                return False

            self._clear_events()

            # --- BRIDGE PRELOAD: stash bridge actions for execution phase ---
            # When the trigger was bridge-detected, actions are already fetched.
            # Avoids redundant get_pending_actions() call in _try_gre_bridge().
            bridge_trigger = game_state.get("_bridge_trigger")
            self._bridge_preloaded_actions = bridge_trigger.get("actions") if bridge_trigger else None
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
            # display string is parsed. ActionsAvailable migrated here in
            # Phase E, so priority windows are typed-path-owned too; the
            # legacy strategic path below only runs when the typed path
            # declines (returns None: no bridge, no options, dry run).
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
                pending is not None and pending != "Action Required" and pending != "Priority (Pass Only)"
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
                    logger.info(f"Autopilot: auto-submitting single-candidate target (instance_id={auto_id})")
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
                            f"Autopilot: submit_targets({auto_id}) failed — falling through to LLM planning"
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
                # P1-5: one trigger batch dispatches every trigger against
                # the SAME snapshot. On 2026-07-05 23:01:01 the first
                # dispatch paid the PayCosts and the second re-entered this
                # branch on the consumed snapshot → "Pending is null" →
                # blind cancel_action() → false MANUAL REQUIRED + a 316KB
                # bug report. Verify the window still exists before acting.
                if not self._config.dry_run:
                    still_pending = self._live_pending_request_is("PayCosts")
                    if still_pending is False:
                        logger.info(
                            "Autopilot: PayCosts snapshot is stale (window "
                            "already consumed) — dropping trigger"
                        )
                        self._state = AutopilotState.IDLE
                        return True
                # Optional-cost gate (2026-07-05 Go-Shintai incident): only
                # blind auto-pay costs of actions we initiated; out-of-band
                # cancellable harmful triggers get a pay/decline decision.
                decline_reason = self._should_decline_optional_cost(game_state)
                if decline_reason:
                    logger.warning(f"Autopilot: NOT auto-paying — {decline_reason}")
                    if self._config.dry_run:
                        self._record_autopilot_decision(
                            game_state,
                            trigger,
                            action_type="pay_costs",
                            summary=f"[dry-run] would decline optional cost: {decline_reason}",
                        )
                        return True
                    if (
                        self._gre_bridge.connected or self._gre_bridge.connect()
                    ) and self._gre_bridge.cancel_action():
                        self._log_execution_path(ExecutionPath.GRE_AWARE, "decline optional PayCosts")
                        self._record_autopilot_decision(
                            game_state,
                            trigger,
                            action_type="pay_costs",
                            summary=f"declined optional cost: {decline_reason}",
                        )
                        return True
                    self._record_autopilot_decision(
                        game_state,
                        trigger,
                        action_type="pay_costs",
                        summary=f"decline failed, needs manual: {decline_reason}",
                    )
                    self._manual_required_bridge_result(
                        GameAction(
                            action_type=ActionType.PAY_COSTS,
                            card_name="decline",
                            reasoning=decline_reason,
                        ),
                        game_state,
                        "bridge_submit_failed",
                        "Optional cost needs a manual pay/decline decision",
                    )
                    return False
                # User preference (2026-04-30): always click Auto Pay when
                # MTGA offers it — never try to manually decide which lands
                # to tap. submit_auto_tap walks PayCostsRequest's children
                # for the AutoTapActionsRequest and submits its solution
                # (= what the in-game Auto Pay button does).
                logger.info("Autopilot: submitting AutoTap solution for PayCosts")
                if not self._config.dry_run and (self._gre_bridge.connected or self._gre_bridge.connect()):
                    auto_tap_ok = self._gre_bridge.submit_auto_tap()
                    if not auto_tap_ok:
                        # #40 (live 2026-07-06): the AutoTapActionsRequest
                        # child can populate a beat AFTER the PayCostsRequest
                        # appears — an immediate poll saw "no AutoTapActions-
                        # Request available" on an [OK]-tagged commander cast,
                        # cancelled, and 3 strikes game-suppressed Hei Bai.
                        # One short retry before concluding it's unpayable.
                        time.sleep(0.4)
                        auto_tap_ok = self._gre_bridge.submit_auto_tap()
                        if auto_tap_ok:
                            logger.info("Autopilot: AutoTap child arrived late — retry succeeded")
                    if not auto_tap_ok and self._live_pending_request_is("PayCosts") is False:
                        # The P1-5 guard above checks the window on entry, but
                        # the window can also be consumed *while* we are here:
                        # a concurrent trigger pays it, or the 0.4s retry sleep
                        # gives another dispatch time to land. "Pending is null"
                        # then means the cost was paid, not that payment failed
                        # — escalating to MANUAL REQUIRED here is a false alarm
                        # (issue #405, and the same 316KB bug report).
                        logger.info(
                            "Autopilot: PayCosts window consumed during auto-pay "
                            "(paid by a concurrent trigger) — treating as done"
                        )
                        self._record_autopilot_decision(
                            game_state,
                            trigger,
                            action_type="pay_costs",
                            summary="PayCosts already paid by a concurrent trigger",
                        )
                        self._state = AutopilotState.IDLE
                        return True
                    if auto_tap_ok:
                        self._log_execution_path(ExecutionPath.GRE_AWARE, "auto_pay via submit_auto_tap")
                        self._record_autopilot_decision(
                            game_state,
                            trigger,
                            action_type="pay_costs",
                            summary="submitted AutoTap solution via bridge",
                        )
                        return True
                    # Issue #414: our own just-submitted cast reaching a
                    # PayCosts with no autotap child is a LEGAL cast that
                    # MTGA wants paid manually (observed on every command-
                    # zone Hei Bai cast — even [OK]-tagged ones). Cancelling
                    # silently fizzles the spell and accrues rollback
                    # strikes; hand it to the user instead.
                    if self._last_cast_submitted and (
                        time.monotonic() - self._last_cast_submitted_ts
                        <= self._OPTIONAL_COST_OWN_ACTION_WINDOW_S
                    ):
                        self._pause_for_manual(
                            f"Pay for {self._last_cast_submitted[1]} manually "
                            "(tap lands or click Auto Pay) — the bridge found "
                            "no auto-payment route for this cast",
                            game_state,
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
                    game_state,
                    trigger,
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
                a
                for a in legal
                if not a.lower().startswith("done (confirm")
                and a.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                and "Wait" not in a
            ]
            if has_done_confirm and not meaningful_non_done:
                done_action = next(a for a in legal if a.lower().startswith("done (confirm"))
                logger.info(f"Autopilot: auto-confirming '{done_action}'")
                self._record_autopilot_decision(
                    game_state,
                    trigger,
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
                            str(name) for name in (ctx.get("legal_attackers") or []) if name
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
                bridge_request_type
                in (
                    "OptionalAction",
                    "OptionalActionReq",
                    "OptionalActionRequest",
                    "OptionalActionMessage",
                    "OptionalActionMessageRequest",
                    "OptionalActionMessageReq",
                )
                or bridge_request_class
                in (
                    "OptionalAction",
                    "OptionalActionReq",
                    "OptionalActionRequest",
                    "OptionalActionMessage",
                    "OptionalActionMessageRequest",
                    "OptionalActionMessageReq",
                )
                or ((game_state.get("decision_context") or {}).get("type") == "optional_action")
            ):
                legal = self._get_legal_actions(game_state)
                # Accept / Decline are the actual meaningful choices and must
                # flow through to the planner so the LLM decides — they are
                # NOT filtered out here on purpose.
                meaningful = [
                    a
                    for a in legal
                    if a.lower() not in {"pass", "action: activate_mana", "action: floatmana"}
                    and "Wait" not in a
                ]
                if not meaningful:
                    logger.info("Autopilot: auto-declining optional action (no meaningful actions)")
                    if not self._config.dry_run:
                        if self._gre_bridge.connected or self._gre_bridge.connect():
                            if self._gre_bridge.submit_optional(False):
                                self._log_execution_path(
                                    ExecutionPath.GRE_AWARE,
                                    "auto-decline optional via submit_optional(False)",
                                )
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
                    game_state,
                    trigger,
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
                    combat_meaningful = [a for a in meaningful if not a.lower().startswith("activate ")]
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
                if trigger == "opponent_turn" and not can_do_anything:
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
                        logger.info(
                            f"Autopilot: combat step {step} without decision context — submitting via GRE bridge"
                        )
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
            # R1: bridge window identity beats the log-lagged turn counter
            # for staleness decisions (None when the bridge is offline).
            pre_window_identity = self._snapshot_window_identity(game_state)
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
                    # R2: reform runs in the background — the tactical call
                    # uses whatever plan text exists NOW. The old blocking
                    # reform added ~5s to the first own-turn window and
                    # helped chains self-induce staleness discards.
                    if (
                        self._is_local_active_turn(game_state)
                        and not self._game_plan_reform_inflight.is_set()
                    ):
                        self._game_plan_reform_inflight.set()

                        def _reform_async(gs=game_state):
                            try:
                                self._game_plan_mgr.maybe_reform(gs)
                                self._planner.set_game_plan(self._game_plan_mgr.plan_text())
                                self._announce_game_plan()
                            except Exception as e:
                                logger.debug("async game-plan reform failed: %s", e)
                            finally:
                                self._game_plan_reform_inflight.clear()

                        threading.Thread(
                            target=_reform_async,
                            daemon=True,
                            name="game-plan-reform",
                        ).start()
                    self._planner.set_game_plan(self._game_plan_mgr.plan_text())
                    self._announce_game_plan()
                except Exception as e:
                    logger.debug("game-plan refresh skipped: %s", e)

            _plan_started_at = time.perf_counter()
            plan = self._planner.plan_actions(game_state, trigger, legal_actions, decision_context)
            # P2-3: remember this window's advice so a coach fall-through on
            # the same window reuses it instead of re-running plan_actions.
            self._last_plan_advice = (
                self._priority_window_signature(game_state),
                plan.voice_advice or plan.overall_strategy or "",
                time.time(),
            )
            # Opt-in trajectory capture for real-match data collection. No-op
            # unless a recorder was attached (engine._trajectory_recorder).
            self._maybe_record_trajectory(
                game_state,
                trigger,
                legal_actions,
                decision_context,
                plan,
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
            if self._is_non_passable_interactive(game_state) and self._plan_cannot_legally_submit(plan):
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
                    plan = self._deterministic_fallback(game_state, trigger, legal_actions, decision_context)

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
                    ) and self._gre_bridge.auto_respond():
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
                        a
                        for a in (legal_actions or [])
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
            _skip_staleness = plan.actions and plan.actions[0].action_type in (
                ActionType.MULLIGAN_KEEP,
                ActionType.MULLIGAN_MULL,
                ActionType.CHOOSE_STARTING_PLAYER,
                ActionType.PASS_PRIORITY,
                ActionType.RESOLVE,
                ActionType.CLICK_BUTTON,
            )
            if not _skip_staleness:
                try:
                    fresh_state = self._get_game_state()
                    fresh_turn = fresh_state.get("turn", {})
                    stale = False
                    _plan_ms = (time.perf_counter() - _plan_started_at) * 1000.0

                    # R1: when the SAME bridge window is still pending, the
                    # plan is fresh no matter what the log-parsed counters
                    # say — they lag the bridge at turn boundaries.
                    fresh_window_identity = self._live_window_identity() if pre_window_identity else None
                    window_fresh = self._window_identities_match(pre_window_identity, fresh_window_identity)
                    if window_fresh:
                        logger.info(
                            "Staleness: decision window unchanged "
                            f"({pre_window_identity[1]} gsid={pre_window_identity[0]}, "
                            f"planning took {_plan_ms:.0f}ms) — plan is fresh"
                        )
                    elif pre_window_identity and fresh_window_identity:
                        logger.warning(
                            "STALE: decision window changed "
                            f"{pre_window_identity} → {fresh_window_identity} "
                            f"(planning took {_plan_ms:.0f}ms)"
                        )
                        stale = True
                    elif fresh_turn.get("turn_number", 0) != pre_turn_num:
                        logger.warning(
                            f"STALE: turn advanced {pre_turn_num} → "
                            f"{fresh_turn.get('turn_number')} "
                            f"(planning took {_plan_ms:.0f}ms)"
                        )
                        stale = True
                    elif fresh_turn.get("active_player", 0) != pre_active:
                        logger.warning(
                            f"STALE: active player changed {pre_active} → {fresh_turn.get('active_player')}"
                        )
                        stale = True
                    elif fresh_turn.get("phase", "") != pre_phase:
                        is_sorcery_play = any(
                            a.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL)
                            for a in plan.actions
                        )
                        has_combat_action = any(
                            a.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS)
                            for a in plan.actions
                        )
                        now_combat = "Combat" in fresh_turn.get("phase", "")

                        # Only discard a sorcery-speed plan that got overtaken by
                        # combat. If the plan ALSO includes a combat action
                        # (declare attackers/blockers), moving into combat is
                        # exactly where we want to be — keep the plan; the
                        # executor stale-skips the now-illegal sorcery steps and
                        # submits the combat action.
                        if is_sorcery_play and now_combat and not has_combat_action:
                            logger.warning(
                                f"STALE: phase changed {pre_phase} → {fresh_turn.get('phase')} (sorcery plan in combat)"
                            )
                            stale = True
                        else:
                            logger.info(
                                f"Phase changed {pre_phase} → {fresh_turn.get('phase')}, proceeding with caution"
                            )

                    if stale:
                        self._notify("AUTOPILOT", "Plan discarded (game moved on)")
                        self._record_user_takeover(
                            plan,
                            fresh_state,
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
            # Pass-only plans get at most one spoken explanation per turn —
            # and none when the pass was forced (no real alternatives), since
            # there is no decision to explain. The reasoning always reaches
            # the UI via the _notify above.
            pass_only = bool(plan.actions) and all(
                getattr(action, "action_type", None) == ActionType.PASS_PRIORITY for action in plan.actions
            )
            speak_plan = not pass_only or self._should_speak_pass_plan(game_state)
            if self._config.enable_tts_preview and self._speak_fn and speak_plan:
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
                if (
                    exec_turn.get("turn_number", 0) != pre_turn_num
                    or exec_turn.get("active_player", 0) != pre_active
                ):
                    # R1: trust the bridge window over the log-lagged turn
                    # counter — same rule as the post-planning check.
                    if self._window_identities_match(pre_window_identity, self._live_window_identity()):
                        logger.info(
                            "Pre-execution: turn counter drifted but the "
                            "decision window is unchanged — proceeding"
                        )
                        game_state = exec_state
                    else:
                        logger.warning("STALE at execution time — game moved on during countdown")
                        self._notify("AUTOPILOT", "Plan discarded (game moved during countdown)")
                        self._record_user_takeover(
                            plan,
                            exec_state,
                            reason="plan_went_stale_during_countdown",
                        )
                        self._state = AutopilotState.IDLE
                        return False
                else:
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
                            # R1/P1-2: distinguish a model hallucination from
                            # a window that moved while we were planning —
                            # they need different fixes and the old log
                            # blamed the model for both.
                            was_plan_time_legal = any(card in la.lower() for la in (legal_actions or []))
                            if was_plan_time_legal:
                                logger.warning(
                                    f"Dropping stale-window action: {action.action_type.value} "
                                    f"'{action.card_name}' was legal at plan time but the "
                                    f"window moved; now: {fresh_legal[:5]}"
                                )
                                self._notify(
                                    "AUTOPILOT",
                                    f"Skipped: {action.card_name} (window moved)",
                                )
                            else:
                                logger.warning(
                                    f"Rejecting hallucinated action: {action.action_type.value} "
                                    f"'{action.card_name}' not in legal actions: {fresh_legal[:5]}"
                                )
                                self._notify("AUTOPILOT", f"Rejected: {action.card_name} (not legal)")
                            continue
                    validated.append(action)
                if len(validated) < len(plan.actions):
                    logger.info(
                        f"Legal actions guardrail: {len(plan.actions)} planned -> {len(validated)} valid"
                    )
                    plan = ActionPlan(
                        actions=validated,
                        overall_strategy=plan.overall_strategy,
                    )

            # P0-4: a fallback "[auto-pick] Pass" is a shrug, not a decision.
            # On our own window, try an unambiguous plan-advancing play first
            # (the guard is deliberately conservative: the plan's wanted
            # card, the sole legal land drop, or the sole legal cast). 30
            # fallback passes on 2026-07-05 skipped castable [OK] spells —
            # The Spirit Oasis was passed away twice on the same menu.
            is_fallback_pass = (
                (plan.overall_strategy or "").startswith("[auto-pick]")
                and plan.actions
                and all(a.action_type == ActionType.PASS_PRIORITY for a in plan.actions)
            )
            if is_fallback_pass and self._try_submit_plan_advancing_play(game_state):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    "fallback pass replaced by plan-advancing play",
                )
                self._actions_executed += 1
                self._last_exec_success_ts = time.time()
                self._state = AutopilotState.IDLE
                return True

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

                action_text = f"[{i + 1}/{len(plan.actions)}] {action}"
                self._notify("AUTOPILOT", action_text)

                if self._is_action_blocked(action, game_state):
                    # Backstop: never dead-loop on a non-passable interactive
                    # request. If the blocked action is a pass/resolve the GRE
                    # won't accept here, submit a typed safe default instead of
                    # halting the turn forever.
                    if action.action_type in (
                        ActionType.PASS_PRIORITY,
                        ActionType.RESOLVE,
                    ) and self._is_non_passable_interactive(game_state):
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
                            logger.warning(f"STALE mid-execution: turn advanced at action {i + 1}")
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
                        self._planner.invalidate_turn_plan("bridge moved past plan step (stale-skip)")
                        self._notify_turn_plan(None)
                    except Exception as e:
                        logger.debug(f"invalidate_turn_plan on stale-skip failed: {e}")
                    self._state = AutopilotState.IDLE
                    return True

                # P1-6: "no-op, window closed" means the action did NOT
                # happen. Counting it executed let a coach-mandated lethal
                # attack silently evaporate on 2026-07-05 23:02:30 — the
                # no-op was "verified" by unrelated phase drift, marked
                # "Plan complete", and never re-attempted (and the false
                # completion fed a hallucinated "we attacked" into the next
                # prompt). Keep the turn-plan step pending (attack intent
                # survives for the next DeclareAttackers window) and yield.
                if click_result.error and "window closed" in click_result.error:
                    logger.warning(
                        f"Autopilot: {action.action_type.value} was a "
                        "window-closed no-op — not executed; keeping the plan "
                        "step pending and yielding to next cycle"
                    )
                    self._notify(
                        "AUTOPILOT",
                        f"Window closed before {action.action_type.value} — will retry",
                    )
                    self._state = AutopilotState.IDLE
                    return True

                self._actions_executed += 1
                self._last_exec_success_ts = time.time()
                # P0-9: plan-executed actions belong in the match packet too
                # (only typed decisions were recorded before — match 1 on
                # 2026-07-05 saved decisions=0 despite 8 submissions).
                try:
                    from arenamcp.match_packets import get_current_packet

                    _packet = get_current_packet()
                    if _packet:
                        _packet.add_executed_action(
                            action.action_type.value,
                            card_name=action.card_name or "",
                            detail=str(action)[:200],
                            turn=(game_state.get("turn") or {}).get("turn_number"),
                            path=str(getattr(click_result, "description", "") or ""),
                        )
                except Exception as e:
                    logger.debug(f"MatchPacket executed-action record failed: {e}")
                # Clear the persistent-failure counter for this action key
                # so a future failure starts counting from 0 (#231).
                self._reset_persistent_failure(action, game_state)

                # Livelock bookkeeping: count real submissions (not no-ops)
                # toward runaway protection, and remember the last cast so a
                # later rollback (PayCosts cancel / targeting escape) can be
                # attributed to it.
                result_src = click_result.error or ""
                is_real_submission = not any(k in result_src for k in ("stale-skip", "no-op", "intermission"))
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
                    if (
                        action.action_type in (ActionType.CAST_SPELL, ActionType.ACTIVATE_ABILITY)
                        and action.card_name
                    ):
                        # P0-6: activations roll back through PayCosts exactly
                        # like casts (Utter Insignificance activated 3x into
                        # an unpayable {C} on 2026-07-05 — no record, no
                        # strikes, livelock).
                        self._last_cast_submitted = (
                            pre_turn_num,
                            action.card_name.strip().lower(),
                        )
                        self._last_cast_submitted_ts = time.monotonic()
                        # P1-4: bot submissions explain own stack objects.
                        self._recent_bot_submissions.append(
                            (time.monotonic(), action.card_name.strip().lower())
                        )
                        del self._recent_bot_submissions[:-12]

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
                    # P1-7: the verified execution is the ONLY source of
                    # "Already executed this turn" prompt facts.
                    try:
                        self._planner.note_executed(action)
                    except Exception as e:
                        logger.debug(f"note_executed failed: {e}")
                    # P1-8: an execution matching a just-recorded "takeover"
                    # proves it was turn-counter lag — relabel in place so
                    # the match-end flush drops it.
                    try:
                        self._reclassify_matching_takeovers(action)
                    except Exception as e:
                        logger.debug(f"takeover reclassify failed: {e}")
                    try:
                        outcome = self._planner.advance_turn_plan(action)
                    except Exception as e:
                        logger.debug(f"advance_turn_plan failed: {e}")
                        outcome = "neutral"
                    if outcome == "advanced":
                        self._emit_turn_plan_payload()
                    elif outcome == "diverged" and self._planner.get_turn_plan_payload() is not None:
                        # A user-visible action matched no remaining step —
                        # real divergence. (P2-8: "neutral" outcomes — passes,
                        # cost payments, sub-decisions — used to land here and
                        # killed 6/6 match-2 turn plans within seconds.)
                        self._planner.invalidate_turn_plan("executed action diverged from plan")
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
                    f"Autopilot: resetting plan failure counter (was {self._consecutive_plan_failures})"
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
                        post_turn.get("priority_player") == post_local_seat and post_local_seat is not None
                    )
                    if has_priority:
                        post_legal = self._get_legal_actions(post_plan_state)
                        _PASSTHROUGH = {"pass", "action: activate_mana", "action: floatmana"}
                        post_meaningful = [a for a in post_legal if a.lower() not in _PASSTHROUGH]
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

    def _get_legal_actions(self, game_state: dict[str, Any]) -> list[str]:
        """Get legal actions from the rules engine."""
        try:
            from arenamcp.rules_engine import RulesEngine

            return RulesEngine.get_legal_actions(game_state)
        except Exception as e:
            logger.error(f"Failed to get legal actions: {e}")
            return []

    def _should_speak_pass_plan(self, game_state: dict[str, Any]) -> bool:
        """Decide whether this pass-only plan deserves a spoken explanation.

        - Forced pass (nothing legal beyond pass/mana abilities): never —
          there was no decision, so narrating it is pure noise.
        - Chosen pass: once per turn. The first "no useful responses,
          passing" of a turn is informative; the re-phrasings at every later
          priority window of the same turn are not.
        """
        legal = game_state.get("legal_actions") or []
        meaningful = [
            action
            for action in legal
            if str(action) != "Pass"
            and not str(action).startswith(("Action: Activate_Mana", "Action: FloatMana", "Wait"))
        ]
        if not meaningful:
            return False
        turn = (game_state.get("turn") or {}).get("turn_number") or 0
        if turn and turn == getattr(self, "_last_pass_speech_turn", -1):
            return False
        self._last_pass_speech_turn = turn
        return True

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

    def _notify_turn_plan(self, payload: dict[str, Any] | None) -> None:
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

    def _get_vision_coord(self, card_name: str, zone: str | None = None) -> ScreenCoord | None:
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
            if self._has_vision_scan and hasattr(self._mapper, "get_element_coord"):
                return self._mapper.get_element_coord(card_name, zone=zone, screenshot_bytes=png_bytes)

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

    # Interactive families served by the typed-decision path, including
    # Mulligan and — since Phase E — ActionsAvailable (priority windows),
    # which previously stayed on the legacy strategic path.
    _TYPED_DECISION_FAMILIES = frozenset(
        {"SelectTargets", "SelectN", "Search", "Mulligan", "Group", "ActionsAvailable"}
    )

    def _try_typed_decision_path(self, game_state: dict[str, Any], trigger: str) -> bool | None:
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
        from arenamcp.action_planner import DECLINE_DECISION

        if option_ids == [DECLINE_DECISION]:
            # Safe move is to not take this window at all (e.g. harmful
            # targeting whose only legal candidates are our own permanents).
            if decision.can_cancel and self._gre_bridge.cancel_action():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"typed-decision {decision.request_type}: declined (cancel)",
                )
                self._record_autopilot_decision(
                    game_state,
                    trigger,
                    action_type="decision",
                    summary=f"declined {decision.request_type} (own-permanents-only harmful targeting)",
                )
                self._state = AutopilotState.IDLE
                return True
            self._pause_for_manual(
                f"{decision.request_type}: harmful targeting is forced onto "
                "your own permanents — pick manually",
                game_state,
            )
            return True
        if not option_ids:
            return None

        labels = [(decision.find(oid).label if decision.find(oid) else oid) for oid in option_ids]
        if submit_option(self._gre_bridge, decision, option_ids):
            self._request_tracker.note_submitted(fp)
            for label in labels:
                clean_name = str(label or "").strip().lower()
                for prefix in ("cast ", "play land: ", "play ", "activate ability: ", "activate "):
                    if clean_name.startswith(prefix):
                        clean_name = clean_name[len(prefix):].strip()
                        break
                clean_name = clean_name.split("[")[0].strip()
                if clean_name:
                    self._recent_bot_submissions.append((time.monotonic(), clean_name))
            del self._recent_bot_submissions[:-24]
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
            self._last_exec_success_ts = time.time()
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
        if now - self._last_bridge_wait_failed_at < self._config.bridge_reconnect_wait_cooldown:
            return False
        self._notify(
            "AUTOPILOT",
            f"Bridge offline — waiting up to {wait_budget:.0f}s for the plugin to reconnect...",
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

    # --- State Verification ---

    def _verify_action(self, action: GameAction, pre_state: dict[str, Any]) -> bool:
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
        last_post_state: dict[str, Any] | None = None

        while time.time() < deadline:
            try:
                post_state = self._get_game_state()
                last_post_state = post_state

                post_bridge_state_id = int(post_state.get("_bridge_game_state_id", 0) or 0)
                if (
                    post_bridge_state_id
                    and pre_bridge_state_id
                    and post_bridge_state_id != pre_bridge_state_id
                ):
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
                    logger.info(
                        f"Action verified: global state changed ({pre_turn.get('phase')} -> {post_turn.get('phase')})"
                    )
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
                    pre_local = next(
                        (p.get("seat_id") for p in pre_state.get("players", []) if p.get("is_local")), None
                    )
                    post_local = next(
                        (p.get("seat_id") for p in post_state.get("players", []) if p.get("is_local")), None
                    )
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
                    if post_atk > pre_atk or (post_atk == 0 and pre_atk > 0):  # attacking finished
                        logger.info("Action verified: attackers declared")
                        return True

                if action.action_type == ActionType.DECLARE_BLOCKERS:
                    # Blocks don't change zone or phase — combat damage is the
                    # same step as Declare Blockers. The reliable signal is
                    # the bridge's pending request moving off DeclareBlockers
                    # (next step is usually ActionsAvailable for second main /
                    # combat damage triggers, or the phase advancing).
                    pre_bridge_class = pre_state.get("_bridge_request_class") or ""
                    post_bridge_class = post_state.get("_bridge_request_class") or ""
                    if "DeclareBlockers" in pre_bridge_class and "DeclareBlockers" not in post_bridge_class:
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
