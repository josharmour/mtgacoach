"""Check loyalty sequences in the deck description and the turn planner's free text."""

import re
from itertools import pairwise

LOYALTY_TIMING = (
    "Plan one loyalty activation per planeswalker permanent per turn, in our main phase. "
    "Put a second activation on a later turn unless an identified effect allows it. "
    "Entry and upkeep triggers do not spend loyalty. A same-name legendary token replaces "
    "or is lost to the existing copy under the legend rule. If choosing a postcombat "
    "sacrifice, leave loyalty unused until after the attack and check that the creature survived."
)

_SEPARATE = re.compile(
    r"\b(?:or|instead|alternat\w*|versus|rather than|recast\w*|blink\w*|flicker\w*|"
    r"(?:next|later|following|another|subsequent) (?:own |our )?turn|on a later main phase)\b|\bT(?:\+\d+|\d+)\b",
    re.I,
)
_EXTRA = re.compile(
    r"loyalty abilities[^.\n]*(?:twice|additional time|additional activation|as though none)", re.I
)
_TIMED = re.compile(r"\b(?:this|same|every|each) (?:turn|upkeep)\b|\bT\d+\b", re.I)


def walkers_in(cards):
    return [
        c
        for c in cards
        if isinstance(c, dict)
        and (
            "planeswalker" in str(c.get("type_line", "")).lower()
            or "CardType_Planeswalker" in (c.get("card_types") or [])
        )
    ]


def conflicting_loyalty_sequence(text, walkers, *, turn_step=False):
    """Identify a same-turn pair on one source. Leave alternatives/uncertain attribution alone."""
    if not walkers or not text:
        return False
    prose = text.replace("−", "-")
    if not re.search(r"[+-]\d|\[0\]", prose):
        return False
    names = {w.get("name", "").split(",")[0].lower() for w in walkers}
    named = {n for n in names if n and n in prose.lower()}
    if len(named or names) != 1:
        return False
    source = next(w for w in walkers if w.get("name", "").split(",")[0].lower() in (named or names))
    # Distinct battlefield instances can have independent activations.
    if (
        len(
            {
                w["instance_id"]
                for w in walkers
                if w.get("name") == source.get("name")
                and w.get("instance_id")
                and (w.get("counters") or {}).get("Loyalty") is not None
            }
        )
        > 1
    ):
        return False
    # Remove stat adjustments before looking for costs, preserving mixed-sign loyalty shorthand.
    prose = re.sub(r"([+-])\d+/\1\d+\b", "stats", prose)
    costs = list(re.finditer(r"(?<![\w+])(?:\[([+-]?\d+)\]|([+-]\d+))(?!\w)", prose))
    for first, second in pairwise(costs):
        between = prose[first.end() : second.start()]
        if not (turn_step or _TIMED.search(prose) or re.search(r"\bthen\b", between, re.I)):
            continue
        if _SEPARATE.search(between) or re.match(
            r"[^.;,]*?\b(?:next|following|later|subsequent) (?:own )?turn\b", prose[second.end() :], re.I
        ):
            continue
        printed = set(
            re.findall(
                r"(?:^|\n)\s*\[?([+-]?\d+)\]?\s*:", str(source.get("oracle_text") or "").replace("−", "-")
            )
        )
        if printed and not all((m[1] or m[2]) in printed for m in (first, second)):
            continue
        return True
    return False


def additional_activation(cards):
    return any(_EXTRA.search(str(c.get("oracle_text") or "")) for c in cards if not c.get("is_phased_out"))
