"""A spell's own cost reduction, read from its Oracle text.

"This spell costs {X} less to cast, where X is the greatest power among
creatures you control" (Ghalta the Unstoppable), "This spell costs {1} less
to cast for each artifact you control", "... where X is the number of
creatures you control". Only the spell's own static reduction ("this spell")
is read; another permanent's "spells you cast cost {1} less" is not. The
reduction pays generic mana only (``reduced_mana_value``).

Pro YFRA game 2026-10-18 (match 1 turn 15): Seed Suture made the Beast 5/5,
then Ghalta cost {3}{G}. The planner read Ghalta at its printed nine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_HEAD = r"\bthis spell costs \{(?P<n>x|\d+)\} less to cast\b"
_GREATEST_POWER = re.compile(
    _HEAD + r",? where x is the greatest power among (?:other )?creatures you control\b"
)
_NUMBER_OF = re.compile(_HEAD + r",? where x is the number of (?P<what>[a-z -]+?) you control\b")
_FOR_EACH = re.compile(_HEAD + r" for each (?P<what>[a-z -]+?) you control\b")


@dataclass(frozen=True)
class CostReduction:
    """``amount`` generic mana less per unit of ``per``: "greatest_power" or a permanent noun."""

    amount: int
    per: str
    text: str


def parse_cost_reduction(oracle_text: str) -> CostReduction | None:
    text = re.sub(r"<[^>]*>", "", str(oracle_text or "")).lower()
    match = _GREATEST_POWER.search(text)
    if match and match.group("n") == "x":
        return CostReduction(1, "greatest_power", match.group(0))
    for pattern in (_NUMBER_OF, _FOR_EACH):
        match = pattern.search(text)
        if match:
            n = match.group("n")
            amount = 1 if n == "x" else int(n)
            if pattern is _NUMBER_OF and n != "x":
                continue
            what = re.sub(r"^(?:a|an|other)\s+", "", match.group("what").strip())
            return CostReduction(amount, what.rstrip("s") if what.endswith("s") else what, match.group(0))
    return None


def _matches(card: dict, noun: str) -> bool:
    type_line = str(card.get("type_line") or "").lower()
    if noun in ("permanent", "card"):
        return True
    words = noun.split()
    return all(re.search(rf"\b{re.escape(w)}s?\b", type_line) for w in words if w not in ("and", "or"))


def reduction_amount(reduction: CostReduction | None, creature_powers, permanents=()) -> int:
    """How much generic mana ``reduction`` saves now: ``creature_powers`` are our creatures'
    powers, ``permanents`` our permanents (cards with type lines)."""
    if reduction is None:
        return 0
    if reduction.per == "greatest_power":
        return reduction.amount * max(0, max(creature_powers, default=0))
    if reduction.per == "creature":
        return reduction.amount * len(list(creature_powers))
    return reduction.amount * sum(1 for c in permanents if isinstance(c, dict) and _matches(c, reduction.per))


def reduced_mana_value(mana_value: int, pips: int, reduction: int) -> int:
    """The mana to pay once ``reduction`` generic is taken off: coloured pips stay."""
    generic = max(0, mana_value - pips)
    return mana_value - min(generic, max(0, reduction))
