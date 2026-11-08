"""Evaluate repeated global-damage modes and cash in a doomed body before sweeping."""

import re

from arenamcp.combat_keywords import is_creature_now
from arenamcp.damage_decisions import hit_reward
from arenamcp.loyalty_removal import _POWER_DRAW, RemovalLine, _draw_fodder, _enough_library
from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import _damage_protected, find_source, lethal_damage
from arenamcp.ward import attached_to, ward_of

_SWEEP = re.compile(r"\bdeals (\d+) damage to each creature\.", re.I)
_FACE = re.compile(r"\bdeals (\d+) damage to each opponent\.", re.I)


def _bodies(state):
    return [c for c in state.get("battlefield") or [] if is_creature_now(c) and not c.get("is_phased_out")]


def _dies(c, damage):
    need = lethal_damage(c)
    return need is not None and need <= damage and not _damage_protected(c, targeted=False)


def _opponent_life(state, local):
    return next(
        (p.get("life_total") for p in state.get("players") or [] if p.get("seat_id") not in (None, local)),
        None,
    )


def repeat_sweep(decision, state, chosen):
    """Swap redundant face modes for additional kills, without sacrificing an additional friendly body."""
    if not 1 < decision.min_select == decision.max_select <= 5 or not decision.selection_is_valid(chosen):
        return None
    labels = [(o.option_id, o.label) for o in decision.options if o.meta.get("repeatedSelectAllowed")]
    sweep = [
        (oid, int(m[1]))
        for oid, label in labels
        if (m := _SWEEP.search(label)) and label.rstrip().endswith(m[0])
    ]
    face = [
        (oid, int(m[1]))
        for oid, label in labels
        if (m := _FACE.search(label)) and label.rstrip().endswith(m[0])
    ]
    if len(sweep) != 1 or len(face) != 1:
        return None
    (sid, amount), (fid, face_dmg) = sweep[0], face[0]
    if sid not in chosen or fid not in chosen or set(chosen) - {sid, fid}:
        return None
    local = get_local_seat_id(state)
    life = _opponent_life(state, local)
    if local is None or life is None or life <= face_dmg * chosen.count(fid):
        return None
    base = chosen.count(sid) * amount
    board = _bodies(state)
    if any(hit_reward(c) for c in board if c.get("controller_seat_id") != local):
        return None
    best = None
    most_kills = 0
    for more in range(1, chosen.count(fid) + 1):
        dmg = base + more * amount
        extra = [c for c in board if _dies(c, dmg) and not _dies(c, base)]
        if any(c.get("controller_seat_id") in (None, local) for c in extra):
            break
        if extra and len(extra) > most_kills:
            # Preserve all unneeded face damage.
            ids = [sid] * (chosen.count(sid) + more) + [fid] * (chosen.count(fid) - more)
            if decision.selection_is_valid(ids):
                most_kills = len(extra)
                best = (
                    ids,
                    f"Use {dmg} damage on each creature; {base} misses {', '.join(c['name'] for c in extra)}. No additional friendly creature dies.",
                )
    return best


def before_selected_sweep(decision, state, chosen, reason=""):
    """Sequence an offered power-sacrifice before a chosen sweeper if it completes an extra kill and draw."""
    from arenamcp.combat_strategy import loyalty

    local = get_local_seat_id(state)
    if (
        local is None
        or state.get("stack")
        or (state.get("turn") or {}).get("active_player") != local
        or len(chosen) != 1
    ):
        return None
    cast = decision.find(chosen[0])
    if not cast or cast.meta.get("actionType") != "ActionType_Cast" or cast.payable is False:
        return None
    text = re.sub(r"<[^>]+>", "", find_source(state, cast.meta).get("oracle_text") or "")
    match = _SWEEP.search(text)
    if not match:
        return None
    count = _repeat_count(text)
    if not count:
        return None
    if count > 1 and not re.search(
        r"damage (?:to )?each creature|\bsweep(?:s|ing|er)?\b|(?:clear|wipe)(?:s|ing)? (?:their|the) board",
        str(reason),
        re.I,
    ):
        return None  # The same card can be cast solely for a different mode.
    dmg = int(match[1]) * count
    board = _bodies(state)
    life = _opponent_life(state, local)
    if life is None or (face := _FACE.search(text)) and life <= int(face[1]) * count:
        return None
    for o in decision.options:
        ability = _POWER_DRAW.search(o.label)
        if not ability or o.meta.get("actionType") != "ActionType_Activate" or o.payable is False:
            continue
        walker = find_source(state, o.meta)
        if walker.get("controller_seat_id") != local or (loyalty(walker) or 0) < int(ability[1]):
            continue
        for food in board:
            if (
                not _draw_fodder(food, local, ability[2])
                or not _dies(food, dmg)
                or not _enough_library(state, food["power"])
            ):
                continue
            for enemy in sorted(board, key=lambda c: c.get("power") or 0, reverse=True):
                if (
                    enemy.get("controller_seat_id") in (None, local)
                    or hit_reward(enemy)
                    or _damage_protected(enemy, targeted=True)
                    or ward_of(enemy, attached_to(enemy, state["battlefield"]))
                ):
                    continue
                spends_walker = loyalty(walker) == int(ability[1])
                if spends_walker and not (
                    re.search(r"\b(?:when|whenever)\b[^.]*\battacks?\b", enemy.get("oracle_text") or "", re.I)
                    and any(
                        c is not enemy
                        and c.get("controller_seat_id") == enemy.get("controller_seat_id")
                        and _dies(c, dmg)
                        for c in board
                    )
                ):
                    continue
                if (
                    (need := lethal_damage(enemy)) is not None
                    and dmg < need <= dmg + food["power"]
                    and life > food["power"]
                ):
                    why = (
                        f"Before sweeping, sacrifice {food['name']} to {walker['name']} for {food['power']} damage "
                        f"to {enemy['name']} and draw {food['power']} {'card' if food['power'] == 1 else 'cards'}. The planned sweep would kill {food['name']}; "
                        f"{dmg} damage then finishes {enemy['name']} if both effects resolve."
                    )
                    if spends_walker:
                        why += " This uses its last loyalty to remove their attack engine."
                    return o.option_id, RemovalLine(
                        walker["name"], food["instance_id"], enemy["instance_id"], why
                    )
    return None


