"""Shared boundaries between hypothetical language and asserted rules facts."""

import re

_CONDITION = r"\b(?:if|unless|when|whenever|once|after|until|while|assuming|suppose|provided)\b"
_STATEMENT_BREAK = r"[.!?;|\n]|\b(?:but|however|yet)\b"


def conditional_claim(text: str, subject_start: int, claim_end: int) -> bool:
    """A hypothetical keyword is not an assertion about the current board.

    Keep the scope local: "If X gains reach" and "X has reach only if ..."
    abstain, but a separate assertion after a sentence/plan boundary or "but"
    is still checked. A subsequent "so if we attack" does not qualify X's
    asserted keyword. Do not reuse the broader combat-condition vocabulary:
    "has reach because ..." is an assertion, not a hypothetical.
    """
    before = re.split(_STATEMENT_BREAK, text[:subject_start])[-1]
    if re.search(_CONDITION, before):
        return True
    after = re.split(_STATEMENT_BREAK, text[claim_end:])[0]
    return bool(re.match(r"\s*,?\s*(?:only\s+)?" + _CONDITION, after))


# Contraction-aware (review round 7): in "doesn't" the n is preceded by the
# word char "s", so a leading \b can never anchor before "n't" — the cue must
# match as a contraction SUFFIX. \wn['’]t requires a preceding letter, so it
# only fires inside a genuine contraction (doesn't/can't/won't/isn't),
# including the typographic apostrophe. Adding cues only widens denial
# detection, which is the abstain (safe) direction.
_NEGATION_CUE = re.compile(r"\b(?:not|never|without|neither|nor|no)\b|\wn['’]t\b")


def negated_claim(text: str, subject_start: int, claim_end: int) -> bool:
    """A denied effect is not an assertion that it happens.

    Conservative counterpart to conditional_claim: when the clause carrying
    the claim denies the effect ("does not immediately put", "puts no
    counters", "doesn't immediately put") — or the denial scope is
    ambiguous — there is no positive
    assertion left to refute, so the caller must abstain. The cue must sit
    in the same clause as the claim (statement and contrastive breaks reset
    scope), so "does not draw; it puts a counter" still asserts the counter.
    A cue governing some other word in the clause ("no doubt scaling it")
    also abstains — a missed detection is the safe direction.
    """
    if claim_end <= subject_start:
        return False
    clause_start = subject_start
    for match in re.finditer(_STATEMENT_BREAK, text[:claim_end]):
        if match.end() > subject_start:
            clause_start = match.end()
            break
    return bool(_NEGATION_CUE.search(text[clause_start:claim_end]))
