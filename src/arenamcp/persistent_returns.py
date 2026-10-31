"""Bounded self-returning enchantment engines; never creature stand-ins."""

import re


def returned_enchantment(card):
    raw = card.get("_card") or card
    text = str(raw.get("oracle_text") or "").lower().replace("’", "'")
    name = str(raw.get("name") or "").lower()
    if name:
        text = text.replace(name, "cardname")
    if "creature" not in str(raw.get("type_line") or "").lower() or raw.get("is_token"):
        return None
    if not re.search(
        r"when (?:cardname|this creature) dies, if it was a creature, return it to the battlefield", text
    ):
        return None
    if not re.search(r"it(?:'s| is) an enchantment", text):
        return None
    return {**raw, "type_line": "Enchantment", "power": None, "toughness": None, "is_tapped": False}


def engine_value(card):
    """Residual material value, not a claim that its next trigger has happened."""
    text = str(card.get("oracle_text") or "").lower()
    if "whenever you gain life" in text and "loses that much life" in text:
        return 4.0
    if "draw a card" in text:
        return 3.0
    return 2.0


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
