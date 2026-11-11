"""Compact, versioned board snapshots attached to every recorded decision.

A match bundle records each decision's options, the choice and the plan text,
but not the board — so a decision could not be replayed (Fable Nov B1).
``build_decision_snapshot`` projects the very game_state dict the planner was
given — never a rebuild from logs — down to what a replay needs: the turn,
each player's life/counters/mana pool, every public zone's cards by instance
id with tapped/counter state, the stack, and the decision context.

Dropped on purpose (replay noise): raw GRE/commentary events, action history,
recent events, player names, timers and engine bookkeeping. Opposing hidden
cards stay hidden: the opponent's hand is a count and its library a count;
only already-visible library cards are materialized.

``game_state_from_snapshot`` rebuilds a planner-shaped game_state from a
snapshot alone. The nested ``zones`` payload aliases the top-level card lists
(``_build_public_zones`` hands back references), so the snapshot stores the
lists once at the top level plus the zone counts, and the restore re-aliases
them exactly as the server does.
"""

from __future__ import annotations

import copy
from typing import Any

#: Bump when the projection changes in a way that invalidates old snapshots.
DECISION_SNAPSHOT_VERSION = 1

# Top-level keys of the planner-facing game_state a replay needs. Everything
# else (recent/raw/commentary events, action history, names, timers, engine
# bookkeeping) is noise for a replay and is dropped.
_STATE_KEYS: tuple[str, ...] = (
    "match_id",
    "game_number",
    "event_id",
    "format_name",
    "turn",
    "players",
    "battlefield",
    "hand",
    "graveyard",
    "stack",
    "exile",
    "command",
    "local_seat_id",
    "opponent_seat_id",
    "controlled_turn",
    "decision_seat_id",
    "decision_context",
    "legal_actions",
    "legal_actions_raw",
    "pending_selection_objects",
    "_bridge_request_type",
    "_bridge_request_class",
    "_bridge_actions",
    "_bridge_can_pass",
    "_bridge_can_cancel",
    "_bridge_allow_undo",
    "_bridge_game_state_id",
    "hand_complete",
    "hand_count",
    "deck_cards",
    "damage_taken",
    "designations",
    "dungeon_status",
    "commander_grp_ids",
    "commander_casts",
)

# zones aliases these top-level card lists; storing them twice would double
# every snapshot. They are rebuilt by game_state_from_snapshot().
_ZONE_ALIAS_KEYS = frozenset({"battlefield", "my_hand", "hand", "graveyard", "stack", "exile", "command"})

# Raw published snapshots spell the turn block ``turn_info``; the planner-facing
# state spells it ``turn``. Normalize to ``turn``.
_TURN_FALLBACK_KEY = "turn_info"


def build_decision_snapshot(game_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project the planner's game_state into a compact replayable snapshot.

    Returns None when there is no usable game_state; never raises.
    """
    if not isinstance(game_state, dict) or not game_state:
        return None
    try:
        return _build(game_state)
    except Exception:
        # A snapshot must never break decision recording.
        return None


def _build(game_state: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for key in _STATE_KEYS:
        if key in game_state:
            state[key] = copy.deepcopy(game_state[key])
    if "turn" not in state:
        turn = game_state.get(_TURN_FALLBACK_KEY)
        if isinstance(turn, dict):
            state["turn"] = copy.deepcopy(turn)

    zones = game_state.get("zones")
    if isinstance(zones, dict):
        zone_meta: dict[str, Any] = {}
        aliases: list[str] = []
        for key, value in zones.items():
            if key in _ZONE_ALIAS_KEYS:
                # Hoist a card list only when the top-level spelling is absent.
                top_key = "hand" if key in ("my_hand", "hand") else key
                if top_key not in state and isinstance(value, list):
                    state[top_key] = copy.deepcopy(value)
                if isinstance(value, list):
                    aliases.append(key)
            else:
                zone_meta[key] = copy.deepcopy(value)
        if zone_meta or aliases:
            state["zones"] = zone_meta
        # Which alias keys the original zones carried (possibly none), so the
        # restore can reproduce it exactly: the server's zones always carry
        # them; some internal shapes carry only the counts.
        state["_zone_aliases"] = sorted(set(aliases))

    return {"version": DECISION_SNAPSHOT_VERSION, "state": state}


def game_state_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Rebuild a planner-shaped game_state from a snapshot alone.

    The nested ``zones`` payload is re-aliased to the restored card lists,
    mirroring ``server._build_public_zones``.
    """
    state = copy.deepcopy((snapshot or {}).get("state") or {})
    aliases = state.pop("_zone_aliases", None)
    zones = dict(state.pop("zones") or {})
    if aliases is None:
        # The server's public zones always alias the card lists.
        aliases = ["battlefield", "command", "exile", "graveyard", "hand", "my_hand", "stack"]
    hand = state.get("hand") or []
    for key in aliases:
        if key in ("my_hand", "hand"):
            zones.setdefault("my_hand", hand)
            zones.setdefault("hand", hand)
        else:
            zones.setdefault(key, state.get(key) or [])
    state["zones"] = zones
    return state
