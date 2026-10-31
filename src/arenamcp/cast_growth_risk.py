"""Visible opposing counters triggered by our casts (#334)."""

import re


def per_cast_growth(card):
    raw = card.get("_card") or card
    text = str(raw.get("oracle_text") or "").lower()
    name = str(raw.get("name") or card.get("name") or "").lower()
    if name:
        text = text.replace(name, "this creature")
    match = re.search(
        r"whenever (?:an|one of your) opponents? casts (?:a|any) spell, (?:you may )?put (a|one|two|\d+) \+1/\+1 counters? on (?:this creature|it|~|cardname)\.",
        text,
    )
    if not match:
        return 0
    return {"a": 1, "one": 1, "two": 2}.get(match[1], int(match[1]) if match[1].isdigit() else 0)


def grown(card, casts=1):
    amount = per_cast_growth(card) * casts
    if not amount:
        return card
    result = dict(card)
    for field in ("power", "toughness", "modified_power", "modified_toughness"):
        if type(result.get(field)) is int:
            result[field] += amount
    return result


def before_targeting_cast(state, source_id, card):
    # A spell already on the stack has already triggered this effect. Pending
    # opposing abilities are separately treated as contested by stack damage.
    if any(c.get("instance_id") == source_id for c in state.get("stack", [])):
        return card
    source = next((c for c in state.get("hand", []) if c.get("instance_id") == source_id), None)
    local = state.get("local_seat_id")
    if source and local is not None and card.get("controller_seat_id") not in (None, local):
        return grown(card)
    return card
