"""Conservative impossibility proof for a hand trick's required mana colors.

This is an upper bound on colors, not a full payment solver. Unknown sources,
alternative payments and cost modifiers retain the existing guard abstention.
"""

import re

from arenamcp.combat_solver import _has
from arenamcp.rules_engine import _normalize_mana_symbols

_COLORS = set("WUBRGC")
_BASIC = dict(zip(("plains", "island", "swamp", "mountain", "forest"), "WUBRG", strict=True))


def missing_trick_color(state: dict, local: dict, card: dict, attacker_ids: set[int]) -> bool:
    """True only when an ordinary hand cost requires a color we cannot produce.

    Count every potentially producible color, even mutually exclusive choices
    and unknown quantities. This deliberately cannot reject for insufficient
    quantity. An attacking tap source cannot also tap during the combat window;
    mana floated before declaring attackers empties at the step boundary.
    """
    cost = _normalize_mana_symbols(card.get("mana_cost") or "")
    if not re.fullmatch(r"(?:\{(?:\d+|[WUBRGC])\})+", cost):
        return False
    needed = set(re.findall(r"\{([WUBRGC])\}", cost))
    if not needed:
        return False
    # A visible ritual, untap, alternate payment or cost reduction can bridge
    # the missing color. We do not simulate chains of hand spells here.
    for spell in state.get("hand") or []:
        text = _normalize_mana_symbols(spell.get("oracle_text") or "")
        if re.search(
            r"\badd\b|\buntap\b|\btreasure\b|cost[^.]*less|"
            r"without paying|rather than pay|spend mana as though",
            text,
            re.I,
        ):
            return False
    seat = local.get("seat_id")
    colors = set()
    floating = state.get("floating_mana", local.get("mana_pool") or {})
    if not isinstance(floating, dict):
        return False
    for color, amount in floating.items():
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            return False
        if amount > 0:
            if str(color).upper() not in _COLORS:
                return False
            colors.add(str(color).upper())
    for source in state.get("battlefield") or []:
        text = _normalize_mana_symbols(source.get("oracle_text") or "")
        # Visible payment changes/untaps can make an apparently missing color
        # available. Do not turn an unmodeled mechanism into a forced hold.
        if re.search(
            r"\buntap\b|\btreasure\b|without paying|rather than pay|"
            r"cost[^.]*less|spend mana as though",
            text,
            re.I,
        ):
            return False
        if (source.get("controller_seat_id") or source.get("owner_seat_id")) != seat:
            continue
        if "oracle_text" not in source:
            return False
        adds = list(re.finditer(r"\badd\b", text, re.I))
        kind = str(source.get("type_line") or "").lower()
        basics = {color for basic, color in _BASIC.items() if basic in kind}
        if not adds and "land" not in kind:
            continue
        if not adds and not basics:
            return False  # unknown land ability, including a potential fetch
        # Only an unconditional tap ability can safely be removed as spent.
        tap_adds = re.findall(r"\{T\}:\s*Add\b", text, re.I)
        tap_only = len(tap_adds) == len(adds)
        if not tap_only:
            return False
        if source.get("is_tapped") or (
            source.get("instance_id") in attacker_ids and not _has(source, "vigilance")
        ):
            continue
        if len(adds) == 1 and re.search(
            r"Spend (?:this|that) mana only to activate abilities(?: of creature sources)?\.", text, re.I
        ):
            continue
        if adds:
            # An overestimate is intentional: input symbols on filter sources
            # and any-color choices cannot cause a false impossibility proof.
            produced = set(re.findall(r"\{([WUBRGC])\}", text))
            if re.search(r"any (?:one )?(?:color|type)", text, re.I):
                produced |= _COLORS
            if not produced:
                return False
            colors |= produced
        colors |= basics
    return bool(needed - colors)
