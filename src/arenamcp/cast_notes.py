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


_TEMP_GY_GRANT_RE = re.compile(
    r"\btarget (?P<kind>instant or sorcery|instant|sorcery|nonland) card in your graveyard gains flashback until end of turn\b"
    r"|\byou may cast target (?P<cast_kind>instant or sorcery|instant|sorcery|nonland) card from your graveyard (?:this turn|until end of turn)\b"
    r"|\buntil end of turn, you may cast target (?P<uet_kind>instant or sorcery|instant|sorcery|nonland) card from your graveyard\b",
    re.IGNORECASE,
)


def _matches_gy_kind(gy_card: dict, kind: str) -> bool:
    types = (
        f"{gy_card.get('type_line') or ''} "
        f"{' '.join(str(t) for t in (gy_card.get('card_types') or gy_card.get('types') or []))}"
    ).lower()
    if kind == "instant or sorcery":
        return "instant" in types or "sorcery" in types
    if kind == "instant":
        return "instant" in types
    if kind == "sorcery":
        return "sorcery" in types
    if kind == "nonland":
        return bool(types.strip()) and "land" not in types
    return False


def unpayable_graveyard_flashback(
    state: dict, card: dict | None, meta: dict | None = None
) -> dict[str, Any] | None:
    """When ``card`` grants temporary 'until end of turn' flashback/cast to a card in your graveyard
    and either your graveyard has no legal target or casting ``card`` leaves too little mana to cast
    any target in your graveyard this turn (e.g. Snapcaster Mage with 2 mana open and only Cryptic
    Command in the graveyard), return a structured dict describing why; else None."""
    if not isinstance(state, dict) or not card:
        return None
    oracle = str(card.get("oracle_text") or "")
    match = _TEMP_GY_GRANT_RE.search(oracle)
    if match is None or "without paying its mana cost" in oracle.lower():
        return None
    kind = (
        match.group("kind") or match.group("cast_kind") or match.group("uet_kind") or "instant or sorcery"
    ).lower()
    local = state.get("local_seat_id")
    if local is None:
        local = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    gy_targets = [
        c
        for c in (state.get("graveyard") or [])
        if isinstance(c, dict)
        and (local is None or c.get("owner_seat_id") in (None, local))
        and _matches_gy_kind(c, kind)
    ]
    from arenamcp.mana import mana_cost_to_cmc
    from arenamcp.play_safety import _effective_cost, exact_mana_total

    total_mana = exact_mana_total(state)
    auto_tap = (meta or {}).get("autoTapActions")
    printed_cmc = mana_cost_to_cmc(_effective_cost(card, meta or {}))
    cast_cost = max(len(auto_tap) if isinstance(auto_tap, list) else 0, printed_cmc)
    mana_after = max(0, total_mana - cast_cost) if total_mana is not None else None
    card_name = str(card.get("name") or "this card")
    if not gy_targets:
        return {
            "reason": "empty_graveyard",
            "card_name": card_name,
            "kind": kind,
            "mana_after": mana_after,
            "targets": [],
        }
    if mana_after is None:
        return None
    targets_info: list[tuple[str, str, int]] = []
    for g in gy_targets:
        g_cost = str(g.get("mana_cost") or "").strip()
        if not g_cost:
            return None
        g_cmc = mana_cost_to_cmc(g_cost)
        if g_cmc <= mana_after:
            return None
        targets_info.append((str(g.get("name") or "card"), g_cost, g_cmc))
    return {
        "reason": "unpayable_targets",
        "card_name": card_name,
        "kind": kind,
        "mana_after": mana_after,
        "targets": targets_info,
    }


def graveyard_flashback_note(state: dict, card: dict | None, meta: dict | None = None) -> str:
    """Warn on an option when casting ``card`` grants temporary 'until end of turn' graveyard flashback
    that cannot be paid for this turn."""
    info = unpayable_graveyard_flashback(state, card, meta)
    if info is None:
        return ""
    if info["reason"] == "empty_graveyard":
        return (
            f"  [FLASHBACK UNTIL END OF TURN ONLY: your graveyard has no {info['kind']} card to target "
            f"— casting {info['card_name']} now gets no flashback spell]"
        )
    targets_desc = ", ".join(f"{name} ({cost})" if cost else name for name, cost, _cmc in info["targets"][:3])
    return (
        f"  [FLASHBACK UNTIL END OF TURN ONLY: casting {info['card_name']} leaves {info['mana_after']} mana, "
        f"not enough to cast {targets_desc} this turn — the flashback expires at end of turn and does NOT "
        "carry over to future turns]"
    )
