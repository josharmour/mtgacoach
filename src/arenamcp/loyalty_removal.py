"""Price a power-based loyalty ability against creatures supporting an evasive attack."""

import re
from dataclasses import dataclass

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
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


_TAP_RESOURCE = re.compile(
    r"^Tap an untapped (artifact|creature) you control: Add (?:\{o?[WUBRGC]\}|one mana of any color)\.\s*$",
    re.I | re.M,
)


def mana_engine(card, battlefield):
    """The board objects this permanent lets its controller tap for mana."""
    match = _TAP_RESOURCE.search(card.get("oracle_text") or "")
    if not match or card.get("is_phased_out"):
        return []
    return [
        c
        for c in battlefield
        if c.get("controller_seat_id") == card.get("controller_seat_id")
        and not c.get("is_phased_out")
        and match[1].lower() in str(c.get("type_line") or "").lower()
    ]


def mana_engine_context(state):
    board = state.get("battlefield") or []
    local = get_local_seat_id(state)
    if local is None:
        return ""
    notes = []
    for card in board:
        if card.get("controller_seat_id") in (None, local):
            continue
        resources = mana_engine(card, board)
        if len(resources) >= 2:
            ready = sum(not c.get("is_tapped") for c in resources)
            notes.append(
                f"{card['name']} lets the opponent tap {len(resources)} matching permanents for mana "
                f"({ready} untapped now). Reassess the prior attack plan against this engine."
            )
    if notes and (alternative := engine_removal_note(state)):
        notes.append(alternative)
    return "\n".join(notes)


def engine_removal_note(state):
    """A concrete alternative for both the background planner and the current window."""
    from arenamcp.play_safety import _damage_protected, lethal_damage

    board = state.get("battlefield") or []
    local = get_local_seat_id(state)
    if local is None:
        return ""
    for walker in board:
        if walker.get("controller_seat_id") != local or walker.get("is_phased_out"):
            continue
        rules = re.sub(r"\[([−-]\d+)\]:\s*", r"[\1: ", walker.get("oracle_text") or "")
        ability = _POWER_DRAW.search(rules)
        if not ability or (loyalty(walker) or 0) < int(ability[1]):
            continue
        for target in board:
            if target.get("controller_seat_id") in (None, local) or not is_creature_now(target):
                continue
            resources = mana_engine(target, board)
            need = lethal_damage(target)
            if (
                len(resources) < 2
                or need is None
                or _damage_protected(target, targeted=True)
                or ward_of(target, attached_to(target, board))
            ):
                continue
            for food in board:
                if (
                    not _draw_fodder(food, local, ability[2])
                    or food["power"] < need
                    or not _enough_library(state, food["power"])
                ):
                    continue
                return (
                    f"Compare using {walker['name']} −{ability[1]} on {target['name']}: "
                    f"sacrifice {food['name']} for {food['power']} damage and {food['power']} cards. "
                    f"If it resolves, {target['name']} loses its ability to turn {len(resources)} current permanents into mana sources. "
                    "Compare with pumping and attacking, including which attackers the current combat line actually sends. "
                    "Normally activate loyalty in your own main phase with an empty stack. Recheck after combat and on the next turn."
                )
    return ""


def _enough_library(state, count):
    available = state.get("library_count")
    if available is None:
        available = (state.get("zones") or {}).get("library_count")
    return not isinstance(available, int) or available >= count


