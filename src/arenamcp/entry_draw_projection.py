"""Unsupported forecast boundary for G04/#308's entry/draw/ping sequence.

The search currently adds creature spells as durable bodies without resolving
this public trigger chain. Do not rank that incomplete projection as a complete
line. This is not a prohibition on casting: the separate sequence values retain
the draw, possible body loss, face damage and Army growth as conditional branches.
"""

from arenamcp.combat_keywords import is_creature_now
from arenamcp.public_sequence import _draw_entry
from arenamcp.sequence_value import _punisher


def model_reasons(model):
    """Known battlefield engines plus a modeled creature cast or pending entry.

    Bounded to the retained incident's mechanics, not arbitrary draw/entry
    effects. Once-per-turn use is not in BoardModel. Even a use observed now
    would not establish availability on later turns of this search horizon.
    """
    board = [c for c in model.battlefield if not c.get("is_phased_out")]
    engines = [c for c in board if c.get("controller_seat_id") == model.local and _draw_entry(c)]
    if not engines:
        return ()
    punishers = [c for c in board if c.get("controller_seat_id") == model.opponent and _punisher(c)]
    if not punishers:
        return ()
    entering = [s.card for s in model.spells] + [b.get("_card") or b for b in model.our_stack_bodies]
    for card in entering:
        if not is_creature_now(card):
            continue
        power = card.get("modified_power", card.get("power"))
        for engine in engines:
            if (
                card.get("instance_id") is not None
                and card.get("instance_id") == engine.get("instance_id")
            ):
                continue  # "other creatures": an engine does not draw for itself
            if type(power) is int and power > int(_draw_entry(engine)[1]):
                continue
            # Unknown entry power also cannot establish a harmless entry.
            return (
                f"entry-draw continuation: {card.get('name', 'creature')} may trigger "
                f"{engine.get('name', 'draw engine')}, then {punishers[0].get('name', 'draw punisher')}. "
                "Trigger availability, the drawn card, opponent ping target and Army growth are not "
                "resolved by this search. Do not infer a surviving body, future activation or forced "
                "combat outcome; compare the conditional public outcomes instead.",
            )
    return ()
