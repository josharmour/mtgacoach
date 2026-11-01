"""Ground haste-equipment plans and passes in current cards and offered targets."""

import re

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.optional_prompts import _int, _local_seat


def _grants_haste(card):
    return bool(
        re.search(
            r"\bequipped creature (?:has|gains)\b[^.\n]*\bhaste\b", str(card.get("oracle_text") or "").lower()
        )
    )


def _creature(card):
    types = card.get("card_types")
    if types:
        return any(str(t).removeprefix("CardType_") == "Creature" for t in types)
    return is_creature_now(card)


def planned_haste_names(text, state, cast_ids=(), *, at=None):
    """A stated equipment grant is a future plan, not a claim of innate haste.

    Abstain from a sickness contradiction only for a named, known haste source
    already controlled or being cast, and a named friendly battlefield creature.
    This does not certify payment, a target, or multiple simultaneous wearers.
    """
    from arenamcp.game_plan import _named_creatures

    local = _local_seat(state)
    if local is None:
        return set()
    board = state.get("battlefield") or []
    mine = [c for c in board if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local]
    sources = [c for c in mine if _grants_haste(c)] + [
        c for c in state.get("hand") or [] if c.get("instance_id") in cast_ids and _grants_haste(c)
    ]
    creatures = [c for c in mine if _creature(c)]
    names = list(dict.fromkeys(c.get("name") for c in creatures if c.get("name")))
    source_names = list(dict.fromkeys(c.get("name") for c in sources if c.get("name")))
    result = set()
    for match in re.finditer(r"[^.!?;]+", str(text)):
        if at is not None and not match.start() <= at < match.end():
            continue
        sentence = match.group()
        if not _named_creatures(sentence, source_names):
            continue
        if not re.search(r"\b(?:equip\w*|giv\w*|grant\w*)\b", sentence, re.I):
            continue
        if re.search(r"\b(?:not|never|cannot|can't|won't|doesn't|without)\b", sentence, re.I):
            continue
        if re.search(r"\b(?:next turn|later|eventually|if drawn)\b", sentence, re.I):
            continue
        result.update(_named_creatures(sentence, names))
    return result


def equipment_opportunity_note(decision, state):
    """Name current free-equip options with Arena-offered, untapped fresh targets."""
    turn = state.get("turn") or {}
    local = _local_seat(state)
    phase = str(turn.get("phase") or "").removeprefix("Phase_")
    if (
        decision.request_type != "ActionsAvailable"
        or local is None
        or turn.get("active_player") != local
        or phase not in {"Main1", "Main2"}
        or state.get("stack")
    ):
        return ""
    cards = {c.get("instance_id"): c for c in state.get("battlefield") or []}
    lines = []
    for option in decision.options:
        meta = option.meta or {}
        gear = cards.get(meta.get("instanceId"), {})
        if (
            meta.get("actionType") != "ActionType_Activate"
            or option.payable is False
            or any((_int(cost.get("count")) or 0) > 0 for cost in meta.get("manaCost") or [])
            or not re.search(r"\bEquip \{(?:o)?0\}", option.label, re.I)
            or not _grants_haste(gear)
            or (gear.get("controller_seat_id") or gear.get("owner_seat_id")) != local
        ):
            continue
        candidates = {
            _int(t.get("targetInstanceId"))
            for slot in meta.get("targets") or []
            for t in slot.get("targets") or []
            if str(t.get("highlight") or "").removeprefix("HighlightType_") in {"Hot", "Tepid"}
        }
        fresh = []
        for iid in sorted(candidates - {None}):
            card = cards.get(iid, {})
            if (
                (card.get("controller_seat_id") or card.get("owner_seat_id")) != local
                or not _creature(card)
                or card.get("is_tapped")
                or card.get("is_phased_out")
                or iid == gear.get("attached_to_id")
                or has_combat_keyword(card, "haste")
                or has_combat_keyword(card, "shroud")
                or not (
                    card.get("summoning_sickness")
                    or card.get("turn_entered_battlefield") == turn.get("turn_number")
                )
            ):
                continue
            fresh.append(f"{card.get('name') or 'Creature'} [id:{iid}]")
        if not fresh:
            continue
        lines.append(
            f"{option.option_id} {gear.get('name')}: equip {{0}}; Arena offers fresh untapped targets "
            + ", ".join(fresh)
            + ". Haste removes their summoning-sickness restriction while wearing it. "
            + (
                "Before combat, that can enable an attack or a tap ability. "
                if phase == "Main1"
                else "Combat has passed: haste can enable a tap ability, not an attack this turn. "
            )
            + "Equip one wearer at a time; moving it does not untap or leave haste behind. "
            "Evaluate the actual payoff and colored mana before banking mana; extra colorless cannot pay green costs. "
            "Shroud, when granted, protects that wearer from targeted effects but also blocks your own targeting; "
            "it does not stop sacrifice or sweepers. Weigh losing protection on an existing wearer."
        )
    return "EQUIPMENT OPPORTUNITY: " + " ".join(lines) if lines else ""


def equipment_pass_concern(decision, state, chosen, reason):
    if chosen != ["pass"] or not re.search(
        r"\bno useful (?:\w+ )?target|\bgains? nothing\b|\b(?:haste|equip\w*)[^.;]*\b(?:useless|worthless)\b",
        str(reason),
        re.I,
    ):
        return ""
    note = equipment_opportunity_note(decision, state)
    return (
        note
        + " Explain the concrete reason to pass rather than dismissing haste because the target is summoning sick."
        if note
        else ""
    )
