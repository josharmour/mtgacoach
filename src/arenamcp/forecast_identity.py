"""Cheap snapshot identity shared by forecast publishers and the desktop.

No clock, animation tick or connection heartbeat can make old advice current.
The digest covers the decision and observed cards, including intra-turn changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def forecast_key(state: dict[str, Any] | None) -> str:
    if not state or not state.get("match_id"):
        return ""
    turn = state.get("turn") or {}
    data = {
        key: state.get(key)
        for key in (
            "match_id",
            "game_number",
            "local_seat_id",
            "battlefield",
            "hand",
            "stack",
            "graveyard",
            "exile",
            "command",
            "pending_decision",
            "decision_seat_id",
            "legal_actions",
            "decision_context",
            "game_over",
            "result",
        )
    }
    data["turn"] = {
        key: turn.get(key) for key in ("turn_number", "active_player", "priority_player", "phase", "step")
    }
    data["players"] = [
        {
            key: player.get(key)
            for key in (
                "seat_id",
                "life_total",
                "mana_pool",
                "poison_counters",
                "counters",
                "hand_count",
                "library_count",
            )
        }
        for player in state.get("players") or []
        if isinstance(player, dict)
    ]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:24]
