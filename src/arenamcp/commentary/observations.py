"""Conservative public projection of a published Coach snapshot.

Call outside GameState's lock. Unknown fields, raw GRE data, action proposals,
hand/library contents and deck data never cross this boundary. Events must be
provided explicitly by their future parser adapter: disappearing from the stack
alone is not evidence that a spell resolved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from arenamcp.player_names import snapshot_names

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
_IDENTITY_FIELDS = ("grp_id", "name", "type_line", "mana_cost", "oracle_text")


def _scalars(value: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    # Do not copy nested values supplied under otherwise innocent field names.
    return {
        key: value[key] for key in fields if key in value and type(value[key]) in (str, int, bool, type(None))
    }


def generated_or_changed_print(card):
    return any(
        card.get(key)
        for key in ("token_creator_id", "is_copy", "copied_from_grp_id", "is_token", "generated")
    ) or any(card.get(key) not in (None, 0, card.get("grp_id")) for key in ("original_grp_id", "base_grp_id"))


def _face_down(card: Mapping[str, Any]) -> bool:
    return bool(
        card.get("face_down")
        or card.get("isFacedown")
        or card.get("face_down_reason")
        or card.get("grp_id") == 3
    )


def card_identity_visible(card: Mapping[str, Any], zone: str) -> bool:
    if _face_down(card) or card.get("identity_known") is False:
        return False
    visibility = card.get("visibility")
    return visibility in ("public", "Public", "Visibility_Public") or (zone != "exile" and visibility is None)


def _public_card(card: Mapping[str, Any], zone: str = "") -> dict[str, Any]:
    result = _scalars(card, _CARD_FIELDS)
    hidden = _face_down(card)
    result["face_down"] = hidden
    if not card_identity_visible(card, zone):
        result["name"] = "Face-down card" if hidden else "Unknown card"
        # Private printed characteristics must not reveal a face-down card.
        result.pop("power", None)
        result.pop("toughness", None)
    else:
        if zone == "exile":
            result["visibility"] = "public"
        result.update(_scalars(card, _IDENTITY_FIELDS))
        if isinstance(card.get("public_native_print"), str) and not generated_or_changed_print(card):
            from arenamcp.card_facts.source_identity import legacy_lookup_hash

            digest = legacy_lookup_hash(card)
            if (
                digest
                and type(card.get("grp_id")) is int
                and card["public_native_print"] == f"{card['grp_id']}:{digest}"
            ):
                result["public_native_print"] = card["public_native_print"]
        result.update(_scalars(card, ("object_kind",)))
        if generated_or_changed_print(card):
            result["generated"] = True
        if isinstance(card.get("card_types"), list):
            result["card_types"] = [t for t in card["card_types"][:12] if isinstance(t, str)]
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
    turn = snapshot.get("turn_info") or snapshot.get("turn") or {}
    zones = snapshot.get("zones") or {}
    public = {
        "player_names": snapshot_names(snapshot),
        "turn_info": _scalars(turn, ("turn_number", "active_player", "priority_player", "phase", "step")),
        "players": [
            _scalars(player, ("seat_id", "life_total", "is_local"))
            for player in snapshot.get("players", ())
            if isinstance(player, Mapping)
        ],
        "zones": {
            zone: [_public_card(card, zone) for card in zones.get(zone, ()) if isinstance(card, Mapping)]
            for zone in _PUBLIC_ZONES
        },
    }
    public_events = tuple(event for event in events if event.visibility == "public")
    if any(event.source_sequence > source_sequence for event in public_events):
        raise ValueError("Evidence cannot come from a future source sequence")
    return Observation(epoch, source_sequence, snapshot_id, arrived_at, public, public_events)
