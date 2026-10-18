"""What an optional-action prompt is asking, and when to refuse a life payment without a model.

2026-10-11 Historic Brawl (bug_20261011_131147, turn 10, 5 life): Witch-Blessed
Meadow asked "pay 3 life, or it enters tapped". The bridge's source id was a
trigger object that named nothing and the prompt's CardId parameter was never
read, so the model saw only "Accept the optional effect". It answered accept,
reasoning "banks another free Shrine", carried over from Hei Bai's prompt five
seconds earlier. The player declined first; the autopilot would have gone to 2.

This module is deterministic. It names the prompting cards with their rules
text (from the log's decision context when it is this exact request, else from
the bridge's prompt parameters) and declines paying life for our own optional
effect when the payment would leave us within reach of their board.
"""

from __future__ import annotations

import re
from typing import Any

# The life total at or below which a voluntary life payment is refused, whatever
# their board shows (their hand and draws can still reach it).
LOW_LIFE_FLOOR = 5

_PAY_LIFE = re.compile(r"\byou may pay (\d+) life\b", re.IGNORECASE)
_ZONES = ("battlefield", "hand", "stack", "graveyard", "exile", "command")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _local_seat(state: dict) -> int | None:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    return _int(seat)


def _same_request(decision: Any, context: dict) -> bool:
    """Whether the log's decision context describes this very request."""
    request_id = tuple(getattr(decision, "request_id", ()) or ())
    raw = context.get("raw") or {}
    if len(request_id) != 2 or not all(value and value > 0 for value in request_id):
        return False
    return (_int(raw.get("gameStateId")), _int(raw.get("msgId"))) == request_id


def _find_card(state: dict, instance_id: int) -> dict:
    for zone in _ZONES:
        for card in state.get(zone) or []:
            if isinstance(card, dict) and _int(card.get("instance_id")) == instance_id:
                return card
    return {}


def prompt_cards(decision: Any, state: dict) -> list[dict[str, Any]]:
    """Cards the optional prompt names: instance_id, name, oracle_text, controller_seat_id."""
    context = state.get("decision_context") or {}
    if context.get("type") == "optional_action" and _same_request(decision, context):
        cards = [dict(card) for card in context.get("prompt_cards") or [] if isinstance(card, dict)]
        if cards:
            return cards
    accept = decision.find("optional:accept") if hasattr(decision, "find") else None
    meta = getattr(accept, "meta", None) or {}
    names = list(meta.get("promptCards") or [])
    cards = []
    for index, instance_id in enumerate(meta.get("promptCardIds") or []):
        card = _find_card(state, _int(instance_id) or 0)
        cards.append(
            {
                "instance_id": instance_id,
                "name": card.get("name") or (names[index] if index < len(names) else ""),
                "oracle_text": card.get("oracle_text") or "",
                "controller_seat_id": card.get("controller_seat_id") or card.get("owner_seat_id"),
            }
        )
    return [card for card in cards if card.get("name") or card.get("oracle_text")]


def prompt_note(decision: Any, state: dict) -> str:
    """The OPTIONAL PROMPT lines for the typed-decision prompt."""
    context = state.get("decision_context") or {}
    question = ""
    if context.get("type") == "optional_action" and _same_request(decision, context):
        question = str(context.get("prompt") or "")
    local = _local_seat(state)
    cards = prompt_cards(decision, state)
    lines = [f"OPTIONAL PROMPT: {question or 'accept or decline the effect described below'}"]
    for card in cards:
        side = ""
        controller = _int(card.get("controller_seat_id"))
        if controller is not None and local is not None:
            side = " (yours)" if controller == local else " (opponent's)"
        oracle = re.sub(r"\s+", " ", str(card.get("oracle_text") or "")).strip()
        lines.append(
            f"  Asked by {card.get('name') or 'unknown card'}{side}: {oracle or 'rules text unknown'}"
        )
    if not cards and not question:
        lines.append(
            "  The card asking is not identified. Do not assume it is the same effect as an earlier "
            "prompt; accept only if accepting is safe whatever the effect is."
        )
    return "\n".join(lines)


def _our_life(state: dict, local: int | None) -> int | None:
    for player in state.get("players") or []:
        if player.get("seat_id") == local or (local is None and player.get("is_local")):
            return _int(player.get("life_total"))
    return None


def _their_power(state: dict, local: int | None) -> int:
    total = 0
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict):
            continue
        controller = _int(card.get("controller_seat_id") or card.get("owner_seat_id"))
        types = (
            " ".join(str(t) for t in card.get("card_types") or []) + " " + str(card.get("type_line") or "")
        )
        if controller is None or controller == local or "Creature" not in types:
            continue
        total += max(0, _int(card.get("power")) or 0)
    return total


