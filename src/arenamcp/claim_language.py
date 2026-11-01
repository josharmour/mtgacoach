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
