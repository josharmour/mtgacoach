#!/usr/bin/env python3
"""
XMage action string classifier for MageZero decision records.

Maps action strings like "Play Adarkar Wastes", "Cast Malcolm, Alluring Scoundrel",
"Pass", "use Kitsa, Otterball Elite" to structured ``{verb, card}`` dicts.

Comma handling: card names like "Malcolm, Alluring Scoundrel" contain commas.
We match known card names from the card map (longest-first) against the action
string rather than splitting on comma.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

# ── Load card map ──────────────────────────────────────────────────────────────

_CARD_MAP_PATH = os.path.join(os.path.dirname(__file__), "magezero_card_map.json")

# Known card names (all canonical scryfall names + XMage names), longest-first
_KNOWN_CARDS: list[str] = []


def _load_cards() -> list[str]:
    """Load known card names from the card map, sorted longest-first."""
    try:
        with open(_CARD_MAP_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    names = set()
    for xmage_name, entry in data.items():
        names.add(xmage_name)
        scryfall = entry.get("scryfall", xmage_name)
        if scryfall != xmage_name:
            names.add(scryfall)
        # For double-faced cards ("A // B"), also register both sides
        if " // " in scryfall:
            parts = scryfall.split(" // ")
            names.update(parts)

    # Sort longest string first so "Malcolm, Alluring Scoundrel" matches
    # before a substring like "Malcolm" would.
    return sorted(names, key=lambda n: (-len(n), n))


def _get_cards() -> list[str]:
    global _KNOWN_CARDS
    if not _KNOWN_CARDS:
        _KNOWN_CARDS = _load_cards()
    return _KNOWN_CARDS


# ── Verb patterns ──────────────────────────────────────────────────────────────

# Pattern order matters: more specific prefixes checked first.
_VERB_PATTERNS: list[tuple[str, str]] = [
    # (verb, regex_prefix)
    ("pass", r"^Pass$"),
    ("play", r"^Play\s+"),
    ("cast", r"^Cast\s+"),
    ("activate", r"^use\s+"),
    ("activate", r"^Activate\s+"),
    ("attack", r"^Attack\s+with\s+"),
    ("block", r"^Block\s+with\s+"),
]


def _match_card_in_action(action: str, prefix_len: int) -> Optional[str]:
    """Try to find a known card name in the remainder of *action* after
    stripping the verb prefix (of length *prefix_len*).  Returns the longest
    matching card name, or None.
    """
    remainder = action[prefix_len:].strip()
    if not remainder:
        return None

    cards = _get_cards()
    for cname in cards:
        # Match either exact remainder or remainder that starts with card name
        # (things like " [OK]" suffixes after cast)
        if remainder == cname or remainder.startswith(cname + " ") or remainder.startswith(cname + " ["):
            return cname
    return None


def classify_action(action: str) -> dict:
    """Classify a single XMage action string.

    Returns a dict with keys:
        verb : str   — one of "play", "cast", "pass", "activate", "attack",
                       "block", "other"
        card : str or None  — the card name if one was identified

    Examples
    --------
    >>> classify_action("Play Adarkar Wastes")
    {'verb': 'play', 'card': 'Adarkar Wastes'}
    >>> classify_action("Cast Malcolm, Alluring Scoundrel")
    {'verb': 'cast', 'card': 'Malcolm, Alluring Scoundrel'}
    >>> classify_action("Pass")
    {'verb': 'pass', 'card': None}
    >>> classify_action("use Kitsa, Otterball Elite")
    {'verb': 'activate', 'card': 'Kitsa, Otterball Elite'}
    >>> classify_action("Attack with Kitsa, Otterball Elite")
    {'verb': 'attack', 'card': 'Kitsa, Otterball Elite'}
    >>> classify_action("Block with Axebane Ferox")
    {'verb': 'block', 'card': 'Axebane Ferox'}
    >>> classify_action("Unknown command")
    {'verb': 'other', 'card': None}
    """
    for verb, prefix in _VERB_PATTERNS:
        m = re.match(prefix, action)
        if m:
            if verb == "pass":
                return {"verb": "pass", "card": None}
            card = _match_card_in_action(action, m.end())
            return {"verb": verb, "card": card}

    return {"verb": "other", "card": None}


def classify_actions(actions: list[str]) -> list[dict]:
    """Batch classify a list of action strings."""
    return [classify_action(a) for a in actions]
