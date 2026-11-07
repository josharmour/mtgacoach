"""Consequences of damage that cross the cast, target and combat decision windows."""

import re

from arenamcp.combat_keywords import is_creature_now
from arenamcp.combat_solver import can_attack_next_turn
from arenamcp.decisions import expand_target_selection
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import _damage_protected, damage_effect, lethal_damage
from arenamcp.ward import attached_to, ward_of


def hit_reward(card):
    """A benefit for the controller triggered by damage to this permanent, including lethal damage."""
    text = re.sub(r"<[^>]+>", "", str(card.get("oracle_text") or ""))
    name = re.escape(str(card.get("name") or "CARDNAME"))
    for m in re.finditer(
        rf"\bwhenever (?:this (?:creature|permanent)|cardname|{name}) is dealt damage, ([^.]+)", text, re.I
    ):
        if re.match(r"(?:you (?:may )?)?(?:search your library|draw |create |gain \d+ life)", m[1], re.I):
            return m[1]
    return ""


def opponent_board(state):
    local = get_local_seat_id(state)
    return [
        c
        for c in state.get("battlefield") or []
        if local is not None
        and c.get("controller_seat_id") not in (None, local)
        and not c.get("is_phased_out")
        and is_creature_now(c)
    ]


def named_damage_recipient(state, reasoning):
    """Only bind affirmative damage verbs to one uniquely named opposing battlefield object."""
    selected = []
    for card in opponent_board(state):
        name = str(card.get("name") or "").split(",")[0]
        if not name or not card.get("instance_id"):
            continue
        if re.search(
            r"\b(?:pings?|burns?|aims? (?:it |the damage )?at|deal(?:s)? (?:\d+ )?damage to)\s+"
            + re.escape(name)
            + r"(?![\w'-])",
            str(reasoning),
            re.I,
        ):
            selected.append(card["instance_id"])
    return selected[0] if len(selected) == 1 else None


def attack_facts(state):
    local = get_local_seat_id(state)
    if local is None or (state.get("turn") or {}).get("active_player") != local:
        return ""
    theirs = opponent_board(state)
    able = [
        c
        for c in theirs
        if can_attack_next_turn(c)
        and (c.get("power") or 0) > 0
        and not (
            c.get("is_tapped")
            and any("stun" in str(k).lower() and v for k, v in (c.get("counters") or {}).items())
        )
    ]
    if not able:
        return ""
    return (
        f"Opponent has {sum(c.get('is_tapped') is False for c in theirs)} visibly untapped creatures now. "
        f"At their next normal combat, {len(able)} current creatures with {sum(c['power'] for c in able)} total power "
        "could attack before blocks or other effects. Summoning sickness now does not protect us next turn."
    )


def combat_contradiction(state, reasoning):
    if re.search(r"\ball (?:of )?their creatures (?:are )?tapped", str(reasoning), re.I) and any(
        c.get("is_tapped") is False for c in opponent_board(state)
    ):
        return attack_facts(state)
    return ""


def strategy_facts(state):
    from arenamcp.sweep_sequence import sweep_plan_note

    rows = [
        f"Damaging {c['name']} triggers its controller's ability even if lethal: {benefit}."
        for c in opponent_board(state)
        if (benefit := hit_reward(c))
    ]
    attack = attack_facts(state)
    if attack:
        rows.append(attack)
    if sweep := sweep_plan_note(state):
        rows.append(sweep)
    return "\n".join(rows)


def _amount(oracle, state, decision):
    fixed = damage_effect({"oracle_text": oracle}, kicked=False)
    if fixed and fixed[1] in ("any", "target"):
        return fixed[0]
    # In an enters trigger 'that creature' need not be the trigger's parent.
    # Arena identifies the referenced creature in this target slot's prompt.
    if not re.search(r"that creature deals damage equal to its power to any target\b", oracle, re.I):
        return None
    ctx = state.get("decision_context") or {}
    live = state.get("_bridge_request_payload") or {}
    if (live.get("sourceId") or live.get("source_id")) not in (None, ctx.get("source_id")):
        return None
    slots = ctx.get("targets") or []
    if (
        len(slots) != 1
        or len(decision.slots) != 1
        or slots[0].get("targetIdx") != decision.slots[0].target_idx
    ):
        return None
    ids = {
        param.get("numberValue")
        for param in (slots[0].get("prompt") or {}).get("parameters") or []
        if param.get("parameterName") == "CardId"
    }
    bodies = [c for c in state.get("battlefield") or [] if c.get("instance_id") in ids and is_creature_now(c)]
    if len(ids) == len(bodies) == 1:
        return (
            bodies[0].get("modified_power")
            if isinstance(bodies[0].get("modified_power"), int)
            else bodies[0].get("power")
        )
    return None


def safer_damage_target(decision, state, chosen, oracle, intended=None):
    """Replace a costly on-damage trigger only with a proven kill or lethal opposing player."""
    if len(chosen) != 1 or decision.max_select != 1 or not chosen[0].startswith("tgt:"):
        return None
    theirs = {f"tgt:{c['instance_id']}": c for c in opponent_board(state) if c.get("instance_id")}
    target = theirs.get(chosen[0])
    if not target or not (reward := hit_reward(target)):
        return None
    damage = _amount(oracle, state, decision)
    if type(damage) is not int or damage < 1:
        return None
    board = state.get("battlefield") or []
    our_life = next(
        (
            p.get("life_total")
            for p in state.get("players") or []
            if p.get("seat_id") == get_local_seat_id(state)
        ),
        None,
    )
    choices = []
    for o in decision.options:
        oid = o.option_id
        if not expand_target_selection(decision, [oid]):
            continue
        player = o.meta.get("player_seat")
        if (
            player is not None
            and player != get_local_seat_id(state)
            and any(
                p.get("seat_id") == player and 0 < (p.get("life_total") or 0) <= damage
                for p in state.get("players") or []
            )
        ):
            choices.append(((2, 0, 0), oid, o.label))
        c = theirs.get(oid)
        if (
            c
            and isinstance(our_life, int)
            and (target.get("power") or 0) < our_life
            and (
                oid == intended
                or (c.get("power") or 0) >= (target.get("power") or 0)
                or (
                    "that many" in reward.lower()
                    and damage >= 3
                    and re.search(r"\b(?:whenever|at the beginning)\b", c.get("oracle_text") or "", re.I)
                )
            )
            and not hit_reward(c)
            and (need := lethal_damage(c)) is not None
            and need <= damage
            and not _damage_protected(c, targeted=True)
            and not ward_of(c, attached_to(c, board))
        ):
            choices.append(((1, oid == intended, c.get("power") or 0), oid, c["name"]))
    if choices:
        _, pick, name = max(choices)
        return [pick], (
            f"Aim {damage} damage at {name}. Even lethal damage to {target['name']} "
            f"gives its controller this trigger: {reward}."
        )
    return None
