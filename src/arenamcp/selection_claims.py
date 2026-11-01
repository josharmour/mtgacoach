"""Facts about who receives a selected card, distinct from who selects it."""

from __future__ import annotations

import re
from typing import Any

_OPPONENT_RETURN = re.compile(
    r"choose a (?:nonlegendary )?creature card for your opponent to return to the battlefield[.!]?",
    re.IGNORECASE,
)


def opponent_return_note(decision: Any) -> str:
    """Ground Drana's recipient in the current Arena prompt (ledger #344).

    A graveyard object's owner or last controller does not establish who will
    control it after a zone change. Nor does being the player making the choice.
    Do not infer a recipient from those fields, a stale context, or merely
    'from your opponent's graveyard'. Unknown prompts remain unknown.
    """
    if getattr(decision, "request_type", "") != "SelectN":
        return ""
    prompt = " ".join(str(getattr(decision, "prompt_text", "") or "").split())
    if not _OPPONENT_RETURN.fullmatch(prompt):
        return ""
    return (
        "RETURN RECIPIENT: the selected creature returns under the OPPONENT's control. "
        "You choose the card, but it will be their creature, not your blocker or attacker; "
        "its lifelink would gain life for them. Ownership and its previous controller "
        "do not change who receives it from this effect. Compare what each option gives the opponent."
    )


# Narrow, affirmative local-benefit claims. In particular #344's 'blocks and
# trades with their attackers' attributes the selected creature to our defense.
_LOCAL_BENEFIT = re.compile(
    r"\b(?:is|becomes?|as|joins?)\s+(?:one of\s+)?(?:our|my|your)\s+"
    r"(?:new\s+)?(?:creature|blocker|attacker|defen[cs]e|board)\b"
    r"|\b(?:we|i|you)\s+(?:get|gain|receive)\b"
    r"|\b(?:blocks?|trades?|blocking|trading)\b[^.!?]*\btheir\s+attackers\b"
    r"|\b(?:gaining|gain|gains)\s+(?:us\s+)?life\s+to\s+survive\b",
    re.IGNORECASE,
)
_UNCERTAIN_OR_NEGATED = re.compile(
    r"\b(?:if|unless|would|could|not|never|no|can't|cannot|doesn't|don't|won't)\b", re.IGNORECASE
)


def correct_return_claims(text: str, decision: Any, chosen: list[str]) -> tuple[str, list[str]]:
    """Remove a verified ownership reversal without judging the selected card.

    Only the current selected option supplies identity; a same-named battlefield
    object or an old graveyard instance cannot leak into a later decision.
    Independent reasons (e.g. weaker than another option) remain intact.
    """
    if not opponent_return_note(decision):
        return text, []
    selected = [option for option in decision.options if option.option_id in chosen]
    if not selected:
        return text, []
    names = [str((option.meta.get("card") or {}).get("name") or option.label) for option in selected]
    kept = []
    rejected = False
    for sentence in re.split(r"(?<=[.!?])\s+", str(text or "")):
        refers_to_pick = any(name and name.casefold() in sentence.casefold() for name in names) or bool(
            re.search(r"\b(?:selected|chosen) creature\b", sentence, re.IGNORECASE)
        )
        if refers_to_pick and _LOCAL_BENEFIT.search(sentence) and not _UNCERTAIN_OR_NEGATED.search(sentence):
            rejected = True
        else:
            kept.append(sentence)
    if not rejected:
        return text, []
    fact = (
        f"{', '.join(names)} returns under the opponent's control, not ours; "
        "it does not become our blocker, and any lifelink life gain goes to its controller"
    )
    kept.append("Rules correction: " + fact + ".")
    return " ".join(kept), [fact]
