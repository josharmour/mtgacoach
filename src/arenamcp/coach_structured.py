"""Structured coach answers: the model picks a numbered legal action + a spoken line.

Free-text advice had to be matched back to a legal action by regex, and when
the match failed the sanitizer substituted a guessed action — ~13% of advice
since 2026-09-01, sometimes the opposite of what the model said. Asking for
``{"action": N, "say": "..."}`` over a numbered CHOICES list makes the action
legal by construction; anything unparseable falls back to the old free-text
path, so this can only remove replacements, never add failures.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Choices that carry no decision for the player — numbering them just invites
# the model to "pick" waiting.
_NON_DECISIONS = re.compile(r"(?i)^\s*wait\b")

STRUCTURED_FORMAT_INSTRUCTION = (
    "Reply with ONLY one JSON object, no other text: "
    '{"action": <number from CHOICES>, "say": "<your spoken advice>"}. '
    'Use "action": 0 when your advice is not one of the CHOICES yet '
    "(for example an attack or block plan for later this turn). "
    'The "say" text follows every style rule above and must name the play.'
)


@dataclass(frozen=True)
class StructuredAdvice:
    index: int  # 1-based choice number; 0 = advice not tied to a current choice
    action: str | None  # the verified legal action string, None when index == 0
    say: str


def structured_advice_enabled() -> bool:
    env = os.environ.get("MTGACOACH_STRUCTURED_ADVICE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        from arenamcp.settings import get_settings

        return bool(get_settings().get("structured_advice", True))
    except Exception:
        return True


def build_choices(game_state: dict[str, Any]) -> list[str]:
    """Numbered choices = the exact list the legality sanitizer validates against."""
    if game_state.get("pending_decision") in ("Mulligan", "Mulligan Bottom"):
        return []
    try:
        from arenamcp.rules_engine import RulesEngine

        actions = [str(a) for a in (RulesEngine.get_legal_actions(game_state) or [])]
    except Exception as e:
        logger.debug(f"structured choices unavailable: {e}")
        return []
    actions = [a for a in actions if a.strip() and not _NON_DECISIONS.match(a)]
    return actions if len(actions) >= 1 else []


def format_choices(choices: list[str]) -> str:
    lines = [f"{i}. {a}" for i, a in enumerate(choices, 1)]
    return "CHOICES (legal right now):\n" + "\n".join(lines) + "\n\n" + STRUCTURED_FORMAT_INSTRUCTION


_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_SAY_RE = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)', re.DOTALL)
_ACTION_RE = re.compile(r'"action"\s*:\s*"?(\d+)')


def parse_structured_advice(text: str, choices: list[str]) -> StructuredAdvice | None:
    """Parse a model reply; None when it isn't a usable structured answer."""
    if not text or not choices:
        return None
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.IGNORECASE).strip()
    data: dict[str, Any] | None = None
    m = _OBJ_RE.search(body)
    if m:
        try:
            loaded = json.loads(m.group(0))
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, TypeError):
            data = None
    if data is None:
        # Truncated / slightly malformed JSON: salvage the fields by regex.
        say_m = _SAY_RE.search(body)
        act_m = _ACTION_RE.search(body)
        if not say_m:
            return None
        try:
            say = json.loads(f'"{say_m.group(1)}"')
        except ValueError:
            say = say_m.group(1)
        data = {"say": say, "action": int(act_m.group(1)) if act_m else 0}

    say = str(data.get("say") or "").strip()
    try:
        index = int(data.get("action") or 0)
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index > len(choices):
        index = 0
    action = choices[index - 1] if index else None
    if not say and not action:
        return None
    if not say and action:
        say = re.sub(r"\s*\[[^\]]+\]", "", action).strip() + "."
    return StructuredAdvice(index=index, action=action, say=say)


def is_verified(choice: StructuredAdvice, game_state: dict[str, Any]) -> bool:
    """A picked choice counts as verified unless it's a cast GRE couldn't pay for.

    In GRE's list an untagged "Cast X" means neither autotap nor the rules
    engine found a payment; leave that case to the existing sanitizer. The
    locally computed list (no GRE) only contains affordable casts and never
    carries tags, so untagged is fine there.
    """
    if not choice.action:
        return False
    low = choice.action.lower()
    from_gre = bool(game_state.get("legal_actions"))
    if from_gre and low.startswith("cast ") and "[ok]" not in low:
        return False
    return True
