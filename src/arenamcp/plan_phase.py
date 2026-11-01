"""Phase facts retained through nested selection prompts (ledger #309).

Haste may still enable mana abilities after combat; an equipment selection
must not be rejected merely because its attack rationale is wrong.
"""

from __future__ import annotations

import re

from arenamcp.board_model import canonical_phase_step


def phase_decision_note(state: dict) -> str:
    turn = state.get("turn") or {}
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None
    )
    phase, _step = canonical_phase_step(turn)
    if local is None or turn.get("active_player") != local or phase not in ("Phase_Main2", "Phase_Ending"):
        return ""
    # A current legal attack request is stronger evidence than a stale phase
    # snapshot, e.g. an extra combat started during the native diff boundary.
    request = " ".join(str(state.get(key) or "") for key in ("_bridge_request_type", "_bridge_request_class"))
    if "DeclareAttack" in request:
        return ""
    return (
        f"CURRENT TIMING: {phase} on our turn. The normal attack declaration window has passed; "
        "haste does not create another combat phase. A new attack needs an explicit additional "
        "combat effect and its attack window. Haste may still enable tap abilities now, and "
        "equipping may still provide shroud or other protection. Evaluate those remaining benefits."
    )


def phase_claim_notes(text: str, state: dict) -> list[str]:
    if not phase_decision_note(state):
        return []
    for sentence in re.split(r"[.;\n]", str(text).lower().replace("’", "'")):
        if re.search(
            r"\b(?:next turn|next combat|additional combat|extra combat|another combat|"
            r"cannot|can't|won't|not|no longer|unable|doesn't|don't)\b",
            sentence,
        ):
            continue
        if re.search(
            r"\b(?:can|will|could)\s+(?:now\s+)?attack\b|"
            r"\battack(?:s|ing)?\s+(?:now|immediately|this turn|this combat)\b",
            sentence,
        ):
            return [
                "The normal attack declaration window has passed; haste does not create another combat "
                "phase. Evaluate protection or tap abilities instead unless an additional combat is created"
            ]
    return []