def life_payment_decline(decision: Any, state: dict) -> str:
    """Why to decline paying life for our own optional effect now, or ""."""
    if getattr(decision, "request_type", "") != "OptionalAction" or decision.find("optional:decline") is None:
        return ""
    local = _local_seat(state)
    life = _our_life(state, local)
    if life is None:
        return ""
    power = _their_power(state, local)
    for card in prompt_cards(decision, state):
        controller = _int(card.get("controller_seat_id"))
        if controller is not None and local is not None and controller != local:
            continue  # an opponent's ward or tax: the ward policy owns it
        match = _PAY_LIFE.search(str(card.get("oracle_text") or ""))
        if not match:
            continue
        cost = int(match.group(1))
        after = life - cost
        if after <= max(LOW_LIFE_FLOOR, power):
            reason = (
                f"within reach of the {power} power on their board"
                if power >= after
                else "too low to spend on tempo"
            )
            return (
                f"Decline paying {cost} life for {card.get('name') or 'this effect'}: at {life} life it "
                f"would leave us at {after}, {reason}."
            )
    return ""


# "Its owner may put it on top of their library. If they do, Clash of Elements deals 2
# damage to them. If they didn't ..., they put it on the bottom."
_TOP_OR_BOTTOM = re.compile(
    r"\bowner may put it on (?:the )?top of (?:their|his or her) library\b.*?\bif they do\b[^.]*?"
    r"\bdeals? (\d+) damage to them\b",
    re.IGNORECASE | re.DOTALL,
)
_COST_SYMBOL = re.compile(r"\{([^}]+)\}")


def _mana_value(card: dict) -> int:
    total = 0
    for symbol in _COST_SYMBOL.findall(str(card.get("mana_cost") or "")):
        symbol = symbol.lstrip("oO")
        total += int(symbol) if symbol.isdigit() else (0 if symbol.upper() == "X" else 1)
    return total


def _is_token(card: dict) -> bool:
    kind = str(card.get("object_kind") or "").upper()
    return kind == "TOKEN" or "token" in str(card.get("type_line") or "").lower().split("—")[0]


def top_or_bottom_choice(decision: Any, state: dict) -> tuple[str, str]:
    """(option id, reason) for "put your permanent on top of your library and take N damage,
    or on the bottom"; ("", "") when no rule applies.

    Pro YFRA game 2026-10-18, match 1: Clash of Elements on Garruk (turn 12) -
    top, take 2, redraw and recast him; on the Beast token (turn 14) - bottom,
    since a token leaves the library at once. Top is taken only for a nontoken
    planeswalker or a permanent of mana value 3 or more, and only when the
    damage leaves us out of reach of their board.
    """
    if getattr(decision, "request_type", "") != "OptionalAction" or not hasattr(decision, "find"):
        return "", ""
    accept, decline = decision.find("optional:accept"), decision.find("optional:decline")
    if accept is None or decline is None:
        return "", ""
    local = _local_seat(state)
    cards = prompt_cards(decision, state)
    damage = None
    for card in cards:
        match = _TOP_OR_BOTTOM.search(str(card.get("oracle_text") or ""))
        if match:
            damage = int(match.group(1))
            break
    if damage is None:
        return "", ""
    recipients = {_int(i) for i in (getattr(accept, "meta", None) or {}).get("recipients") or []}
    ours = []
    for card in cards:
        iid = _int(card.get("instance_id"))
        full = _find_card(state, iid or 0) or card
        if (recipients and iid not in recipients) or _TOP_OR_BOTTOM.search(str(full.get("oracle_text") or "")):
            continue
        controller = _int(full.get("controller_seat_id") or full.get("owner_seat_id"))
        if local is not None and controller == local:
            ours.append(full)
    if len(ours) != 1:
        return "", ""
    target = ours[0]
    name = target.get("name") or "our permanent"
    if _is_token(target):
        return "optional:decline", (
            f"Put {name} on the bottom: a token leaves the library at once, so the top only costs "
            f"{damage} life."
        )
    types = " ".join(str(t) for t in target.get("card_types") or []) + " " + str(target.get("type_line") or "")
    valuable = "planeswalker" in types.lower() or _mana_value(target) >= 3
    life = _our_life(state, local)
    if not valuable or life is None:
        return "", ""
    after = life - damage
    if after <= max(LOW_LIFE_FLOOR, _their_power(state, local)):
        return "", ""
    return "optional:accept", (
        f"Put {name} on top of the library and take {damage}: we redraw it next turn (at {life} life, "
        f"{after} after) instead of losing it to the bottom."
    )
