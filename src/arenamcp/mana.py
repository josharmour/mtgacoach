"""Canonical mana calculation, color identity parsing, and player seat utilities."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from arenamcp.combat_keywords import printed_combat_keywords

# Standard WUBRG color order
COLOR_ORDER = ("W", "U", "B", "R", "G")
COLOR_SET = frozenset(COLOR_ORDER)
ALL_MANA_COLORS = frozenset({"W", "U", "B", "R", "G", "C"})

_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def has_autotap_solution(action: dict[str, Any]) -> bool:
    """Whether Arena supplied a payment solution, including a zero-cost one."""
    return bool(action.get("hasAutoTap")) or isinstance(action.get("autoTapSolution"), dict)


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


_HASTE_GRANT_RE = re.compile(r"\bequipped creature\b[^.]*?\b(?:has|gains)\b[^.]*?\bhaste\b")
_EQUIP_COST_RE = re.compile(r"(?:^|\n)\s*equip\s*((?:\{[^}]+\})+)")
_TAP_MANA_RE = re.compile(r"\{o?t\}[^:\n]*:\s*add\b([^.\n]*)")


def _rules_text(card: dict[str, Any]) -> str:
    text = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return text


def haste_equipment_mana_hint(
    battlefield: list[dict[str, Any]], local_seat: int | None, turn_number: int
) -> str:
    """Name the equip-then-tap line for summoning-sick mana creatures.

    2026-10-04 the planner equipped Lightning Greaves to a fresh Notary
    Hobbits copy "for shroud; haste is incidental", leaving 9 mana unused:
    haste lets each sick creature with a {T}: Add ability tap the turn it
    entered, and a cheap equip can be moved to the next one after it taps.
    Rules-text driven: haste-granting equipment with a mana equip cost, and
    untapped summoning-sick creatures with a tap mana ability.
    """
    if local_seat is None or not turn_number:
        return ""
    mine = [
        card
        for card in battlefield or []
        if isinstance(card, dict)
        and (card.get("controller_seat_id") or card.get("owner_seat_id")) == local_seat
    ]

    def is_creature(card: dict[str, Any]) -> bool:
        kinds = card.get("card_types") or []
        if kinds:
            return any(str(kind).removeprefix("CardType_") == "Creature" for kind in kinds)
        return "creature" in str(card.get("type_line") or "").lower()

    equipment = []
    for card in mine:
        if "equipment" not in str(card.get("type_line") or "").lower():
            continue
        text = _rules_text(card)
        cost = _EQUIP_COST_RE.search(text)
        if not _HASTE_GRANT_RE.search(text) or not cost:
            continue
        cost_text = cost.group(1).replace("{o", "{")
        if mana_cost_to_cmc(cost_text) <= 2:
            equipment.append((card, cost_text))
    if not equipment:
        return ""

    def produced(card: dict[str, Any], ability: str) -> int:
        count = re.search(r"\bfor each (?:other )?(\w+?)s? you control\b", ability)
        if count:
            kind = count.group(1)
            return sum(
                1
                for other in mine
                if kind
                in {str(subtype).lower().removeprefix("subtype_") for subtype in other.get("subtypes") or []}
                or re.search(rf"\b{re.escape(kind)}s?\b", str(other.get("type_line") or "").lower())
            )
        return max(1, len(re.findall(r"\{o?[wubrgc]\}", ability)))

    sick: list[tuple[dict[str, Any], int]] = []
    for card in mine:
        if not is_creature(card) or card.get("is_tapped"):
            continue
        entered_now = card.get("summoning_sickness") or card.get("turn_entered_battlefield") == turn_number
        text = _rules_text(card)
        ability = _TAP_MANA_RE.search(text)
        if (
            not entered_now
            or not ability
            or "haste" in printed_combat_keywords(str(card.get("oracle_text") or ""))
        ):
            continue
        sick.append((card, produced(card, ability.group(1))))
    if not sick:
        return ""

    gear, cost_text = equipment[0]
    names = Counter(str(card.get("name") or "creature") for card, _ in sick)
    listed = ", ".join(f"{name} x{count}" if count > 1 else name for name, count in names.items())
    total = sum(amount for _, amount in sick)
    wearer = next((card for card, _ in sick if card.get("instance_id") == gear.get("attached_to_id")), None)
    ready = f" {wearer.get('name')} already wears it and can tap now." if wearer else ""
    return (
        f"HASTE-EQUIP MANA: {gear.get('name')} grants haste (equip {cost_text}). "
        f"Untapped summoning-sick mana creatures: {listed} (up to +{total} mana this turn).{ready} "
        "Haste here is mana, not incidental: equip one (sorcery speed), let Arena's auto-pay tap it "
        "for a spell, then move the equipment to the next untapped one and repeat. "
        "Spells unpayable now can become payable after equipping."
    )
