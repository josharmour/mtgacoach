"""Pay a cost one mana source at a time when Arena offers no Auto Pay.

An X spell that needs every source (Shivan Devastator at X=5, 2026-10-18
21:40) arrives as a PayCostsReq with paymentActions but no autoTapActionsReq.
Arena's own bot pays such a request by activating one mana ability per round
(PayCostsGoldfishHandler); the server answers each with a new PayCostsReq for
what is still owed. This picks the source for one round so colored mana is
never spent on generic first.
"""

from __future__ import annotations

from typing import Any

COLORED = ("W", "U", "B", "R", "G", "C")


def payment_shortfall(context: dict[str, Any]) -> str:
    """Why the offered sources cannot pay the whole cost, '' when they can or it is unknown.

    Tapping one source of a cost that can never be completed only ends in a
    cancel (2026-10-19 11:17:34: Slickshot Show-Off's plot {1}{R} with one
    Mountain offered); cancel at once instead.
    """
    if not context.get("requirements_modeled", False) or context.get("has_effect_cost"):
        return ""
    try:
        requirements = context.get("mana_requirements") or {}
        needs = {color: int(count) for color, count in requirements.items() if count}
        pool = int(context.get("pool_mana_count") or 0)
        sources = {
            source["instance_id"]: source
            for source in context.get("payment_sources") or []
            if isinstance(source, dict) and source.get("instance_id")
        }
        supply = sum(int(source.get("amount") or 1) for source in sources.values()) + pool
    except (TypeError, ValueError):
        return ""
    if not sources or any(not source.get("colors") for source in sources.values()):
        return ""  # what a source makes is unknown: leave it to the payment steps
    owed = sum(needs.values())
    if owed and supply < owed:
        return f"{supply} mana offered for a cost of {owed}"
    if not pool:
        for color in COLORED:
            if needs.get(color) and not any(color in (s.get("colors") or ()) for s in sources.values()):
                return f"nothing offered makes {color}"
    return ""


def choose_payment_source(context: dict[str, Any]) -> dict[str, Any] | None:
    """{"instance_id", "ability_grp_id", "color"} for the next mana ability to
    activate, or None when the cost cannot be planned safely.

    Colored requirements are paid first, scarcest color first, from the source
    with the fewest colors (a basic before a dual); treasures are spent last.
    """
    if not context.get("requirements_modeled", False):
        return None
    sources = [s for s in context.get("payment_sources") or [] if s.get("instance_id") and s.get("colors")]
    if not sources:
        return None
    needs = {color: int(count) for color, count in (context.get("mana_requirements") or {}).items() if count}
    if needs.get("Any"):
        return None  # "mana of any one color" choices are not modeled

    def rank(source: dict[str, Any]) -> tuple:
        return (bool(source.get("treasure")), len(source["colors"]), source["instance_id"])

    colored = [color for color in COLORED if needs.get(color)]
    if colored:
        candidates = {color: [s for s in sources if color in s["colors"]] for color in colored}
        if any(not found for found in candidates.values()):
            return None  # a colored requirement nothing here can pay
        color = min(colored, key=lambda c: (len(candidates[c]), COLORED.index(c)))
        source = min(candidates[color], key=rank)
        return {
            "instance_id": source["instance_id"],
            "ability_grp_id": source.get("ability_grp_id", 0),
            "color": color,
        }
    if needs.get("generic"):
        source = min(sources, key=rank)
        return {
            "instance_id": source["instance_id"],
            "ability_grp_id": source.get("ability_grp_id", 0),
            "color": source["colors"][0],
        }
    return None
