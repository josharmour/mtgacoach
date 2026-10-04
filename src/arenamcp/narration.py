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
        "Explain your own intended next play in first person ('I plan to cast…', 'I'm holding "
        "removal for…'), or give concise sportscaster commentary on observed events. Do not "
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


def action_narration(text: str, *, planned: bool = False) -> str:
    """Render validated action labels as driver intent or ongoing submission.

    This is not a free-form paraphraser: only verbs at sentence/clause starts
    change. Observations, existing first-person language, card names, and
    target assignments stay intact. Use ``planned=True`` before submission.
    """

    def rewrite(match: re.Match[str]) -> str:
        boundary, verb = match.groups()
        lower = " ".join(verb.lower().split())
        if lower in ("don't attack", "do not attack", "don't block", "do not block"):
            action = "attack" if lower.endswith("attack") else "block"
            phrase = f"I plan not to {action}" if planned else f"I'm not {_VERBS[action]}"
        elif lower == "mulligan":
            phrase = "I plan to mulligan" if planned else "I'm taking a mulligan"
        elif lower == "resolve":
            # Arena's Resolve action passes priority; it cannot certify the
            # spell's eventual resolution or outcome.
            phrase = "I plan to pass priority" if planned else "I'm passing priority"
        else:
            phrase = f"I plan to {lower}" if planned else f"I'm {_VERBS[lower]}"
        return boundary + phrase

    return _ACTION_START.sub(rewrite, text)


def submission_narration(action_text: str, reasoning: str = "") -> str:
    """An accepted action plus its existing rationale, never a second LLM call."""
    result = action_narration(action_text)
    if reasoning:
        # A rationale starting with an instruction describes a purpose, not
        # a new unsubmitted action for the listener to perform.
        if _ACTION_START.match(reasoning):
            reasoning = action_narration(reasoning, planned=True)
        result += " " + reasoning
    return result
