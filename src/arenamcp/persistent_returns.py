"""Bounded self-returning enchantment engines; never creature stand-ins."""

import re


def returned_enchantment(card):
    raw = card.get("_card") or card
    text = str(raw.get("oracle_text") or "").lower().replace("’", "'")
    name = str(raw.get("name") or "").lower()
    if name:
        text = text.replace(name, "cardname")
    types = raw.get("card_types")
    creature = (
        any(str(kind).lower().removeprefix("cardtype_") == "creature" for kind in types)
        if types is not None
        else "creature" in str(raw.get("type_line") or "").lower()
    )
    if (
        not creature
        or raw.get("is_token")
        or "token" in str(raw.get("object_kind") or "").lower()
        or "token" in str(raw.get("type_line") or "").lower()
        or raw.get("is_phased_out")
        or raw.get("face_down")
        or raw.get("is_copy")
    ):
        return None
    if not re.search(
        r"when (?:cardname|this creature) dies, if it was a creature, return it to the battlefield", text
    ):
        return None
    if not re.search(r"it(?:'s| is) an enchantment", text):
        return None
    # This is a fresh permanent, not the dying body with a different label.
    # Current GRE types otherwise override type_line and turn it back into a
    # creature; old counters/combat state and a thief's control also cannot
    # survive the zone change (#270/#307).
    returned = {
        **raw,
        "type_line": "Enchantment",
        "card_types": ["CardType_Enchantment"],
        "subtypes": [],
        "power": None,
        "toughness": None,
        "is_tapped": False,
        "is_attacking": False,
        "is_blocking": False,
        "counters": {},
        "attachments": [],
        "damage": 0,
        "damaged_this_turn": False,
    }
    if "under its owner's control" in text:
        returned["controller_seat_id"] = raw.get("owner_seat_id")
    return returned


def return_notes(battlefield, local_seat):
    """Conditional combat facts, not a promise that a trigger resolves.

    Body value and engine value are separate: losing Innocence's body may
    still be costly, while killing Tenacity's body does not remove its drain.
    These facts belong beside ordinary death-trigger warnings in the prompt.
    """
    from arenamcp.death_bodies import death_is_suppressed

    cards = [
        c
        for c in battlefield or []
        if isinstance(c, dict) and not c.get("is_phased_out") and not c.get("face_down")
    ]
    suppressed = death_is_suppressed(cards)
    notes = []
    for card in cards:
        engine = returned_enchantment(card)
        if engine is None:
            continue
        label = f"{card.get('name', 'Creature')} [id:{card.get('instance_id')}]"
        if suppressed:
            notes.append(f"{label}: visible death-trigger suppression prevents its return trigger")
            continue
        owner = engine.get("controller_seat_id")
        side = (
            "its owner's"
            if owner is None or local_seat is None
            else "your"
            if owner == local_seat
            else "their"
        )
        text = str(engine.get("oracle_text") or "").lower()
        role = "draw engine" if "draw a card" in text else "engine"
        if "whenever you gain life" in text and "loses that much life" in text:
            role = "lifegain-drain engine"
        notes.append(
            f"{label}: if it dies as a creature and its return trigger resolves, it returns under "
            f"{side} control as a noncreature enchantment retaining its {role}. "
            "The creature body is lost, but the engine is not removed; exile, graveyard interference "
            "or countering the trigger can prevent the return. Compare the body cost separately"
        )
    return notes


def engine_value(card):
    """Residual material value, not a claim that its next trigger has happened."""
    text = str(card.get("oracle_text") or "").lower()
    if "whenever you gain life" in text and "loses that much life" in text:
        return 4.0
    if "draw a card" in text:
        return 3.0
    return 2.0


def return_value(card):
    """Bounded residual engine value, independent of whether return is prevented."""
    raw = card.get("_card") or card
    # Ordinary creatures dominate combat searches. Avoid return parsing there.
    if "dies, if it was a creature" not in str(raw.get("oracle_text") or "").lower():
        return 0
    engine = returned_enchantment(card)
    return int(engine_value(engine)) if engine is not None else 0


def return_credit(card):
    """Material retained by the dying controller (negative when the opponent gets it).

    Unknown ownership earns no credit. Visible death-trigger suppression or
    potential graveyard-exile replacement withholds credit rather than guessing
    how that replacement resolves. This is not an alternate-zone interpreter.
    """
    raw = card.get("_card") or card
    if raw.get("_return_credit_blocked") or card.get("_return_credit_blocked"):
        return 0
    amount = return_value(card)
    if not amount:
        return 0
    controller = raw.get("controller_seat_id", raw.get("owner_seat_id"))
    returned = returned_enchantment(card)
    recipient = returned.get("controller_seat_id", returned.get("owner_seat_id"))
    if controller is None or recipient is None:
        return 0
    return amount if controller == recipient else -amount


def annotate_return_credit(battlefield):
    """Refresh the visible interference that affects combat return valuation."""
    from arenamcp.death_bodies import death_is_suppressed

    cards = [c for c in battlefield if not c.get("is_phased_out") and not c.get("face_down")]
    blocked = death_is_suppressed(cards) or any(
        "exile" in (text := str(c.get("oracle_text") or "").lower())
        and "instead" in text and ("graveyard" in text or "die" in text)
        for c in cards
    )
    for card in battlefield:
        if blocked:
            card["_return_credit_blocked"] = True
        else:
            card.pop("_return_credit_blocked", None)


def gain_drain(cards, gained, life):
    """Known unconditional gain -> that-much loss, including the visible return loop."""
    if gained <= 0:
        return 0
    texts = [str((c.get("_card") or c).get("oracle_text") or "").lower() for c in cards]
    # This transition extension covers the observed returning engine, not every
    # historical drain card whose clocks have separate existing approximations.
    returning_texts = [
        t
        for t in texts
        if "dies, if it was a creature, return it to the battlefield" in t
        and ("it is an enchantment" in t or "it's an enchantment" in t or "it’s an enchantment" in t)
    ]
    count = sum(
        bool(re.search(r"whenever you gain life, (?:target|each) opponent loses that much life", t))
        for t in returning_texts
    )
    reverse = any(re.search(r"whenever an opponent loses life, you gain that much life", t) for t in texts)
    return max(life, count * gained) if count and reverse else count * gained
