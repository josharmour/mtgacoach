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


# Arena's keyword ability grpIds, as GRE game objects list them in
# uniqueAbilities (printed and granted alike).
ABILITY_KEYWORDS = {
    1: "deathtouch",
    2: "defender",
    3: "double strike",
    6: "first strike",
    8: "flying",
    9: "haste",
    10: "hexproof",
    12: "lifelink",
    13: "reach",
    14: "trample",
    15: "vigilance",
    104: "indestructible",
    142: "menace",
}


def ability_keywords(ability_ids) -> list[str]:
    """The combat keywords among a game object's ability grpIds."""
    found = []
    for ability_id in ability_ids or []:
        keyword = ABILITY_KEYWORDS.get(ability_id)
        if keyword and keyword not in found:
            found.append(keyword)
    return found


def has_combat_keyword(card: dict, keyword: str) -> bool:
    """Printed keywords, plus keywords the game object currently has.

    2026-10-05: Titanbones wore an opponent's Medic's Kitesail. Its rules
    text says only reach, so the solver let 2/2s "chump" a 13-power flyer
    and sent both of our flyers into a lethal crackback.
    """
    keyword = keyword.lower()
    return keyword in (card.get("keywords") or ()) or keyword in printed_combat_keywords(
        card.get("oracle_text") or ""
    )
