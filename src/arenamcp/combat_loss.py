"""Player-facing combat consequences, independent of life totals (#247/#281).

Only printed, unconditional self abilities are recognized. Activated grants,
conditional triggers, replacement effects and unknown library sizes stay out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from arenamcp.library_counts import LIBRARY_COUNT_SOURCES
from arenamcp.poison_clock import poison_count


@dataclass(frozen=True)
class LossState:
    poison: int = 0
    library: int | None = None

    def hit(self, poison: int, mill: int) -> LossState:
        if not poison and (not mill or self.library is None):
            return self
        return LossState(self.poison + poison, None if self.library is None else max(0, self.library - mill))

    @property
    def lost(self) -> bool:
        return self.poison >= 10

    @property
    def draw_loses(self) -> bool:
        return self.library == 0

    def draw(self) -> LossState:
        return self.hit(0, 1)


def loss_state(state: dict | None, *, opponent: bool = False) -> LossState:
    state = state or {}
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    if local is None:
        return LossState()
    player = next(
        (p for p in players if (p.get("seat_id") != local if opponent else p.get("seat_id") == local)), {}
    )
    zones = state.get("zones") or {}
    key = "opponent_library_count" if opponent else "library_count"
    raw = zones.get(key, state.get(key))
    source = zones.get(key + "_source", state.get(key + "_source"))
    library = raw if type(raw) is int and raw >= 0 and source != "unknown" else None
    if library == 0 and source not in LIBRARY_COUNT_SOURCES:
        library = None
    return LossState(poison_count(player), library)


@lru_cache(maxsize=2048)
def _printed(name: str, oracle: str) -> tuple[int, bool, tuple[int, ...]]:
    text = re.sub(r"<[^>]*>", "", oracle).lower()
    text = re.sub(r"\([^)]*\)", "", text)
    poison = 0
    infect = False
    mills = []
    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "twenty": 20,
    }
    trigger = (
        r"whenever (?:this creature|"
        + re.escape(name.lower())
        + r") deals combat damage to a player, that player mills (\d+|\w+|that many)(?: cards?)?\."
    )
    for line in text.splitlines():
        line = line.strip()
        # Keyword lists only; do not activate future grants or other creatures' abilities.
        for ability in re.split(r",\s*", line):
            if re.fullmatch(r"(?:toxic|poisonous) \d+\.?", ability):
                poison += int(re.search(r"\d+", ability)[0])
            elif ability == "infect":
                infect = True
        match = re.fullmatch(trigger, line)
        if match:
            amount = match[1]
            if amount == "that many":
                mills.append(-1)  # scales with combat damage, not the power or life loss
            elif amount.isdigit() or amount in numbers:
                mills.append(int(amount) if amount.isdigit() else numbers[amount])
    return poison, infect, tuple(mills)


def player_hit(card: dict, damage: int, hits: int) -> tuple[int, int, int]:
    """(life loss, poison, milled) from actual positive combat-damage events."""
    toxic, infect, mills = _printed(str(card.get("name") or ""), str(card.get("oracle_text") or ""))
    keywords = [str(k).lower() for k in card.get("keywords") or []]
    infect |= "infect" in keywords
    # Snapshot keywords and printed text often describe the same ability.
    if not toxic:
        toxic = sum(int(k.split()[-1]) for k in keywords if re.fullmatch(r"(?:toxic|poisonous) \d+", k))
    return (
        0 if infect else damage,
        (damage if infect else 0) + toxic * hits,
        sum(damage if amount == -1 else amount * hits for amount in mills),
    )


def infect_source(card: dict) -> bool:
    if "infect" in (str(k).lower() for k in card.get("keywords") or ()):
        return True
    text = str(card.get("oracle_text") or "")
    return "infect" in text.lower() and _printed(str(card.get("name") or ""), text)[1]


class CombatDetail(tuple):
    """Existing combat tuple plus the defending player's alternate-loss state."""

    def __new__(cls, values, loss: LossState = LossState(), terminal: bool = False, counter_deltas=None, damage_deltas=None):
        result = super().__new__(cls, values)
        result.loss = loss
        result.terminal = terminal
        result.counter_deltas = dict(counter_deltas or {})
        result.damage_deltas = dict(damage_deltas or {})
        return result
