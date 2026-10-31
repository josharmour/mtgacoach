"""Plain-language plan steps shared by the compact plan and search preview."""

from __future__ import annotations


def plan_step_text(step: object) -> str:
    if isinstance(step, str):
        return step.strip()
    if not isinstance(step, dict):
        return ""
    kind = str(step.get("action_type") or step.get("type") or "").lower()
    verbs = {
        "play_land": "Play",
        "cast_spell": "Cast",
        "cast": "Cast",
        "activate_ability": "Activate",
        "attack": "Attack with",
        "declare_attackers": "Attack with",
        "block": "Block with",
        "declare_blockers": "Block with",
        "pass_priority": "Pass priority",
        "pass": "Pass priority",
        "keep_hand": "Keep hand",
        "mulligan": "Mulligan",
    }
    card = str(step.get("card_name") or "").strip()
    verb = verbs.get(kind, kind.replace("_", " ").capitalize())
    text = " ".join(part for part in (verb, card) if part)
    targets = step.get("target_names")
    if isinstance(targets, list):
        names = [name.strip() for name in targets if isinstance(name, str) and name.strip()]
        if names:
            text += " → " + ", ".join(names)
    if not text:
        return str(step.get("description") or "").strip()
    return text


def plan_steps_text(steps: object, limit: int = 4) -> str:
    if not isinstance(steps, (list, tuple)):
        return plan_step_text(steps)
    return " → ".join(text for step in steps[:limit] if (text := plan_step_text(step)))
