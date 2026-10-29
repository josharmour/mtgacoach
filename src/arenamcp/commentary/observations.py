"""Conservative public projection of a published Coach snapshot.

Call outside GameState's lock. Unknown fields, raw GRE data, action proposals,
hand/library contents and deck data never cross this boundary. Events must be
provided explicitly by their future parser adapter: disappearing from the stack
alone is not evidence that a spell resolved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import Epoch, Evidence, Observation

_PUBLIC_ZONES = ("battlefield", "stack", "graveyard", "exile", "command")
_CARD_FIELDS = (
    "instance_id",
    "controller_seat_id",
    "owner_seat_id",
    "tapped",
    "is_tapped",
    "power",
    "toughness",
    "is_attacking",
    "is_blocking",
    "is_blocked",
    "attack_target_id",
    "damage",
)
_IDENTITY_FIELDS = ("grp_id", "name")


def _scalars(value: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    # Do not copy nested values supplied under otherwise innocent field names.
    return {
        key: value[key] for key in fields if key in value and type(value[key]) in (str, int, bool, type(None))
    }


def _public_card(card: Mapping[str, Any]) -> dict[str, Any]:
    result = _scalars(card, _CARD_FIELDS)
    hidden = bool(card.get("face_down") or card.get("isFacedown") or card.get("grp_id") == 3)
    result["face_down"] = hidden
    private = card.get("visibility") not in (None, "public", "Public", "Visibility_Public")
    if hidden or private:
        result["name"] = "Face-down card" if hidden else "Unknown card"
        # Private printed characteristics must not reveal a face-down card.
        result.pop("power", None)
        result.pop("toughness", None)
    else:
        result.update(_scalars(card, _IDENTITY_FIELDS))
    return result


def observation_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    source_sequence: int,
    snapshot_id: str,
    arrived_at: float,
    events: Iterable[Evidence] = (),
) -> Observation:
    epoch = Epoch(snapshot.get("match_id"), snapshot.get("game_number"))
    turn = snapshot.get("turn_info") or {}
    zones = snapshot.get("zones") or {}
    public = {
        "turn_info": _scalars(turn, ("turn_number", "active_player", "priority_player", "phase", "step")),
        "players": [
            _scalars(player, ("seat_id", "life_total", "is_local"))
            for player in snapshot.get("players", ())
            if isinstance(player, Mapping)
        ],
        "zones": {
            zone: [_public_card(card) for card in zones.get(zone, ()) if isinstance(card, Mapping)]
            for zone in _PUBLIC_ZONES
        },
    }
    public_events = tuple(event for event in events if event.visibility == "public")
    if any(event.source_sequence > source_sequence for event in public_events):
        raise ValueError("Evidence cannot come from a future source sequence")
    return Observation(epoch, source_sequence, snapshot_id, arrived_at, public, public_events)
