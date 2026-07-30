#!/usr/bin/env python3
"""WP3-Taxonomy: parse 33,402 Forge card scripts and build functional card taxonomy.

Scans /Users/joshu/.forge/res/cardsfolder/ for .txt scripts, extracts:
  - Name, ManaCost, Types
  - All ability primitives from A: lines and SVar sub-abilities
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
DECK_FILES = ["UWTempo.dck", "Standard-MonoR.dck", "Standard-MonoG.dck",
              "Standard-MonoB.dck", "Standard-MonoW.dck", "Standard-MonoU.dck"]

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
        {"primitive": "Sacrifice", "check": lambda p: p.get("Defined", "") in ("Opponent", "EachOpponent", "TargetedController")},
        # Bounce (Battlefield -> Hand) — tempo removal
        {"primitive": "ChangeZone", "check": lambda p: p.get("Origin") == "Battlefield" and p.get("Destination") == "Hand"},
        # GainControl — steal a permanent (removes from opponent's control)
        {"primitive": "GainControl"},
    ],
    "COUNTER": [
        {"primitive": "Counter"},
    ],
    "TUTOR": [
        # Search library -> hand for non-land, non-basic cards
        {"primitive": "ChangeZone",
         "check": lambda p: p.get("Origin") == "Library"
                             and p.get("Destination") == "Hand"
                             and "Land" not in p.get("ChangeType", "")},
    ],
    "RECURSION": [
        # Graveyard -> Battlefield (reanimate)
        {"primitive": "ChangeZone",
         "check": lambda p: p.get("Origin") == "Graveyard"
                             and p.get("Destination") == "Battlefield"},
        # Graveyard -> Hand (raise dead)
        {"primitive": "ChangeZone",
         "check": lambda p: p.get("Origin") == "Graveyard"
                             and p.get("Destination") == "Hand"},
    ],
    "RAMP": [
        # Mana-producing abilities (rituals + mana abilities)
        {"primitive": "Mana"},
        # Land-fetch (Library -> Battlefield for lands)
        {"primitive": "ChangeZone",
         "check": lambda p: p.get("Origin") == "Library"
                             and p.get("Destination") == "Battlefield"
                             and "Land" in p.get("ChangeType", "")},
    ],
    "DRAW": [
        {"primitive": "Draw"},
        # Dig — looting, filtering library
        {"primitive": "Dig"},
    ],
    "COMBAT_TRICK": [
        # Instant-speed pump to creatures
        {"primitive": "Pump", "sp_ab": "SP",
         "check": lambda p: "Creature" in p.get("ValidTgts", "")},
        # PumpAll at instant speed to your creatures
        {"primitive": "PumpAll", "sp_ab": "SP"},
    ],
    "TOKEN": [
        # Creating creature tokens
        {"primitive": "Token"},
    ],
}


def parse_card_script(text):
    """Parse a Forge .txt card script into a structured dict.

    Handles:
      - Single-faced (first Name: line)
      - Double-faced cards (multiple Name: lines)
      - A: ability lines parsed into param dicts
      - SVar definitions (sub-abilities for recursive traversal)

    Returns dict with keys: name, all_names, mana_cost, types, primitives, svars.
    """
    result = {
        "name": "",
        "all_names": [],   # all Name: lines (for DFC lookup)
        "mana_cost": "",
        "types": "",
        "primitives": [],  # list of {"primitive": str, "sp_ab": str, **params}
        "svars": {},       # SVar definitions for sub-ability traversal
    }

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Name (collect ALL Name lines for DFC support)
        if line.startswith("Name:"):
            name_val = line[len("Name:"):].strip()
            result["all_names"].append(name_val)
            if not result["name"]:
                result["name"] = name_val

        # ManaCost
        elif line.startswith("ManaCost:") and not result["mana_cost"]:
            result["mana_cost"] = line[len("ManaCost:"):].strip()

        # Types
        elif line.startswith("Types:") and not result["types"]:
            result["types"] = line[len("Types:"):].strip()

        # A: lines — ability definitions
        elif line.startswith("A:") and "$" in line:
            params = _parse_ability_line(line)
            if params:
                result["primitives"].append(params)

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
    remainder = rest[dollar_idx + 1:].strip()

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
    remainder = rest[dollar_idx + 1:].strip()

    # Type is always the part after the last colon before $, Name is everything before
    colon_idx = prefix.rfind(":")
    if colon_idx < 0:
        return {"svar_name": prefix, "svar_type": "", "primitive": remainder.split("|")[0].strip()}

    svar_name = prefix[:colon_idx]
    svar_type = prefix[colon_idx + 1:]

    if svar_type != "DB":
        # Not a sub-ability — static ability, stored but not traversed
        return {"svar_name": svar_name, "svar_type": svar_type}

    # Parse like a regular ability line
    parts = [p.strip() for p in remainder.split("|")]
    params = {
        "svar_name": svar_name,
        "svar_type": svar_type,
        "sp_ab": "DB",  # Sub-abilities are DB type
        "primitive": parts[0] if parts else "",
    }

    for part in parts[1:]:
        if "$" in part:
            key, _, val = part.partition("$")
            params[key.strip()] = val.strip()

    return params


def resolve_primitives(card):
    """Resolve all primitives including recursively following SubAbility
    chains and Charm Choices.

    Charm cards (primitive == "Charm") have a Choices$ field listing
    SVars for each mode. Each mode is a DB sub-ability whose primitives
    are part of the card's function.

    Returns list of param dicts including sub-ability and charm-mode
    primitives.
    """
    all_primitives = list(card["primitives"])
    seen_svars = set()

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

    return all_primitives


def _resolve_sub(card, svar_name, all_primitives, seen_svars):
    """Helper to recursively follow sub-ability chains."""
    sub = card["svars"].get(svar_name)
    if not sub or sub.get("svar_type") != "DB":
        return
    all_primitives.append(sub)
    next_sub = sub.get("SubAbility")
    if next_sub and next_sub not in seen_svars:
        seen_svars.add(next_sub)
        _resolve_sub(card, next_sub, all_primitives, seen_svars)


def classify_card(card):
    """Given a parsed card, return (roles_set, unmatched_primitives_set)."""
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

    return roles, unmatched_primitives


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
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip().rstrip("\r")
                    if not line or line.startswith("LAYOUT"):
                        continue
                    m = re.match(r"^\d+\s+\[[\w:]+]\s+(.+)$", line)
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
    parsed_cards = {}    # canonical name -> entry
    name_aliases = {}    # alt_name -> canonical_name (for DFCs)
    all_unmatched = Counter()
    distinct_primitives_found = set()
    role_counter = Counter()
    cards_with_ability_api = 0

    for path in scripts:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
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
            "primitives": sorted(
                set(p.get("primitive", "") for p in prims if p.get("primitive"))
            ),
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

    print(f"\n  === TAXONOMY REPORT ===")
    print(f"  Total scripts scanned:       {total_scripts}")
    print(f"  Successfully parsed:         {len(parsed_cards)}")
    print(f"  Parse errors:                {parse_errors}")
    print(f"  Cards with an ability API:   {cards_with_ability_api} ({pct_api:.1f}%)")
    print(f"  Distinct primitives:         {len(distinct_primitives_found)}")
    print(f"  Cards with no role:          {num_no_roles} ({pct_no_roles:.1f}%)")
    print(f"  DFC aliases registered:      {len(name_aliases)}")

    print(f"\n  Role histogram:")
    for role in ["REMOVAL", "COUNTER", "TUTOR", "RECURSION", "RAMP",
                 "DRAW", "COMBAT_TRICK", "TOKEN"]:
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

    print(f"\n  Top 20 most common UNMAPPED primitives:")
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
    basics = {"Plains", "Island", "Swamp", "Mountain", "Forest",
              "Snow-Covered Plains", "Snow-Covered Island",
              "Snow-Covered Swamp", "Snow-Covered Mountain",
              "Snow-Covered Forest"}
    unique_deck_names = {n for n in deck_names if n not in basics}
    covered = sum(1 for n in unique_deck_names
                  if n in output and output[n]["roles"])
    pct_coverage = 100 * covered / max(len(unique_deck_names), 1)
    print(f"\n  Deck coverage ({len(unique_deck_names)} unique cards, basics excluded):")
    print(f"    Resolved to >=1 role: {covered} ({pct_coverage:.1f}%)")

    not_found = [n for n in unique_deck_names if n not in output]
    no_role = sorted(n for n in unique_deck_names
                     if n in output and not output[n]["roles"])
    if not_found:
        print(f"    Missing from taxonomy: {not_found}")
    if no_role:
        print(f"    In taxonomy but 0 roles ({len(no_role)} cards):")
        for n in no_role:
            print(f"      {n}")

    print(f"\n  Output: {OUTPUT_JSON} ({len(output)} entries)")


if __name__ == "__main__":
    main()
