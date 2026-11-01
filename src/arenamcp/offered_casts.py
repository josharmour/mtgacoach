"""Current explicit free casts from exile, without inventing future permissions."""

import re
from dataclasses import replace

from arenamcp.mana import get_local_seat_id

_FREE = re.compile(r"^cast without paying (?:its |the |their )?mana cost[.!]?$", re.I)


def mana_cost_waived(meta: dict) -> bool:
    return bool(
        meta.get("alternativeGrpId") and _FREE.fullmatch(str(meta.get("alternativeCostText") or "").strip())
    )


def offered_cost(meta: dict, card: dict) -> str | None:
    """Known remaining mana cost; None when additional payment isn't modelled."""
    if not mana_cost_waived(meta):
        return None
    text = str(card.get("oracle_text") or "").lower()
    if not text or "additional cost" in text or "{x}" in str(card.get("mana_cost") or "").lower():
        return None
    raw = meta.get("manaCost")
    if raw is None or raw == []:
        return "{0}"
    if not isinstance(raw, list) or any(
        not isinstance(p, dict) or not isinstance(p.get("count"), int) or p["count"] < 0 for p in raw
    ):
        return None
    from arenamcp.board_model import _action_cost

    names = {"Generic", "White", "Blue", "Black", "Red", "Green", "Colorless"}
    normal = []
    for part in raw:
        colors = part.get("color")
        values = colors if isinstance(colors, list) else re.findall(r"[A-Za-z_]+", str(colors or ""))
        values = [str(c).removeprefix("ManaColor_") for c in values]
        if part["count"] and (not values or any(c not in names for c in values)):
            return None
        normal.append({**part, "color": ["ManaColor_" + c for c in values]})
    return _action_cost(normal) or "{0}"


def free_exile_casts(state: dict) -> list[tuple[dict, dict, str]]:
    """(card, offered action, remaining cost), only in a current own main phase."""
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if (
        local is None
        or turn.get("active_player") != local
        or state.get("stack")
        or str(turn.get("phase") or "").lower() not in {"main1", "main2", "phase_main1", "phase_main2"}
    ):
        return []
    exiled = {
        c.get("instance_id"): c
        for c in state.get("exile") or []
        if isinstance(c, dict) and c.get("owner_seat_id", c.get("controller_seat_id")) == local
    }
    found = {}
    for action in state.get("_bridge_actions") or []:
        if (
            not isinstance(action, dict)
            or str(action.get("actionType") or "").removeprefix("ActionType_").lower() != "cast"
            or action.get("_decision_payable") is False
        ):
            continue
        card = exiled.get(action.get("instanceId"))
        cost = offered_cost(action, card) if card else None
        # A normal card that returns to hand must pay its normal cost again;
        # leave those unusual recurring lines outside this one-use model.
        if cost is not None and not re.search(
            r"\breturn\b[^.]*\b(?:owner.s|your) hand\b", str(card.get("oracle_text") or ""), re.I
        ):
            found[card["instance_id"]] = (card, action, cost)
    return list(found.values())


def bind_offered_casts(decision, state: dict):
    """Bind this exact menu to a copied snapshot; confirm simple zero-mana options."""
    if decision.request_type != "ActionsAvailable":
        return decision, state
    bound = {
        **state,
        "_bridge_actions": [{**o.meta, "_decision_payable": o.payable} for o in decision.options if o.meta],
    }
    free = {a.get("instanceId") for _, a, cost in free_exile_casts(bound) if cost == "{0}"}
    options = tuple(
        replace(o, payable=True)
        if o.payable is None and o.meta.get("instanceId") in free and mana_cost_waived(o.meta)
        else o
        for o in decision.options
    )
    return replace(decision, options=options), bound
