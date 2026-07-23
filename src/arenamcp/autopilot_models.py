"""Autopilot enums and config dataclasses extracted from autopilot.py (pure move, no behavior change)."""

from dataclasses import dataclass
from enum import Enum


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
