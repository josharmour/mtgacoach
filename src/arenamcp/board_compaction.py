"""Compact board text for the decision prompts on token swarms (ledger #182).

Brawl token boards (Leonardo, Big Brother with Hare Apparents against Myrel's
Soldiers, 2026-10-19 match 41b07b15) sent 38-119K-character decision prompts:
the time to first token on the shared GPUs reached 7.9 s and then the 8.0 s
cap (15:22:18, 15:48:58, 15:54:35 in standalone.log), and the decisions fell
back to board math. Three quarters of such a prompt was the "If X blocks Y"
trade matrix: one line per attacker and blocker, so fifteen attackers into
thirty-three untapped blockers made 528 lines, most of them the same line
("If Soldier 1/1 blocks Rabbit 1/1: TRADE (both die)") over and over. The
board listed every token on its own line too.

Identical lines are written once with how many cards they stand for:

- ``group_trades``: "If Soldier 1/1 (any of 23) blocks Rabbit 1/1 (any of 12):
  TRADE (both die)".
- ``group_labels``: "Rabbit #1, Rabbit #2, ... Rabbit #12" -> "Rabbit #1..#12";
  each card's number stays readable.
- ``group_counted``: "Soldier 1/1, Soldier 1/1, ..." -> "Soldier 1/1 ×23".
- ``number_ranges``: "#1..#3, #5" for the board's grouped permanents.

Only text that is already the same is merged: cards that differ in power,
toughness, tapped / sick / attacking / blocking state, counters, damage,
keywords or attachments render differently and stay apart (the board keys a
group on a card's full rendering). Pure; reads strings only.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable

# "Rabbit #12" / "Rabbit #12 1/1 (FLY)": a numbered label, its number and what follows it.
_NUMBERED = re.compile(r"^(?P<name>.+?) #(?P<n>\d+)(?P<rest>(?: .*)?)$")


def number_ranges(numbers: Iterable[int], *, sep: str = ", ") -> str:
    """'#1..#3, #5' for [1, 2, 3, 5] (sorted, duplicates dropped)."""
    values = sorted(set(int(n) for n in numbers))
    runs: list[str] = []
    index = 0
    while index < len(values):
        end = index
        while end + 1 < len(values) and values[end + 1] == values[end] + 1:
            end += 1
        first, last = values[index], values[end]
        runs.append(f"#{first}" if first == last else f"#{first}..#{last}")
        index = end + 1
    return sep.join(runs)


def group_labels(labels: Iterable[str], *, minimum: int = 3) -> list[str]:
    """Labels with each run of numbered twins ("Rabbit #1", "Rabbit #2", ...) as one entry
    ("Rabbit #1..#12"), placed where its first member stands. Twins must match in everything
    after the number ("Rabbit #3 1/1" and "Rabbit #4 2/2" stay apart). Classes with fewer than
    ``minimum`` members, and labels without a number, are left as they are."""
    items = list(labels)
    classes: dict[tuple[str, str], list[int]] = {}
    for label in items:
        match = _NUMBERED.match(str(label))
        if match:
            classes.setdefault((match.group("name"), match.group("rest")), []).append(int(match.group("n")))
    out: list[str] = []
    done: set[tuple[str, str]] = set()
    for label in items:
        match = _NUMBERED.match(str(label))
        key = (match.group("name"), match.group("rest")) if match else None
        if key is None or len(classes[key]) < minimum:
            out.append(label)
        elif key not in done:
            done.add(key)
            out.append(f"{key[0]} {number_ranges(classes[key], sep='/')}{key[1]}")
    return out


def group_counted(labels: Iterable[str], *, minimum: int = 2) -> list[str]:
    """Identical labels as one entry with a count ("Soldier 1/1 ×23"), in first-appearance order."""
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return [label if count < minimum else f"{label} ×{count}" for label, count in counts.items()]


def group_trades(entries: Iterable[tuple[str, Hashable, str, Hashable, str]]) -> list[str]:
    """The "If <blocker> blocks <attacker>: <result>" lines, each distinct line once.

    ``entries``: (blocker label "Soldier 1/1", blocker identity, attacker label
    "Rabbit 1/1", attacker identity, result text). A line that stands for every
    pairing of several blockers with several attackers names how many of each
    ("If Soldier 1/1 (any of 23) blocks Rabbit 1/1 (any of 12): ..."); one that
    stands for only some of those pairings says how many it covers.
    """
    groups: dict[tuple[str, str, str], tuple[set, set, set]] = {}
    for blocker, blocker_id, attacker, attacker_id, result in entries:
        blockers, attackers, pairs = groups.setdefault((blocker, attacker, result), (set(), set(), set()))
        blockers.add(blocker_id)
        attackers.add(attacker_id)
        pairs.add((blocker_id, attacker_id))
    lines = []
    for (blocker, attacker, result), (blockers, attackers, pairs) in groups.items():
        if len(pairs) == 1:
            lines.append(f"  If {blocker} blocks {attacker}: {result}")
        elif len(pairs) == len(blockers) * len(attackers):
            many_b = f" (any of {len(blockers)})" if len(blockers) > 1 else ""
            many_a = f" (any of {len(attackers)})" if len(attackers) > 1 else ""
            lines.append(f"  If {blocker}{many_b} blocks {attacker}{many_a}: {result}")
        else:
            lines.append(f"  If {blocker} blocks {attacker}: {result} [{len(pairs)} such pairs]")
    return lines


__all__ = ["group_counted", "group_labels", "group_trades", "number_ranges"]