def _draw_fodder(card, local, subtype):
    return (
        card.get("controller_seat_id") == local
        and is_creature_now(card)
        and not card.get("is_phased_out")
        and isinstance(card.get("power"), int)
        and card["power"] > 0
        and subtype.casefold()
        in {str(t).casefold().removeprefix("subtype_") for t in card.get("subtypes") or []}
    )


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
        if points is None or points < int(m[1]):
            continue
        best = None
        for food in board:
            power = food.get("power")
            if not _draw_fodder(food, local, m[2]) or not _enough_library(state, power):
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
                stops_attack = before >= points - int(m[1]) and saved >= 4 and after < points - int(m[1])
                resources = mana_engine(target, board)
                cuts_mana = len(resources) >= 2 and power >= 3
                if not (stops_attack or cuts_mana):
                    continue
                # Let the normal planner resolve a possible lethal attack instead.
                if (
                    cuts_mana
                    and not stops_attack
                    and (
                        opp_life is None
                        or opp_life
                        <= power
                        + sum(
                            max(0, c.get("power") or 0)
                            for c in board
                            if c.get("controller_seat_id") == local and is_creature_now(c)
                        )
                    )
                ):
                    continue
                rank = (stops_attack, saved, len(resources), power)
                if best is None or rank > best[0]:
                    reason = (
                        f"Sacrifice {food['name']} to deal {power} to {target['name']} and draw {power}. "
                        f"If it resolves, their evasive attack drops from {before} to {after} power."
                    )
                    if cuts_mana:
                        reason = (
                            f"Sacrifice {food['name']} for {power} damage to {target['name']} and draw {power}. "
                            f"Removing that mana ability cuts off its use of {len(resources)} current permanents."
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


def reserve_postcombat_loyalty(decision, state, step):
    """Keep a planned postcombat activation available if its attacker can survive visible blocks."""
    from dataclasses import replace

    from arenamcp.combat_strategy import _eligible_blockers, able_attackers

    if (
        decision.request_type != "ActionsAvailable"
        or not step
        or state.get("stack")
        or not decision.find("pass")
    ):
        return decision
    turn = state.get("turn") or {}
    local = get_local_seat_id(state)
    if turn.get("active_player") != local or str(turn.get("phase")).lower().removeprefix("phase_") != "main1":
        return decision
    intent = step.get("postcombat_sacrifice") or {}
    hold = str(step.get("hold") or "").lower()
    if not intent and (
        not re.search(r"after combat|post[- ]?combat|second main", hold)
        or not re.search(r"sacrific", hold)
        or re.search(r"\b(?:if|unless|avoid|never|not|instead)\b", hold)
    ):
        return decision
    attack = str(step.get("attack") or "").lower()
    board = state.get("battlefield") or []
    opp = next((p for p in state.get("players") or [] if p.get("seat_id") != local), {})
    if not opp or not isinstance(opp.get("life_total"), int):
        return decision
    blockers = _eligible_blockers(state, opp["seat_id"])
    if any(
        not isinstance(c.get("power"), int)
        or has_combat_keyword(c, "deathtouch")
        or has_combat_keyword(c, "double strike")
        or re.search(r"when(?:ever)?[^.]*blocks?\b", c.get("oracle_text") or "", re.I)
        for c in blockers
    ):
        return decision
    power = sum(max(0, c["power"]) for c in blockers)
    reserved = set()
    for option in decision.options:
        ability = _POWER_DRAW.search(option.label)
        if not ability or option.meta.get("actionType") != "ActionType_Activate" or option.payable is False:
            continue
        source = next((c for c in board if c.get("instance_id") == option.meta.get("instanceId")), {})
        if intent and (intent.get("source") != source.get("name") or intent.get("cost") != -int(ability[1])):
            continue
        if not intent and not re.search(r"[−-]" + ability[1] + r"(?!\d)", hold):
            continue
        for food in able_attackers(state):
            if not _draw_fodder(food, local, ability[2]):
                continue
            name = r"(?<!\w)" + re.escape(food["name"].lower()) + r"(?!\w)"
            if (
                re.search(name, attack)
                and (intent.get("attacker") == food["name"] if intent else re.search(name, hold))
                and food["power"] < opp["life_total"]
                and (food.get("toughness") or 0) - (food.get("damage") or 0) > power
            ):
                reserved.add(option.meta.get("instanceId"))
    if not reserved or defensive_minus(decision, state):
        return decision
    options = tuple(
        o
        for o in decision.options
        if not (
            o.meta.get("instanceId") in reserved
            and o.meta.get("actionType") == "ActionType_Activate"
            and re.search(r"\[[+−-]?\d+:", o.label)
        )
    )
    return replace(decision, options=options)


def postcombat_activation(decision, state, step):
    """Finish the planned activation only while the named source and attacker still support it."""
    intent = (step or {}).get("postcombat_sacrifice") or {}
    turn = state.get("turn") or {}
    local = get_local_seat_id(state)
    if (
        not intent
        or decision.request_type != "ActionsAvailable"
        or state.get("stack")
        or turn.get("active_player") != local
        or str(turn.get("phase")).lower().removeprefix("phase_") != "main2"
    ):
        return None
    board = state.get("battlefield") or []
    for option in decision.options:
        ability = _POWER_DRAW.search(option.label)
        if not ability or option.payable is False or option.meta.get("actionType") != "ActionType_Activate":
            continue
        if -int(ability[1]) != intent.get("cost"):
            continue
        walker = next((c for c in board if c.get("instance_id") == option.meta.get("instanceId")), {})
        if walker.get("name") != intent.get("source") or walker.get("controller_seat_id") != local:
            continue
        food = next(
            (
                c
                for c in board
                if c.get("name") == intent.get("attacker") and _draw_fodder(c, local, ability[2])
            ),
            None,
        )
        if food and _enough_library(state, food["power"]):
            return (
                option.option_id,
                f"Combat is over and {food['name']} survived. Use the planned {intent['cost']} for {food['power']} damage and cards.",
            )
    return None


def align_offered_removal(plan, state):
    """Install the same urgent first action the live picker would execute, only on a real menu."""
    from arenamcp.decisions import build_pending_decision, label_sibling_activations

    turn = state.get("turn") or {}
    if (
        state.get("stack")
        or turn.get("active_player") != get_local_seat_id(state)
        or str(turn.get("phase")).lower().removeprefix("phase_") not in {"main1", "main2"}
    ):
        return
    step = plan.step_for(turn.get("turn_number"))
    actions = state.get("_bridge_actions") if "_bridge_actions" in state else state.get("legal_actions_raw")
    # Background forecasts made before the opponent finishes never promise
    # an activation. Only reconcile against this snapshot's actual offer.
    if not step or not actions or not engine_removal_note(state):
        return
    abilities = [a for a in actions if a.get("actionType") == "ActionType_Activate"]
    if not abilities:
        return
    menu = build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "actions": abilities, "can_pass": True}
    )
    if menu is None or not (pick := defensive_minus(label_sibling_activations(menu), state)):
        return
    oid, line = pick
    participants = [menu.find(oid).meta["instanceId"], line.fodder, line.target]
    plan.issues.append("Current loyalty activation replaces the forecast until its damage and draw resolve.")
    plan.develop_next = line.reason
    plan.path = line.reason
    # The removed creature, sacrificed attacker and new hand all invalidate
    # the projected combat and follow-up turns. Replan on the resulting board.
    plan.turn_plan = [{**step, "cast": [], "land": "", "attack": "", "hold": line.reason}]
    plan.turn_plan[0].pop("postcombat_sacrifice", None)
    plan.turn_plan[0]["removal_participants"] = participants
    plan.switch_if = ["Reassess when the ability resolves or its target changes."]
