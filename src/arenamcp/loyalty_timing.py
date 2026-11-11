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


def _single_named_source(text, walkers):
    """Return the one walker the prose attributes loyalty costs to, or None."""
    if not walkers or not text:
        return None
    prose = text.replace("−", "-")
    if not re.search(r"[+-]\d|\[0\]", prose):
        return None
    names = {w.get("name", "").split(",")[0].lower() for w in walkers}
    named = {n for n in names if n and n in prose.lower()}
    if len(named or names) != 1:
        return None
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
        return None
    return source


def _offending_pair(prose, source, *, turn_step=False):
    """Return the (first, second) cost matches of a same-turn pair, or None."""
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
        return first, second
    return None


def conflicting_loyalty_sequence(text, walkers, *, turn_step=False):
    """Identify a same-turn pair on one source. Leave alternatives/uncertain attribution alone."""
    source = _single_named_source(text, walkers)
    if source is None:
        return False
    return _offending_pair(text.replace("−", "-"), source, turn_step=turn_step) is not None


_CLAUSE_END = re.compile(r"[.;,\n]")
_CLAUSE_START = re.compile(r"(?:\b(?:then|and)\b|[;:,])\s*$")


_COORD = re.compile(r"\b(?:and|or)\b", re.I)


def strip_conflicting_activation(text, walkers, *, turn_step=False):
    """Remove only the clause that repeats one walker's activation in the same turn.

    Returns ``(new_text, changed)``. The first activation and all surrounding
    strategy survive. Two situations come back unchanged so nothing is eaten:
    the second activation has no connector or punctuation to cut at, or its
    clause continues past a coordinating conjunction ("...sacrifice it and
    attack for lethal"), where ability payload and independent strategy cannot
    be told apart — the caller keeps the original text and records the problem.
    """
    source = _single_named_source(text, walkers)
    if source is None:
        return text, False
    prose = text.replace("−", "-")
    result = prose
    for _ in range(8):
        pair = _offending_pair(result, source, turn_step=turn_step)
        if pair is None:
            return result.strip(), result != prose
        first, second = pair
        if second.start() - first.end() <= 2:
            # Tight shorthand such as "+1/-2": both costs form one clause.
            start = first.start()
        else:
            window = result[max(0, second.start() - 16) : second.start()]
            boundary = _CLAUSE_START.search(window)
            if not boundary:
                return prose, False
            start = max(0, second.start() - 16) + boundary.start()
        end_match = _CLAUSE_END.search(result, second.end())
        end = end_match.start() if end_match else len(result)
        if end <= start:
            return prose, False
        if _COORD.search(result[second.end() : end]):
            # The clause runs on past a coordinating conjunction: what follows
            # may be independent strategy ("...and attack for lethal") or more
            # ability payload ("...4 damage and 4 cards"). No safe cut point.
            return prose, False
        result = (result[:start] + result[end:]).strip()
        result = re.sub(r"\s{2,}", " ", result)
        result = re.sub(r"\s+([;,:])", r"\1", result)
        result = re.sub(r"([;,:])(?:\s*[;,:])+", r"\1", result)
        result = result.strip().rstrip(" ,;:")
        if not result:
            return "", True
    return prose, False


def additional_activation(cards):
    return any(_EXTRA.search(str(c.get("oracle_text") or "")) for c in cards if not c.get("is_phased_out"))
