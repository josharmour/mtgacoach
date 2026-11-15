"""Allocation optimization preserves the original complete quality algorithm."""

import random
from copy import deepcopy

from arenamcp import combat_protection as cp


def legacy(card):
    raw = cp._raw(card)
    printed = cp._printed(str(raw.get("oracle_text") or ""))
    found = set(printed)
    if not card.get("_protection_future"):
        for obj in (raw, card):
            for ability in obj.get("ability_grp_ids") or []:
                if ability in cp.COLOR_PROTECTION_ABILITIES:
                    found.add(cp.COLOR_PROTECTION_ABILITIES[ability])
            for field in ("keywords", "granted_abilities"):
                for value in obj.get(field) or []:
                    found.update(cp._printed(str(value)))
    removed = [str(v).lower() for v in raw.get("removed_abilities") or []]
    if any(v in ("all abilities", "protection") for v in removed):
        return frozenset()
    for value in removed:
        found.difference_update(cp._printed(value))
    return frozenset(found)


def test_quality_allocation_full_field_differential():
    rng = random.Random(621)
    words = [
        "",
        "Flying",
        "Protection from red",
        "Protection from everything",
        "Protection from white and from blue",
        "Protection from colorless",
    ]

    def node():
        return dict(
            oracle_text=rng.choice(words),
            ability_grp_ids=rng.sample([185, 186, 189, 999], rng.randrange(4)),
            keywords=rng.sample(words, rng.randrange(4)),
            granted_abilities=rng.sample(words, rng.randrange(4)),
            removed_abilities=rng.sample(words + ["all abilities", "protection"], rng.randrange(3)),
        )

    for _ in range(1000):
        card = node()
        if rng.choice([False, True]):
            card["_card"] = node()
        card["_protection_future"] = rng.choice([False, True])
        before = deepcopy(card)
        assert cp.protection_qualities(card) == legacy(card)
        assert card == before
