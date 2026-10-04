"""Mode-aware voice framing without another model request.

As in ArenaOnAir's booth, distinguish a submitted play from its resolution.
Only validated action descriptions are rewritten; card names and observations
remain intact. A planned action is never announced as an observed outcome.
"""

from __future__ import annotations

import re


def narration_policy(mode: str) -> str:
    if mode != "autopilot":
        return (
            "VOICE ROLE — ADVISOR: the human is playing. Address the player as you/your "
            "and give clear recommendations. Do not claim you are controlling the game."
        )
    return (
        "VOICE ROLE — AUTOPILOT: you drive the local player's actions and the human watches. "
        "Use first-person possessives for the local player's resources ('my hand', 'my creatures'). "
        "Group one combat declaration into one sentence, combining creatures by target. "
        "Say 'Attacking with…', 'Blocking with…', or 'Casting…', without an 'I'm' prefix. "
        "Describe intended plays or give concise sportscaster commentary on observed events. "
        "Use first-person possessives to identify your resources; omit repetitive first-person action prefixes. Do not "
        "instruct the human to play, attack, target, or click. A recommendation is still a plan, "
        "not an executed action. An accepted action submission is not proof of resolution; "
        "a cast is not a resolved spell, an attack is not damage, and a selected tutor card is "
        "not yet in hand. Describe results only when current state/events confirm them; frame "
        "future benefits conditionally. Explain one relevant strategic consequence, without "
        "repeating the full plan or routine priority passes. Keep internal IDs, protocol labels, "
        "and analysis notes out of speech. This voice role overrides imperative coaching examples."
    )


_VERBS = {
    "cast": "casting",
    "play": "playing",
    "activate": "activating",
    "attack": "attacking",
    "block": "blocking",
    "target": "targeting",
    "choose": "choosing",
    "select": "selecting",
    "pass": "passing",
    "keep": "keeping",
    "confirm": "confirming",
    "return": "returning",
    "pay": "paying",
    "tap": "tapping",
    "sacrifice": "sacrificing",
    "discard": "discarding",
    "exile": "exiling",
    "reveal": "revealing",
    "search": "searching",
    "find": "finding",
    "scry": "scrying",
    "order": "ordering",
    "click": "clicking",
    "wait": "waiting",
    "hold": "holding",
    "put": "putting",
    "let": "letting",
    "add": "adding",
    "hover": "hovering",
    "move": "moving",
    "scroll": "scrolling",
    "press": "pressing",
    "double-click": "double-clicking",
}
_ACTION_START = re.compile(
    r"(^|(?<=[.;!?])\s+)(don't\s+attack|do not\s+attack|don't\s+block|do not\s+block|"
    r"mulligan|resolve|" + "|".join(_VERBS) + r")\b",
    re.IGNORECASE,
)


def spoken_name(label: str) -> str:
    """Remove execution IDs from speech while retaining token/copy distinctions."""
    token = label.lstrip().startswith("*")
    name = re.sub(r"\s*\[(?:id:)?\d+\]\s*$", "", label, flags=re.I).lstrip("*").strip()
    if name.casefold() == "opponent":
        return "the opponent"
    return f"{name} token" if token else name


def spoken_list(names: list[str]) -> str:
    if len(names) < 2:
        return "".join(names)
    if len(names) == 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def combat_declaration(verb: str, assignments: dict[str, str]) -> str:
    """Speak one declaration, grouping creatures without changing target IDs."""
    by_target: dict[str, list[str]] = {}
    for creature, target in assignments.items():
        by_target.setdefault(target, []).append(spoken_name(creature))
    preposition = "at" if verb.casefold() == "attack" else "against"
    clauses = [
        f"{spoken_list(creatures)} {preposition} {spoken_name(target)}"
        for target, creatures in by_target.items()
    ]
    if len(clauses) < 2:
        return f"{verb} with {''.join(clauses)}"
    return f"{verb} with " + ", ".join(clauses[:-1]) + ", and " + clauses[-1]


def _driver_possessives(text: str) -> str:
    # Lowercase prose only: do not rewrite card titles such as Your Temple
    # Is Under Attack. Sentence-initial Your is prose before a lowercase noun.
    text = re.sub(r"\byours\b", "mine", text)
    text = re.sub(r"\byour\b", "my", text)
    return re.sub(r"(^|[.!?]\s+)Your(?=\s+[a-z])", r"\1My", text)


def action_narration(text: str, *, planned: bool = False) -> str:
    """Render validated action labels as driver intent or ongoing submission.

    This is not a free-form paraphraser: only verbs at sentence/clause starts
    change. Observations, card names, and target assignments stay intact. Use ``planned=True`` before submission.
    """

    def rewrite(match: re.Match[str]) -> str:
        boundary, verb = match.groups()
        lower = " ".join(verb.lower().split())
        if lower in ("don't attack", "do not attack", "don't block", "do not block"):
            action = "attack" if lower.endswith("attack") else "block"
            phrase = f"Not {_VERBS[action]}"
        elif lower == "mulligan":
            phrase = "Taking a mulligan"
        elif lower == "resolve":
            # Arena's Resolve action passes priority; it cannot certify the
            # spell's eventual resolution or outcome.
            phrase = "Passing priority"
        else:
            phrase = _VERBS[lower].capitalize()
        return boundary + phrase

    text, planned_prefixes = re.subn(
        rf"(^|(?<=[.;!?])\s+)I(?: plan to| will|['’]ll)\s+({'|'.join(_VERBS)})\b",
        lambda match: match[1] + match[2].capitalize(),
        text,
        flags=re.I,
    )
    planned = planned or bool(planned_prefixes)
    # Models may already supply the old driver prefix. Only remove it before
    # recognized ongoing action verbs, not arbitrary observations or card text.
    ongoing = "|".join(_VERBS.values())
    text = re.sub(
        rf"(^|(?<=[.;!?])\s+)I(?:['’]m| am)\s+((?:not\s+)?(?:{ongoing}))\b",
        lambda match: match[1] + match[2].capitalize(),
        text,
        flags=re.I,
    )
    # Legacy/native descriptions may still repeat the same combat verb across
    # semicolon clauses. One declaration gets one action verb.
    clauses = re.split(r"\s*;\s*", text)
    if len(clauses) > 1:
        first = re.match(r"^(Attack|Block)\s+", clauses[0], re.I)
        if first and all(re.match(rf"^{first[1]}\s+", clause, re.I) for clause in clauses):
            parts = [
                re.sub(rf"^{first[1]}\s+", "", clause, flags=re.I).removesuffix(".") for clause in clauses
            ]
            text = first[1] + " " + ", ".join(parts[:-1]) + ", and " + parts[-1] + "."
    rendered = _driver_possessives(_ACTION_START.sub(rewrite, text))
    if planned and (_ACTION_START.match(text) or re.match(rf"^({ongoing})\b", text, re.I)):
        return "Plan: " + rendered[0].lower() + rendered[1:]
    return rendered


def submission_narration(action_text: str, reasoning: str = "") -> str:
    """An accepted action plus its existing rationale, never a second LLM call."""
    result = action_narration(action_text)
    if reasoning:
        # A rationale starting with an instruction describes a purpose, not
        # a new unsubmitted action for the listener to perform.
        reasoning = action_narration(reasoning, planned=True)
        result += " " + _driver_possessives(reasoning)
    return result
