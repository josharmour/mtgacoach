"""Pure annotation functions for the MTG card taxonomy.

`annotate_menu_row(name, taxonomy)` returns a string like:
    "Cast Doom Blade [REMOVAL: destroy target creature]"

The taxonomy is a dict: name -> {roles, primitives, types, mana_cost}.

When a card has multiple roles they are comma-separated:
    "Cast Lightning Helix [REMOVAL: 3 damage to any target]"
"""

# Short descriptions for each role, used in annotation
_ROLE_DESC = {
    "REMOVAL": "remove target permanent",
    "COUNTER": "counter target spell",
    "TUTOR": "search library for card",
    "RECURSION": "return from graveyard",
    "RAMP": "accelerate mana",
    "DRAW": "draw cards",
    "COMBAT_TRICK": "pump creatures",
    "TOKEN": "create creature tokens",
    "GROWTH": "put +1/+1 counters",
    "LIFEGAIN": "gain life",
    "ANIMATION": "animate permanent",
    "DISRUPTION": "force discard",
    "ATTACKER": "large attacker",
    "BLOCKER": "defensive creature",
    "EVASION": "evasive creature",
    "LIFELINK": "lifelink creature",
}

# Primitives that sound natural in annotation, mapped to short phrases
_PRIMITIVE_DESC = {
    "Destroy": "destroy target",
    "DealDamage": "deal damage",
    "Counter": "counter target",
    "ChangeZone": "move card",
    "Draw": "draw cards",
    "Dig": "dig for cards",
    "Mana": "produce mana",
    "Pump": "pump creature",
    "PumpAll": "pump creatures",
    "Token": "create token",
    "GainControl": "gain control",
    "Sacrifice": "force sacrifice",
    "Charm": "choose mode",
    "PutCounter": "put counters",
    "GainLife": "gain life",
    "Animate": "animate permanent",
    "Discard": "force discard",
}

# Short type abbreviations
_TYPE_ABBREV = {
    "Instant": "instant",
    "Sorcery": "sorcery",
    "Creature": "creature",
    "Enchantment": "enchantment",
    "Artifact": "artifact",
    "Planeswalker": "planeswalker",
    "Land": "land",
    "Battle": "battle",
}


def annotate_menu_row(name, taxonomy):
    """Return an annotated menu row string, or the plain name if unknown.

    Format: "Cast Doom Blade [REMOVAL: destroy target nonblack creature]"
    If the card is not in the taxonomy, returns plain name.
    If the card has no roles, returns plain name.
    """
    entry = taxonomy.get(name)
    if not entry:
        return name

    roles = entry.get("roles", [])
    if not roles:
        return name

    # Build role tags
    role_tags = "/".join(roles)

    # Build a short description from the types + primitives
    primitives = entry.get("primitives", [])
    desc = _describe_card(name, entry, roles, primitives)

    return f"Cast {name} [{role_tags}: {desc}]"


def _describe_card(name, entry, roles, primitives):
    """Build a short one-line description of what the card does."""
    types = entry.get("types", "")
    mana_cost = entry.get("mana_cost", "")

    # For single-role cards, give a role-specific description
    if len(roles) == 1:
        role = roles[0]
        if role == "REMOVAL":
            if "Destroy" in primitives and "DealDamage" not in primitives:
                return "destroy target"
            if "DealDamage" in primitives:
                return "deal damage"
            if "ChangeZone" in primitives:
                # Check if it's bounce or exile
                return "remove target"
            if "GainControl" in primitives:
                return "steal target permanent"
            return "remove target"
        if role == "DRAW":
            return "draw cards"
        if role == "COUNTER":
            return "counter target spell"
        if role == "RAMP":
            if "Mana" in primitives:
                return "produce mana"
            return "fetch land"
        if role == "RECURSION":
            return "return from graveyard"
        if role == "TUTOR":
            return "search library"
        if role == "TOKEN":
            return "create tokens"
        if role == "COMBAT_TRICK":
            return "pump creatures"
        if role == "GROWTH":
            return "put +1/+1 counters"
        if role == "LIFEGAIN":
            return "gain life"
        if role == "ANIMATION":
            return "animate permanent"
        if role == "DISRUPTION":
            return "force discard"
        if role == "ATTACKER":
            return "large aggressive creature"
        if role == "BLOCKER":
            return "defensive creature"
        if role == "EVASION":
            return "evasive creature"
        if role == "LIFELINK":
            return "lifelink creature"

    # Multi-role: use most distinctive primitive
    if primitives:
        prim = primitives[0]
        return _PRIMITIVE_DESC.get(prim, prim.lower())

    return "ability"


def get_card_type_abbrev(types_str):
    """Get a short type description for a card."""
    if not types_str:
        return ""
    seen = set()
    parts = []
    for t in types_str.split():
        abbrev = _TYPE_ABBREV.get(t, t.lower())
        if abbrev not in seen:
            parts.append(abbrev)
            seen.add(abbrev)
    return " ".join(parts)
