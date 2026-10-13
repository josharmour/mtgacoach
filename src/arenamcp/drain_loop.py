"""Recognise an opponent's infinite life-drain loop and the removal that breaks it.

2026-10-11 Historic Brawl (bug_20261011_131147, turn 9): the opponent had
Vito, Thorn of the Dusk Rose ("whenever you gain life, target opponent loses
that much life") and resolved Exquisite Blood ("whenever an opponent loses
life, you gain that much life"). One lifelink hit started the loop and each
cycle cost us 3 life, from 22 down to 5. Tear Asunder could exile Exquisite
Blood in every one of the 13 priority windows. The autopilot spent its one
attempt on a 15-second high-effort model call that timed out, and nothing
warned the player that the pair was a combo.

Any pair of a "gain life -> opponent loses life" trigger and an "opponent
loses life -> gain life" trigger under one controller is a loop: each trigger
feeds the other and neither amount reaches zero. This module is
deterministic. It finds such a pair on the opponent's side (battlefield, and
optionally the stack), names the removal in our hand that can break it, and
writes the warning line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arenamcp.rules_engine import RulesEngine

# "Whenever you gain life, target opponent loses that much life." (Vito,
# Sanguine Bond); "..., each opponent loses 1 life." (Marauding Blight-Priest).
_GAIN_TO_DRAIN = re.compile(
    r"whenever you gain life\b[^.]*?\b(?:target opponent|each opponent|target player|each player)\b"
    r"[^.]*?\bloses?\b[^.]*?\blife",
    re.IGNORECASE,
)
# "Whenever an opponent loses life, you gain that much life." (Exquisite
# Blood, Bloodthirsty Conqueror).
_DRAIN_TO_GAIN = re.compile(
    r"whenever (?:an opponent|a player|another player|each opponent|one or more opponents?) "
    r"loses? life\b[^.]*?\byou gain\b[^.]*?\blife",
    re.IGNORECASE,
)
# Clauses that remove a permanent from the battlefield.
_REMOVAL_CLAUSE = re.compile(
    r"\b(?:exile|destroy) (?:up to one )?target\b|\breturn (?:up to one )?target\b[^.]*\bowner[’']?s hand\b",
    re.IGNORECASE,
)
# A clause that only applies under a condition we can't verify (kicker paid,
# bargained, a threshold met): the unconditional clause is the safe reading.
_CONDITIONAL = re.compile(r"\b(?:kicked|bargained|if you|if it|if this|instead|unless)\b", re.IGNORECASE)

GAIN_TO_DRAIN = "gain_to_drain"
DRAIN_TO_GAIN = "drain_to_gain"


def _text(card: dict) -> str:
    return str(card.get("oracle_text") or "")


def _controller(card: dict) -> int | None:
    value = card.get("controller_seat_id") or card.get("owner_seat_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _local_seat(state: dict) -> int | None:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    try:
        return int(seat) if seat is not None else None
    except (TypeError, ValueError):
        return None


def _opponent_seat(state: dict, local: int | None) -> int | None:
    for player in state.get("players") or []:
        seat = player.get("seat_id")
        if seat is not None and seat != local and not player.get("is_local"):
            return int(seat)
    return None


def piece_role(card: dict) -> str:
    """Which half of a drain loop this card is, or "" when it is neither."""
    if str(card.get("object_kind") or "").upper() == "ABILITY":
        return ""
    text = _text(card)
    if _GAIN_TO_DRAIN.search(text):
        return GAIN_TO_DRAIN
    if _DRAIN_TO_GAIN.search(text):
        return DRAIN_TO_GAIN
    return ""


@dataclass(frozen=True)
class DrainLoop:
    """Both halves of an opponent's drain loop."""

    gainers: tuple[dict, ...]  # "whenever you gain life, an opponent loses life"
    drainers: tuple[dict, ...]  # "whenever an opponent loses life, you gain life"
    pending: tuple[dict, ...] = ()  # pieces still on the stack

    @property
    def live(self) -> bool:
        """Both halves are on the battlefield: any life gain or loss now chains."""
        return not self.pending

    @property
    def pieces(self) -> tuple[dict, ...]:
        """The pieces on the battlefield (removal can only reach these)."""
        pending = {id(card) for card in self.pending}
        return tuple(card for card in (*self.drainers, *self.gainers) if id(card) not in pending)

    def names(self) -> str:
        gainer = self.gainers[0].get("name") or "a lifegain drain"
        drainer = self.drainers[0].get("name") or "a life-loss gain"
        return f"{gainer} + {drainer}"

    def key(self) -> tuple:
        return tuple(sorted(int(card.get("instance_id") or 0) for card in (*self.gainers, *self.drainers)))


def opponent_drain_loop(state: dict | None, *, include_stack: bool = False) -> DrainLoop | None:
    """The opponent's drain loop, or None.

    ``include_stack`` also counts their spells still on the stack, so the
    warning comes the moment the second piece is cast.
    """
    if not isinstance(state, dict):
        return None
    local = _local_seat(state)
    if local is None:
        return None
    gainers: list[dict] = []
    drainers: list[dict] = []
    pending: list[dict] = []
    zones = ["battlefield"] + (["stack"] if include_stack else [])
    for zone in zones:
        for card in state.get(zone) or []:
            if not isinstance(card, dict):
                continue
            controller = _controller(card)
            if controller is None or controller == local:
                continue
            role = piece_role(card)
            if not role:
                continue
            (gainers if role == GAIN_TO_DRAIN else drainers).append(card)
            if zone == "stack":
                pending.append(card)
    if not gainers or not drainers:
        return None
    return DrainLoop(tuple(gainers), tuple(drainers), tuple(pending))


