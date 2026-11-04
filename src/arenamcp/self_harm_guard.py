"""Grounded checks for lethal friendly damage; model explanations are not facts.

Deliberately limited to resolved fixed damage and known creature toughness.
Unknown prevention/replacement effects are not treated as a proven death.
"""

from __future__ import annotations

import re

from arenamcp.play_safety import damage_effect, death_trigger_benefit, lethal_damage


def lethal_friendly_damage(state, oracle, chosen, *, kicked=False, justifications=None):
    """Map unsafe target IDs to facts, allowing supported, intentional trigger payoffs."""
    parsed = damage_effect({"oracle_text": oracle}, kicked=kicked)
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    if not parsed or parsed[1] not in {"any", "target"} or local is None:
        return {}
    board = [c for c in state.get("battlefield", []) if isinstance(c, dict)]
    unsafe = {}
    for card in board:
        oid = f"tgt:{card.get('instance_id')}"
        if oid not in chosen or card.get("controller_seat_id") != local:
            continue
        needed = lethal_damage(card)
        if needed is None or parsed[0] < needed:
            continue
        # Do not invent certainty when supplied abilities can prevent/replace damage.
        rules = " ".join(str(c.get("oracle_text") or "") for c in board).lower()
        if re.search(r"\bprevent\b|\bprotection from\b|\binstead\b", rules):
            continue
        counters = card.get("counters") or {}
        if card.get("shield_counters") or (isinstance(counters, dict) and counters.get("shield", 0)):
            continue
        reason = str((justifications or {}).get(oid) or "").lower()
        # A genuine trigger plus explicit intent can justify killing our creature.
        death = death_trigger_benefit(card)
        damage_trigger = re.search(
            r"whenever (?:this creature|"
            + re.escape(str(card.get("name", "")).lower())
            + r") is dealt damage[^.]*",
            str(card.get("oracle_text") or "").lower(),
        )
        if (death and re.search(r"\bdies|\bdeath|\bundying|\bpersist|\bafterlife", reason)) or (
            damage_trigger and re.search(r"\btrigger|\benrage|\bdealt damage", reason)
        ):
            continue
        unsafe[oid] = (
            f"{parsed[0]} damage is lethal to our {card.get('name', oid)} "
            f"({card.get('power', '?')}/{card.get('modified_toughness', card.get('toughness'))}); "
            "no supported intentional damage/death payoff"
        )
    return unsafe


def false_power_survival_claim(state, explanation):
    """A narrow contradiction: +N/+0 cannot save a known Shock-sized victim.

    Requires explicit stack target links and a power-only trigger in supplied rules.
    Negative/conditional clauses are not proof of a false claim.
    """
    for card in state.get("battlefield", []) or []:
        if card.get("controller_seat_id") != state.get("local_seat_id"):
            continue
        rules = str(card.get("oracle_text") or "").lower()
        if not re.search(r"gets? \+\d+/\+0", rules) or "noncreature spell" not in rules:
            continue
        if lethal_damage(card) is None:
            continue
        threatened = any(
            card.get("instance_id") in (spell.get("targeting") or [])
            and spell.get("controller_seat_id") not in (None, state.get("local_seat_id"))
            and (damage := damage_effect(spell)) is not None
            and damage[0] >= lethal_damage(card)
            for spell in state.get("stack", []) or []
        )
        if not threatened:
            continue
        for clause in re.split(r"[.;!?]", str(explanation or "").lower()):
            if re.search(
                r"\b(?:not|no|never|cannot|can't|doesn't|won't|still dies|regardless|if|unless)\b", clause
            ):
                continue
            if (
                str(card.get("name") or "").lower().split(" ")[0] in clause
                and re.search(r"\b(?:surviv\w*|sav\w*|protect\w*)\b", clause)
                and re.search(r"\bpump\w*\b|\bprowess\b|\+\d+/\+0", clause)
            ):
                return (
                    f"{card['name']}'s +power/+0 trigger does not increase toughness or prevent lethal damage"
                )
    return ""
