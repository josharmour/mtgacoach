"""Current-payment facts for a completed turn plan's remaining mana hold."""

import re

from arenamcp.mana import get_local_seat_id, has_autotap_solution, mana_cost_to_cmc
from arenamcp.play_safety import exact_mana_total
from arenamcp.rules_engine import RulesEngine, _normalize_mana_symbols

_HOLD = re.compile(r"\b(?:hold|holding|keep|keeping|reserve|reserving|leave|leaving)\b", re.I)
_MANA = re.compile(r"\{(?:\d+|[WUBRGC])\}|\b(?:mana|[WUBRGC]|white|blue|black|red|green|colorless)\b", re.I)
_CONDITIONAL = re.compile(
    r"\b(?:if|unless|after|next turn|later|untap|drawn|draw into|not|don't|cannot|can't)\b", re.I
)
_ALTERNATIVE = re.compile(
    r"without paying|rather than pay|additional cost|costs? [^.]*less|\b(?:convoke|delve|improvise|affinity)\b",
    re.I,
)


def current_mana_hold(hold: str, state: dict) -> str:
    """Replace only a provably impossible assertion of currently held spell mana.

    Call after the step's land/casts are complete. The saved plan remains intact:
    a later untap can make its original reservation valid again. No attempt is
    made to infer a successful optional payment merely from submission.
    """
    if not hold or state.get("stack"):
        return hold
    local = get_local_seat_id(state)
    if local is None:
        return hold
    controlled = [
        c
        for c in state.get("battlefield") or []
        if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local
    ]
    for card in controlled:
        if (
            not re.search(
                r"\b(?:creature|land|artifact|enchantment|planeswalker|battle)\b",
                str(card.get("type_line") or ""),
                re.I,
            )
            or card.get("oracle_text") is None
        ):
            return hold
        text = _normalize_mana_symbols(str(card.get("oracle_text") or ""))
        # Sacrifice-only mana, filters, conditional production, and alternative
        # cast permissions are not fully described by the simple current total.
        if re.search(r"\badd\b", text, re.I) and (
            "{T}" not in text
            or re.search(r"activate only|spend this mana|\{[1-9]\d*\}[^:]*\{T\}", text, re.I)
        ):
            return hold
        if _ALTERNATIVE.search(text):
            return hold
    if any(_ALTERNATIVE.search(str(c.get("oracle_text") or "")) for c in state.get("battlefield") or []):
        return hold  # global cost changes can come from either controller
    # Some snapshots supply a player pool before the normalized floating pool.
    # Do not turn that incomplete representation into a zero-mana assertion.
    if not state.get("floating_mana") and any(
        p.get("mana_pool") for p in state.get("players") or [] if p.get("seat_id") == local
    ):
        return hold
    if any(
        re.search(r"\badd\b", str(c.get("oracle_text") or ""), re.I)
        and re.search(r"from your hand|exile[^:]*:", str(c.get("oracle_text") or ""), re.I)
        for c in state.get("hand") or []
    ):
        return hold
    total = exact_mana_total(state)
    if total is None or total != RulesEngine._get_mana_pool(state, local).get("total"):
        return hold  # richer multi-mana/filter payment enumeration remains advisory
    clauses = []
    for clause in hold.split(";"):
        if not _HOLD.search(clause) or not _MANA.search(clause) or _CONDITIONAL.search(clause):
            clauses.append(clause.strip())
            continue
        unavailable = []
        for card in state.get("hand") or []:
            name = str(card.get("name") or "")
            cost = _normalize_mana_symbols(str(card.get("mana_cost") or "")).upper()
            if not name or not re.search(r"\bfor\s+" + re.escape(name) + r"(?=$|[\s.,:])", clause, re.I):
                continue
            if not re.fullmatch(r"(?:\{(?:\d+|[WUBRGC])\})+", cost) or _ALTERNATIVE.search(
                str(card.get("oracle_text") or "")
            ):
                continue
            offers = [
                a
                for a in state.get("_bridge_actions") or []
                if a.get("instanceId") == card.get("instance_id")
                and str(a.get("actionType") or "").removeprefix("ActionType_").lower() == "cast"
            ]
            if any(
                has_autotap_solution(a) or a.get("alternativeCostText") or a.get("alternativeGrpId")
                for a in offers
            ):
                continue
            # A numeric shortfall is sufficient evidence. Do not infer a
            # colored shortfall from approximate or restricted source colors.
            if mana_cost_to_cmc(cost) > total:
                unavailable.append(f"{name} is not currently payable ({cost}; {total} mana available)")
        clauses.append("; ".join(unavailable) if unavailable else clause.strip())
    return "; ".join(c for c in clauses if c)
