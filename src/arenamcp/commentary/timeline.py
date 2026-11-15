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
    if scope.startswith("audit:"):
        from .audited_facts import scope_keys, value

        public = [value(observation, key) for key in scope_keys(scope)]
        private = {}
    elif scope.startswith("facts:"):
        parts = set(scope.removeprefix("facts:").split(","))
        # Exact full components, not relaxed predicates. Only known audited
        # dependency sets may select this scope; unknown facts keep full scope.
        public = {
            "identity": {
                "local_seat_id": observation.public_state.get("local_seat_id"),
                "player_names": observation.public_state.get("player_names", {}),
                "seats": [p.get("seat_id") for p in observation.public_state.get("players", [])],
            },
            **({"players": observation.public_state.get("players", [])} if "players" in parts else {}),
            **({"zones": observation.public_state.get("zones", {})} if "zones" in parts else {}),
            **(
                {"battlefield": observation.public_state.get("zones", {}).get("battlefield", [])}
                if "battlefield" in parts
                else {}
            ),
        }
        private = observation.private_state if "private" in parts else {}
    elif scope.startswith("cards:"):
        ids = {int(i) for i in scope.removeprefix("cards:").split(",")}
        public = {
            zone: [c for c in cards if c.get("instance_id") in ids]
            for zone, cards in public.get("zones", {}).items()
        }
        private = {"hand": [c for c in private.get("hand", []) if c.get("instance_id") in ids]}
    elif scope in {"strategy:creature_presence", "strategy:land_counts"}:
        # These scopes are only issued for the bounded deterministic claims in
        # strategy.py. Do not use them for arbitrary model-authored analysis.
        # Life, stack, tap/counter and private-hand updates cannot falsify those
        # claims, but unknown identities, control/type changes and lost boards can.
        from arenamcp.player_names import actor_name

        board = observation.public_state.get("zones", {}).get("battlefield", [])
        players = observation.public_state.get("players", [])
        kind = "creature" if scope.endswith("creature_presence") else "land"
        public = {
            "known": all(not c.get("face_down") and c.get("type_line") for c in board),
            "players": [
                (
                    p.get("seat_id"),
                    actor_name(observation.public_state, p.get("seat_id")),
                    sum(
                        c.get("controller_seat_id") == p.get("seat_id")
                        and bool(c.get("name"))
                        and not c.get("face_down")
                        and kind in c.get("type_line", "").lower()
                        for c in board
                    ),
                )
                for p in players
            ],
        }
        if kind == "creature":
            public["players"] = [(seat, name, bool(count)) for seat, name, count in public["players"]]
        private = {}
    elif scope == "public":
        # Audit excludes turn-dependent claims for this scope. A phase/turn
        # transition alone does not erase an unchanged public engine.
        public.pop("turn_info", None)
        private = {}
    elif scope:
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
