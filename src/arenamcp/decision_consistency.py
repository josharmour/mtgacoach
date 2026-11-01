"""Narrow pre-commit checks for explicit intent and pending prowess (#329/#333)."""

import re

_CONCLUSION_START = (
    r"(?:^|\b(?:so|therefore|thus|hence|instead|however|but)\b\s*[:,]?\s*)"
    r"(?:(?:I|we)\s+)?(?:(?:will|should|must|shall)\s+)?"
)
_PASS_CONCLUSION = re.compile(
    _CONCLUSION_START + r"(?:(?:choose|prefer|recommend)\s+|let's\s+|"
    r"the (?:best|better|correct|right) (?:play|choice|action) is (?:to )?)?"
    r"pass(?:ing)?\b",
    re.I,
)
_HOLD_PASS_CONCLUSION = re.compile(
    _CONCLUSION_START + r"hold (?:it|this|that|the spell)\b[^.;!?]*?\band pass\b", re.I
)
_OTHER_CONCLUSION = re.compile(
    _CONCLUSION_START + r"(?:cast|play|activate|deploy|use)\b",
    re.I,
)


def explicit_current_pass_conclusion(rationale):
    """An affirmative final action conclusion, not a strategic preference score.

    Inspect current-action clauses in order so an earlier cast suggestion
    does not veto a later pass conclusion, or vice versa. Questions, quoted
    advice, hypothetical branches and after-cast passes are not commitments.
    Return the supporting clause for a recheck/trace; unknown phrasing abstains.
    """
    text = str(rationale or "").replace("’", "'")
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\'[^\'\n]*\'(?!\w)', "", text)
    conclusion = ""
    prior_cast = False
    for sentence in re.finditer(r"[^.;!?]+[.;!?]?", text):
        clause = sentence[0].strip()
        if clause.endswith("?") or re.search(
            r"\b(?:if|unless|could|might|would|maybe|perhaps|whether|after|before|then|once|first|until|when)\b",
            clause,
            re.I,
        ):
            # A later conditional/sequential action leaves the terminal
            # commitment ambiguous; do not reuse an earlier Pass sentence.
            if re.search(r"\b(?:pass|cast|play|activate|deploy|use)\b", clause, re.I):
                conclusion = ""
            if (
                _OTHER_CONCLUSION.search(clause)
                and not re.search(r"\b(?:if|unless|could|might|would|maybe|perhaps|whether)\b", clause, re.I)
                and not clause.endswith("?")
            ):
                prior_cast = True
            continue
        events = [(m.start(), False, m) for m in _OTHER_CONCLUSION.finditer(clause)]
        events += [(m.start(), True, m) for m in _PASS_CONCLUSION.finditer(clause)]
        events += [(m.start(), True, m) for m in _HOLD_PASS_CONCLUSION.finditer(clause)]
        for _, passing, match in sorted(events, key=lambda event: event[0]):
            tail = clause[match.end() :]
            if re.match(r"\s+(?:next turn|later|is|was|seems|looks|on|up|over|no|not)\b", tail, re.I):
                continue
            if passing:
                # Passing on another option or after a planned cast is not a
                # current Pass action. An explicit reversal connector can
                # supersede the earlier cast recommendation.
                if re.search(r"\bto (?:let|allow|resolve)\b", tail, re.I):
                    continue
                if prior_cast and not re.match(
                    r"(?:so|therefore|thus|hence|instead|however|but)\b", match[0], re.I
                ):
                    continue
                conclusion = clause
            else:
                prior_cast = True
                conclusion = ""
    return conclusion


def consistent_action(decision, state, chosen, rationale):
    if len(chosen) != 1 or not any(o.option_id == "pass" for o in decision.options):
        return None
    option = next((o for o in decision.options if o.option_id == chosen[0]), None)
    if (
        not option
        or str((option.meta or {}).get("actionType", "")).removeprefix("ActionType_").lower() != "cast"
    ):
        return None
    if explicit_current_pass_conclusion(rationale):
        return (
            "pass",
            "Pass: the selected cast contradicted the explicit current-action conclusion.",
        )
    stack = state.get("stack") or []
    text = str(rationale or "").lower().replace("’", "'")
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    turn = state.get("turn") or state.get("turn_info") or {}
    if (
        local is None
        or turn.get("active_player") != local
        or str(turn.get("phase", "")).lower() not in {"main1", "phase_main1"}
    ):
        return None
    # Only our creature spells are pending; an opponent response/ability is a
    # real urgency window and must still reach the ordinary tactical planner.
    if not stack or any(
        c.get("controller_seat_id") != local or "creature" not in str(c.get("type_line", "")).lower()
        for c in stack
    ):
        return None
    pending = [
        c
        for c in stack
        if re.search(
            r"\bprowess\b|whenever you cast a noncreature spell", str(c.get("oracle_text", "")), re.I
        )
    ]
    if not pending or not re.search(r"\b(?:prowess|pump|pumps|pumping|grow|grows|boost|boosts)\b", text):
        return None
    meta = option.meta or {}
    iid = meta.get("instanceId") or meta.get("instance_id")
    spell = next((c for c in state.get("hand", []) if c.get("instance_id") == iid), None)
    if not spell or "creature" in str(spell.get("type_line", "")).lower():
        return None
    return (
        "pass",
        f"Let {pending[0].get('name') or 'the prowess creature'} resolve first; casting now cannot trigger a creature still on the stack.",
    )
