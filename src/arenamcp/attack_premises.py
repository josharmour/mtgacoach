"""Correct attack reasoning that rests on a false tapped-enemy premise.

2026-11-12 (Gar vs Minsc, T8): the plan attacked our hasty 4/4 Boo at their
3-loyalty Minsc saying "their big Boo is tapped so crackback is limited."
Three things are wrong with that premise, generically:

- their untapped creatures can still block THIS attack, alone or together,
  so a planeswalker kill is not guaranteed until the best block is
  evaluated (their untapped Curator 3/3 + Halfling 2/3 double block kills
  the 4/4 and — even with trample — leaves the walker standing);
- a tapped creature untaps during its controller's untap step, so being
  tapped now says nothing about their NEXT combat's attackers. That threat
  stands even when their whole board is tapped today;
- when the attacker was committed as a blocker earlier this turn (or held
  back by a guard), spending it on attack reverses that commitment and the
  reasoning must say what changed or why spending it beats keeping it.

The correction is narration-only: the pick stands, the reasoning gains the
board facts, and the trace records the premise sentence. Legal tactical
attacks are never overridden, holding is never claimed to win, and no card
or match identity is consulted.

Limits (deliberately bounded):
- The best-block evaluation is single-attacker combat arithmetic against
  their untapped creatures as a group: summed power versus attacker
  toughness, summed toughness versus attacker power, and a trample check
  from the attacker's keywords/oracle text. No per-creature "can't block"
  restrictions, no damage-ordering tricks, no instants-on-the-stack
  modelling; any untapped opposing creature counts as a potential blocker.
- Whether a tapped creature actually untaps is assumed unless an effect
  keeps it tapped; the wording says "unless an effect keeps it tapped".
- Blocker commitments are read from this turn's decision memory only:
  guard hold-back notes and the model's own recorded reasoning that names
  the creature with a blocking/save/reserve intent. Outage fallbacks
  (``model_unavailable``) are history, not commitments.
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

# A recorded reasoning that names a creature AND states a blocking/save/reserve
# intent for it is a commitment spending that creature on attack reverses.
_COMMITMENT = re.compile(
    r"\bblocks?\w*|chump\w*|reserv\w*|sav\w*|keeps?\s+(?:\w+\s+){0,2}back"
    r"|holds?\s+(?:\w+\s+){0,2}back|hold\s+back|absorb\w*",
    re.IGNORECASE,
)


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


def _stat(card: dict, key: str) -> int | None:
    try:
        value = card.get(key)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _has_trample(attacker: dict) -> bool:
    text = " ".join(
        str(part)
        for part in (
            " ".join(str(keyword) for keyword in attacker.get("keywords") or []),
            attacker.get("oracle_text") or "",
        )
    )
    return "trample" in text.casefold()


def best_block_clause(attacker: dict, blockers: list[dict], target: str = "") -> str:
    """What their best block does to one attacker and the thing it attacks.

    Deterministic single-attacker arithmetic with their blockers pooled as
    one group. Returns "" when nothing can be stated (no blockers, or the
    attacker's stats are missing).
    """
    if not blockers:
        return ""
    power = _stat(attacker, "power")
    toughness = _stat(attacker, "toughness")
    if power is None or toughness is None:
        return ""
    their_power = sum(_stat(card, "power") or 0 for card in blockers)
    their_toughness = sum(_stat(card, "toughness") or 0 for card in blockers)
    victim = target or "the attack's target"
    parts = []
    if their_power >= toughness:
        parts.append(f"together their {their_power} power would kill the {power}/{toughness} attacker")
    else:
        parts.append(f"their best block cannot kill the {power}/{toughness} attacker")
    if _has_trample(attacker):
        excess = power - their_toughness
        if excess > 0:
            parts.append(f"but only {excess} damage tramples through to {victim}")
        else:
            parts.append(
                f"and even trample leaves {victim} unharmed this combat "
                f"({power} power minus {their_toughness} lethal blocker damage)"
            )
    else:
        parts.append(f"and a blocked attack deals no damage to {victim} this combat")
    return "; ".join(parts) + "."


def tapped_crackback_correction(
    state: dict,
    reasoning: str,
    attackers: list[dict] | None = None,
    target: str = "",
) -> str:
    """The board facts an attack's "tapped enemy limits crackback" premise ignores.

    Returns "" when the reasoning does not make that premise or the board
    does not contradict it. Otherwise independent clauses:

    - current blockers (their untapped creatures): who can block this attack
      now and what their best block does to the attacker and its target;
    - next combat (their tapped creatures): they untap before their next
      attack unless an effect keeps them tapped.

    Either clause alone fires — an all-tapped enemy board still threatens
    next combat, and an all-untapped one still blocks this attack.
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
    clauses = []
    if untapped:
        clause = f"Their untapped {_describe(untapped)} can still block this attack now, alone or together"
        solo = attackers[0] if attackers and len(attackers) == 1 else None
        math_bit = best_block_clause(solo, untapped, target) if solo else ""
        clause += f" — {math_bit}" if math_bit else "."
        clauses.append(clause)
    if tapped:
        clauses.append(
            "Tapped now is not tapped next combat: unless an effect keeps it tapped, "
            f"their tapped {_describe(tapped)} untap before their next attack."
        )
    return " ".join(clauses)


def _commitment_snippet(reasoning: str, name: str) -> str:
    """The first sentence of ``reasoning`` that names ``name``, trimmed."""
    for sentence in _sentences(reasoning):
        if re.search(rf"\b{re.escape(name.casefold())}\b", sentence.casefold()):
            return " ".join(sentence.split())[:120]
    return " ".join(str(reasoning).split())[:120]


def held_blocker_reconciliation(records: list[Any], attacker_names: list[str]) -> str:
    """Ask what changed when an attack spends a creature committed as a blocker.

    Two commitment sources from this turn's decision memory:

    - guard hold-back notes ("attack held by guard: NAME not attacking");
    - the model's own recorded reasoning that names the creature with a
      blocking/save/reserve intent (outage fallbacks excluded).

    The note asks for the actual justification — what changed on the board,
    or why spending the creature now beats keeping it home — and never
    claims that holding wins.
    """
    wanted = [str(name).strip() for name in attacker_names or [] if str(name).strip()]
    if not wanted:
        return ""
    held_by_guard: set[str] = set()
    committed: dict[str, str] = {}
    for record in records or []:
        labels = [str(label) for label in getattr(record, "chosen_labels", []) or []]
        joined_labels = ", ".join(labels)
        for label in labels:
            match = _HELD_NOTE.match(label)
            if match:
                held_by_guard.update(name.strip() for name in match.group(1).split(","))
        if getattr(record, "model_unavailable", False):
            continue  # an outage fallback is history, not a commitment
        reasoning = str(getattr(record, "reasoning", "") or "")
        text = f"{joined_labels} {reasoning}".casefold()
        for name in wanted:
            if name in committed:
                continue
            if not re.search(rf"\b{re.escape(name.casefold())}\b", text):
                continue
            if not _COMMITMENT.search(text):
                continue
            snippet = _commitment_snippet(reasoning, name) or joined_labels[:120]
            committed[name] = snippet
    notes = []
    spent_guard = sorted(held_by_guard & set(wanted))
    if spent_guard:
        notes.append(
            f"Attacking with {', '.join(spent_guard)} reverses this turn's hold-back; "
            "the reasoning must say what changed or why spending it now beats keeping it home."
        )
    for name in wanted:
        if name in committed:
            notes.append(
                f'Earlier this turn you committed {name} as a blocker ("{committed[name]}"); '
                "attacking with it reverses that commitment — say what changed on the board "
                "or why spending it beats keeping it home."
            )
    return " ".join(notes)
