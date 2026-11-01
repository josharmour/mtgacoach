"""Bounded, controller-scoped prohibitions on casting outside the hand (#329).

Reads current rules, not card names. Does not treat putting a card onto the
battlefield as casting, or infer a ban merely from an absent Arena action.
"""

from __future__ import annotations

import re

from arenamcp.combat_strategy import _rules_text

_OUTSIDE_HAND = re.compile(
    r"^(your opponents|you|players|each player) can't cast spells from anywhere other than "
    r"(their hands|your hand)\.?$",
    re.MULTILINE,
)


def outside_hand_ban(card: dict, *, ours: bool | None) -> bool:
    raw = card.get("_card") or card
    if card.get("is_phased_out") or raw.get("is_phased_out"):
        return False
    # Removed-ability identities aren't resolved here; don't reintroduce printed
    # restrictions when the snapshot reports ability removal.
    if card.get("removed_abilities") or raw.get("removed_abilities"):
        return False
    for match in _OUTSIDE_HAND.finditer(_rules_text(raw).replace("’", "'").strip()):
        who, hand = match.groups()
        if raw.get("controller_seat_id") is None and who in ("you", "your opponents"):
            continue
        if who == "your opponents" and hand == "their hands" and ours is False:
            return True
        if who == "you" and hand == "your hand" and ours is True:
            return True
        if who in ("players", "each player") and hand == "their hands":
            return True
    return False


def zone_blockers(battlefield, local: int | None, zone: str) -> tuple[str, ...]:
    if zone == "hand":
        return ()
    result = []
    for card in battlefield:
        if not isinstance(card, dict):
            continue
        controller = card.get("controller_seat_id")
        ours = controller == local if controller is not None and local is not None else None
        if outside_hand_ban(card, ours=ours):
            result.append(str(card.get("name") or "a permanent"))
    return tuple(result)
