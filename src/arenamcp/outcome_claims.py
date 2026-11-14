"""Bounded record-backed contradictions; unknown support always abstains."""

import re

from arenamcp.ability_synthesizer import Damage, cast_time_entries
from arenamcp.claim_language import conditional_claim, negated_claim


def _asserted_claim(text, claim):
    """Keep governing prefix denial in scope, not just the named subject.

    Ambiguous denial anywhere in the current statement abstains. Independent
    statements and contrastive boundaries reset the scope; no global change to
    claim_language callers that supply their own subject boundaries.
    """
    if claim is None:
        return False
    lowered = text.lower()
    if len(lowered) != len(text):
        return False  # preserve span offsets; unusual case mapping is unknown
    text = lowered
    boundary = re.compile(r"[.!?;|\n]|\b(?:but|however|yet)\b", re.I)
    start = 0
    for match in boundary.finditer(text, 0, claim.start()):
        start = match.end()
    following = boundary.search(text, claim.end())
    end = following.start() if following else len(text)
    return not negated_claim(text, start, end) and not conditional_claim(text, start, claim.end())


def record_outcome_notes(source, meta, state, reasoning):
    from arenamcp.game_plan import _keyword_truth
    from arenamcp.line_search_moves import _known_record, _record_key

    record = _known_record(*_record_key(source))
    if record is None:
        return []
    name = str(source.get("name") or "")
    text = str(reasoning or "")
    if not name or name.casefold() not in text.casefold():
        return []  # no attribution of a lifecycle/damage claim to this card
    board = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    # Death/return engines, damage modifiers or unreadable objects can make
    # an apparently impossible outcome real. Do not try to implement them here.
    for card in board:
        rules = str(card.get("oracle_text") or "")
        if not rules and not card.get("keywords"):
            return []
        if re.search(
            r"\b(?:dies|dying|graveyard|return|sacrifice|instead|damage|trigger|counters?)\b", rules, re.I
        ):
            return []
    if re.search(r"\b(?:if|unless|when|once|after|would|could|might|assuming)\b", text, re.I):
        return []
    notes = []
    if len(cast_time_entries(record)) != len(record.effects):
        return []  # a non-entry lifecycle effect may restore/replace the body
    if re.search(r"\b(?:dies|dying|undying|persist|graveyard)\b", str(source.get("oracle_text") or ""), re.I):
        return []
    # Selected alternative cost is explicit metadata, never a display label or
    # a guess from available modes. Match the complete recorded cost.
    selected = str(meta.get("alternativeCostText") or "").strip().casefold().rstrip(".")
    for mode in record.modes:
        if mode.stays_on_battlefield or not selected or not mode.cost_text:
            continue
        cost = mode.cost_text.strip().casefold().rstrip(".")
        accepted = {cost, mode.name.casefold() + "—" + cost, mode.name.casefold() + " — " + cost}
        if selected not in accepted:
            continue
        subject = rf"(?:evoking\s+)?{re.escape(name)}"
        claim = re.search(
            subject + r"\b[^.!?;]*?\b(?:leaves?\b[^.!?;]*?\bbody\b|survives?\b[^.!?;]*?\bnext turn\b"
            r"|(?:remains?|stays?)\s+on\s+(?:our |the )?battlefield\b)",
            text,
            re.I,
        )
        if _asserted_claim(text, claim):
            notes.append(f"{name}'s selected {mode.name} mode does not leave its body on the battlefield.")
    entries = cast_time_entries(record)
    if not entries or any(e.conditions or not isinstance(e.effect, Damage) for e in entries):
        return notes
    if len(entries) != len(record.effects) or _keyword_truth([source], "deathtouch", board) is not False:
        return notes
    amount = sum(e.effect.amount for e in entries)
    if not isinstance(amount, int) or amount <= 0:
        return notes
    for target in board:
        target_name = str(target.get("name") or "")
        if not target_name or not isinstance(target.get("toughness"), (int, float)):
            continue
        if any(v for k, v in target.items() if "damage" in k.lower()):
            continue  # marked damage needs its own complete resolution model
        claim = re.search(
            rf"\b{re.escape(name)}\s+kills?\s+{re.escape(target_name)}\b[^.!?;]*?\b(?:damage|ping)\b",
            text,
            re.I,
        )
        if _asserted_claim(text, claim) and amount < target["toughness"]:
            notes.append(
                f"{name}'s recorded {amount} damage cannot by itself kill {target_name} at {target['toughness']} toughness."
            )
    return notes
