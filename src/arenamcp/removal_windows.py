"""Narrow, evidence-based removal intent checks. Unknown clauses stay unknown."""

import re
from dataclasses import dataclass

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.mana import get_local_seat_id, mana_cost_to_cmc
from arenamcp.mulligan_policy import hand_card
from arenamcp.play_safety import find_source
from arenamcp.ward import available_mana, cast_targeting_text, ward_of, ward_payable

_TARGET = re.compile(
    r"\b(?:destroy|exile) target (?P<kind>(?:nonland |noncreature )?permanent|creature|artifact(?: or enchantment)?|enchantment)(?P<tail>[^.\n]*)",
    re.I,
)
_LIMIT = re.compile(r"with (mana value|power|toughness) (\d+) or (less|greater)", re.I)
_PLAINS = re.compile(r"with mana value less than or equal to the number of plains you control", re.I)


def target_eligibility(source: dict, target: dict, state: dict) -> bool | None:
    """Whether supported destroy/exile clauses can target this battlefield object."""
    local = get_local_seat_id(state)
    text = cast_targeting_text(source).replace("’", "'")
    clauses = list(_TARGET.finditer(text))
    if not clauses or local is None:
        return None
    results = []
    for clause in clauses:
        tail = clause["tail"].lower()
        # Duration is not an extra target restriction.
        tail = re.split(r"\buntil\b", tail, maxsplit=1)[0].strip()
        control_clause = _LIMIT.sub("", _PLAINS.sub("", tail))
        leftover = re.sub(
            r"\ban opponent controls\b|\byou control\b|\byou don't control\b", "", control_clause
        ).strip()
        if leftover:
            results.append(None)
            continue
        types = str(target.get("type_line") or "").lower()
        controller = target.get("controller_seat_id")
        if not types or controller is None:
            results.append(None)
            continue
        kind = clause["kind"].lower()
        if kind == "nonland permanent":
            allowed = "land" not in types
        elif kind == "noncreature permanent":
            allowed = "creature" not in types
        elif kind == "permanent":
            allowed = any(
                k in types for k in ("creature", "land", "artifact", "enchantment", "planeswalker", "battle")
            )
        else:
            allowed = any(k in types for k in kind.split(" or "))
        if (
            "an opponent controls" in control_clause or "you don't control" in control_clause
        ) and controller == local:
            allowed = False
        if "you control" in control_clause and controller != local:
            allowed = False
        if not allowed:
            results.append(False)
            continue
        if has_combat_keyword(target, "shroud") or (
            controller != local and has_combat_keyword(target, "hexproof")
        ):
            results.append(False)
            continue
        # Other protection needs the full targeting engine; do not assert eligibility.
        if re.search(
            r"\bprotection\b|\bhexproof from\b", str(target.get("oracle_text") or ""), re.I
        ) or target.get("protection_from_players"):
            results.append(None)
            continue
        limit = _LIMIT.search(tail)
        plains = _PLAINS.search(tail)
        if limit or plains:
            stat = limit[1].lower() if limit else "mana value"
            if stat == "mana value":
                value = target.get("cmc", target.get("mana_value"))
                if value is None and target.get("mana_cost"):
                    value = mana_cost_to_cmc(target["mana_cost"])
            else:
                value = target.get(stat)
            try:
                value = float(value)
            except (TypeError, ValueError):
                results.append(None)
                continue
            if plains:
                maximum = sum(
                    c.get("controller_seat_id") == local
                    and bool(re.search(r"\bplains\b", str(c.get("type_line") or ""), re.I))
                    for c in state.get("battlefield") or []
                )
                allowed = value <= maximum
            else:
                allowed = value <= int(limit[2]) if limit[3].lower() == "less" else value >= int(limit[2])
        results.append(allowed)
    return True if True in results else None if None in results else False


@dataclass(frozen=True)
class RemovalWindow:
    option_id: str
    source: str
    target: str
    target_id: int
    face: str

    @property
    def reason(self):
        return (
            f"{self.source} can answer {self.target} now, but the supplied {self.face} face has "
            "hexproof/shroud. Holding this targeted spell to answer that transformation is not a valid line. "
            "Craft also exiles the source as a cost. Choose the current removal or explain a different "
            "concrete benefit for holding; do not assume craft prerequisites or an alternate win."
        )


