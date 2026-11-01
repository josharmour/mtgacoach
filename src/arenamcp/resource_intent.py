"""Prove a narrow pre-development removal-plus-ward reservation conflict (#328)."""

import re

from arenamcp.mana import get_local_seat_id, mana_cost_to_cmc
from arenamcp.offered_casts import offered_cost
from arenamcp.play_safety import _effective_cost, exact_mana_total, find_source
from arenamcp.removal_windows import target_eligibility
from arenamcp.ward import attached_to, ward_of


def _known_cost(card, meta):
    if meta.get("alternativeGrpId") or meta.get("alternativeCostText"):
        cost = offered_cost(meta, card)
    else:
        cost = _effective_cost(card, meta)
    if not cost or not re.fullmatch(r"(?:\{(?:\d+|[WUBRGC])\})+", cost):
        return None
    return mana_cost_to_cmc(cost)


def removal_reservation_conflict(decision, state: dict, chosen: list[str], reasoning: str) -> str:
    """Check a promised mana reserve, not the strategic value of holding removal.

    Only an explicit current-mana promise for a named legal opposing warded
    target qualifies. Unknown/refunding costs, future mana and changed plans
    remain the planner's decision. No inference from merely retaining a card.
    """
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if (
        decision.request_type != "ActionsAvailable"
        or len(chosen) != 1
        or local is None
        or turn.get("active_player") != local
        or state.get("stack")
        or str(turn.get("phase") or "").lower() not in {"main1", "main2", "phase_main1", "phase_main2"}
    ):
        return ""
    option = decision.find(chosen[0])
    if option is None or option.meta.get("actionType") != "ActionType_Cast":
        return ""
    development = find_source(state, option.meta)
    if not development or "creature" not in str(development.get("type_line") or "").lower():
        return ""
    # A developer can refund mana, create a source, or change a cost on entry.
    # Those mechanisms need their own projection, not a generic subtraction.
    if re.search(
        r"\b(?:mana|add|untap|treasure|convoke|delve|improvise|affinity|emerge|destroy|exile|return|sacrifice|remove|ward|loses)\b|\bland\b[^.]*battlefield|costs? .*less|rather than pay",
        str(development.get("oracle_text") or ""),
        re.I,
    ):
        return ""
    # Other existing entry/cast engines can create mana or change the ward.
    if any(
        re.search(
            r"\b(?:when|whenever)\b[^.]*\b(?:cast|enters?)\b[^.]*\b(?:add|untap|treasure|mana|destroy|exile|return|remove|ward|loses)\b|costs? .*less",
            str(c.get("oracle_text") or ""),
            re.I,
        )
        for c in state.get("battlefield") or []
    ):
        return ""
    total = exact_mana_total(state)
    spend = _known_cost(development, option.meta)
    if total is None or spend is None or spend <= 0:
        return ""
    clauses = re.split(r"[.;!?]", str(reasoning or "").replace("’", "'"))
    board = state.get("battlefield") or []
    for answer in decision.options:
        if (
            answer.option_id == option.option_id
            or answer.payable is False
            or answer.meta.get("actionType") != "ActionType_Cast"
        ):
            continue
        removal = find_source(state, answer.meta)
        name = str(removal.get("name") or "")
        cost = _known_cost(removal, answer.meta)
        if not name or cost is None:
            continue
        if re.search(
            r"additional cost|costs? .*less|rather than pay|\b(?:convoke|delve|improvise|affinity|emerge)\b",
            str(removal.get("oracle_text") or ""),
            re.I,
        ):
            continue
        for clause in clauses:
            if re.search(
                r"\b(?:not|can't|cannot|don't|if|unless|later|next turn|after untap)\b", clause, re.I
            ):
                continue
            promise = re.search(
                r"\b(?:keep(?:ing)?|leave|leaving|hold(?:ing)?|reserv(?:e|ing))\b[^.;!?]{0,50}\bmana\b[^.;!?]{0,30}\bfor\s+"
                + re.escape(name)
                + r"(?=$|[\s,:#])",
                clause,
                re.I,
            )
            if not promise:
                continue
            targets = [
                c
                for c in board
                if c.get("controller_seat_id") not in (None, local)
                and c.get("name")
                and re.search(r"\b" + re.escape(str(c["name"])) + r"(?=$|[\s,:#])", clause, re.I)
            ]
            if len(targets) != 1:
                continue
            target = targets[0]
            if target_eligibility(removal, target, state) is not True:
                continue
            ward = ward_of(target, attached_to(target, board))
            if ward is None or ward.mana is None or ward.mana <= 0:
                continue
            reserve = cost + ward.mana
            # The resource conflict specifically arises from this development.
            if total >= reserve and total - spend < reserve:
                return (
                    f"{development.get('name')} costs {spend} of the {total} available mana, leaving {total - spend}. "
                    f"{name} on {target['name']} needs {cost} spell mana plus {ward.mana} ward mana ({reserve} total). "
                    "These plays cannot both be funded now. Reconsider development or explicitly accept delaying that removal."
                )
    return ""
