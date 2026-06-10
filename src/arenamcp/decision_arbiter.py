"""Single source of truth for "is there a decision to act on right now?".

Bridge-authoritative arbitration (fable-improvements.md, item 4):

- When the GRE bridge is CONNECTED, its pending request is THE decision.
  A connected, idle bridge means there is no decision — no matter what
  stale log parsing left in the snapshot. Planning, advising, or speaking
  against a log-derived "decision" while the bridge is idle produced the
  2026-06-09 ghost-decision spirals (LLM call + repeated TTS every ~2s
  against a window the client had already consumed).

- When the bridge is DOWN, log-derived state is the only source we have,
  and arbitration falls back to it unchanged.

Every consumer (autopilot entry, the coaching-loop backstop, decision
advice) must route through :func:`arbitrate` rather than poking at
``_bridge_*`` / ``pending_decision`` fields directly, so they can never
disagree about whether a decision exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbitratedDecision:
    """The canonical "something needs deciding" summary."""

    source: str  # "bridge" | "log"
    request_type: str  # bridge request type/class, or log decision label
    pending_decision: Optional[str]
    decision_type: str  # decision_context "type" field ("" when absent)


def _bridge_connected(game_state: dict[str, Any]) -> bool:
    return bool(
        game_state.get("_bridge_connected") or game_state.get("bridge_connected")
    )


def _bridge_has_pending(game_state: dict[str, Any]) -> bool:
    bridge_trigger = game_state.get("_bridge_trigger") or {}
    return bool(
        game_state.get("_bridge_has_pending")
        or game_state.get("_bridge_request_type")
        or game_state.get("_bridge_request_class")
        or game_state.get("bridge_pending_interaction")
        or bridge_trigger.get("has_pending")
    )


def arbitrate(
    game_state: dict[str, Any],
    *,
    bridge_connected: Optional[bool] = None,
) -> Optional[ArbitratedDecision]:
    """Return the canonical pending decision, or None if nothing needs us.

    Args:
        game_state: snapshot (with the bridge poller's ``_bridge_*`` overlay
            when the bridge is up).
        bridge_connected: explicit connectivity override for callers that
            know better than the snapshot (e.g. they hold the live bridge
            object). When None, the snapshot fields decide.
    """
    connected = (
        bridge_connected
        if bridge_connected is not None
        else _bridge_connected(game_state)
    )
    ctx = game_state.get("decision_context") or {}
    decision_type = str(ctx.get("type") or "")

    if connected:
        if not _bridge_has_pending(game_state):
            # Connected and idle → there is no decision. Full stop.
            return None
        return ArbitratedDecision(
            source="bridge",
            request_type=str(
                game_state.get("_bridge_request_type")
                or game_state.get("_bridge_request_class")
                or game_state.get("pending_decision")
                or ""
            ),
            pending_decision=game_state.get("pending_decision"),
            decision_type=decision_type,
        )

    # Bridge down — log-derived state is all we have.
    pending = game_state.get("pending_decision")
    if not pending and not game_state.get("legal_actions"):
        return None
    return ArbitratedDecision(
        source="log",
        request_type=str(pending or "actions_available"),
        pending_decision=pending,
        decision_type=decision_type,
    )
