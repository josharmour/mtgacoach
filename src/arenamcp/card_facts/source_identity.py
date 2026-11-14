"""Shared conservative admission for the existing single-face facts schema.

No dialect translation or name-based aliasing. Unsupported inputs have no
executable hash; this deliberately does not invent a v2 multiface schema.
"""

from __future__ import annotations

import re
from typing import Any

from arenamcp.card_facts.hashing import rules_hash


def _get(card: Any, key: str, default: Any = None) -> Any:
    return card.get(key, default) if isinstance(card, dict) else getattr(card, key, default)


def executable_source_hash(card: Any) -> str | None:
    """Legacy hash unchanged, only when complete single-face input is supported."""
    for key in ("card_faces", "related_faces", "linked_faces", "linked_face_ids"):
        value = _get(card, key, None)
        if value is not None and (type(value) is not list or value):
            return None
    name = _get(card, "name")
    fields = [name, *(_get(card, key) for key in ("mana_cost", "type_line", "oracle_text"))]
    if any(type(value) is not str for value in fields):
        return None
    if not name.strip() or name.startswith("Unknown(") or "//" in name:
        return None
    # Joined faces and native presentation syntax must never masquerade as
    # canonical single-face Oracle content. Preserve input, do not strip it.
    for value in fields:
        if "<" in value or ">" in value or "\n---\n" in value:
            return None
        tokens = re.findall(r"\{([^{}]*)\}", value)
        remainder = re.sub(r"\{[^{}]*\}", "", value)
        if "{" in remainder or "}" in remainder:
            return None
        for token in tokens:
            if not re.fullmatch(
                r"(?:[0-9]+|[WUBRGCSXYZTQEP]|CHAOS|[WUBRG2]/[WUBRG]|[WUBRG]/P|[WUBRG]/[WUBRG]/P)", token
            ):
                return None
    return rules_hash(*fields)
