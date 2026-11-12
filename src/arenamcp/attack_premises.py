"""Correct attack reasoning that rests on a false tapped-enemy premise.

2026-11-12 (Gar vs Minsc, T8): the plan attacked our hasty 4/4 Boo at their
3-loyalty Minsc saying "their big Boo is tapped so crackback is limited."
Two things are wrong with that premise, generically:

- a tapped creature untaps during its controller's untap step, so being
  tapped now says nothing about their NEXT combat's attackers;
- their untapped creatures can still block THIS attack, alone or together,
  so a planeswalker kill is not guaranteed until the best block is
  evaluated (their untapped Curator 3/3 + Halfling 2/3 could double block
  the 4/4, kill it, and leave the walker standing).

The correction is narration-only: the pick stands, the reasoning gains the
board facts, and the trace records the premise sentence. Legal tactical
attacks are never overridden and no card or match identity is consulted.

Limits (deliberately bounded):
- Fires only when the opponent has BOTH a tapped creature (the premise
  refers to something real) and an untapped creature (an immediate block
  threat exists). With their whole board tapped the premise is true for
  this combat and no correction is warranted.
- Per-creature "can't block" restrictions on their untapped creatures are
  not modelled; any untapped opposing creature counts as a potential
  blocker.
- Whether the tapped creature actually untaps is assumed unless an effect
  keeps it tapped; the wording says "unless an effect keeps it tapped".
"""

from __future__ import annotations

import re
from typing import Any

# A sentence that ties one of THEIR creatures being tapped to a crackback /
# blocking-safety conclusion. Both halves must be in the same sentence so a
# passing mention of a tapped permanent of ours never triggers it.
_PREMISE = re.compile(
    r"(?:\btheir\b|\benemy\b|\bopponent(?:'s)?\b|\bopposing\b)[^.!?]{0,80}\btapped\b"
    r"|\btapped\b[^.!?]{0,80}(?:\btheir\b|\benemy\b|\bopponent(?:'s)?\b|\bopposing\b)",
    re.IGNORECASE,
)
_CONSEQUENCE = re.compile(
    r"crack[ -]?back|retaliat\w*|attack\w*\s+back|hits?\s+back|swings?\s+back"
    r"|can(?:'|no)t\s+block|won'?t\s+(?:be\s+able\s+to\s+)?block|no\s+blocks?\b",
    re.IGNORECASE,
)

_HELD_NOTE = re.compile(r"attack held by guard: (.+?) not attacking")


def _sentences(reasoning: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(reasoning or "")) if s.strip()]


def _opponent_seat(state: dict) -> int | None:
    local = state.get("local_seat_id")
    if local is None:
        for player in state.get("players") or []:
            if player.get("is_local"):
                local = player.get("seat_id")
                break
    if local is None:
        return None
    for player in state.get("players") or []:
        seat = player.get("seat_id")
        if seat is not None and seat != local:
            return seat
    return None


def _their_creatures(state: dict) -> list[dict]:
    seat = _opponent_seat(state)
    if seat is None:
        return []
    creatures = []
    for card in state.get("battlefield") or []:
        if card.get("controller_seat_id") != seat:
            continue
        if "Creature" not in str(card.get("type_line") or ""):
            continue
        creatures.append(card)
    return creatures


def _describe(cards: list[dict]) -> str:
    parts = []
    for card in cards[:3]:
        name = str(card.get("name") or "a creature")
        power = card.get("power")
        toughness = card.get("toughness")
        stats = f" {power}/{toughness}" if power is not None and toughness is not None else ""
        parts.append(f"{name}{stats}")
    return ", ".join(parts)


def tapped_crackback_correction(state: dict, reasoning: str) -> str:
    """The board facts an attack's "tapped enemy limits crackback" premise ignores.

    Returns "" when the reasoning does not make that premise or the board
    does not contradict it; otherwise one sentence pair to append.
    """
    premise = next(
        (s for s in _sentences(reasoning) if _PREMISE.search(s) and _CONSEQUENCE.search(s)),
        "",
    )
    if not premise:
        return ""
    theirs = _their_creatures(state)
    tapped = [card for card in theirs if card.get("is_tapped")]
    untapped = [card for card in theirs if not card.get("is_tapped")]
    if not tapped or not untapped:
        # Either the premise misreads the board (handled elsewhere) or their
        # whole board is tapped and the premise holds for this combat.
        return ""
    return (
        "Tapped now is not tapped next combat: unless an effect keeps it tapped, "
        "their tapped creature untaps before their next attack. Their untapped "
        f"{_describe(untapped)} can still block this attack now, alone or together — "
        "evaluate their best block, including a double block, and whether the "
        "attacker survives it before counting what it is aimed at destroyed."
    )


def held_blocker_reconciliation(records: list[Any], attacker_names: list[str]) -> str:
    """Ask for a rationale when an attack spends a creature a guard held back.

    Guards record holds as "attack held by guard: NAME[, NAME] not attacking".
    Attacking with one of those names this turn reverses that commitment; the
    reasoning must say why the revised line is worth it.
    """
    held: set[str] = set()
    for record in records or []:
        if getattr(record, "source", "") != "guard":
            continue
        for label in getattr(record, "chosen_labels", []) or []:
            match = _HELD_NOTE.match(str(label))
            if match:
                held.update(name.strip() for name in match.group(1).split(","))
    reversed_names = sorted(held & {str(name).strip() for name in attacker_names})
    if not reversed_names:
        return ""
    return (
        f"Attacking with {', '.join(reversed_names)} reverses this turn's hold-back; "
        "say why the revised line is worth it."
    )
