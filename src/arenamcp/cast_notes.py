"""Deterministic timing notes for typed-decision options (2026-10-10 FRA game 3).

- At 15:28:51 the model cast Divining Duelist (flash) during the opponent's
  first-strike damage step "to add a fresh 3/2 blocker": blockers had been
  declared 13 seconds earlier, so the creature could not block.
- At 15:28:56 it chose the Duelist's "draw a card, then discard a card" mode
  with an empty hand "to dig for removal": the drawn card is the only card
  in hand, so it is the card discarded. The mode is a mill of one.

Both are facts the prompt can state next to the option.
"""

from __future__ import annotations

import re
from typing import Any

_LOOT_RE = re.compile(
    r"\bdraw (?:a|one|1|two|2|three|3|\d+) cards?,? then discard (?:a|one|1|two|2|\d+) cards?", re.I
)
_DRAW_RE = re.compile(r"\bdraw (a|one|1|two|2|three|3|\d+) cards?", re.I)
_DISCARD_RE = re.compile(r"then discard (a|one|1|two|2|\d+) cards?", re.I)
_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3}


def _count(token: str) -> int:
    token = token.lower()
    return _WORDS.get(token) or (int(token) if token.isdigit() else 1)


def _step(state: dict) -> str:
    turn = state.get("turn") or {}
    return re.sub(r"^step_", "", str(turn.get("step") or "").strip().lower())


def _in_combat_after_blocks(state: dict) -> bool:
    turn = state.get("turn") or {}
    phase = str(turn.get("phase") or "").lower()
    step = _step(state)
    after = step.startswith(("declareblock", "firststrike", "combatdamage", "endcombat", "end_combat"))
    return after and ("combat" in phase or step != "")


def _our_turn(state: dict) -> bool | None:
    turn = state.get("turn") or {}
    active = turn.get("active_player")
    local = state.get("local_seat_id")
    if active is None or local is None:
        return None
    return active == local


def combat_cast_note(state: dict, card: dict | None) -> str:
    """'[enters after blockers were declared: cannot block this combat]' for a creature cast now, else ''."""
    if not isinstance(state, dict) or not card:
        return ""
    types = " ".join(str(t) for t in (card.get("card_types") or card.get("types") or []))
    if "Creature" not in types and "Creature" not in str(card.get("type_line") or ""):
        return ""
    if not _in_combat_after_blocks(state):
        return ""
    ours = _our_turn(state)
    if ours is False:
        return "  [enters after blockers were declared: it CANNOT block this combat; it is not a blocker against this attack]"
    if ours is True:
        return "  [enters after attackers were declared: it cannot attack this combat]"
    return "  [enters mid-combat after blocks: it cannot block or attack this combat]"


def loot_mode_note(label: Any, state: dict) -> str:
    """'[hand is empty: ...]' for a draw-then-discard mode when the only card to discard is the one drawn."""
    text = str(label or "")
    if not isinstance(state, dict) or not _LOOT_RE.search(text):
        return ""
    draw_match, discard_match = _DRAW_RE.search(text), _DISCARD_RE.search(text)
    draws = _count(draw_match.group(1)) if draw_match else 1
    discards = _count(discard_match.group(1)) if discard_match else 1
    hand = len(state.get("hand") or [])
    if hand + draws - discards <= 0 and draws <= discards:
        return (
            f"  [your hand has {hand} card(s): you would draw {draws} and must discard {discards}, "
            "so the card(s) drawn are the card(s) discarded — no card is gained; it only fills the graveyard]"
        )
    if hand == 0:
        return f"  [your hand is empty: you keep {draws - discards} of the {draws} drawn]"
    return ""


_PUMP_RE = re.compile(r"gets \+\d+/\+\d+ until end of turn", re.IGNORECASE)
_END_STEP_SAC_RE = re.compile(r"at the beginning of (?:the|your) end step, sacrifice", re.IGNORECASE)


def _main_phase_before_combat(state: dict) -> bool:
    turn = state.get("turn") or {}
    phase = str(turn.get("phase") or "").lower()
    return "main" in phase and "2" not in phase and "second" not in phase and not str(turn.get("step") or "")


def trick_timing_note(state: dict, card: dict | None) -> str:
    """'[a combat trick cast before combat: ...]' for a pump instant in our precombat main, else ''.

    2026-10-11 10:44: Blazing Crescendo (+3/+1 until end of turn) was cast in
    main phase 1 "so Titanbones can attack through blockers", then the attack
    window held everything back; the trick, two mana and the information
    were wasted.
    """
    if not isinstance(state, dict) or not card or _our_turn(state) is not True:
        return ""
    if "instant" not in str(card.get("type_line") or "").lower():
        return ""
    oracle = str(card.get("oracle_text") or "")
    if not _PUMP_RE.search(oracle) or not _main_phase_before_combat(state):
        return ""
    return (
        "  [a combat trick cast BEFORE combat: the opponent blocks knowing the pump, and it is wasted unless the "
        "creature then attacks. Hold it for combat (after blockers) or for a response; cast it now only if the "
        "attack is already decided and the pump itself wins something]"
    )


def _printed_power(card: dict, catalog: dict | None) -> int | None:
    entry = (catalog or {}).get(card.get("grp_id")) or (catalog or {}).get(str(card.get("grp_id")))
    raw = (entry or {}).get("power", card.get("printed_power"))
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def turn_investments_note(state: dict) -> str:
    """Our creatures that only pay off by attacking now: end-step sacrifices and this turn's own pumps."""
    if not isinstance(state, dict) or _our_turn(state) is not True:
        return ""
    local = state.get("local_seat_id")
    if local is None:
        local = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    turn = int(((state.get("turn") or {}).get("turn_number")) or 0)
    catalog = state.get("deck_catalog") if isinstance(state.get("deck_catalog"), dict) else {}
    lines = []
    for card in state.get("battlefield") or []:
        if local is not None and card.get("controller_seat_id") not in (None, local):
            continue
        if "creature" not in str(card.get("type_line") or "").lower() or card.get("is_tapped"):
            continue
        name = str(card.get("name") or "a creature")
        oracle = str(card.get("oracle_text") or "")
        if _END_STEP_SAC_RE.search(oracle) and card.get("turn_entered_battlefield", -1) == turn:
            lines.append(
                f"{name} is sacrificed at the beginning of this end step: it has no value after this combat, "
                "so attacking with it costs nothing (it dies either way)"
            )
        printed = _printed_power(card, catalog)
        try:
            power = int(card.get("power"))
        except (TypeError, ValueError):
            power = None
        if printed is not None and power is not None and power > printed and not card.get("counters"):
            lines.append(
                f"{name} is pumped to {card.get('power')}/{card.get('toughness')} until end of turn by your own spell "
                f"this turn (printed {printed}): the pump is wasted unless it attacks"
            )
    if not lines:
        return ""
    return (
        "THIS TURN'S INVESTMENTS (deterministic; made after the game plan above was formed, so follow through on them):\n"
        + "\n".join(f"  - {line}" for line in lines)
    )
