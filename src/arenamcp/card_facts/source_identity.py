"""Shared conservative admission for the existing single-face facts schema.

No dialect translation or name-based aliasing. Unsupported inputs have no
executable hash; this deliberately does not invent a v2 multiface schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from arenamcp.card_facts.hashing import rules_hash


def _get(card: Any, key: str, default: Any = None) -> Any:
    return card.get(key, default) if isinstance(card, Mapping) else getattr(card, key, default)


# These public layouts describe a single printed face. Native sources often
# supply no layout at all. An unrecognized explicit layout has no F1 contract.
_SINGLE_FACE_LAYOUTS = frozenset(
    {
        "normal",
        "saga",
        "class",
        "case",
        "leveler",
        "mutate",
        "prototype",
        "host",
        "augment",
        "planar",
        "scheme",
        "vanguard",
        "token",
        "emblem",
        "dungeon",
    }
)


def supported_layout(layout: Any) -> bool:
    return layout is None or (type(layout) is str and layout in _SINGLE_FACE_LAYOUTS)


def single_face_source(card: Any) -> bool:
    if _get(card, "single_face_source", True) is not True or not supported_layout(_get(card, "layout")):
        return False
    for key in ("card_faces", "related_faces", "linked_faces", "linked_face_ids"):
        value = _get(card, key, None)
        if value is not None and (type(value) is not list or value):
            return False
    return True


def executable_source_hash(card: Any, *, legacy_lookup: bool = False) -> str | None:
    """Legacy hash unchanged, only when complete single-face input is supported."""
    if not single_face_source(card):
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
        if "\n---\n" in value:
            return None
        if "<" in value or ">" in value:
            if not legacy_lookup or not _legacy_presentation_valid(value):
                return None
        tokens = re.findall(r"\{([^{}]*)\}", value)
        remainder = re.sub(r"\{[^{}]*\}", "", value)
        if "{" in remainder or "}" in remainder:
            return None
        for token in tokens:
            canonical = re.fullmatch(
                r"(?:[0-9]+|[WUBRGCSXYZTQEP]|CHAOS|[WUBRG2]/[WUBRG]|[WUBRG]/P|[WUBRG]/[WUBRG]/P)", token
            )
            native = legacy_lookup and re.fullmatch(
                r"(?:(?:o(?:[0-9]+|[WUBRGCSXYZTQEP]))+|o[WUBRG2]/o[WUBRGP](?:/oP)?)", token
            )
            if not canonical and not native:
                return None
    return rules_hash(*fields)


def _legacy_presentation_valid(text: str) -> bool:
    """Validate known rendering syntax without changing any hashed bytes."""
    stack = []
    for part in re.split(r"(<[^>]*>)", text):
        if part in ("<i>", "<nobr>", "<indent=4%>"):
            stack.append(part.split("=")[0][1:].rstrip(">"))
        elif part in ("</i>", "</nobr>", "</indent>"):
            if not stack or stack.pop() != part[2:-1]:
                return False
        elif "<" in part or ">" in part:
            return False
    return not stack


def legacy_lookup_hash(card: Any) -> str | None:
    """Read existing exact-text records; never normalize native/Oracle identity.

    This admits known legacy native presentation for lookup only. It does not
    authorize compilation or certify effects, and preserves all identity guards.
    """
    return executable_source_hash(card, legacy_lookup=True)