def invalid_transform_hold(decision, state: dict, chosen: list[str], reasoning: str) -> RemovalWindow | None:
    """An explicit hold-for-craft claim contradicted by current targeting facts."""
    if chosen != ["pass"] or decision.request_type != "ActionsAvailable" or state.get("stack"):
        return None
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if (
        local is None
        or turn.get("active_player") != local
        or str(turn.get("phase") or "").lower() not in {"main1", "main2", "phase_main1", "phase_main2"}
    ):
        return None
    reason = str(reasoning or "").lower().replace("’", "'")
    # Scope negation to the hold clause, not an unrelated later explanation.
    claims = re.split(r"[.;]", reason)
    claim = next(
        (c for c in claims if re.search(r"\bhold\b.*\b(?:for|until|after)\b.*\b(?:craft|transform)", c)), ""
    )
    if not claim or re.search(r"\b(?:not|can't|cannot|instead|unless|if)\b", claim):
        return None
    if any(
        re.search(
            r"(?:as though|lose|can't have)[^.]*\b(?:hexproof|shroud)\b",
            str(c.get("oracle_text") or ""),
            re.I,
        )
        for c in state.get("battlefield") or []
    ):
        return None  # a visible exception needs the full rules engine
    for option in decision.options:
        if option.payable is not True or option.meta.get("actionType") != "ActionType_Cast":
            continue
        source = find_source(state, option.meta)
        name = str(source.get("name") or "")
        if not name or name.lower() not in claim or hand_card(source).instant_speed:
            continue
        for target in state.get("battlefield") or []:
            target_name = str(target.get("name") or "")
            if (
                target.get("controller_seat_id") in (None, local)
                or not target_name
                or target_name.lower() not in claim
            ):
                continue
            if not re.search(r"\bcraft\b", str(target.get("oracle_text") or ""), re.I):
                continue
            if target_eligibility(source, target, state) is not True:
                continue
            ward = ward_of(
                target,
                [
                    c
                    for c in state.get("battlefield") or []
                    if c.get("attached_to_id") == target.get("instance_id")
                ],
            )
            cost = (
                sum(int(c.get("count") or 0) for c in option.meta.get("manaCost") or [])
                or hand_card(source).mana_value
            )
            if ward is not None and ward_payable(ward, state, available_mana(state) - cost) is not True:
                continue
            for face in target.get("related_faces") or []:
                if re.search(
                    r"(?:^|[,\n])\s*(?:hexproof|shroud)\s*(?:$|[,\n]|\()",
                    str(face.get("oracle_text") or ""),
                    re.I,
                ):
                    return RemovalWindow(
                        option.option_id,
                        name,
                        target_name,
                        target["instance_id"],
                        str(face.get("name") or "transformed"),
                    )
    return None


def invalid_removal_intent(decision, state: dict, chosen: list[str], reasoning: str) -> str:
    """Refute a specifically named illegal removal target before announcing the cast."""
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1 or decision.find("pass") is None:
        return ""
    option = decision.find(chosen[0])
    if option is None or option.meta.get("actionType") != "ActionType_Cast":
        return ""
    source = find_source(state, option.meta)
    if not source or not _TARGET.search(cast_targeting_text(source)):
        return ""
    local = get_local_seat_id(state)
    for zone in ("battlefield", "stack"):
        for target in state.get(zone) or []:
            name = str(target.get("name") or "")
            if not name or target.get("controller_seat_id") in (None, local):
                continue
            intended = re.search(
                r"\b(?:exil(?:e|ing)|destroy(?:ing)?|remov(?:e|ing)|kill(?:ing)?|target(?:ing)?)\s+"
                + re.escape(name)
                + r"(?=$|[\s.,;:#])",
                str(reasoning or ""),
                re.I,
            )
            if not intended:
                continue
            prefix = str(reasoning or "")[: intended.start()].rsplit(".", 1)[-1].rsplit(";", 1)[-1]
            if re.search(r"\b(?:not|can't|cannot|don't|never|if)\b", prefix, re.I):
                continue
            legal = target_eligibility(source, target, state)
            if zone == "stack" and legal is not None:
                return f"{name} is still on the stack; {source.get('name')} targets a battlefield permanent, not that spell."
            if legal is False:
                return f"{name} is not a legal target for any supported removal mode of {source.get('name')} on this board."
    return ""


def plains_cap_targets(state: dict, instance_id: int, name: str = "") -> tuple[int, frozenset] | None:
    """Facts that can reopen a cancelled Plains-scaled removal, not unrelated requests."""
    source = find_source(state, {"instanceId": instance_id}, name=name)
    if not source or not _PLAINS.search(cast_targeting_text(source)):
        return None
    local = get_local_seat_id(state)
    if local is None:
        return None
    battlefield = state.get("battlefield") or []
    count = sum(
        c.get("controller_seat_id") == local
        and bool(re.search(r"\bplains\b", str(c.get("type_line") or ""), re.I))
        for c in battlefield
    )
    targets = frozenset(
        c["instance_id"]
        for c in battlefield
        if c.get("instance_id")
        and c.get("controller_seat_id") not in (None, local)
        and target_eligibility(source, c, state) is True
    )
    return count, targets
