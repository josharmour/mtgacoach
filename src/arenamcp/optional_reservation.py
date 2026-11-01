"""Do not spend an unchanged, explicit same-turn spell reservation on an optional trigger."""

import re

from arenamcp.decision_memory import DecisionMemory, board_signature
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import exact_mana_total
from arenamcp.rules_engine import RulesEngine, _normalize_mana_symbols


def reserved_optional_mana(planner, state: dict, oracle: str) -> str:
    """A proven reservation conflict, else ''. Unknown payment/intent stays advisory.

    A fresh tactical board or another explicit priority decision can supersede
    the hold. Merely putting the known optional trigger on the stack cannot.
    """
    memory = getattr(planner, "_decision_memory", None)
    if not isinstance(memory, DecisionMemory):
        return ""
    records = memory.this_turn(state)
    record = next((r for r in reversed(records) if r.request_type == "ActionsAvailable"), None)
    if record is None or record.model_unavailable or record.outcome:
        return ""
    if record.chosen != ["pass"] and record.chosen_labels != ["Pass"]:
        return ""
    local = get_local_seat_id(state)
    if local is None:
        return ""
    # Compare the tactical board without just this optional ability. A new
    # opponent response, life change, creature or buff permits a fresh plan.
    context = state.get("decision_context") or {}
    payload = state.get("_bridge_request_payload") or {}
    source_id = context.get("source_id") or context.get("sourceId") or payload.get("sourceId")
    source = next((c for c in state.get("stack") or [] if str(c.get("instance_id")) == str(source_id)), None)
    if not source or str(source.get("object_kind") or "").upper() != "ABILITY":
        return ""
    if source.get("controller_seat_id") != local:
        return ""
    stripped = {**state, "stack": [c for c in state.get("stack") or [] if c is not source]}
    if record.signature not in (board_signature(state), board_signature(stripped)):
        return ""
    text = _normalize_mana_symbols(str(oracle or ""))
    costs = re.findall(r"\bmay pay\s+((?:\{(?:\d+|[WUBRGC])\})+)(?=[.,\s])", text, re.I)
    if len(costs) != 1 or len(re.findall(r"\bmay pay\b", text, re.I)) != 1:
        return ""
    optional = costs[0].upper()
    total = exact_mana_total(state)
    if total is None:
        return ""
    pool = RulesEngine._get_mana_pool(state, local)
    # The exact-source matcher prices ordinary sources; variable/multiple
    # output producers need richer per-source payment enumeration.
    if total != pool.get("total") or not RulesEngine._can_afford(optional, pool):
        return ""
    reason = str(record.reasoning or "").replace("’", "'")
    for spell in state.get("hand") or []:
        name = str(spell.get("name") or "")
        cost = _normalize_mana_symbols(str(spell.get("mana_cost") or "")).upper()
        if not name or not re.fullmatch(r"(?:\{(?:\d+|[WUBRGC])\})+", cost):
            continue
        # Require an affirmative mana reservation, not just mentioning a card.
        held = re.search(
            r"\b(?:hold|holding|keep|keeping|reserve|reserving)\b[^.;!?]{0,60}"
            r"\b(?:mana|[WUBRGC]|white|blue|black|red|green)\b[^.;!?]{0,30}"
            r"\bfor\s+" + re.escape(name) + r"(?=$|[\s.,;:])",
            reason,
            re.I,
        )
        if not held or re.search(r"\b(?:not|don't|cannot|can't|if|instead)\b", held[0], re.I):
            continue
        prefix = reason[: held.start()].rsplit(".", 1)[-1].rsplit(";", 1)[-1]
        if re.search(r"\b(?:not|don't|cannot|can't|if)\b", prefix, re.I):
            continue
        if re.search(
            r"additional cost|costs? .*less|rather than pay", str(spell.get("oracle_text") or ""), re.I
        ):
            continue
        if RulesEngine._can_afford(cost, pool) and not RulesEngine._can_afford(optional + cost, pool):
            return f"Keep the same-turn mana reservation for {name}: paying optional {optional} makes its {cost} unpayable on the unchanged board."
    return ""