def sweep_plan_note(state):
    """Show the global damage threshold to the planner before it spends a doomed body."""
    board = _bodies(state)
    local = get_local_seat_id(state)
    if local is None:
        return ""
    for c in state.get("hand") or []:
        text = re.sub(r"<[^>]+>", "", c.get("oracle_text") or "")
        match = _SWEEP.search(text)
        if not match:
            continue
        count = _repeat_count(text)
        if not count:
            continue
        damage = count * int(match[1])
        ours = [x["name"] for x in board if x.get("controller_seat_id") == local and _dies(x, damage)]
        theirs = [
            x["name"] for x in board if x.get("controller_seat_id") not in (local, None) and _dies(x, damage)
        ]
        if ours or theirs:
            return (
                f"{c['name']}: {count} global damage mode(s) deal {damage} to each creature. "
                f"Ours that die: {', '.join(ours) or 'none'}; theirs: {', '.join(theirs) or 'none'}. "
                "Compare using a creature that would die for a legal sacrifice ability first; "
                "marked damage can finish a larger creature in the sweep."
            )
    return ""


def _repeat_count(text):
    repeat = re.match(r"Choose (two|three)\. You may choose the same mode more than once\.", text, re.I)
    if repeat:
        return {"two": 2, "three": 3}[repeat[1].lower()]
    return int(
        not re.search(r"\bchoose\b", text, re.I)
        and len(text.strip().splitlines()) == 1
        and bool((match := _SWEEP.search(text)) and match.end() == len(text.strip()))
    )


def fallback_sweep(decision, state):
    """Rate known repeatable damage modes if no non-damage mode has an opposing target."""
    local = get_local_seat_id(state)
    life = _opponent_life(state, local)
    count = decision.min_select
    if local is None or not isinstance(life, int) or not 1 < count == decision.max_select <= 5:
        return None
    artifacts = []
    sweep = face = None
    for o in decision.options:
        if not o.meta.get("repeatedSelectAllowed"):
            return None
        if (m := _SWEEP.search(o.label)) and o.label.endswith(m[0]):
            sweep = o.option_id, int(m[1])
        elif (m := _FACE.search(o.label)) and o.label.endswith(m[0]):
            face = o.option_id, int(m[1])
        elif o.label.endswith("Destroy target artifact."):
            artifacts = [
                c
                for c in state.get("battlefield") or []
                if "artifact" in str(c.get("type_line") or "").lower()
                and c.get("controller_seat_id") != local
            ]
        else:
            return None
    if not sweep or not face:
        return None
    board = _bodies(state)
    best = None
    for n in range(count + 1):
        ids = [sweep[0]] * n + [face[0]] * (count - n)
        if not decision.selection_is_valid(ids):
            continue
        dmg, burn = n * sweep[1], (count - n) * face[1]
        dead = [c for c in board if _dies(c, dmg)]
        value = sum(
            (1 if c.get("controller_seat_id") not in (None, local) else -1)
            * (2 + max(0, c.get("power") or 0))
            for c in dead
        )
        if dmg and any(hit_reward(c) and c.get("controller_seat_id") != local for c in board):
            continue
        score = (burn >= life, value, burn)
        if best is None or score > best[0]:
            best = (
                score,
                ids,
                f"{dmg} damage to each creature ({', '.join(c['name'] for c in dead) or 'none'} die), {burn} to the opponent",
            )
    if (
        best
        and best[0][0] is False
        and any(not _dies(c, best[1].count(sweep[0]) * sweep[1]) for c in artifacts)
    ):
        return None  # An artifact mode has independent value; defer to the normal chooser.
    return best[1:] if best else None
