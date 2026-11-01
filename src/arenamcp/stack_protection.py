"""#350: a legal permanent-protection target cannot protect a spell from a counter.

Only exact stack target links, source instances, simple supplied effects and
current offered permanent targets establish the contradiction. No I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PROTECTION = re.compile(
    r"target (?:permanent|creature) you control gains "
    r"(?:hexproof and indestructible|hexproof|indestructible) until end of turn\."
    r"(?: you gain (?P<life>\d+) life\.)?",
    re.I,
)
_COUNTER = re.compile(r"^counter target [^.]*\bspell\.", re.I)


def _iid(value):
    return value if type(value) is int and value > 0 else None


@dataclass(frozen=True)
class ProtectionFacts:
    source_name: str
    counter_name: str
    spell_name: str
    spell_id: int
    life: str
    target_names: tuple[str, ...]

    def note(self):
        life = f" It can still gain {self.life} life if it resolves." if self.life else ""
        return (
            f"{self.spell_name} [instance {self.spell_id}] is a spell on the stack, not a battlefield permanent. "
            f"{self.source_name}'s current legal targets are battlefield permanents ({', '.join(self.target_names)}); "
            f"that spell is not offered. Giving one of those permanents hexproof or indestructible "
            f"does not stop {self.counter_name} from targeting the spell. "
            f"{self.source_name} can protect its actual permanent target.{life}"
        )


def protection_facts(decision, state, chosen):
    if decision.request_type not in {"ActionsAvailable", "SelectTargets"}:
        return None
    if len(chosen) != 1 or not decision.selection_is_valid(chosen):
        return None
    local = _iid(state.get("local_seat_id"))
    if local is None:
        return None
    permanents = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    board = {c.get("instance_id"): c for c in permanents}
    if len(board) != len(permanents):
        return None
    stack = [c for c in state.get("stack") or [] if isinstance(c, dict)]
    selected = [o for o in decision.options if o.option_id == chosen[0]]
    if len(selected) != 1:
        return None
    if decision.request_type == "ActionsAvailable":
        meta = selected[0].meta or {}
        if meta.get("actionType") != "ActionType_Cast":
            return None
        source_id = _iid(meta.get("instanceId"))
        sources = [
            c for c in state.get("hand") or [] if isinstance(c, dict) and c.get("instance_id") == source_id
        ]
        groups = meta.get("targets") or []
        if len(groups) != 1 or not isinstance(groups[0], dict):
            return None
        targets = [
            _iid(t.get("targetInstanceId")) for t in groups[0].get("targets") or [] if isinstance(t, dict)
        ]
    elif decision.request_type == "SelectTargets" and decision.min_select == decision.max_select == 1:
        ids = {_iid(getattr(decision, "source_instance_id", 0))}
        for key in ("_bridge_request_payload", "decision_context"):
            context = state.get(key) or {}
            ids.add(_iid(context.get("sourceId") or context.get("source_id")))
        ids.discard(None)
        if len(ids) != 1:
            return None
        source_id = next(iter(ids))
        sources = [c for c in stack if c.get("instance_id") == source_id]
        targets = [_iid((o.meta or {}).get("instanceId")) for o in decision.options]
    else:
        return None
    if not source_id or len(sources) != 1 or not targets or None in targets:
        return None
    source = sources[0]
    if source_id in board or (
        decision.request_type == "ActionsAvailable" and any(c.get("instance_id") == source_id for c in stack)
    ):
        return None
    if source.get("controller_seat_id") != local or not source.get("name"):
        return None
    if (
        decision.request_type == "ActionsAvailable"
        and meta.get("grpId") is not None
        and meta["grpId"] != source.get("grp_id")
    ):
        return None
    raw_rules = str(source.get("oracle_text") or "")
    if re.search(r"\bcounter(?:ed|s)?\b", raw_rules, re.I):
        return None  # Do not discard a real anti-counter effect as parenthetical text.
    rules = re.sub(r"\([^()]*\)", "", raw_rules)
    effect = _PROTECTION.fullmatch(" ".join(rules.split()))
    if not effect or any(i not in board or board[i].get("controller_seat_id") != local for i in targets):
        return None
    stack_ids = [c.get("instance_id") for c in stack]
    if any(i in stack_ids for i in targets):
        return None  # contradictory zone membership is not proof
    threats = []
    for counter in stack:
        controller = counter.get("controller_seat_id")
        if (
            controller is None
            or controller == local
            or not _COUNTER.match(str(counter.get("oracle_text") or ""))
        ):
            continue
        linked = counter.get("targeting") or []
        if len(linked) != 1 or not _iid(linked[0]):
            continue
        victims = [c for c in stack if c.get("instance_id") == linked[0]]
        if len(victims) != 1:
            continue
        victim = victims[0]
        if victim.get("controller_seat_id") != local or linked[0] in board or linked[0] in targets:
            continue
        if "ability" in str(victim.get("object_kind") or "").lower() or not victim.get("type_line"):
            continue
        if not victim.get("name") or not counter.get("name"):
            continue
        threats.append((counter, victim))
    if len(threats) != 1:
        return None
    counter, victim = threats[0]
    return ProtectionFacts(
        source["name"],
        counter["name"],
        victim["name"],
        victim["instance_id"],
        effect["life"] or "",
        tuple(dict.fromkeys(str(board[i].get("name") or i) for i in targets)),
    )


def _false_clause(clause, facts):
    low = clause.lower().replace("’", "'")
    if re.search(
        r"\b(?:if|unless|would|could|might|not|never|cannot|can't|doesn't|won't|isn't|triggers?|triggered)\b",
        low,
    ):
        return False
    aliases = (facts.source_name.lower(), facts.source_name.split()[-1].lower())
    bound = re.search(
        r"\b(?:hexproof|indestructible|" + "|".join(re.escape(n) for n in aliases) + r")\b", low
    )
    counter = re.search(r"\b(?:counter|counterspell)\b", low) or facts.counter_name.lower() in low
    outcome = re.search(r"\b(?:fizzles?|fails?|stops?|prevent(?:s)?|protect(?:s|ing)?|save(?:s)?)\b", low)
    if not bound or not counter or not outcome or outcome.start() < bound.start():
        return False
    between = low[bound.end() : outcome.start()].replace("and indestructible", "")
    return not re.search(r"\b(?:and|while|whereas|but)\b", between)


def correct_protection_claims(text, decision, state, chosen):
    facts = protection_facts(decision, state, chosen)
    if facts is None:
        return text, []
    clauses = re.split(r"(?<=[.;!?])\s+", str(text or ""))
    kept = [c for c in clauses if not _false_clause(c, facts)]
    if len(kept) == len(clauses):
        return text, []
    note = facts.note()
    return " ".join([*kept, "Rules correction: " + note]), [note]


def protection_prompt_note(decision, state):
    if decision.request_type not in {"ActionsAvailable", "SelectTargets"}:
        return ""
    notes = []
    for option in decision.options:
        facts = protection_facts(decision, state, [option.option_id])
        if facts and facts.note() not in notes:
            notes.append(facts.note())
    return "\n".join(notes)


def review_protection_claim(decision, state, data, chosen, ask, parse, message, trace):
    """One deadline-bounded reconsideration, allowing independently useful protection/life.

    Failure preserves the legal choice but replaces the false explanation;
    neither a forced pass nor a forced cancel is justified by this rule fact.
    """
    corrected, notes = correct_protection_claims(data.get("reasoning", ""), decision, state, chosen)
    if not notes:
        return data, chosen
    original = data.get("reasoning", "")
    check = {"original_ids": list(chosen), "original_reasoning": original, "facts": notes}
    trace["stack_protection_check"] = check
    try:
        revised, picked = parse(
            ask(
                message
                + "\n\nSTACK PROTECTION CHECK: "
                + " ".join(notes)
                + " Reconsider the legal options. Do not justify this play as stopping the counterspell. "
                "Life gain or protection of an actual battlefield permanent may independently justify it; "
                "if you keep it, state that real benefit. Use the same JSON schema.",
                effort="low",
                budget=12.0,
            )
        )
        if not decision.selection_is_valid(picked):
            raise ValueError("invalid revised selection")
        if any(o.option_id in picked and o.payable is False for o in decision.options):
            raise ValueError("unpayable revised selection")
        check.update(revised_ids=list(picked), revised_reasoning=revised.get("reasoning", ""))
        revised_text, repeated = correct_protection_claims(
            revised.get("reasoning", ""), decision, state, picked
        )
        if repeated:
            check["repeated_false_claim"] = True
            revised = {**revised, "reasoning": revised_text}
            trace["uncorrected_reasoning"] = check["revised_reasoning"]
        return revised, picked
    except Exception as error:
        check["failed"] = type(error).__name__
        trace["uncorrected_reasoning"] = original
        return {**data, "reasoning": corrected}, chosen
