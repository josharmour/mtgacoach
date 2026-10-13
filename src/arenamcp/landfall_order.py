"""Cast a landfall permanent before the land drop that would trigger it.

2026-10-12 turn 11 (live, Mac): with Terrasymbiosis out, the model played
Ba Sing Se ("a free land drop ... keeping my 5 mana intact to cast Evolution
Sage") and cast Evolution Sage four seconds later, so its "Landfall — Whenever
a land you control enters, proliferate" missed the land drop, and so did the
Terrasymbiosis draw. The game plan's T11 step cast Sage that turn; the line
search cannot value the trigger, and every land-first default (the planner
preflight, the board-math fallback) played the land without looking.

Casting first costs nothing when the card is cast this turn anyway: Arena's
payable flag says the current mana covers it, and the land's mana arrives
after. So the reorder applies only to a payable, non-X permanent whose
"whenever a land you control enters" trigger works from the battlefield, and
only when this turn's plan casts it (the model's own reasoning without hold
language, the strategy block's THIS TURN line, or the searched line's casts).
Everything here is log-derivable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from arenamcp.play_safety import find_source

# "Landfall — Whenever a land you control enters", the older "Whenever a land
# enters the battlefield under your control", and "one or more lands". Not
# "if you had a land enter this turn" (Searing Blaze wants the land first) and
# not lands an opponent controls.
_TRIGGER = re.compile(
    r"whenever (?:a|one or more|another) (?:basic )?lands? "
    r"(?:you control enters?|enters?(?: the battlefield)? under your control)"
)
_HOLD = re.compile(r"\b(?:hold|holding|held|save|saving|sandbag\w*|next turn|later|wait|waiting)\b")
_THIS_TURN_CASTS = re.compile(r"\bcasts? ([^;|\n\[]*)")
_HOLD_WINDOW = 30


@dataclass(frozen=True)
class LandfallFirst:
    option_id: str
    card_name: str
    land_name: str
    reason: str


def landfall_trigger(card: dict[str, Any]) -> bool:
    """A permanent whose own trigger fires on our land drops while it is on the battlefield."""
    types = str(card.get("type_line") or "").lower()
    if not types.strip() or re.search(r"\b(?:instant|sorcery)\b", types):
        return False
    text = str(card.get("oracle_text") or "").lower()
    for match in _TRIGGER.finditer(text):
        end = text.find(".", match.end())
        # Bloodghast: the trigger only returns it from the graveyard.
        if "from your graveyard" not in text[match.start() : end if end >= 0 else len(text)]:
            return True
    return False


def _named_without_hold(name: str, reasoning: str) -> bool:
    text, needle = reasoning.casefold(), name.casefold()
    start = text.find(needle)
    while start >= 0:
        window = text[max(0, start - _HOLD_WINDOW) : start + len(needle) + _HOLD_WINDOW]
        if not _HOLD.search(window):
            return True
        start = text.find(needle, start + 1)
    return False


def _in_this_turn_line(name: str, plan_block: str) -> bool:
    needle = name.casefold()
    for line in plan_block.splitlines():
        if "THIS TURN" in line:
            return any(needle in found.casefold() for found in _THIS_TURN_CASTS.findall(line))
    return False


def planned_this_turn(
    name: str, *, reasoning: str = "", plan_block: str = "", planned_names: Iterable[str] = ()
) -> bool:
    """Whether this turn's plan casts ``name`` (so casting it now spends nothing extra)."""
    if any(str(n).casefold() == name.casefold() for n in planned_names):
        return True
    return _named_without_hold(name, reasoning) or _in_this_turn_line(name, plan_block)


def _own_main_phase_with_empty_stack(state: dict[str, Any]) -> bool:
    turn = state.get("turn") or {}
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )
    if local is None or turn.get("active_player") != local:
        return False
    return "main" in str(turn.get("phase") or "").lower() and not (state.get("stack") or [])


def landfall_first(
    decision: Any,
    game_state: dict[str, Any],
    picked: str,
    *,
    reasoning: str = "",
    plan_block: str = "",
    planned_names: Iterable[str] = (),
) -> LandfallFirst | None:
    """The planned landfall cast to make before the land ``picked`` names, or None."""
    if decision.request_type != "ActionsAvailable" or not _own_main_phase_with_empty_stack(game_state):
        return None
    land = decision.find(picked)
    if land is None or (land.meta or {}).get("actionType") != "ActionType_Play":
        return None
    land_name = str(find_source(game_state, land.meta or {}).get("name") or "") or "the land"
    planned_names = list(planned_names)
    for option in decision.options:
        meta = option.meta or {}
        if option.payable is not True or meta.get("actionType") != "ActionType_Cast":
            continue
        card = find_source(game_state, meta)
        name = str(card.get("name") or "")
        # An X spell trades the land's extra mana for the trigger: the model's call.
        if not name or "{x}" in str(card.get("mana_cost") or "").lower() or not landfall_trigger(card):
            continue
        if not planned_this_turn(
            name, reasoning=reasoning, plan_block=plan_block, planned_names=planned_names
        ):
            continue
        return LandfallFirst(
            option_id=option.option_id,
            card_name=name,
            land_name=land_name,
            reason=f"Cast {name} before playing {land_name} so its landfall trigger sees the land drop.",
        )
    return None


def landfall_cast_pending(game_state: dict[str, Any], legal_actions: Iterable[str]) -> str:
    """Name of a payable ("[OK]") landfall permanent in a legacy legal-action list, else ''."""
    hand = {
        str(c.get("name") or "").casefold(): c for c in game_state.get("hand") or [] if isinstance(c, dict)
    }
    for legal in legal_actions:
        text = str(legal)
        if not text.lower().startswith("cast ") or "[OK]" not in text:
            continue
        name = text[5:].split("[OK]", 1)[0].strip()
        card = hand.get(name.casefold())
        if card and "{x}" not in str(card.get("mana_cost") or "").lower() and landfall_trigger(card):
            return name
    return ""
