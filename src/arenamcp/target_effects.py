"""Conservative target polarity shared by planning and automatic submission."""

import re

_QUANTITY = (
    r"(?:(?:up to )?(?:one|two|three|four|five|six|seven|eight|nine|ten|x|\d+)|any number of|another) "
)
_HARMFUL = re.compile(
    rf"\b(?:destroy|exile|sacrifice|sacrifices|counter|return|fight|fights) (?:{_QUANTITY})?target\b"
    rf"|\bdamage to (?:{_QUANTITY})?target\b"
    r"|\bgets? [-−]|\bloses (?:all abilities|flying)\b"
    r"|\btarget [^.]*?(?:you don['’]t control|an opponent controls)"
)
_BENEFICIAL = re.compile(
    r"\b(?:target|enchanted|equipped) [^.]*?\bgets? \+(?:\d+|x)/\+(?:\d+|x)"
    r"|\b(?:target|enchanted|equipped) [^.]*?\b(?:gains?|has|have) "
    r"(?:hexproof|indestructible|flying|lifelink|vigilance|trample|haste|first strike|double strike|protection)\b"
    r"|\bput [^.]*?\+1/\+1 counters? on [^.]*?\btarget\b"
    r"|\battach [^.]*?\bto target creature you control\b"
)
_BLINK = re.compile(r"\bexile[^.]*?\breturn (?:it|them|that card|those cards)\b[^.]*?to the battlefield")
_ATTACH_KEYWORD = re.compile(r"^\s*(?:equip|reconfigure)\b[^\n]*$", re.IGNORECASE)


def source_effect_text(oracle: str, parent_oracle: str = "") -> str:
    """Rules text that decides target polarity for a request's source.

    Arena's equip ability object carries only its keyword line ("Equip {o0}"),
    which names no effect, so Lightning Greaves' equip read as unclassified
    and was cancelled (2026-10-04 22:50). By rule it attaches the parent to a
    creature you control; the parent's own text says what that grants.
    """
    if _ATTACH_KEYWORD.match(re.sub(r"<[^>]*>", "", oracle or "").strip()):
        return f"Attach to target creature you control.\n{parent_oracle or ''}".strip()
    return oracle or ""


def target_effect_is_harmful(oracle: str) -> bool | None:
    """True for harm, False for a known benefit, None for unknown/mixed effects."""
    text = re.sub(r"<[^>]*>", "", oracle.lower())
    if re.search(r"target [^.]*?you control[^.]*?(?:deals?|fights?)\b[^.]*?target", text):
        return None
    harmful = beneficial = False
    for sentence in re.split(r"[.\n]", oracle.lower()):
        if _BLINK.search(sentence):
            beneficial = True
            sentence = _BLINK.sub("", sentence)
        harmful = harmful or bool(_HARMFUL.search(sentence))
        beneficial = beneficial or bool(_BENEFICIAL.search(sentence))
    if harmful == beneficial:
        return None
    return harmful
