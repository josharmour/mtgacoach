"""Compare a creature's printed entry-damage budget with current opposing bodies."""

import re

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import claimed_kills, find_source, lethal_damage, named_burn_damage


def _printed_budget(card):
    text = str(card.get("oracle_text") or "")
    if not text or " damage" not in text.lower():
        return None
    name = re.escape(str(card.get("name") or "CARDNAME"))
    subject = rf"(?:this creature|this permanent|cardname|{name})"
    clause = re.compile(
        rf"when {subject} enters(?: the battlefield)?, (?:it|{subject}) deals (\d+) damage "
        r"(?:divided as you choose among any number of target creatures(?: and/or planeswalkers)?"
        r"|to (?:any target|target creature(?: or planeswalker)?))\.",
        re.I,
    )
    matches = [m for line in text.splitlines() if (m := clause.fullmatch(line.strip()))]
    return int(matches[0][1]) if len(matches) == 1 else None


def _targets(state):
    local = get_local_seat_id(state)
    return [
        c
        for c in state.get("battlefield") or []
        if local
        and c.get("controller_seat_id") not in (None, local)
        and not c.get("is_phased_out")
        and is_creature_now(c)
        and (not c.get("damaged_this_turn") or type(c.get("damage")) is int)
    ]


def entry_damage_note(card, state):
    amount = _printed_budget(card)
    if not amount:
        return ""
    targets = [(c, lethal_damage(c)) for c in _targets(state)]
    facts = [f"{c['name']} needs {needed}" for c, needed in targets if needed and c.get("name")]
    if not facts:
        return ""
    return f" [One printed entry trigger supplies {amount} total damage. Current lethal-damage thresholds: {', '.join(facts[:6])}.]"


def entry_kill_concerns(decision, state, picks, reason):
    """One recheck before spending the alternate cost on an explicitly claimed kill."""
    option = decision.find(picks[0]) if len(picks or []) == 1 else None
    if (
        not option
        or option.meta.get("actionType") != "ActionType_Cast"
        or not option.meta.get("alternativeCostText")
    ):
        return ()
    card = find_source(state, option.meta)
    amount = _printed_budget(card)
    if not amount or state.get("stack") or has_combat_keyword(card, "deathtouch"):
        return ()
    # A second trigger, modified damage, or an on-board source of another point
    # can make a seemingly short line work. Never veto the cast on the menu.
    for permanent in state.get("battlefield") or []:
        rules = str(permanent.get("oracle_text") or "")
        if not rules or re.search(
            r"additional time|trigger twice|deathtouch|instead[^.]*damage|damage[^.]*instead|"
            r"(?:double|triple|twice)[^.]*damage|damage[^.]*(?:plus|additional)|damage to (?:any target|target creature)",
            rules,
            re.I,
        ):
            return ()
    if named_burn_damage(reason, state, card) or re.search(
        r"\b(?:blink|flicker|copy|copies|double|twice|another trigger|second trigger|two entries|"
        r"follow.up|combined|if|unless|either|alternatively)\b",
        reason,
        re.I,
    ):
        return ()
    # Only join victims explicitly attributed to this cast. A separate spell
    # or attack later in the plan owns its own damage budget.
    source = re.compile(r"(?<!\w)" + re.escape(card["name"]) + r"(?!\w)", re.I)
    targets = _targets(state)
    victims = {}
    for clause in re.split(
        r"[.;!?]|\b(?:then|next turn|instead)\b|\band\s+(?=[^,.;!?]{1,60}?\s+(?:also\s+)?(?:kills?|deals?)\b)",
        reason,
        flags=re.I,
    ):
        if not source.search(clause) or re.search(r"\bor\b", clause, re.I):
            continue
        named = claimed_kills(clause, targets)
        if len(named) > 1:
            # Shared surnames (e.g. two Sentinels) are ambiguous abbreviations.
            named = [
                t for t in named if re.search(r"(?<!\w)" + re.escape(t["name"]) + r"(?!\w)", clause, re.I)
            ]
        if len({t.get("name") for t in named}) != len(named):
            return ()  # A shared printed name does not establish which copy was meant.
        for target in named:
            needed = lethal_damage(target)
            if not needed or target.get("is_attacking") or target.get("is_blocking"):
                return ()
            victims[target.get("instance_id") or target["name"]] = (target["name"], needed)
    total = sum(n for _, n in victims.values())
    if total <= amount:
        return ()
    thresholds = ", ".join(f"{name} needs {n}" for name, n in victims.values())
    return (
        f"One {card['name']} entry supplies {amount} total damage. {thresholds}. "
        "Reconsider the cast using that shared damage budget and any separate source.",
    )
