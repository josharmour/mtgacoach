"""Protection from a chosen player, grounded in GRE's persistent player link.

The choice is attached to the object, not its current controller. Ability IDs
are from Arena's card database: 90240 chooses a player on entry, and 101226 /
45307 grant protection from that chosen player (True-Name Nemesis).
"""

from __future__ import annotations

PLAYER_PROTECTION_ABILITIES = {90240: frozenset({101226, 45307})}


def active_player_protection(links: dict, ability_ids: list[int]) -> list[int]:
    abilities = set(ability_ids)
    return sorted(
        {
            int(player)
            for choice, player in links.items()
            if PLAYER_PROTECTION_ABILITIES.get(int(choice), frozenset()) & abilities and int(player) > 0
        }
    )


def source_controller(card: dict) -> int | None:
    """Current controller of a source, including a projected spell/ETB mode."""
    inner = card.get("_card")
    for candidate in (card, inner if isinstance(inner, dict) else {}):
        for key in ("_source_controller_seat_id", "controller_seat_id"):
            if candidate.get(key):
                return int(candidate[key])
    # Missing control is unknown, not proof the source is uncontrolled.
    # Ownership alone cannot establish whose protection applies to a permanent.
    return None


def protected_players(card: dict) -> tuple[int, ...]:
    inner = card.get("_card")
    value = card.get("protection_from_players")
    if value is None and isinstance(inner, dict):
        value = inner.get("protection_from_players")
    return tuple(sorted(int(player) for player in (value or ()) if int(player) > 0))


def protected_from_player_source(protected: dict, source: dict) -> bool:
    players = protected_players(protected)
    if not players:
        return False
    controller = source_controller(source)
    return controller is not None and controller in players
