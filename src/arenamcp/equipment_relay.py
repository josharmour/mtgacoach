"""Bounded free-equip/colorless-mana sequences, grounded in current targets/cast costs."""

import re

from arenamcp.optional_prompts import _int, _local_seat

_TAP_TRIBAL = re.compile(r"^\{o?t\}:\s*add \{o?c\} for each (\w+) you control\.?$", re.I | re.M)


def relay_cast_costs(state):
    """Cost evidence only, before typed-menu binding drops unpayable actions.

    Never appended to the legal options or used to submit a cast. Rebuilt for
    each incoming decision so a previous private quote is not carried forward.
    """
    ids = {c.get("instance_id") for c in state.get("hand") or []}
    return [
        {
            key: action[key]
            for key in ("actionType", "instanceId", "manaCost", "alternativeGrpId", "disqualifyingSourceId")
            if key in action
        }
        for action in [*(state.get("_bridge_actions") or []), *(state.get("legal_actions_raw") or [])]
        if str(action.get("actionType") or "").removeprefix("ActionType_") == "Cast"
        and action.get("instanceId") in ids
    ]


def colorless_relay_note(decision, state, gear, targets):
    """Called only after validating the current free haste-equip and legal targets.

    No colored/variable/additional-cost payoff, assumed future draw, or absent
    cast action is priced. Recheck Arena after each step; this is not an executor.
    """
    local = _local_seat(state)
    board = state.get("battlefield") or []
    mine = [c for c in board if c.get("controller_seat_id") == local and not c.get("is_phased_out")]
    # Suppression/conditional activation rules are not interpreted by this helper.
    if any(
        re.search(
            r"(?:activated abilities|mana abilities)[^.\n]*(?:can't|cannot)|can't activate",
            str(c.get("oracle_text") or ""),
            re.I,
        )
        for c in board
        if not c.get("is_phased_out")
    ):
        return ""
    candidates = list(targets)
    wearer = next((c for c in mine if c.get("instance_id") == gear.get("attached_to_id")), None)
    if wearer and any(
        o.meta.get("hasteRelayMana") and o.meta.get("instanceId") == wearer.get("instance_id")
        for o in decision.options
    ):
        candidates.insert(0, wearer)
    yields = []
    for card in candidates:
        if card.get("is_tapped") or card.get("removed_abilities"):
            continue
        match = _TAP_TRIBAL.search(str(card.get("oracle_text") or "").strip())
        if not match:
            continue
        tribe = match.group(1).lower()
        amount = sum(
            tribe in {str(t).lower().removeprefix("subtype_") for t in c.get("subtypes") or []}
            or bool(re.search(rf"\b{re.escape(tribe)}\b", str(c.get("type_line") or "").lower()))
            for c in mine
        )
        if amount:
            yields.append((card, amount))
    if not yields:
        return ""
    player = next((p for p in state.get("players") or [] if p.get("seat_id") == local), {})
    pool = player.get("mana_pool") or {}
    banked = max(0, _int(pool.get("C")) or 0) if isinstance(pool, dict) else 0
    total = sum(amount for _, amount in yields)
    actions = [
        *(state.get("_bridge_actions") or []),
        *(state.get("legal_actions_raw") or []),
        *(state.get("_equipment_relay_costs") or []),
    ]
    payoffs = []
    for card in state.get("hand") or []:
        if (
            card.get("owner_seat_id", local) != local
            or "as an additional cost" in str(card.get("oracle_text") or "").lower()
        ):
            continue
        for action in actions:
            if (
                str(action.get("actionType") or "").removeprefix("ActionType_") != "Cast"
                or action.get("instanceId") != card.get("instance_id")
                or action.get("alternativeGrpId")
                or action.get("disqualifyingSourceId")
            ):
                continue
            costs = action.get("manaCost") or []
            if not costs:
                continue
            needed = 0
            for cost in costs:
                colors = {
                    s.lower().removeprefix("manacolor_")
                    for s in re.findall(r"[A-Za-z_]+", str(cost.get("color") or ""))
                }
                count = _int(cost.get("count"))
                if not colors or not colors <= {"generic", "colorless"} or count is None or count < 0:
                    break
                needed += count
            else:
                if banked < needed <= banked + total:
                    payoffs.append(
                        f"{card.get('name')} [id:{card.get('instance_id')}] costs {needed} generic/colorless"
                    )
            break
    if not payoffs:
        return ""
    steps = "; ".join(
        f"{card.get('name')} [id:{card.get('instance_id')}] taps for {amount} colorless"
        for card, amount in yields
    )
    return (
        f"COLORLESS RELAY: {steps}; {total} colorless from these untapped producers"
        + (f" plus {banked} already banked" if banked else "")
        + ". Current Arena cast costs: "
        + "; ".join(payoffs)
        + ". Equip one legal target, wait for resolution, then use its offered Bank mana action before moving the equipment. "
        "Recheck every fresh menu. Once the spell is payable using banked mana and the current wearer, cast with auto-pay; "
        "do not tap extra creatures unnecessarily. Spend within this main phase; passing to combat loses ordinary floating mana. "
        "This is a concrete payment route, not a guarantee against responses or a requirement to cast. Compare spending the bodies' taps and moving protection."
    )
