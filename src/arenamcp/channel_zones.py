"""Ground Channel plans in the card's current zone (ledger #303)."""

import re

_CHANNEL = re.compile(r"\bchannel\s*[—–-]", re.IGNORECASE)


def has_channel(card: dict) -> bool:
    return bool(_CHANNEL.search(str(card.get("oracle_text") or "")))


def channel_land_note(card: dict) -> str:
    if not has_channel(card):
        return ""
    return (
        " [Playing this land moves it out of hand: its Channel ability requires "
        "discarding it from hand and is unavailable on the battlefield.]"
    )


def battlefield_channel_note(state: dict) -> str:
    local = state.get("local_seat_id")
    if local is None:
        local = next((p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None)
    if local is None:
        return ""
    names = sorted(
        {
            str(card["name"])
            for card in state.get("battlefield", []) or []
            if card.get("name") and card.get("controller_seat_id") == local and has_channel(card)
        }
    )
    if not names:
        return ""
    return (
        "CHANNEL ZONES: "
        + ", ".join(names)
        + " are on your battlefield. Those battlefield copies cannot be discarded from hand "
        "to activate Channel. A separate copy in hand may still be channeled; check its cost and targets."
    )
