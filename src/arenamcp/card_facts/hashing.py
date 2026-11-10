"""Normalised hashing for card-facts records.

``rules_hash`` identifies the *printed* content of a card so that records can be
stored one per hash and so that a client can tell whether its cached record is
still about the same printing of the card.

The hash is a SHA-256 over four inputs joined with ``\\n``:

1. the card name,
2. its mana cost,
3. its type line,
4. its rules text (all faces concatenated).

Each input is normalised before hashing so that trivially different byte strings
produce the same hash:

Normalisation recipe (reproduce exactly when porting to another language):

1. Apply Unicode normalisation form NFC.
2. Case-fold the result (Python ``str.casefold``, which is stronger than
   ``lower`` for some alphabets).
3. Replace every run of whitespace characters (spaces, tabs, newlines) with a
   single ASCII space.
4. Strip leading and trailing whitespace.

Nothing else is changed: mana-cost braces ({2}{R}), punctuation and dashes are
hashed exactly as supplied. Callers must therefore pass the text exactly as the
card source prints it (for example Scryfall's ``name``, ``mana_cost``,
``type_line`` and ``oracle_text`` fields).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Normalise one hash input per the recipe in this module's docstring."""
    folded = unicodedata.normalize("NFC", text).casefold()
    return _WHITESPACE_RUN.sub(" ", folded).strip()


def rules_hash(
    name: str,
    mana_cost: str = "",
    type_line: str = "",
    rules_text: str = "",
) -> str:
    """SHA-256 over the normalised name, mana cost, type line and rules text."""
    parts = [
        normalise(name),
        normalise(mana_cost),
        normalise(type_line),
        normalise(rules_text),
    ]
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
