"""Combat declarations on big boards and when they deserve high effort (ledger #155, #159, #162).

Brawl token swarms (Leonardo, Big Brother with Hare Apparents, 2026-10-19):

- #155/#162 (match 2bb162d7, 14:27-14:29): 25 and 39 attackers, 41 creatures
  on the battlefield. Both attack answers were dropped (their shape: see
  ``action_legality.normalize_attack_declaration``) and a 45,000-character
  block prompt ran out the 15 s budget; the combat solver's safe default
  decided every one of those combats, after the wait. On such boards the
  solver is the primary path (``big_board_declaration``), and the menu shows
  a run of identical tokens as one line (``compress_menu``).
- #159 (match be1a945f T10): declaration plans always ran at low effort, even
  with 25 power against their 12 life. ``declaration_effort`` raises it when
  lethal is within reach (Sneak included) or their attack threatens ours.

Pure; reads planner snapshots and the legal-action menu only.
"""

from __future__ import annotations

import re
from typing import Any

# Attack menus at least this long, and block windows with at least this many creatures on the
# battlefield, are declared by the combat solver (be1a945f's 15 attackers were answered well in
# 10 s; 2bb162d7's 25 and 39 were not, nor its blocks at 41 creatures).
BIG_ATTACK_MENU = 20
BIG_BLOCK_BOARD = 36
_PT = re.compile(r"\((\d+)/(\d+)\)\s*$")


def _entries(legal_actions: list[str] | None, prefix: str) -> list[str]:
    return [str(a).strip() for a in legal_actions or [] if str(a).strip().lower().startswith(prefix)]


def _creatures(state: dict) -> list[dict]:
    return [
        card
        for card in state.get("battlefield") or []
        if isinstance(card, dict)
        and "creature"
        in f"{card.get('type_line') or ''} {' '.join(map(str, card.get('card_types') or []))}".lower()
    ]


def _seats(state: dict) -> tuple[Any, Any]:
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    opponent = next((p.get("seat_id") for p in players if p.get("seat_id") not in (None, local)), None)
    return local, opponent


def _life(state: dict, seat: Any) -> int | None:
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("seat_id") == seat:
            life = player.get("life_total")
            return life if isinstance(life, int) and not isinstance(life, bool) else None
    return None


def _power(card: dict) -> int:
    power = card.get("power")
    return power if isinstance(power, int) and not isinstance(power, bool) and power > 0 else 0


def big_board_declaration(state: dict | None, legal_actions: list[str] | None) -> str:
    """Why this combat declaration goes to the solver without a model call, or ''."""
    if not isinstance(state, dict):
        return ""
    attackers = _entries(legal_actions, "attack with:")
    if len(attackers) >= BIG_ATTACK_MENU:
        return f"{len(attackers)} attackers on the menu"
    if _entries(legal_actions, "block with:"):
        count = len(_creatures(state))
        if count >= BIG_BLOCK_BOARD:
            return f"{count} creatures on the battlefield"
    return ""


def declaration_effort(state: dict | None, legal_actions: list[str] | None) -> tuple[str, str]:
    """("high", why) for a declare-attackers/blockers plan within lethal reach or in danger, else
    ("", '')."""
    if not isinstance(state, dict):
        return "", ""
    local, opponent = _seats(state)
    our_life, opp_life = _life(state, local), _life(state, opponent)
    creatures = _creatures(state)
    ours = [c for c in creatures if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local]
    theirs = [c for c in creatures if (c.get("controller_seat_id") or c.get("owner_seat_id")) == opponent]
    attack_menu = _entries(legal_actions, "attack with:")
    if attack_menu and opp_life:
        powers = [int(m.group(1)) for entry in attack_menu if (m := _PT.search(entry))]
        attack = sum(powers)
        sneak, name = _sneak_bonus(state, ours, min(powers) if powers else 0)
        if attack + sneak >= opp_life:
            extra = f" + Sneak {name} {sneak}" if sneak else ""
            return "high", f"within lethal reach: attackers {attack}{extra} against their {opp_life} life"
        crack = sum(_power(c) for c in theirs)
        if our_life is not None and crack >= our_life:
            return "high", f"their creatures hit for {crack} against our {our_life} life"
    if _entries(legal_actions, "block with:") and our_life is not None:
        incoming = sum(_power(c) for c in theirs if c.get("is_attacking"))
        if incoming >= our_life:
            return "high", f"{incoming} attacking against our {our_life} life"
    return "", ""


def _sneak_bonus(state: dict, ours: list[dict], weakest: int) -> tuple[int, str]:
    try:
        from arenamcp.decision_memory import mana_available
        from arenamcp.sneak import entry_power, sneak_casts

        mana = mana_available(state)
        best, name = 0, ""
        for cast in sneak_casts(state):
            if cast.mana_value <= mana:
                gain = entry_power(cast.card, len(ours)) - weakest
                if gain > best:
                    best, name = gain, cast.name
        return best, name
    except Exception:
        return 0, ""


_NUMBERED = re.compile(
    r"^(?P<verb>(?:attack|block) with:\s*)(?P<name>.+?)\s*#(?P<n>\d+)\s*(?P<pt>\(\d+/\d+\))?$", re.I
)


def group_menu(menu: list[str]) -> list[str]:
    """``menu`` with each class of numbered combatants ("Rabbit #1 (1/1)", "Rabbit #2 (1/1)", ...)
    gathered where its first member stands; everything else keeps its place."""

    def key(entry: str):
        match = _NUMBERED.match(str(entry).strip())
        return (match.group("verb").lower(), match.group("name"), match.group("pt")) if match else None

    out: list[str] = []
    done: set = set()
    for entry in menu:
        cls = key(entry)
        if cls is None:
            out.append(entry)
        elif cls not in done:
            done.add(cls)
            out.extend(other for other in menu if key(other) == cls)
    return out


def compress_menu(menu: list[str], *, minimum: int = 4) -> list[str]:
    """Menu lines for the prompt (numbering unchanged): a run of ``minimum`` or more consecutive
    identical numbered combatants ("Attack with: Rabbit #1 (1/1)" ... "#12") is one line,
    "5-16. Attack with: Rabbit #1..#12 (1/1): 12 identical" (``group_menu`` makes the runs)."""
    lines: list[str] = []
    index = 0
    while index < len(menu):
        match = _NUMBERED.match(str(menu[index]).strip())
        end = index + 1
        if match:
            key = (match.group("verb").lower(), match.group("name"), match.group("pt"))
            while end < len(menu):
                other = _NUMBERED.match(str(menu[end]).strip())
                if not other or (other.group("verb").lower(), other.group("name"), other.group("pt")) != key:
                    break
                end += 1
        if match and end - index >= minimum:
            last = _NUMBERED.match(str(menu[end - 1]).strip())
            pt = f" {match.group('pt')}" if match.group("pt") else ""
            lines.append(
                f"  {index + 1}-{end}. {match.group('verb')}{match.group('name')} #{match.group('n')}..#"
                f"{last.group('n')}{pt}: {end - index} identical"
            )
            index = end
            continue
        lines.append(f"  {index + 1}. {menu[index]}")
        index += 1
    return lines
