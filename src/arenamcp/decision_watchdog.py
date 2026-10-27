"""Decision watchdog: no request of ours may sit unanswered while autoplay is on.

The autopilot only acts when a trigger reaches it. On 2026-10-18 at 18:35:41
a Search (Kodama of the West Tree's land fetch) was pending in the bridge,
the autopilot yielded once to "wait for Arena to reveal the search
choices", and no trigger ever came back: Player.log had summarized the
GreToClientEvent, the poller saw no change, and the backstop keyed on the
log's (empty) pending decision. Two and a half minutes later Arena's
inactivity timer ended the game (ResultReason_Timeout) without a single
MANUAL REQUIRED.

This module decides, from the bridge's live request alone, when to step in:

1. ``REPLAN_AFTER_S`` without an answer: force a re-plan (the autopilot
   gets the window again, whatever the triggers did).
2. ``SAFE_DEFAULT_AFTER_S`` without an answer, or Arena's clock down to
   ``CLOCK_MARGIN_S``: submit a safe legal default for the request type.
   A window already handed to the player (MANUAL REQUIRED, or standing by
   after their own play) waits for the clock margin, or
   ``HANDOFF_SAFE_DEFAULT_AFTER_S`` when the clock is unknown.
3. No safe default, or it did not take: alert loudly (spoken + desktop),
   repeating every ``REALERT_EVERY_S`` while the request stays pending.

Any answer sent to Arena (``GREBridge.last_submission_at``) restarts the
count; a different request (type, class, game state / message id, offered
choices) starts a new one. Combat declarations with a known turn window keep
their deadline across request-ID churn (#237); safe defaults still read the
fresh request and board. The clock is injected, so tests need no waits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def request_key(resp: dict[str, Any] | None, *, window: tuple | None = None) -> tuple | None:
    """Identity of a pending bridge request, or None when nothing is pending."""
    if not isinstance(resp, dict) or not resp.get("has_pending"):
        return None
    from arenamcp.gre_bridge import BridgeDecisionPoller

    kind = str(resp.get("request_type") or "") + str(resp.get("request_class") or "")
    if window is not None and ("DeclareAttack" in kind or "DeclareBlock" in kind):
        # Re-capturing the same combat with a fresh gsid must not give a
        # cancelled planner an endlessly renewed watchdog budget. This key
        # only controls escalation; it never authorizes an old submission.
        return (resp.get("request_type"), resp.get("request_class"), "combat", window)
    return (
        resp.get("request_type"),
        resp.get("request_class"),
        resp.get("game_state_id"),
        resp.get("msg_id"),
        BridgeDecisionPoller._compute_request_sig(resp),
    )


def request_label(resp: dict[str, Any] | None) -> str:
    """Readable name of a pending request ("Search Library")."""
    if not isinstance(resp, dict):
        return "request"
    from arenamcp.gre_bridge import _get_bridge_request_label

    return _get_bridge_request_label(resp.get("request_type"), resp.get("request_class"))


@dataclass(frozen=True)
class WatchdogStep:
    """One thing the watchdog wants done now."""

    action: str  # "replan" | "safe_default" | "alert"
    label: str
    age_s: float
    remaining_s: float | None

    def describe(self) -> str:
        clock = f", {self.remaining_s:.0f}s left on Arena's clock" if self.remaining_s is not None else ""
        return f"{self.label} pending {self.age_s:.1f}s with no answer{clock}"


class DecisionWatchdog:
    """Escalates an unanswered pending request: re-plan, safe default, loud alert."""

    REPLAN_AFTER_S = 12.0
    SAFE_DEFAULT_AFTER_S = 30.0
    HANDOFF_SAFE_DEFAULT_AFTER_S = 90.0
    # Arena's shortest deadline seen live is the 45 s non-active rope; the
    # inactivity timer that loses the game is 150 s.
    CLOCK_MARGIN_S = 20.0
    # A safe default reported as submitted must change the request by then.
    VERIFY_AFTER_S = 4.0
    REALERT_EVERY_S = 15.0

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._key: tuple | None = None
        self._since = 0.0
        self.first_seen_wall = 0.0
        self._replanned = False
        self._defaulted_at: float | None = None
        self._default_failed = False
        self._alerted_at: float | None = None

    def reset(self) -> None:
        self._key = None
        self._replanned = False
        self._defaulted_at = None
        self._default_failed = False
        self._alerted_at = None

    def may_act(
        self,
        resp: dict[str, Any] | None,
        *,
        window: tuple | None = None,
        last_answer_at: float = 0.0,
        clock_remaining_s: float | None = None,
    ) -> bool:
        """Whether ``step`` could ask for anything now (lets callers skip costly inputs)."""
        if request_key(resp, window=window) != self._key or self._key is None:
            return False
        if self._defaulted_at is not None:
            return True
        if clock_remaining_s is not None and clock_remaining_s <= self.CLOCK_MARGIN_S:
            return True
        return self._now() - max(self._since, float(last_answer_at or 0.0)) >= self.REPLAN_AFTER_S

    def step(
        self,
        resp: dict[str, Any] | None,
        *,
        window: tuple | None = None,
        last_answer_at: float = 0.0,
        clock_remaining_s: float | None = None,
        handed_to_user: bool = False,
        wall_now: float | None = None,
    ) -> WatchdogStep | None:
        """What to do about ``resp`` (the bridge's live request) right now, if anything.

        ``last_answer_at`` is on the injected monotonic clock;
        ``clock_remaining_s`` is Arena's time left for this request (None when
        unknown); ``handed_to_user`` means the autopilot already told the
        player to act on this window.
        """
        now = self._now()
        key = request_key(resp, window=window)
        if key is None:
            self.reset()
            return None
        if key != self._key:
            self.reset()
            self._key = key
            self._since = now
            self.first_seen_wall = time.time() if wall_now is None else wall_now
            return None
        age = now - max(self._since, float(last_answer_at or 0.0))
        urgent = clock_remaining_s is not None and clock_remaining_s <= self.CLOCK_MARGIN_S
        label = request_label(resp)

        def made(action: str) -> WatchdogStep:
            return WatchdogStep(action, label, max(0.0, age), clock_remaining_s)

        if self._defaulted_at is None:
            if not self._replanned and not handed_to_user and not urgent and age >= self.REPLAN_AFTER_S:
                self._replanned = True
                return made("replan")
            limit = self.HANDOFF_SAFE_DEFAULT_AFTER_S if handed_to_user else self.SAFE_DEFAULT_AFTER_S
            if urgent or age >= limit:
                self._defaulted_at = now
                return made("safe_default")
            return None
        if not self._default_failed and now - self._defaulted_at < self.VERIFY_AFTER_S:
            return None
        if self._alerted_at is None or now - self._alerted_at >= self.REALERT_EVERY_S:
            self._alerted_at = now
            return made("alert")
        return None

    def note_safe_default(self, submitted: bool) -> None:
        """Record the outcome of the safe default the last step asked for."""
        self._default_failed = not submitted

    def retry_safe_default(self) -> None:
        """The safe default could not run yet (engine busy): ask again on the next step."""
        self._defaulted_at = None
