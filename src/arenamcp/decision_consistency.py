"""Narrow pre-commit checks for explicit intent and pending prowess (#329/#333)."""

import re


def consistent_action(decision, state, chosen, rationale):
    if len(chosen) != 1 or not any(o.option_id == "pass" for o in decision.options):
        return None
    option = next((o for o in decision.options if o.option_id == chosen[0]), None)
    if (
        not option
        or str((option.meta or {}).get("actionType", "")).removeprefix("ActionType_").lower() != "cast"
    ):
        return None
    stack = state.get("stack") or []
    text = str(rationale or "").lower().replace("’", "'")
    # An explicit hold-and-pass conclusion on an empty stack contradicts this
    # cast. Do not infer intent from negation, questions, or a future pass.
    if (
        not stack
        and re.search(r"\bhold (?:it|this|that|the spell)\b[^.;!?]*\band pass\b", text)
        and not re.search(r"\b(?:don't|do not|not|never|if|could|might|then cast)\b", text)
    ):
        return (
            "pass",
            "Hold the spell and pass: the selected cast contradicted the explicit hold-and-pass conclusion.",
        )
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