def _protected(piece: dict) -> bool:
    keywords = {str(k).lower() for k in piece.get("keywords") or []}
    if "hexproof" in keywords or "shroud" in keywords:
        return True
    first = _text(piece).lower().split("\n", 1)[0]
    return bool(re.match(r"(?:hexproof|shroud)\b", first))


def _removal_sentences(card: dict) -> list[str]:
    """Sentences of the card's spell or enters text that remove a permanent unconditionally."""
    sentences: list[str] = []
    is_permanent_spell = any(
        word in str(card.get("type_line") or "").lower()
        for word in ("creature", "artifact", "enchantment", "planeswalker", "battle")
    )
    for line in _text(card).split("\n"):
        line = line.strip()
        if not line or ":" in line.split(".")[0]:
            continue  # activated abilities are not part of casting this card
        lowered = line.lower()
        if is_permanent_spell and not re.match(r"when [^.]*\benters\b", lowered):
            continue  # a permanent's own static or other triggers
        for sentence in re.split(r"(?<=\.)\s+", line):
            if _REMOVAL_CLAUSE.search(sentence) and not _CONDITIONAL.search(sentence):
                sentences.append(sentence)
    return sentences


def card_removes(card: dict, piece: dict, state: dict) -> bool:
    """Whether casting ``card`` can unconditionally remove ``piece`` from the battlefield."""
    if not card or not piece or _protected(piece):
        return False
    local = _local_seat(state)
    if local is None:
        return False
    opponent = _opponent_seat(state, local)
    for sentence in _removal_sentences(card):
        req = RulesEngine._infer_target_requirements(sentence)
        if req["player_target"] or req["must_control"] == "you":
            continue
        if req["zones"] - {"battlefield"} or not (req["types"] or req["permanent_target"]):
            continue
        if RulesEngine._match_battlefield_targets([piece], local, opponent, req):
            return True
    return False


def answers_in_hand(state: dict, loop: DrainLoop) -> list[tuple[dict, dict]]:
    """(card in our hand, loop piece it removes), mana ignored."""
    local = _local_seat(state)
    found: list[tuple[dict, dict]] = []
    for card in state.get("hand") or []:
        if not isinstance(card, dict) or _controller(card) not in (None, local):
            continue
        for piece in loop.pieces:
            if card_removes(card, piece, state):
                found.append((card, piece))
                break
    return found


def warning_line(state: dict, loop: DrainLoop) -> str:
    """One spoken line: the combo, why it kills, and our answer if we hold one."""
    line = (
        f"Infinite drain combo: {loop.names()}. Any life they gain or you lose chains "
        "back and forth until you're dead."
    )
    answers = answers_in_hand(state, loop)
    if answers:
        card, piece = answers[0]
        line += f" Answer it now: {card.get('name')} can remove {piece.get('name')}."
    else:
        line += " Remove either piece the moment you can."
    return line


def facts_flag(state: dict) -> str:
    """The board-facts flag for an assembled loop, or ""."""
    loop = opponent_drain_loop(state)
    if loop is None:
        return ""
    answers = answers_in_hand(state, loop)
    answer = f"; {answers[0][0].get('name')} removes {answers[0][1].get('name')}" if answers else ""
    return (
        f"OPPONENT DRAIN LOOP ASSEMBLED ({loop.names()}): any life gain or loss chains until we are dead "
        f"— removing either piece outranks every other play{answer}"
    )


def _source_card(meta: dict, state: dict) -> dict:
    instance_id = meta.get("instanceId")
    if not instance_id:
        return {}
    for zone in ("hand", "command", "graveyard", "exile", "battlefield"):
        for card in state.get(zone) or []:
            if isinstance(card, dict) and card.get("instance_id") == instance_id:
                return card
    return {}


def _cost(card: dict) -> int:
    return len(re.findall(r"\{[^}]+\}", str(card.get("mana_cost") or "")))


def loop_answer(decision: Any, state: dict) -> tuple[str, dict, dict] | None:
    """(option id, card, piece): the cheapest payable cast that breaks a live loop.

    Only casts: an activation's removal text can't be told apart from the
    permanent's other abilities reliably enough to fire without a model.
    """
    loop = opponent_drain_loop(state)
    if loop is None or not loop.live:
        return None
    best: tuple[int, str, dict, dict] | None = None
    for option in getattr(decision, "options", ()) or ():
        if getattr(option, "payable", None) is False:
            continue
        meta = getattr(option, "meta", {}) or {}
        if str(meta.get("actionType") or "") != "ActionType_Cast":
            continue
        card = _source_card(meta, state)
        if not card:
            continue
        piece = next((p for p in loop.pieces if card_removes(card, p, state)), None)
        if piece is None:
            continue
        rank = _cost(card)
        if best is None or rank < best[0]:
            best = (rank, option.option_id, card, piece)
    if best is None:
        return None
    return best[1], best[2], best[3]


def loop_target(decision: Any, state: dict, source: dict | None) -> str | None:
    """The tgt: option for a loop piece when ``source`` (the removal being targeted) can hit it."""
    loop = opponent_drain_loop(state)
    if loop is None or not source:
        return None
    offered = {getattr(option, "option_id", "") for option in getattr(decision, "options", ()) or ()}
    for piece in loop.pieces:
        option_id = f"tgt:{piece.get('instance_id')}"
        if option_id in offered and card_removes(source, piece, state):
            return option_id
    return None
