"""Actual infect damage allocation and permanent counter changes in combat."""

from arenamcp.combat_loss import infect_source
from arenamcp.combat_protection import prevents_damage


def add_deltas(*changes):
    result = {}
    for change in changes:
        for identity, amount in change.items():
            result[identity] = result.get(identity, 0) + amount
    return result


def counter_change(card, *, minus=0, plus=0):
    """Apply NEW counters once; observed P/T already includes existing counters."""
    shift = plus - minus
    if not shift:
        return card
    raw = card.get("_card") or card
    counters = dict(raw.get("counters") or {})
    remaining = abs(shift)
    cancel = ("p1p1", "+1/+1") if shift < 0 else ("m1m1", "-1/-1")
    for kind in list(counters):
        if str(kind).lower().removeprefix("countertype_") in cancel:
            amount = min(remaining, max(0, int(counters[kind])))
            remaining -= amount
            counters[kind] -= amount
            if not counters[kind]:
                del counters[kind]
    if remaining:
        kind = "CounterType_M1M1" if shift < 0 else "CounterType_P1P1"
        counters[kind] = int(counters.get(kind, 0)) + remaining
    changed = {**card, "power": card["power"] + shift, "toughness": card["toughness"] + shift,
               "counters": counters, "_counter_shift": card.get("_counter_shift", 0) + shift}
    if raw is not card:
        changed["_card"] = {**raw, "counters": counters,
                            "power": int(raw.get("power") or 0) + shift,
                            "toughness": int(raw.get("toughness") or 0) + shift}
    return changed


def apply_deltas(cards, changes):
    return [counter_change(c, minus=changes.get(c.get("instance_id"), 0)) for c in cards]


def resolve_infect(attacker, assigned):
    """Two simultaneous damage steps; counters change stats before the next step.

    Ordinary damage remains marked damage; infect changes toughness and can
    kill an indestructible creature at zero. Only actual unprevented damage
    produces counters or lifelink. Input bodies are never changed.
    """
    from arenamcp.combat_solver import CombatOutcome, _damage_order, _has, _pt

    out = CombatOutcome()
    cards = [attacker, *assigned]
    shrink = {id(c): 0 for c in cards}
    marked = {id(c): 0 for c in cards}
    touched = set()
    dead = set()
    infect = {id(c): infect_source(c) for c in cards}
    first = {id(c): _has(c, "first strike") or _has(c, "double strike") for c in cards}
    double = {id(c): _has(c, "double strike") for c in cards}
    trample = _has(attacker, "trample")
    order = _damage_order(max(0, _pt(attacker)[0]), assigned, _has(attacker, "deathtouch"), attacker)

    def stats(card):
        p, t = _pt(card)
        return max(0, p - shrink[id(card)]), t - shrink[id(card)]

    def strike(source, target, amount, pending):
        if amount <= 0 or prevents_damage(target, source):
            return 0
        if infect[id(source)]:
            pending[id(target)] = pending.get(id(target), 0) + amount
            iid = target.get("instance_id")
            if iid is not None:
                out.counter_deltas[iid] = out.counter_deltas.get(iid, 0) + amount
        else:
            marked[id(target)] += amount
        if _has(source, "deathtouch"):
            touched.add(id(target))
        return amount

    for first_step in (True, False):
        if id(attacker) in dead:
            break
        alive = [b for b in order if id(b) not in dead]
        pending = {}
        # All powers for this step are read before applying its new counters.
        if first[id(attacker)] if first_step else (not first[id(attacker)] or double[id(attacker)]):
            left = stats(attacker)[0]
            dealt = 0
            targets = alive if trample else [b for b in alive if not prevents_damage(b, attacker)]
            for b in targets:
                lethal = 1 if _has(attacker, "deathtouch") else max(0, stats(b)[1] - marked[id(b)])
                amount = min(left, lethal)
                dealt += strike(attacker, b, amount, pending)
                left -= amount
            if trample and left > 0:
                out.damage_through += left
                out.player_hits += 1
                dealt += left
            elif targets and left > 0:
                dealt += strike(attacker, targets[-1], left, pending)
            if _has(attacker, "lifelink"):
                out.life_gained += dealt
        for b in alive:
            if first[id(b)] if first_step else (not first[id(b)] or double[id(b)]):
                dealt = strike(b, attacker, stats(b)[0], pending)
                if _has(b, "lifelink"):
                    out.blocker_life_gained += dealt
        for identity, amount in pending.items():
            shrink[identity] += amount
        for c in cards:
            toughness = stats(c)[1]
            if toughness <= 0 or (not _has(c, "indestructible") and
                                  (marked[id(c)] >= toughness or id(c) in touched)):
                dead.add(id(c))
        if first_step:
            out.first_strike_damage = out.damage_through
            out.first_strike_blocker_gain = out.blocker_life_gained
    out.attacker_died = id(attacker) in dead
    out.blockers_died = [b for b in assigned if id(b) in dead]
    if out.damage_through:
        out.damage_through += int(attacker.get("combat_hit_bonus") or 0)
    return out
