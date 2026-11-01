"""Observed poison and conditional alternate-loss clocks (#281/#247).

Printed text is not a guaranteed activation. A projected rate needs repeated
recent gains and a surviving visible source, not a one-shot Vraska jump.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


def poison_count(player: dict) -> int:
    for kind, value in (player.get("counters") or {}).items():
        if str(kind).lower().removeprefix("countertype_") == "poison":
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


@dataclass(frozen=True)
class PoisonThreat:
    counters: int
    engines: tuple[str, ...] = ()
    proliferators: tuple[str, ...] = ()
    rate: int | None = None
    turns: int | None = None

    @property
    def urgent(self) -> bool:
        return (
            self.counters >= 10
            or (self.counters >= 9 and bool(self.proliferators))
            or (self.turns is not None and self.turns <= 2)
        )

    def fact(self) -> str:
        text = f"we have {self.counters}/10 poison ({max(0, 10 - self.counters)} more loses the game regardless of life)"
        if self.turns is not None:
            text += f"; poison loss in ~{self.turns} turn(s) if recent rate {self.rate}/turn continues"
        if self.counters and self.proliferators:
            text += f"; {max(0, 10 - self.counters)} proliferate resolution(s) can finish us"
        if self.engines:
            text += "; possible poison sources: " + ", ".join(self.engines)
        return text

    def role_note(self, our_clock: int | None) -> str:
        if not self.urgent and not (self.turns is not None and (our_clock is None or self.turns < our_clock)):
            return ""
        return (
            "poison can beat the life race — answer the poison/proliferate source or win before it; "
            "life-only search lines do not establish survival"
        )


def poison_threat(state: dict) -> PoisonThreat | None:
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    if local is None:
        return None
    ours = next((p for p in players if p.get("seat_id") == local), {})
    count = poison_count(ours)
    opponent = state.get("opponent_seat_id") or next(
        (p.get("seat_id") for p in players if p.get("seat_id") != local), None
    )
    engines, proliferators = [], []
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict) or opponent is None:
            continue
        controller = card.get("controller_seat_id")
        if controller is None:
            controller = card.get("owner_seat_id")
        if controller != opponent:
            continue
        text = str(card.get("oracle_text") or "").lower()
        name = str(card.get("name") or "unnamed permanent")
        proliferate = bool(re.search(r"\bproliferate\b", text))
        poison = bool(re.search(r"\b(?:poison counters?|infect|toxic|poisonous)\b", text))
        if poison or (count > 0 and proliferate):
            engines.append(name)
        if proliferate:
            proliferators.append(name)
    if not count and not engines:
        return None
    current_turn = (state.get("turn") or {}).get("turn_number", 0)
    gains: dict[int, int] = {}
    for event in state.get("recent_events") or []:
        if (
            not isinstance(event, dict)
            or event.get("type") != "player_counter_changed"
            or event.get("seat_id") != local
            or str(event.get("counter_type", "")).lower() != "poison"
        ):
            continue
        turn, amount = event.get("turn"), event.get("amount")
        if isinstance(turn, int) and isinstance(amount, int) and current_turn - 4 <= turn <= current_turn:
            gains[turn] = gains.get(turn, 0) + amount
    positive = [value for value in gains.values() if value > 0]
    rate = min(positive) if len(positive) >= 2 and engines else None
    turns = math.ceil(max(0, 10 - count) / rate) if rate else None
    return PoisonThreat(count, tuple(engines), tuple(proliferators), rate, turns)
