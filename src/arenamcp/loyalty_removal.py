"""Price a power-based loyalty ability against creatures supporting an evasive attack."""

import re
from dataclasses import dataclass

from arenamcp.combat_keywords import is_creature_now
from arenamcp.combat_strategy import loyalty
from arenamcp.mana import get_local_seat_id
from arenamcp.ward import attached_to, ward_of

_POWER_DRAW = re.compile(
    r"\[(?:-|−)(\d+): Sacrifice a creature\. When you do, [^.]+ deals X damage to any target, "
    r"where X is (?:that|the sacrificed) creature's power\. If the sacrificed creature was an? (\w+), draw X cards",
    re.I,
)
_ANTHEM = re.compile(r"^(?:other )?(\w+) you control get \+(\d+)/[+-]\d+\.$", re.I | re.M)


@dataclass(frozen=True)
class RemovalLine:
    walker: str
    fodder: int
    target: int
    reason: str


def _without_lord(board, food, target):
    """Only subtract a simple unconditional tribal bonus from matching creatures."""
    projected = []
    anthem = _ANTHEM.search(str(target.get("oracle_text") or ""))
    for card in board:
        if card is target or card is food:
            continue
        if anthem and card.get("controller_seat_id") == target.get("controller_seat_id"):
            tribe = anthem[1].casefold()
            types = {str(t).casefold().removeprefix("subtype_") for t in card.get("subtypes") or []}
            if isinstance(card.get("power"), int) and (
                tribe == "creatures"
                and "creature" in str(card.get("type_line")).lower()
                or any(
                    tribe in {t, t + "s", t + "es", t[:-1] + "ves" if t.endswith("f") else t} for t in types
                )
            ):
                card = {**card, "power": card["power"] - int(anthem[2])}
        projected.append(card)
    return projected


def defensive_minus(decision, state):
    from arenamcp.play_safety import _damage_protected, evasive_power_next_turn, lethal_damage

    if decision.request_type != "ActionsAvailable" or state.get("stack"):
        return None
    local = get_local_seat_id(state)
    if local is None or (state.get("turn") or {}).get("active_player") != local:
        return None
    board = state.get("battlefield") or []
    players = state.get("players") or []
    opp_life = next((p.get("life_total") for p in players if p.get("seat_id") != local), None)
    for option in decision.options:
        m = _POWER_DRAW.search(option.label)
        if not m or option.meta.get("actionType") != "ActionType_Activate" or option.payable is False:
            continue
        walker = next((c for c in board if c.get("instance_id") == option.meta.get("instanceId")), {})
        before, _ = evasive_power_next_turn(state)
        points = loyalty(walker)
        if points is None or before < 4 or before < points - int(m[1]):
            continue
        best = None
        for food in board:
            power = food.get("power")
            if (
                food.get("controller_seat_id") != local
                or food.get("is_phased_out")
                or not isinstance(power, int)
                or power < 1
            ):
                continue
            if m[2].casefold() not in {
                str(t).casefold().removeprefix("subtype_") for t in food.get("subtypes") or []
            }:
                continue
            if opp_life is not None and power >= opp_life:
                return None  # Let the normal lethal-target policy finish the game.
            for target in board:
                if target.get("controller_seat_id") in (None, local) or target.get("is_phased_out"):
                    continue
                if not is_creature_now(target):
                    continue
                need = lethal_damage(target)
                if (
                    need is None
                    or need > power
                    or _damage_protected(target, targeted=True)
                    or ward_of(target, attached_to(target, board))
                ):
                    continue
                after, _ = evasive_power_next_turn(
                    {**state, "battlefield": _without_lord(board, food, target)}
                )
                saved = before - after
                if saved < 4 or after >= points - int(m[1]):
                    continue
                rank = (saved, power)
                if best is None or rank > best[0]:
                    reason = (
                        f"Sacrifice {food['name']} to deal {power} to {target['name']} and draw {power}. "
                        f"If it resolves, their evasive attack drops from {before} to {after} power."
                    )
                    best = (
                        rank,
                        RemovalLine(walker["name"], food["instance_id"], target["instance_id"], reason),
                    )
        if best:
            return option.option_id, best[1]
    return None


def choose_removal(decision, state, pending):
    key = state.get("match_id"), (state.get("turn") or {}).get("turn_number")
    if pending and pending[0] == key and 0 < decision.request_id[0] - pending[1] <= 12:
        line = pending[2]
        source = str(decision.source_label or (state.get("decision_context") or {}).get("source_card") or "")
        if (
            decision.request_type == "SelectN"
            and decision.prompt_text == "Sacrifice a creature."
            and (
                source in {line.walker, line.walker + " ability"}
                or any(
                    c.get("name") in {line.walker, line.walker + " ability"} for c in state.get("stack") or []
                )
            )
        ):
            oid = f"sel:{line.fodder}"
            if decision.find(oid) and decision.selection_is_valid([oid]):
                return (oid, line.reason), pending
        if decision.request_type == "SelectTargets" and source in {line.walker, line.walker + " ability"}:
            oid = f"tgt:{line.target}"
            if decision.find(oid) and decision.selection_is_valid([oid]):
                return (oid, line.reason), None
    pick = defensive_minus(decision, state)
    if pick:
        oid, line = pick
        return (oid, line.reason), (key, decision.request_id[0], line) if all(key) else None
    return None, None
