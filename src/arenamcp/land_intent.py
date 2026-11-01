"""Verify concrete land-entry/payment claims against the current board (#303)."""

import re
from dataclasses import dataclass

from arenamcp.land_mana import basic_types, enters_tapped, mana_source
from arenamcp.mana import get_local_seat_id
from arenamcp.mulligan_policy import _mana_value, _pip_matching, _symbols
from arenamcp.play_safety import find_source
from arenamcp.rules_engine import _normalize_mana_symbols


@dataclass(frozen=True)
class LandMismatch:
    reason: str
    alternatives: tuple[str, ...]


def _entry(state, land):
    local = get_local_seat_id(state)
    board = [c for c in state.get("battlefield") or [] if c.get("controller_seat_id") == local]
    # External entry replacements or optional entry payments are not a proof.
    if any(
        re.search(r"lands?[^.]*enter[^.]*tapped", str(c.get("oracle_text") or ""), re.I)
        and "land" not in str(c.get("type_line") or "").lower()
        for c in state.get("battlefield") or []
    ):
        return None
    text = str(land.get("oracle_text") or "")
    if re.search(
        r"enters (?:the battlefield )?tapped unless you control two or more other lands", text, re.I
    ):
        return enters_tapped(land, board)
    if re.search(r"pay.*life|reveal", text, re.I):
        return None
    if re.search(r"enters (?:the battlefield )?tapped[.]", text, re.I):
        return True
    if re.search(r"enter", text, re.I):
        return None
    return (
        False if text or (basic_types(land) & {"plains", "island", "swamp", "mountain", "forest"}) else None
    )


def _payment_after(state, land, spell):
    """Exact simple-land payment only; complex ramp/cost effects remain unproved."""
    local = get_local_seat_id(state)
    cost = _normalize_mana_symbols(str(spell.get("mana_cost") or ""))
    if not cost or any(s not in {"W", "U", "B", "R", "G", "C"} and not s.isdigit() for s in _symbols(cost)):
        return None
    if re.search(
        r"additional cost|costs? .*less|rather than pay|\b(?:convoke|delve|improvise|affinity|emerge|offering)\b",
        str(spell.get("oracle_text") or ""),
        re.I,
    ):
        return None
    if any(p.get("mana_pool") for p in state.get("players") or [] if p.get("seat_id") == local):
        return None
    board = [c for c in state.get("battlefield") or [] if c.get("controller_seat_id") == local]
    if any(not c.get("type_line") or "oracle_text" not in c for c in board):
        return None  # an unidentified controlled object may itself provide mana
    # Static cost/mana effects need the complete payment engine, not a one-land count.
    for c in state.get("battlefield") or []:
        text = _normalize_mana_symbols(str(c.get("oracle_text") or ""))
        if re.search(
            r"costs? .*\b(?:less|more)\b|additional.*mana|mana.*additional|twice|\buntap\b|\binstead\b",
            text,
            re.I,
        ):
            return None
        if "land" not in str(c.get("type_line") or "").lower() and re.search(r"\badd\b", text, re.I):
            return None
    sources = []
    for c in [*board, land]:
        if "land" not in str(c.get("type_line") or "").lower() or c.get("is_phased_out"):
            continue
        text = _normalize_mana_symbols(str(c.get("oracle_text") or ""))
        # Only ordinary tap-for-one and the entry/Verge conditions understood by land_mana.
        abilities = re.findall(r"\{T\}:\s*Add ([^.]+)\.", text, re.I)
        if (
            not abilities and not (basic_types(c) & {"plains", "island", "swamp", "mountain", "forest"})
        ) or any(not re.fullmatch(r"\{[WUBRGC]\}(?: or \{[WUBRGC]\})?", a, re.I) for a in abilities):
            return None
        if re.search(r"spend|sacrifice|pay|\{\d+\}.*\{T\}|for each", text, re.I):
            return None
        tapped = _entry(state, c) if c is land else bool(c.get("is_tapped"))
        if tapped is None:
            return None
        if not tapped:
            sources.append(mana_source(c, [*board, land]))
    pips = tuple(frozenset({symbol}) for symbol in _symbols(cost) if symbol in "WUBRGC")
    return _mana_value(cost) <= len(sources) and _pip_matching(pips, sources)


def land_claim_mismatch(decision, state, chosen, reasoning):
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return None
    option = decision.find(chosen[0])
    if option is None or option.meta.get("actionType") != "ActionType_Play":
        return None
    land = find_source(state, option.meta)
    local = get_local_seat_id(state)
    if not land or local is None or "land" not in str(land.get("type_line") or "").lower():
        return None
    text = str(reasoning or "").replace("’", "'")
    clauses = re.split(r"[.;!?]", text)
    positive = [
        c for c in clauses if not re.search(r"\b(?:not|can't|cannot|don't|if|next turn|later)\b", c, re.I)
    ]
    # Bind entry to the chosen land, not another source being kept untapped.
    subject = re.escape(str(land.get("name") or ""))
    untapped = any(
        re.search(
            r"\b(?:play(?:ing)?\s+"
            + subject
            + r"(?:\s+as (?:the|our) land)?|"
            + subject
            + r"\s+(?:enters|will enter|comes in))\s+untapped\b",
            c,
            re.I,
        )
        for c in positive
    )
    promised = []
    for spell in state.get("hand") or []:
        name = str(spell.get("name") or "")
        if not name or "land" in str(spell.get("type_line") or "").lower():
            continue
        if any(
            re.search(r"\b(?:cast|enable|enables|enabling)\s+" + re.escape(name) + r"\b", c, re.I)
            and re.search(r"\b(?:this turn|now)\b", c, re.I)
            for c in positive
        ):
            promised.append(spell)
    issues = []
    if untapped and _entry(state, land) is True:
        issues.append(f"{land['name']} enters tapped on this board")
    for spell in promised:
        if _payment_after(state, land, spell) is False:
            issues.append(f"{land['name']} does not supply the mana to cast {spell['name']} this turn")
    if not issues:
        return None
    alternatives = []
    for candidate in decision.options:
        if candidate.meta.get("actionType") != "ActionType_Play" or candidate.option_id == option.option_id:
            continue
        other = find_source(state, candidate.meta)
        if not other or "land" not in str(other.get("type_line") or "").lower():
            continue
        if untapped and _entry(state, other) is not False:
            continue
        if all(_payment_after(state, other, spell) is True for spell in promised):
            alternatives.append(candidate.option_id)
    return LandMismatch(
        "; ".join(issues) + ". Reconsider the land or give a truthful different plan.", tuple(alternatives)
    )


def land_payment_notes(decision, state):
    """Keep identity/entry and colored access beside each offered land's value tag."""
    if decision.request_type != "ActionsAvailable":
        return []
    notes = []
    spells = [c for c in state.get("hand") or [] if "land" not in str(c.get("type_line") or "").lower()]
    for option in decision.options:
        if option.meta.get("actionType") != "ActionType_Play":
            continue
        land = find_source(state, option.meta)
        if not land or "land" not in str(land.get("type_line") or "").lower():
            continue
        entry = _entry(state, land)
        facts = [] if entry is None else ["enters tapped" if entry else "enters untapped"]
        for spell in spells[:8]:
            payable = _payment_after(state, land, spell)
            if payable is not None:
                facts.append(
                    f"{'can' if payable else 'cannot'} pay {spell.get('mana_cost')} for {spell.get('name')} now"
                )
        if facts:
            notes.append(f"{option.option_id} {land.get('name')}: " + "; ".join(facts))
    return notes
