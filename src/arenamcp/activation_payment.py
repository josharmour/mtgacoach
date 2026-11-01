"""Conservative pre-submission mana shortfalls for activated abilities (#254).

The activating permanent cannot also supply mana when its cost uses {T}.
A missing auto-tap solution alone is not evidence of a shortfall.
"""

import re

from arenamcp.mana import has_autotap_solution, mana_cost_to_cmc
from arenamcp.rules_engine import _normalize_mana_symbols


def activation_payment_reason(state: dict, card: dict, metadata: dict | None = None) -> str:
    """Withhold only when a known mana requirement exceeds known remaining output."""
    from arenamcp.play_safety import _effective_cost, _offered_ability, exact_mana_total

    metadata = metadata or {}
    if not card or has_autotap_solution(metadata):
        return ""
    offered_cost, _effect = _offered_ability(card, metadata)
    offered_cost = _normalize_mana_symbols(offered_cost)
    raw = metadata.get("manaCost")
    if isinstance(raw, list) and raw:
        if any(
            not isinstance(part, dict) or type(part.get("count")) is not int or part["count"] < 0
            for part in raw
        ):
            return ""
        cost = _effective_cost({}, metadata)
        if not cost:
            return ""  # unknown metadata must not be replaced by a printed price
    else:
        symbols = re.findall(r"\{([^}]+)\}", offered_cost)
        cost = "".join(f"{{{symbol}}}" for symbol in symbols if symbol not in ("T", "Q"))
    symbols = re.findall(r"\{([^}]+)\}", cost)
    if not symbols or any(not symbol.isdigit() and symbol not in set("WUBRGC") for symbol in symbols):
        return ""  # X, Phyrexian and other alternate-payment costs remain unknown
    needed = mana_cost_to_cmc(cost)
    if needed <= 0:
        return ""
    reserved = "{T}" in offered_cost
    iid = card.get("instance_id") or metadata.get("instanceId")
    if reserved and not iid:
        return ""
    # Copy only changed objects; the snapshot/bridge card itself must stay untapped.
    battlefield = [
        {**permanent, "is_tapped": True} if reserved and permanent.get("instance_id") == iid else permanent
        for permanent in state.get("battlefield") or []
    ]
    # exact_mana_total currently uses the spell-payment pool, which excludes
    # restricted mana that might legitimately pay an activation. Abstain rather
    # than call such mana unavailable. Variable/reduced output is handled there.
    if any(
        not permanent.get("is_tapped")
        and re.search(
            r"spend this mana|this mana.*(?:spent|spend)", str(permanent.get("oracle_text") or ""), re.I
        )
        for permanent in battlefield
    ):
        return ""
    total = exact_mana_total({**state, "battlefield": battlefield})
    if total is None or needed <= total:
        return ""
    reservation = (
        f" after reserving {card.get('name') or 'the source'} for its {{T}} cost" if reserved else ""
    )
    return f"Activation costs {cost}, but only {total} mana is available{reservation}"
