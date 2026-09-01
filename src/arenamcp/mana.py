"""Canonical mana calculation, color identity parsing, and player seat utilities."""

from __future__ import annotations

import re
from typing import Any

# Standard WUBRG color order
COLOR_ORDER = ("W", "U", "B", "R", "G")
COLOR_SET = frozenset(COLOR_ORDER)
ALL_MANA_COLORS = frozenset({"W", "U", "B", "R", "G", "C"})

_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def mana_cost_to_cmc(mana_cost: str | None) -> int:
    """Calculate converted mana cost (mana value) from a cost string.

    Properly handles single-digit and multi-digit generic costs (e.g. '{10}'),
    colored pips ('{W}', '{U}'), hybrid mana ('{W/U}'), Phyrexian mana ('{G/P}'),
    and variable '{X}' costs (counted as 0).

    Examples:
        >>> mana_cost_to_cmc("{1}{W}{U}")
        3
        >>> mana_cost_to_cmc("{10}{G}{G}")
        12
        >>> mana_cost_to_cmc("{X}{2}{R}")
        3
        >>> mana_cost_to_cmc("{B/G}{B/G}")
        2
    """
    if not mana_cost:
        return 0

    cmc = 0
    symbols = _MANA_SYMBOL_RE.findall(mana_cost)
    for sym in symbols:
        sym_clean = sym.strip()
        if sym_clean.isdigit():
            cmc += int(sym_clean)
        elif "/" in sym_clean:
            # Hybrid or Phyrexian mana: {W/U}, {2/W}, {G/P}
            parts = sym_clean.split("/")
            if parts[0].isdigit():
                cmc += int(parts[0])
            else:
                cmc += 1
        elif sym_clean.upper() in ALL_MANA_COLORS:
            cmc += 1
        elif sym_clean.upper() == "X":
            cmc += 0
        else:
            # Fallback for uncommon symbol notation
            cmc += 1

    return cmc


def parse_color_identity(mana_cost: str | None) -> str:
    """Extract colored mana symbols in canonical WUBRG order.

    Examples:
        >>> parse_color_identity("{1}{U}{R}")
        'UR'
        >>> parse_color_identity("{2}{G}{W}")
        'WG'
        >>> parse_color_identity("{3}")
        ''
    """
    if not mana_cost:
        return ""

    found_colors = set()
    for sym in _MANA_SYMBOL_RE.findall(mana_cost):
        sym_upper = sym.upper()
        if "/" in sym_upper:
            for part in sym_upper.split("/"):
                if part in COLOR_SET:
                    found_colors.add(part)
        elif sym_upper in COLOR_SET:
            found_colors.add(sym_upper)

    return "".join(c for c in COLOR_ORDER if c in found_colors)


def get_local_seat_id(game_state: dict[str, Any] | None) -> int | None:
    """Resolve the local player's seat ID from a game state dictionary.

    Checks:
    1. Direct 'local_seat_id' or 'player_seat' top-level keys.
    2. 'players' list for an object with 'is_local' == True.
    """
    if not isinstance(game_state, dict):
        return None

    if "local_seat_id" in game_state and game_state["local_seat_id"] is not None:
        try:
            return int(game_state["local_seat_id"])
        except (ValueError, TypeError):
            pass

    if "player_seat" in game_state and game_state["player_seat"] is not None:
        try:
            return int(game_state["player_seat"])
        except (ValueError, TypeError):
            pass

    for player in game_state.get("players", []):
        if isinstance(player, dict) and player.get("is_local"):
            seat = player.get("seat_id")
            if seat is not None:
                try:
                    return int(seat)
                except (ValueError, TypeError):
                    pass

    return None
