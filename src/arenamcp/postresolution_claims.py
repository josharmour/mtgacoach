"""Recheck named death claims against the same surviving damage-event target."""

import re

from arenamcp.mana import get_local_seat_id


def surviving_damage_targets(state: dict, reasoning: str) -> tuple[str, ...]:
    """Narrow factual contradictions, not a general inference that removal resolved."""
    if state.get("stack"):
        return ()
    turn = (state.get("turn") or {}).get("turn_number")
    if turn is None:
        return ()
    hit = {
        e.get("target_id")
        for e in state.get("recent_events") or []
        if e.get("type") == "damage_dealt"
        and e.get("turn") == turn
        and type(e.get("amount")) is int
        and e["amount"] > 0
    }
    local = get_local_seat_id(state)
    if not hit or local is None:
        return ()
    board = state.get("battlefield") or []
    text = str(reasoning or "").replace("’", "'")
    facts = []
    for target in board:
        if (
            target.get("instance_id") not in hit
            or target.get("controller_seat_id") in (None, local)
            or target.get("is_phased_out")
        ):
            continue
        damage, toughness = target.get("damage"), target.get("toughness")
        if type(damage) is not int or type(toughness) is not int or not 0 <= damage < toughness:
            continue
        types = target.get("card_types")
        creature = (
            "CardType_Creature" in types
            if isinstance(types, list)
            else "creature" in str(target.get("type_line") or "").lower()
        )
        if not creature:
            continue
        name = str(target.get("name") or "")
        if not name:
            continue
        # A namesake elsewhere may be the object that died. Only a unique current
        # name (and unique shorthand when used) can support this narrow correction.
        if sum(str(c.get("name") or "").casefold() == name.casefold() for c in board) != 1:
            continue
        aliases = [name]
        prefix = name.split()[0]
        if (
            len(prefix) >= 4
            and sum(str(c.get("name") or "").casefold().startswith(prefix.casefold()) for c in board) == 1
        ):
            aliases.append(prefix)
        for alias in aliases:
            claim = re.search(
                r"\b"
                + re.escape(alias)
                + r"(?:\s*\([^)]*\))?\s+(?:is\s+(?:already\s+)?dead|has\s+(?:already\s+)?died|was\s+(?:already\s+)?killed)\b",
                text,
                re.I,
            )
            if not claim:
                continue
            prior = re.split(r"[.;!?]", text[: claim.start()])[-1]
            if re.search(r"\b(?:not|can't|cannot|don't|if|unless|whether)\b", prior, re.I):
                continue
            facts.append(
                f"{name} #{target['instance_id']} is still on the battlefield after the recorded damage event, with {damage} damage on {toughness} toughness. Reconsider the decision with that surviving creature; do not treat it as removed."
            )
            break
    return tuple(facts)


def false_activation_target_kills(
    decision, state: dict, chosen: list[str], reasoning: str, source_id: int, oracle: str = ""
) -> tuple[str, ...]:
    from arenamcp.activation_damage import target_activation_damage
    from arenamcp.play_safety import claimed_kills, damage_would_kill, named_burn_damage, our_stack_damage

    if decision.request_type != "SelectTargets":
        return ()
    parsed = target_activation_damage(state, source_id, oracle)
    if parsed is None:
        return ()
    local = get_local_seat_id(state)
    opponents = [
        c for c in state.get("battlefield") or [] if c.get("controller_seat_id") not in (None, local)
    ]
    named = claimed_kills(reasoning, opponents)
    selected = {str(c).removeprefix("tgt:") for c in chosen if str(c).startswith("tgt:")}
    pending = our_stack_damage(state, exclude=source_id).by_target
    followup = named_burn_damage(reasoning, state, {"name": decision.source_label, "instance_id": source_id})
    return tuple(
        f"The current creature count after the activation's cost gives {parsed[0]} damage. "
        f"That does not kill {c.get('name')} #{c['instance_id']} with its current {c.get('damage', 0)} marked damage and {c.get('toughness')} toughness. "
        "Reconsider the target or state the concrete nonkill benefit; responses can change the count before resolution."
        for c in named
        if str(c.get("instance_id")) in selected
        and damage_would_kill(c, parsed[0] + pending.get(c.get("instance_id"), 0) + followup) is False
    )
