"""Freshness scopes and bounded selection for retrospective commentary."""

import hashlib
import json
import re


def position_fingerprint(observation, scope=""):
    public = {
        **observation.public_state,
        "turn_info": {
            k: v
            for k, v in observation.public_state.get("turn_info", {}).items()
            if k not in {"phase", "step", "priority_player"}
        },
    }
    private = observation.private_state
    if scope:
        public = public.get("players", []) if scope == "players" else public.get("zones", {}).get(scope, [])
        private = {}
    return hashlib.sha256(
        json.dumps(
            [observation.epoch.match_id, observation.epoch.game_number, public, private],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:24]


def valid_for(item, observation):
    return bool(
        item.epoch == observation.epoch
        and (
            item.event_bound
            or (
                item.position_id == position_fingerprint(observation, item.position_scope)
                if item.position_id
                else item.snapshot_id == observation.snapshot_id
            )
        )
    )


def event_lifetime(kind):
    return {"deck_overview": 90, "hand_changed": 8, "outcome": 60}.get(kind, 30)


def event_priority(event, observation):
    base = {
        "deck_overview": 100,
        "game_end": 20,
        "outcome": 15,
        "combat": 10,
        "action_observed": 6,
        "spell_resolved": 5,
        "hand_changed": 5,
        "life_change": 4,
        "game_start": 2,
        "turn_start": 1,
        "land_drop": 0,
    }.get(event.kind, 0)
    if event.kind == "life_change":
        base += min(8, abs(event.payload.get("delta", 0)) if type(event.payload.get("delta")) is int else 0)
    if event.kind == "combat":
        base += min(5, event.payload.get("count", 0)) if type(event.payload.get("count")) is int else 0
    if event.kind in {"action_observed", "spell_resolved"}:
        gid = event.payload.get("grp_id")
        iid = event.payload.get("instance_id")
        for cards in observation.public_state.get("zones", {}).values():
            for card in cards:
                if (gid and card.get("grp_id") == gid) or (iid and card.get("instance_id") == iid):
                    symbols = re.findall(r"\{([^}]+)\}", str(card.get("mana_cost", "")))
                    return base + min(
                        6, sum(int(t) if t.isdigit() else int(t not in {"X", "Y", "Z"}) for t in symbols)
                    )
    return base


def select_events(events, observation, limit=4):
    # Keep the decisive events of a burst, not just the final routine land drop.
    ordered = sorted(events, key=lambda e: (event_priority(e, observation), e.received_at), reverse=True)
    chosen = ordered[:limit]
    return tuple(sorted(chosen, key=lambda e: e.received_at))


def historical_scope(scope, dependencies, text):
    # The audit must explicitly classify it, and immutable references alone
    # cannot license a future prediction or a claim about the current hand.
    return (
        scope == "historical"
        and not dependencies
        and not re.search(
            r"\b(?:now|currently|still|might|could|next|will|would|if|hand|waiting)\b", text, re.I
        )
    )
