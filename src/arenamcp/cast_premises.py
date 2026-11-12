"""Deterministic false-premise concerns for a chosen cast (Fable Nov 2026-11-12).

Two recorded autoplay decisions rested on impossible premises about the card
being cast (evidence /home/joshu/fable-nov-evidence/autoplay-false-premise-
20261112/decisions.json):

- a summoning-sick creature cast now was reasoned about as attacking this turn
  (Skyward Spider T8, Spellbook Vendor T12). The existing validator
  (game_plan.decision_claim_notes) annotates that claim after commitment but
  the selected action stood;
- a creature with no enters-the-battlefield or when-you-cast ability was
  reasoned about as producing an immediate effect on entry (Optimistic
  Scavenger T16: "casting Optimistic Scavenger first adds an Eerie +1/+1
  counter to Veteran Survivor"). Its Eerie trigger waits for an enchantment
  entry or a Room unlock, not its own.

These concerns surface such claims BEFORE the pick is final so the second
look can re-evaluate the choice itself instead of rewriting rationale text.
They are deliberately narrow: only the card being cast is examined, only
assertions tied to casting it count, and unknown oracle text conservatively
produces no concern.
"""

import re

from arenamcp.claim_language import conditional_claim
from arenamcp.game_plan import _is_creature_card
from arenamcp.play_safety import find_source

_HTML_TAG = re.compile(r"<[^>]+>")
_APOSTROPHE = str.maketrans({"’": "'", "‘": "'"})

# An asserted effect tied to casting/entering now. Either an explicit
# immediacy marker, or an effect verb landing on a concrete game object.
_IMMEDIATE_EFFECT = re.compile(
    r"\b(?:immediately|right away|at once|as it enters|upon entering|on entry"
    r"|first (?:adds?|puts?|places?|gives?))\b"
    r"|\b(?:adds?|puts?|places?|gives?|creates?|makes?|draws?|deals?"
    r"|destroys?|exiles?|returns?)\b[^.;]{0,40}?\b(?:counters?|tokens?"
    r"|cards?|damage)\b"
)

# Explicit future/conditional timing is not an immediate-on-cast claim:
# "at the beginning of combat it creates a Role token" is a scheduled trigger,
# not a premise that casting produces the effect now.
_FUTURE_TIMING = re.compile(
    r"\b(?:at the beginning of|beginning of (?:combat|your|the)|next turn"
    r"|the following turn|later(?: this turn)?|on your next|end step"
    r"|end of turn|upkeep|draw step|combat begins|will)\b"
)


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n", text) if part.strip()]


def _own_entry_ability(oracle: str, name: str) -> bool:
    """True when the card itself has an ability that fires on its own entry or cast.

    Unknown oracle text conservatively counts as capable (no concern).
    """
    if not oracle:
        return True
    text = _HTML_TAG.sub("", oracle).translate(_APOSTROPHE)
    esc = re.escape(name.casefold())
    for chunk in re.split(r"[.\n]", text):
        c = chunk.strip().casefold()
        if not c:
            continue
        if re.search(r"\bwhen(?:ever)? you cast\b", c):
            return True
        if re.search(
            rf"\b(?:as|when|whenever)\b[^.;]{{0,80}}?\b(?:this creature"
            rf"|this permanent|{esc}|it)\s+enters\b",
            c,
        ):
            return True
        if re.search(r"\benters (?:the battlefield )?with\b", c) and re.search(
            rf"\b(?:this creature|this permanent|{esc})\b|^as\b|^it\b", c
        ):
            return True
    return False


def _entry_effect_claims(name: str, reasoning: str) -> list[str]:
    """Sentences asserting that casting ``name`` itself produces an effect now."""
    esc = re.escape(name.casefold())
    claims: list[str] = []
    for sentence in _sentences(reasoning.translate(_APOSTROPHE)):
        low = sentence.casefold()
        if not re.search(rf"(?<![a-z0-9]){esc}(?![a-z0-9])", low):
            continue
        linked = (
            re.search(rf"\bcast(?:ing|s|ed)?\b[^.;]{{0,60}}\b{esc}\b", low)
            or re.search(
                rf"\b{esc}\b[^.;]{{0,60}}\b(?:hits? the battlefield"
                rf"|enters(?: the battlefield)?)\b",
                low,
            )
            or re.match(rf"^\s*cast(?:ing)?\s+(?:out\s+)?(?:the\s+)?{esc}\b", low)
        )
        if not linked:
            continue
        effect = _IMMEDIATE_EFFECT.search(low)
        if not effect:
            continue
        if _FUTURE_TIMING.search(low):
            continue
        # A conditional framing ("when an enchantment enters it puts a counter")
        # asserts a scheduled trigger, not an immediate-on-cast effect.
        if conditional_claim(low, effect.start(), effect.end()):
            continue
        claims.append(sentence)
    return claims


def cast_premise_concerns(decision, game_state: dict, chosen: list[str], reasoning) -> list[str]:
    """Impossible-premise concerns about the casts in ``chosen``.

    Returns human-readable concern sentences; empty when nothing impossible is
    asserted. Never mutates state and never invents a replacement choice.
    """
    if not isinstance(game_state, dict) or not chosen:
        return []
    text = reasoning if isinstance(reasoning, str) else ""
    if not text:
        return []
    concerns: list[str] = []
    from arenamcp.game_plan import decision_claim_notes

    for oid in chosen:
        option = decision.find(oid) if decision is not None else None
        meta = (getattr(option, "meta", None) or {}) if option is not None else {}
        if meta.get("actionType") != "ActionType_Cast":
            continue
        card = find_source(game_state, meta)
        if not card or not _is_creature_card(card):
            continue
        name = str(card.get("name") or "")
        if not name:
            continue
        instance_id = card.get("instance_id")
        cast_ids = {instance_id} if instance_id is not None else set()

        # Premise 1: a summoning-sick creature cast now attacking/tapping this turn.
        for note in decision_claim_notes(text, game_state, cast_ids=cast_ids):
            folded = note.casefold()
            if name.casefold() in folded and "summoning sick" in folded:
                concern = (
                    f"{note}. Re-evaluate the play without counting on {name} attacking or tapping this turn."
                )
                if concern not in concerns:
                    concerns.append(concern)

        # Premise 2: casting a creature with no entry/cast ability producing an effect.
        oracle = str(card.get("oracle_text") or "")
        if _own_entry_ability(oracle, name):
            continue
        for claim in _entry_effect_claims(name, text):
            concern = (
                f'"{claim[:180]}" — casting {name} does not produce that effect '
                f"by itself: {name} has no enters-the-battlefield or "
                f"when-you-cast ability. Do not count on an immediate effect "
                f"from casting {name}."
            )
            if concern not in concerns:
                concerns.append(concern)
    return concerns
