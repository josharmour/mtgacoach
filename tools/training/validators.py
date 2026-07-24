"""Pure Python Contract Validator Library for MTGA Coach models.

Validates model responses against ACTION_SCHEMA JSON syntax, decision keyword contracts,
and menu legality without calling LLMs.
"""

from __future__ import annotations

import json
import re
from typing import Any


def validate_action_schema_json(response: str) -> tuple[bool, str]:
    """Verify that response parses into valid ACTION_SCHEMA JSON."""
    if not response or not response.strip():
        return False, "Empty response"

    s = response.strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()

    s = re.sub(r",\s*([\]}])", r"\1", s)

    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"

    if not isinstance(data, dict):
        return False, "Root JSON is not an object"

    actions = data.get("actions")
    if not isinstance(actions, list) or len(actions) == 0:
        return False, "Missing or empty 'actions' list in JSON"

    for act in actions:
        if not isinstance(act, dict):
            return False, "Action item is not an object"
        if "pick" not in act and "action_type" not in act and "reasoning" not in act:
            return False, "Action item missing pick, action_type, or reasoning"

        if "pick" in act and not isinstance(act["pick"], int):
            return False, f"'pick' must be an integer, got {type(act['pick']).__name__}"

    return True, "Valid ACTION_SCHEMA JSON"


def validate_keyword_contract(prompt_user: str, response: str) -> tuple[bool, str]:
    """Verify keyword decision contracts (Mulligan -> KEEP|MULLIGAN, etc.)."""
    usr_upper = prompt_user.upper()
    resp_strip = response.strip()

    if "MULLIGAN" in usr_upper or "KEEP HAND" in usr_upper:
        if resp_strip not in ("KEEP", "MULLIGAN", "Keep", "Mulligan"):
            # Check if JSON pick
            is_valid_json, _ = validate_action_schema_json(response)
            if not is_valid_json:
                return False, f"Mulligan decision response must be KEEP/MULLIGAN or valid JSON, got: {response[:50]}"

    return True, "Keyword contract valid"


def validate_action_legality(prompt_user: str, response: str) -> tuple[bool, str]:
    """Verify that chosen card or pick integer exists in the prompt's Legal: menu."""
    is_valid_json, _ = validate_action_schema_json(response)
    if not is_valid_json:
        return True, "Skipped legality check for non-JSON response"

    try:
        data = json.loads(re.sub(r",\s*([\]}])", r"\1", response.strip()))
        actions = data.get("actions", [])
        if not actions:
            return True, "No actions to check"
        
        act = actions[0]
        pick = act.get("pick")
        card_name = act.get("card_name")

        if "Legal:" in prompt_user:
            legal_section = prompt_user.split("Legal:")[1].split("\n\n")[0]
            if pick is not None:
                pattern = rf"\[{pick}\]"
                if not re.search(pattern, legal_section):
                    return False, f"Pick [{pick}] does not exist in prompt Legal: menu"
            elif card_name:
                if card_name.lower() not in legal_section.lower():
                    return False, f"Card '{card_name}' not found in prompt Legal: menu"
    except Exception as e:
        return False, f"Legality check exception: {e}"

    return True, "Legal action verified"


def validate_all(prompt_system: str, prompt_user: str, response: str) -> tuple[bool, list[str]]:
    """Run all validators over a (system, user, response) triplet."""
    reasons = []
    
    ok_schema, msg_schema = validate_action_schema_json(response)
    if not ok_schema:
        reasons.append(msg_schema)

    ok_kw, msg_kw = validate_keyword_contract(prompt_user, response)
    if not ok_kw:
        reasons.append(msg_kw)

    ok_leg, msg_leg = validate_action_legality(prompt_user, response)
    if not ok_leg:
        reasons.append(msg_leg)

    is_valid = len(reasons) == 0
    return is_valid, reasons
