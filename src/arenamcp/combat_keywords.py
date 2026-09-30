"""Conservative recognition of a creature's printed combat keywords."""

import re
from functools import lru_cache

_KEYWORDS = frozenset(
    {
        "deathtouch",
        "defender",
        "double strike",
        "first strike",
        "flying",
        "haste",
        "hexproof",
        "indestructible",
        "lifelink",
        "menace",
        "reach",
        "shroud",
        "trample",
        "vigilance",
    }
)


@lru_cache(maxsize=2048)
def printed_combat_keywords(oracle: str) -> frozenset[str]:
    """Read keyword lines, excluding grants, conditions, tokens, and reminder text.

    Text such as "another creature gains indestructible" does not make the
    source indestructible. Unmodeled continuous effects must not be invented
    from a keyword appearing somewhere in the source's rules text.
    """
    text = re.sub(r"<[^>]*>", "", oracle).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    found = set()
    for line in re.split(r"[\n.;]", text):
        for phrase in re.split(r",|\band\b", line):
            keyword = phrase.strip()
            if keyword not in _KEYWORDS:
                break
            found.add(keyword)
    return frozenset(found)


def has_combat_keyword(card: dict, keyword: str) -> bool:
    return keyword.lower() in printed_combat_keywords(card.get("oracle_text") or "")
