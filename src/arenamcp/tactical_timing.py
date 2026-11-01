"""Public timing facts for offered casts; advisory, never a removal-first override."""

import re

from arenamcp.board_assessment import card_role
from arenamcp.mulligan_policy import hand_card
from arenamcp.offered_casts import mana_cost_waived
from arenamcp.play_safety import find_source


def timing_notes(decision, state: dict) -> list[str]:
    if decision.request_type != "ActionsAvailable":
        return []
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    turn = state.get("turn") or {}
    casts = [
        (o, find_source(state, o.meta))
        for o in decision.options
        if o.meta.get("actionType") == "ActionType_Cast" and o.payable is not False
    ]
    notes = []
    for option, card in casts:
        if not mana_cost_waived(option.meta):
            continue
        residual = option.meta.get("manaCost")
        notes.append(
            f"ALTERNATIVE COST {option.option_id} ({card.get('name') or 'offered spell'}): "
            "Arena explicitly offers casting without paying its printed mana cost. "
            "Missing auto-tap confirmation is not evidence that the printed cost must be paid. "
            + (
                f"The offered remaining mana cost is {residual}; pay it too. "
                if residual
                else "No remaining mana cost is listed; check additional costs and restrictions separately. "
            )
            + "If this creature triggers on noncreature casts, compare casting and resolving it before "
            "burn/setup spells so it receives those triggers. Preserve urgent interaction or lethal lines."
        )
    if local is None or turn.get("active_player") != local or state.get("stack"):
        return notes
    # #328: show the *pre-development* reservation while both choices are
    # still offered. Arena's explicit cost is used; printed mana can miss a
    # reduction/tax. This is conditional on target legality, not a cast veto.
    from arenamcp.ward import available_mana, cast_targeting_text, opposing_wards, targeted_kinds

    for option, card in casts:
        raw_cost = option.meta.get("manaCost")
        if not card or card_role(card) not in {"removal", "bounce"} or not raw_cost:
            continue
        mana = sum(int(part.get("count") or 0) for part in raw_cost if isinstance(part, dict))
        for target, ward in opposing_wards(state, targeted_kinds(cast_targeting_text(card)))[:3]:
            if ward.mana is None:
                continue
            notes.append(
                f"REMOVAL RESERVATION: If {card.get('name')} targets {target.get('name')} legally, "
                f"budget {mana} spell mana + {ward.mana} ward mana = {mana + ward.mana} before "
                f"development ({available_mana(state)} mana available now). Also check colored mana "
                f"and any non-mana costs in {ward.label}. Payable options are not necessarily payable "
                "together. If you choose development instead, acknowledge delaying this removal."
            )
    slow = [
        card
        for _, card in casts
        if card and card_role(card) in {"removal", "bounce"} and not hand_card(card).instant_speed
    ]
    changing = [
        card
        for card in state.get("battlefield", [])
        if card.get("controller_seat_id") is not None
        and card.get("controller_seat_id") != local
        and re.search(r"\bcraft\b|\btransform(?:s|ed)?\b", str(card.get("oracle_text") or ""), re.I)
    ]
    if slow and changing:
        names = ", ".join(str(c.get("name") or "their permanent") for c in changing[:3])
        removal = ", ".join(str(c.get("name") or "removal") for c in slow[:3])
        notes.append(
            f"REMOVAL WINDOW: {removal} is sorcery-speed without a supplied timing permission. "
            f"Compare removing {names} now, if legal, against its visible transformation. "
            "Do not plan to cast sorcery-speed removal in response on the opponent's turn. "
            "Craft exiles its source as a cost, so even an instant cannot target that source after "
            "activation; inspect the returned face's protection and target restrictions before "
            "claiming a later answer. Do not assume craft prerequisites are met or invent an unseen "
            "face. Holding removal is valid when the alternative line has a concrete benefit."
        )
        for card in changing[:3]:
            for face in card.get("related_faces") or []:
                if re.search(r"\bhexproof\b|\bshroud\b", str(face.get("oracle_text") or ""), re.I):
                    notes.append(
                        f"RETURNED FACE: {face.get('name') or 'other face'} — {face['oracle_text']}. "
                        "Check that protection before treating a held targeted answer as usable."
                    )
    return notes
