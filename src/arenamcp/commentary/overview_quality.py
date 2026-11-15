"""Conservative script hygiene for saved English deck-overview speech.

This is not a language detector or factual/strategic audit. Reject the entire
summary rather than silently translating, deleting characters or modifying the
saved playbook. Latin-script card names and ordinary typography remain intact.
"""

import re
import unicodedata

PLACEHOLDER = re.compile(
    r"^\W*$|^\(.*\)\.?$|\b(omitted|placeholder|see above|to be (?:written|added)|n/?a|tbd|lorem ipsum)\b",
    re.I,
)


def speakable_overview(value: object) -> bool:
    if type(value) is not str or not value.strip() or len(value) > 500:
        return False
    # Incomplete suffixes, model field labels and explicit placeholders aren't speech.
    if not re.search(r"""[.!?]["”’')\]]*$""", value.strip()) or re.search(
        r"^\W*(?:spoken summary|summary omitted)\b|\b(?:placeholder|see above|tbd|lorem ipsum)\b", value, re.I
    ):
        return False
    if PLACEHOLDER.search(value):
        return False
    has_letter = False
    latin_base = False
    for char in value:
        category = unicodedata.category(char)
        if category.startswith("L"):
            if not unicodedata.name(char, "").startswith("LATIN "):
                return False
            has_letter = latin_base = True
        elif category.startswith("M"):
            # Preserve decomposed Latin accents, not arbitrary script marks.
            if not latin_base or not "\u0300" <= char <= "\u036f":
                return False
        elif category.startswith("C"):
            return False  # controls, bidi/zero-width formatting, surrogates
        elif char.isascii() or category.startswith("P") or category == "Zs" or char in "×÷−":
            latin_base = False
        else:
            return False
    return has_letter
