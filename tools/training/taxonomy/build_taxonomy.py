#!/usr/bin/env python3
"""WP3-Taxonomy: parse 33,402 Forge card scripts and build functional card taxonomy.

Scans /Users/joshu/.forge/res/cardsfolder/ for .txt scripts, extracts:
  - Name, ManaCost, Types
  - All ability primitives from A: lines, T: lines (triggers), S: lines
    (statics), and SVar sub-abilities
  - Maps primitives to functional roles (REMOVAL, COUNTER, TUTOR, etc.)

Outputs tools/training/taxonomy/card_taxonomy.json.

Usage:
  python3 tools/training/taxonomy/build_taxonomy.py [--report]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

CARDSFOLDER = "/Users/joshu/.forge/res/cardsfolder"
OUTPUT_JSON = "tools/training/taxonomy/card_taxonomy.json"
DECK_DIR = "/Volumes/repos/magezero/xmage/decks"
DECK_FILES = [
    "UWTempo.dck",
    "Standard-MonoR.dck",
    "Standard-MonoG.dck",
    "Standard-MonoB.dck",
    "Standard-MonoW.dck",
    "Standard-MonoU.dck",
]

# ── Role definitions ──────────────────────────────────────────────────────
#
# Each role has a list of matchers. A matcher is a dict:
#   {"primitive": str|list-of-str, "sp_ab": str|None, "check": callable|None}
#
# "primitive" — required primitive name (or list of accepted names).
# "sp_ab" — optional sp_ab filter (SP, AB, DB, or None = any).
# "check" — optional callable(params_dict) -> bool for additional context.
#
# A primitive from a sub-ability (sp_ab == "DB") represents a card effect
# that happens when the card is cast/activated — it IS the card's function,
# so we don't filter by sp_ab for sub-abilities.

ROLE_MAP = {
    "REMOVAL": [
        # Destroy (any context)
        {"primitive": "Destroy"},
        # DealDamage — direct damage = removal
        {"primitive": "DealDamage"},
        # ChangeZone to Exile
        {"primitive": "ChangeZone", "check": lambda p: p.get("Destination") == "Exile"},
        # ChangeZoneAll to Exile
        {"primitive": "ChangeZoneAll", "check": lambda p: p.get("Destination") == "Exile"},
        # DestroyAll / DamageAll — mass removal
        {"primitive": "DestroyAll"},
        {"primitive": "DamageAll"},
        # Sacrifice forced on opponent's permanents
        # Checks both Defined$ and ValidTgts$ (some scripts use ValidTgts$ Player)
        {
            "primitive": "Sacrifice",
            "check": lambda p: (
                p.get("Defined", "") in ("Opponent", "EachOpponent", "TargetedController")
                or "Player" in p.get("ValidTgts", "")
            ),
        },
        # Bounce (Battlefield -> Hand) — tempo removal
        {
            "primitive": "ChangeZone",
            "check": lambda p: p.get("Origin") == "Battlefield" and p.get("Destination") == "Hand",
        },
        # GainControl — steal a permanent (removes from opponent's control)
        {"primitive": "GainControl"},
        # Fight — two creatures fight
        {"primitive": "Fight"},
        # Creature deals damage based on power
        {"primitive": "DamageReflex"},
    ],
    "COUNTER": [
        {"primitive": "Counter"},
    ],
    "TUTOR": [
        # Search library -> hand for non-land, non-basic cards
        {
            "primitive": "ChangeZone",
            "check": lambda p: (
                p.get("Origin") == "Library"
                and p.get("Destination") == "Hand"
                and "Land" not in p.get("ChangeType", "")
            ),
        },
    ],
    "RECURSION": [
        # Graveyard -> Battlefield (reanimate)
        {
            "primitive": "ChangeZone",
            "check": lambda p: p.get("Origin") == "Graveyard" and p.get("Destination") == "Battlefield",
        },
        # Graveyard -> Hand (raise dead)
        {
            "primitive": "ChangeZone",
            "check": lambda p: p.get("Origin") == "Graveyard" and p.get("Destination") == "Hand",
        },
    ],
    "RAMP": [
        # Mana-producing abilities (rituals + mana abilities)
        {"primitive": "Mana"},
        # Land-fetch (Library -> Battlefield for lands)
        {
            "primitive": "ChangeZone",
            "check": lambda p: (
                p.get("Origin") == "Library"
                and p.get("Destination") == "Battlefield"
                and "Land" in p.get("ChangeType", "")
            ),
        },
    ],
    "DRAW": [
        {"primitive": "Draw"},
        # Dig — looting, filtering library
        {"primitive": "Dig"},
        # Explore — puts a land in hand or a +1/+1 counter
        {"primitive": "Explore"},
        # Surveil — look at top card, put in graveyard
        {"primitive": "Surveil"},
    ],
    # Scry and Mill were mapped to DRAW, whose annotation is literally "draw
    # cards" — neither draws anything. Scry reorders your own library; Mill puts
    # cards into a graveyard (usually the OPPONENT's, i.e. a clock, not card
    # advantage). Distinct role rather than a conflation.
    "LIBRARY": [
        {"primitive": "Scry"},
        {"primitive": "Mill"},
    ],
    "COMBAT_TRICK": [
        # Instant-speed pump to creatures
        {"primitive": "Pump", "sp_ab": "SP", "check": lambda p: "Creature" in p.get("ValidTgts", "")},
        # PumpAll at instant/sorcery speed to your creatures.
        # sp_ab="SP" restored: without it a static/activated lord ability
        # (Coppercoat Vanguard's anthem) is labelled a COMBAT_TRICK, which is a
        # different card role entirely — a trick is held, an anthem is deployed.
        {"primitive": "PumpAll", "sp_ab": "SP"},
    ],
    # The non-instant half of PumpAll. Restoring the sp_ab="SP" filter above
    # correctly stopped labelling lords and auras as COMBAT_TRICK, but that left
    # them with NO role (Coppercoat Vanguard, Combat Research,
    # Shardmage's Rescue). Unclassified is better than mislabelled, but an anthem
    # has a real and different role: a trick is HELD for a blowout, an anthem is
    # DEPLOYED and changes every future combat. Separate role, not a shared one.
    "ANTHEM": [
        {"primitive": "PumpAll"},
        {"primitive": "Pump", "check": lambda p: "Creature" not in p.get("ValidTgts", "")},
    ],
    "TOKEN": [
        # Creating creature tokens
        {"primitive": "Token"},
        # RepeatEach is NOT mapped here. It is Forge's generic loop primitive
        # ("for each opponent...", "for each creature you control..."), used by
        # effects that make tokens AND by effects that drain, draw, or destroy.
        # Adeline happens to loop over opponents to make tokens, which is what
        # made it look like a token maker; the primitive itself carries no such
        # meaning, so mapping it to TOKEN mislabels every non-token loop.
    ],
    "GROWTH": [
        # Putting +1/+1 counters on creatures
        {"primitive": "PutCounter"},
        # PutCounterAll — put counters on multiple creatures
        {"primitive": "PutCounterAll"},
    ],
    "LIFEGAIN": [
        # Gaining life
        {"primitive": "GainLife"},
    ],
    # LoseLife was mapped to LIFEGAIN as "life manipulation (both sides)". The
    # two are opposite effects and lead to opposite plays: LIFEGAIN is a
    # stabiliser you cast when behind, LifeLoss is a clock you point at the
    # opponent. Collapsing them teaches the model they are interchangeable.
    "LIFELOSS": [
        {"primitive": "LoseLife"},
    ],
    "ANIMATION": [
        # Animating non-creature permanents into creatures
        {"primitive": "Animate"},
        # AnimateAll
        {"primitive": "AnimateAll"},
    ],
    "DISRUPTION": [
        # Forcing opponent(s) to discard cards — hand disruption
        {"primitive": "Discard"},
        # RaiseCost — taxing effects (Thalia-style)
        {"primitive": "RaiseCost"},
    ],
    "PROTECTION": [
        # Regenerate
        {"primitive": "Regenerate"},
        # Prevent damage
        {"primitive": "PreventDamage"},
        # Give hexproof/indestructible/ward via pump
        {
            "primitive": "Pump",
            "check": lambda p: any(
                kw in p.get("KW", "") for kw in ("Hexproof", "Indestructible", "Ward", "Protection")
            ),
        },
    ],
}


def parse_card_script(text):
    """Parse a Forge .txt card script into a structured dict.

    Handles:
      - Single-faced (first Name: line)
      - Double-faced cards (multiple Name: lines)
      - A: ability lines parsed into param dicts
      - T: triggered ability lines (stored for later SVar resolution)
      - S: static/continuous ability lines (stored for later classification)
      - SVar definitions (sub-abilities for recursive traversal)

    Returns dict with keys: name, all_names, mana_cost, types, primitives,
    svars, trigger_svars, static_mode_lines, keywords, power, toughness.
    """
    result = {
        "name": "",
        "all_names": [],  # all Name: lines (for DFC lookup)
        "mana_cost": "",
        "types": "",
        "power": "",  # Power from PT: line (may be "*" for CDA)
        "toughness": "",  # Toughness from PT: line
        "keywords": [],  # Keywords from K: lines on front face
        "primitives": [],  # list of {"primitive": str, "sp_ab": str, **params}
        "svars": {},  # SVar definitions for sub-ability traversal
        "trigger_svars": [],  # SVar names referenced by T: line Execute$ fields
        "static_lines": [],  # S: line dicts for static ability classification
        "_alternate": False,  # internal: have we hit the ALTERNATE marker?
    }

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Alternate (DFC separator) — stop collecting body info
        if line.startswith("ALTERNATE"):
            result["_alternate"] = True

        # Name (collect ALL Name lines for DFC support)
        elif line.startswith("Name:"):
            name_val = line[len("Name:") :].strip()
            result["all_names"].append(name_val)
            if not result["name"]:
                result["name"] = name_val

        # ManaCost
        elif line.startswith("ManaCost:") and not result["mana_cost"]:
            result["mana_cost"] = line[len("ManaCost:") :].strip()

        # Types
        elif line.startswith("Types:") and not result["types"]:
            result["types"] = line[len("Types:") :].strip()

        # PT — Power/Toughness (first line only; DFC back face has own PT)
        elif line.startswith("PT:") and not result["power"]:
            pt = line[len("PT:") :].strip()
            if "/" in pt:
                parts = pt.split("/", 1)
                result["power"] = parts[0].strip()
                result["toughness"] = parts[1].strip()

        # K: — Keyword abilities (front face only, skip DFC back face)
        elif line.startswith("K:") and not line.startswith("K:</") and not result["_alternate"]:
            kw_raw = line[len("K:") :].strip()
            if kw_raw:
                for segment in kw_raw.split(","):
                    segment = segment.strip()
                    if not segment:
                        continue
                    kw_name = segment.split(":")[0].strip()
                    if kw_name:
                        result["keywords"].append(kw_name)

        # A: lines — ability definitions (cast/activated)
        elif line.startswith("A:") and "$" in line:
            params = _parse_ability_line(line)
            if params:
                result["primitives"].append(params)

        # T: lines — triggered ability definitions
        elif line.startswith("T:") and "Mode$" in line:
            t_info = _parse_trigger_line(line)
            if t_info and t_info.get("execute_svar") and not result["_alternate"]:
                result["trigger_svars"].append(t_info["execute_svar"])
            if t_info and t_info.get("mode") and not result["_alternate"]:
                result["static_lines"].append(
                    {
                        "mode": "TRIGGER",
                        "trigger_mode": t_info["mode"],
                        "original": t_info,
                    }
                )

        # S: lines — static/continuous ability definitions
        elif line.startswith("S:") and "Mode$" in line:
            s_info = _parse_static_line(line)
            if s_info and not result["_alternate"]:
                result["static_lines"].append(s_info)

        # SVar sub-abilities (recursively traversed)
        elif line.startswith("SVar:") and "$" in line:
            svar = _parse_svar_line(line)
            if svar:
                name = svar.pop("svar_name")
                result["svars"][name] = svar

    return result


def _parse_ability_line(line):
    """Parse A:SP$ Primitive | Key$ Value | ... into param dict."""
    rest = line[2:]  # strip leading A:

    dollar_idx = rest.find("$")
    if dollar_idx < 0:
        return None

    sp_ab = rest[:dollar_idx].strip()  # e.g. "SP", "AB"
    remainder = rest[dollar_idx + 1 :].strip()

    parts = [p.strip() for p in remainder.split("|")]
    params = {"sp_ab": sp_ab, "primitive": parts[0] if parts else ""}

    for part in parts[1:]:
        if "$" in part:
            key, _, val = part.partition("$")
            params[key.strip()] = val.strip()

    return params


def _parse_svar_line(line):
    """Parse SVar:Name:DB$ Primitive | Key$ Value | ... into param dict."""
    rest = line[5:]  # strip SVar:

    dollar_idx = rest.find("$")
    if dollar_idx < 0:
        return None

    prefix = rest[:dollar_idx]  # e.g. "DBGainLife:DB" or "STCantBeCast:Mode"
    remainder = rest[dollar_idx + 1 :].strip()

    # Type is always the part after the last colon before $
    colon_idx = prefix.rfind(":")
    if colon_idx < 0:
        return {"svar_name": prefix, "svar_type": "", "primitive": remainder.split("|")[0].strip()}

    svar_name = prefix[:colon_idx]
    svar_type = prefix[colon_idx + 1 :]

    if svar_type not in ("DB", "AB"):
        # Not a sub-ability — static ability, stored but not traversed
        return {"svar_name": svar_name, "svar_type": svar_type}

    # Parse like a regular ability line
    parts = [p.strip() for p in remainder.split("|")]
    params = {
        "svar_name": svar_name,
        "svar_type": svar_type,
        "sp_ab": "AB" if svar_type == "AB" else "DB",  # AB = activated, DB = sub-ability
        "primitive": parts[0] if parts else "",
    }

    for part in parts[1:]:
        if "$" in part:
            key, _, val = part.partition("$")
            params[key.strip()] = val.strip()

    return params


def _parse_trigger_line(line):
    """Parse T:Mode$ X | Execute$ SVarName | ... into a dict.

    Returns {"mode": mode, "execute_svar": svar_name} or None.
    """
    rest = line[2:]  # strip leading T:
    result = {}

    parts = [p.strip() for p in rest.split("|")]
    for part in parts:
        if part.startswith("Mode$"):
            result["mode"] = part[5:].strip()
        elif part.startswith("Execute$"):
            result["execute_svar"] = part[8:].strip()
        elif part.startswith("ExecuteTrigger$"):
            # Some cards use ExecuteTrigger$ instead — same idea
            if "execute_svar" not in result:
                result["execute_svar"] = part[16:].strip()

    return result if result else None


def _parse_static_line(line):
    """Parse S:Mode$ X | ... into a structured dict.

    Returns dict with keys: mode, affected, add_power, add_toughness, etc.
    """
    rest = line[2:]  # strip leading S:
    result = {"mode": None}

    # Check for Mode$
    if "Mode$" in rest:
        mode_start = rest.find("Mode$") + 5
        mode_end = rest.find("|", mode_start)
        if mode_end < 0:
            result["mode"] = rest[mode_start:].strip()
        else:
            result["mode"] = rest[mode_start:mode_end].strip()

    parts = [p.strip() for p in rest.split("|")]
    for part in parts:
        if "$" in part:
            key, _, val = part.partition("$")
            key = key.strip()
            val = val.strip()
            if key in ("Affected", "ValidCard", "ValidTarget"):
                result["affected"] = val
            elif key == "AddPower":
                result["add_power"] = val
            elif key == "AddToughness":
                result["add_toughness"] = val
            elif key == "AddKeyword":
                result["add_keyword"] = val
            elif key == "AddTrigger":
                result["add_trigger"] = val
            elif key == "Amount":
                result["amount"] = val
            elif key == "ValidCard" and result.get("mode") == "RaiseCost":
                result["raised_cards"] = val
            elif key == "IsPresent":
                result["is_present"] = val

    return result


def resolve_primitives(card):
    """Resolve all primitives including recursively following SubAbility
    chains, Charm Choices, T: line triggered abilities, and S: line
    static abilities.

    Charm cards (primitive == "Charm") have a Choices$ field listing
    SVars for each mode. Each mode is a DB sub-ability whose primitives
    are part of the card's function.

    Cards with no A: lines may still have T: lines (triggered abilities)
    that reference SVar:DB sub-abilities — these are now also resolved.

    Cards with S: (static) lines like Continuous (lord pump) or RaiseCost
    are represented as synthetic primitives.

    Returns list of param dicts including sub-ability and charm-mode
    primitives.
    """
    all_primitives = list(card["primitives"])
    seen_svars = set()

    # 1. Follow A: line SubAbility chains (existing behavior)
    for prim in card["primitives"]:
        # Resolve Charm choices
        if prim.get("primitive") == "Charm":
            choices = prim.get("Choices", "")
            for choice_name in choices.split(","):
                choice_name = choice_name.strip()
                if choice_name and choice_name not in seen_svars:
                    seen_svars.add(choice_name)
                    _resolve_sub(card, choice_name, all_primitives, seen_svars)

        # Resolve SubAbility chain
        sub_name = prim.get("SubAbility")
        if sub_name and sub_name not in seen_svars:
            seen_svars.add(sub_name)
            _resolve_sub(card, sub_name, all_primitives, seen_svars)

    # 2. Resolve T: line triggered SVars (NEW - for cards with no A: lines
    #    but with T: triggered abilities that reference sub-abilities)
    for trigger_svar_name in card.get("trigger_svars", []):
        if trigger_svar_name and trigger_svar_name not in seen_svars:
            seen_svars.add(trigger_svar_name)
            _resolve_sub(card, trigger_svar_name, all_primitives, seen_svars)

    # 3. Resolve S: line static abilities into synthetic primitives
    for s_info in card.get("static_lines", []):
        if s_info.get("mode") == "Continuous":
            # Lord pump effects: S:Mode$ Continuous | Affected$ ... | AddPower$ ...
            # This is functionally similar to PumpAll
            if s_info.get("add_power") or s_info.get("add_toughness") or s_info.get("add_keyword"):
                all_primitives.append(
                    {
                        "sp_ab": "ST",
                        "primitive": "PumpAll",
                        "affected": s_info.get("affected", ""),
                        "is_present": s_info.get("is_present", ""),
                        "_source": "static",
                    }
                )
        elif s_info.get("mode") == "RaiseCost":
            # Tax effects: S:Mode$ RaiseCost | ValidCard$ ... | Amount$ ...
            all_primitives.append(
                {
                    "sp_ab": "ST",
                    "primitive": "RaiseCost",
                    "valid_card": s_info.get("raised_cards", ""),
                    "amount": s_info.get("amount", ""),
                    "_source": "static",
                }
            )
        elif s_info.get("mode") == "TRIGGER":
            # Some T: lines that don't reference SVars may still grant ability
            add_trigger = s_info.get("original", {}).get("add_trigger", "")
            if add_trigger and add_trigger not in seen_svars:
                seen_svars.add(add_trigger)
                _resolve_sub(card, add_trigger, all_primitives, seen_svars)

    return all_primitives


def _resolve_sub(card, svar_name, all_primitives, seen_svars):
    """Helper to recursively follow sub-ability chains.

    Follows SubAbility (standard chain), TrueSubAbility / FalseSubAbility
    (Branch nodes used in conditional effects), and RepeatSubAbility (the body of
    a RepeatEach loop).

    RepeatSubAbility is why RepeatEach must NOT be mapped to a role directly.
    RepeatEach is Forge's generic loop; what the loop DOES lives one hop away.
    Adeline, Resplendent Cathar:

        T:Mode$ AttackersDeclared | Execute$ DBRepeat
        SVar:DBRepeat:DB$ RepeatEach | RepeatPlayers$ Opponent | RepeatSubAbility$ DBToken
        SVar:DBToken:DB$ Token | TokenScript$ ...

    so her Token primitive is two hops from the trigger, and without following
    RepeatSubAbility she resolves to no token role at all. Other RepeatEach cards
    point at DBTap (Raiding Party), TrigSac (Rishadan Brigand), DBFlip (Rakdos) —
    following the reference classifies each by what it actually does instead of
    assuming every loop makes tokens.
    """
    sub = card["svars"].get(svar_name)
    if not sub or sub.get("svar_type") not in ("DB", "AB"):
        return
    all_primitives.append(sub)

    # Standard chain
    next_sub = sub.get("SubAbility")
    if next_sub and next_sub not in seen_svars:
        seen_svars.add(next_sub)
        _resolve_sub(card, next_sub, all_primitives, seen_svars)

    # Branch chains + RepeatEach body
    for branch_key in ("TrueSubAbility", "FalseSubAbility", "RepeatSubAbility"):
        branch_sub = sub.get(branch_key)
        if branch_sub and branch_sub not in seen_svars:
            seen_svars.add(branch_sub)
            _resolve_sub(card, branch_sub, all_primitives, seen_svars)


def classify_card(card):
    """Given a parsed card, return (roles_set, unmatched_primitives_set).

    First maps ability primitives to roles, then applies the creature-body
    heuristic for cards whose function isn't captured by A: line primitives.
    """
    primitives = resolve_primitives(card)
    roles = set()
    unmatched_primitives = set()

    for prim in primitives:
        primitive_name = prim.get("primitive", "")
        if not primitive_name:
            continue

        sp_ab = prim.get("sp_ab", "")
        matched_any_role = False

        for role_name, matchers in ROLE_MAP.items():
            for matcher in matchers:
                # Check primitive name match
                prim_match = matcher.get("primitive")
                if isinstance(prim_match, list):
                    if primitive_name not in prim_match:
                        continue
                elif isinstance(prim_match, str):
                    if primitive_name != prim_match:
                        continue
                else:
                    continue  # malformed matcher

                # Check sp_ab filter (if specified)
                required_sp_ab = matcher.get("sp_ab")
                if required_sp_ab is not None and sp_ab != required_sp_ab:
                    continue

                # Check additional context (if specified)
                check_fn = matcher.get("check")
                if check_fn is not None:
                    try:
                        if not check_fn(prim):
                            continue
                    except Exception:
                        continue

                roles.add(role_name)
                matched_any_role = True

        if not matched_any_role:
            unmatched_primitives.add(primitive_name)

    # Apply creature-body heuristic — supplements primitive roles with
    # combat-relevant body data (ATTACKER, BLOCKER, EVASION, LIFELINK, REMOVAL)
    # for any creature card, regardless of primitive role assignment
    body_roles = classify_creature_body(card)
    roles.update(body_roles)

    return roles, unmatched_primitives


# ── Creature-body heuristic ──────────────────────────────────────────────
#
# For creatures whose function isn't captured by A: line primitives (vanilla
# creatures, creatures with only triggered/static abilities, etc.), derive
# roles from the card's body: power/toughness and keyword abilities.

_EVASION_KEYWORDS = {
    "Flying",
    "Menace",
    "Trample",
    "Fear",
    "Shadow",
    "Intimidate",
    "Unblockable",
    "Skulk",
    "Horsemanship",
}

_ATTACKER_KEYWORDS = {
    "Haste",
    "First Strike",
    "Double Strike",
    "Toxic",
    "Infect",
}

_COMBAT_TRICK_KEYWORDS = {
    "Flash",
    "Prowess",
}

_REMOVAL_KEYWORDS = {
    "Deathtouch",
}


def classify_creature_body(card):
    """Derive combat roles from a creature's body (PT, keywords, types).

    Only meaningful for creatures. Returns a set of roles (ATTACKER, BLOCKER,
    EVASION, LIFELINK, COMBAT_TRICK, REMOVAL).
    """
    roles = set()
    types = card.get("types", "")

    if "Creature" not in types:
        return roles

    power_str = card.get("power", "")
    toughness_str = card.get("toughness", "")
    keywords = card.get("keywords", [])
    kw_set = set(k.strip() for k in keywords if isinstance(k, str))

    # ── Numeric power/toughness ──
    power_val = None
    toughness_val = None
    try:
        power_val = int(power_str)
    except (ValueError, TypeError):
        pass
    try:
        toughness_val = int(toughness_str)
    except (ValueError, TypeError):
        pass

    # ATTACKER: power >= 3, or creature with offensive keyword abilities
    if power_val is not None and power_val >= 3:
        roles.add("ATTACKER")
    if kw_set & _ATTACKER_KEYWORDS:
        roles.add("ATTACKER")

    # BLOCKER: toughness > power (defensive profile)
    if toughness_val is not None and power_val is not None and toughness_val > power_val:
        roles.add("BLOCKER")
    # Vigilance is a blocker enabler (can attack and still block)
    if "Vigilance" in kw_set:
        roles.add("BLOCKER")
    # Defender is a blocker enforcer
    if "Defender" in kw_set:
        roles.add("BLOCKER")

    # EVASION: keywords that make the creature hard to block
    if kw_set & _EVASION_KEYWORDS:
        roles.add("EVASION")

    # LIFELINK
    if "Lifelink" in kw_set:
        roles.add("LIFELINK")

    # COMBAT_TRICK: keywords that synergize with spells/instant-speed play
    if kw_set & _COMBAT_TRICK_KEYWORDS:
        roles.add("COMBAT_TRICK")

    # REMOVAL: deathtouch means it kills in combat
    if kw_set & _REMOVAL_KEYWORDS:
        roles.add("REMOVAL")

    return roles


def dfc_names(all_names):
    """Given the list of all Name: lines from a card script, generate
    both individual face names and the // dual name for lookup.

    Example: ['Unholy Annex', 'Ritual Chamber']
    -> {'Unholy Annex', 'Ritual Chamber', 'Unholy Annex // Ritual Chamber'}
    """
    result = set(all_names)
    if len(all_names) >= 2:
        result.add(" // ".join(all_names))
    return result


def extract_deck_card_names():
    """Extract card names from the specified .dck files."""
    all_names = []
    for fname in DECK_FILES:
        path = os.path.join(DECK_DIR, fname)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip().rstrip("\r")
                    if not line or line.startswith("LAYOUT"):
                        continue
                    m = re.match(r"^\d+\s+\[[\w:]+\]\s+(.+)$", line)
                    if m:
                        all_names.append(m.group(1).strip())
        except FileNotFoundError:
            print(f"  [WARN] Deck file not found: {path}", file=sys.stderr)
    return all_names


def main():
    parser = argparse.ArgumentParser(description="Build card taxonomy from Forge scripts")
    parser.add_argument("--report", action="store_true", help="Print taxonomy report")
    args = parser.parse_args()

    # ── Scan all card scripts ──────────────────────────────────────────
    print("Scanning Forge card scripts...")
    scripts = []
    for root, dirs, files in os.walk(CARDSFOLDER):
        for fn in files:
            if fn.endswith(".txt"):
                scripts.append(os.path.join(root, fn))

    total_scripts = len(scripts)
    print(f"  Found {total_scripts} .txt scripts")
    parse_errors = 0
    parsed_cards = {}  # canonical name -> entry
    name_aliases = {}  # alt_name -> canonical_name (for DFCs)
    all_unmatched = Counter()
    distinct_primitives_found = set()
    role_counter = Counter()
    cards_with_ability_api = 0
    cards_with_svars = 0  # NEW: cards that get primitives from resolved SVars

    for path in scripts:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            parse_errors += 1
            continue

        card = parse_card_script(text)
        if not card["name"]:
            parse_errors += 1
            continue

        prims = resolve_primitives(card)

        # Track stats
        if prims:
            cards_with_ability_api += 1
        # Count cards that gained primitives from T: line SVar resolution
        has_trigger_svars = bool(card.get("trigger_svars"))
        has_static_lines = bool(card.get("static_lines"))
        no_a_prims = not card["primitives"]
        if prims and no_a_prims and (has_trigger_svars or has_static_lines):
            cards_with_svars += 1

        for p in prims:
            distinct_primitives_found.add(p.get("primitive", ""))

        roles, unmatched = classify_card(card)

        for r in roles:
            role_counter[r] += 1
        for u in unmatched:
            all_unmatched[u] += 1

        # Build output entry
        entry = {
            "roles": sorted(roles),
            "primitives": sorted(set(p.get("primitive", "") for p in prims if p.get("primitive"))),
            "types": card["types"],
            "mana_cost": card["mana_cost"],
        }
        parsed_cards[card["name"]] = entry

        # Register DFC aliases
        if len(card["all_names"]) >= 2:
            for alias in dfc_names(card["all_names"]):
                if alias != card["name"]:
                    name_aliases[alias] = card["name"]

    # ── Write output ───────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    output = dict(parsed_cards)
    # Also add aliased names pointing to the same entry
    for alias, canonical in name_aliases.items():
        if alias not in output:
            output[alias] = output[canonical]
    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2, sort_keys=True)

    # ── Report ─────────────────────────────────────────────────────────
    num_no_roles = sum(1 for e in output.values() if not e["roles"])
    pct_no_roles = 100 * num_no_roles / max(len(output), 1)
    pct_api = 100 * cards_with_ability_api / max(total_scripts, 1)

    print("\n  === TAXONOMY REPORT ===")
    print(f"  Total scripts scanned:       {total_scripts}")
    print(f"  Successfully parsed:         {len(parsed_cards)}")
    print(f"  Parse errors:                {parse_errors}")
    print(f"  Cards with ability prims:    {cards_with_ability_api} ({pct_api:.1f}%)")
    print(f"  Cards from trigger/static:   {cards_with_svars}")
    print(f"  Distinct primitives:         {len(distinct_primitives_found)}")
    print(f"  Cards with no role:          {num_no_roles} ({pct_no_roles:.1f}%)")
    print(f"  DFC aliases registered:      {len(name_aliases)}")

    print("\n  Role histogram (all roles, primitive + creature-body):")
    for role in [
        "REMOVAL",
        "COUNTER",
        "TUTOR",
        "RECURSION",
        "RAMP",
        "DRAW",
        "LIBRARY",
        "COMBAT_TRICK",
        "ANTHEM",
        "TOKEN",
        "GROWTH",
        "LIFEGAIN",
        "LIFELOSS",
        "ANIMATION",
        "DISRUPTION",
        "PROTECTION",
        "ATTACKER",
        "BLOCKER",
        "EVASION",
        "LIFELINK",
    ]:
        count = role_counter.get(role, 0)
        pct = 100 * count / max(cards_with_ability_api, 1)
        print(f"    {role:15s} {count:6d} cards ({pct:5.1f}% of API-enabled)")

    # Unmapped primitives leaderboard
    mapped_primitive_names = set()
    for role_name, matchers in ROLE_MAP.items():
        for matcher in matchers:
            pm = matcher.get("primitive")
            if isinstance(pm, list):
                for p in pm:
                    mapped_primitive_names.add(p)
            elif isinstance(pm, str):
                mapped_primitive_names.add(pm)

    print("\n  Top 20 most common UNMAPPED primitives:")
    shown = 0
    for pname, count in all_unmatched.most_common(60):
        if pname in mapped_primitive_names:
            continue
        shown += 1
        print(f"    {pname:25s} appears in {count:6d} cards")
        if shown >= 20:
            break

    # ── Deck coverage ──────────────────────────────────────────────────
    deck_names = extract_deck_card_names()
    basics = {
        "Plains",
        "Island",
        "Swamp",
        "Mountain",
        "Forest",
        "Snow-Covered Plains",
        "Snow-Covered Island",
        "Snow-Covered Swamp",
        "Snow-Covered Mountain",
        "Snow-Covered Forest",
    }
    unique_deck_names = {n for n in deck_names if n not in basics}
    covered = sum(1 for n in unique_deck_names if n in output and output[n]["roles"])
    pct_coverage = 100 * covered / max(len(unique_deck_names), 1)
    print(f"\n  Deck coverage ({len(unique_deck_names)} unique cards, basics excluded):")
    print(f"    Resolved to >=1 role: {covered} ({pct_coverage:.1f}%)")

    not_found = [n for n in unique_deck_names if n not in output]
    no_role = sorted(n for n in unique_deck_names if n in output and not output[n]["roles"])
    if not_found:
        print(f"    Missing from taxonomy: {not_found}")
    if no_role:
        print(f"    In taxonomy but 0 roles ({len(no_role)} cards):")
        for n in no_role:
            entry = output.get(n, {})
            prims = entry.get("primitives", [])
            kw = ""
            print(f"      {n}  prims={prims}")

    print(f"\n  Output: {OUTPUT_JSON} ({len(output)} entries)")


if __name__ == "__main__":
    main()
