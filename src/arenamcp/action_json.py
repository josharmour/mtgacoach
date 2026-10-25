"""Conservative recovery of completed action data before broken trailing prose."""

import json
import re


def recover_action_json(text: str) -> dict | None:
    """Recover only complete actions or a complete pick/numeric choice.

    Never invent a value, repair card names, or turn a partial combat assignment
    into 'no blocks'. The ordinary planner validation still checks recovered
    actions against the current request before they can execute.
    """
    stack: list[str] = []
    quoted = escaped = False
    boundaries: list[tuple[int, tuple[str, ...], bool]] = []
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                break
            stack.pop()
            boundaries.append((i + 1, tuple(stack), False))
        elif char == ",":
            boundaries.append((i, tuple(stack), True))

    for end, closers, at_comma in reversed(boundaries):
        if closers not in ((), ("}",), ("}", "]"), ("}", "]", "}")):
            continue
        # If an action itself is unfinished, only discard trailing narration.
        inside_action = closers == ("}", "]", "}") or closers == ("}",)
        if inside_action and at_comma:
            suffix = text[end:].lstrip(", \r\n\t")
            narration = ("reasoning", "reason", "voice_advice", "overall_strategy")
            starts_prose = re.match(r'"(?:reasoning|reason|voice_advice|overall_strategy)"\s*:', suffix)
            partial_key = suffix.startswith('"') and any(
                ('"' + k + '"').startswith(suffix) for k in narration
            )
            if not (starts_prose or partial_key):
                continue
        try:
            obj = json.loads(text[:end] + "".join(reversed(closers)))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        actions = obj.get("actions", [obj])
        if not isinstance(actions, list) or not actions or not all(isinstance(a, dict) for a in actions):
            continue
        if (inside_action and not isinstance(obj.get("actions"), list)) or closers == ("}", "]", "}"):
            last = actions[-1]
            # Partial payloads of other action types are deliberately rejected.
            numeric = last.get("action_type") == "numeric_input" and type(last.get("numeric_value")) is int
            pick = type(last.get("pick")) is int
            if not (numeric or pick):
                continue
        if not all("action_type" in a or type(a.get("pick")) is int for a in actions):
            continue
        return obj
    return None
