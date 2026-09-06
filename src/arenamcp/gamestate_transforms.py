"""GRE payload normalization helpers for gamestate.

Pure functions extracted from arenamcp.gamestate (2026-07-23) — they
normalize protobuf-JSON GRE fields (repeated fields that may arrive as
scalars, bounded copies for snapshots, UI-message classification).
Re-exported from arenamcp.gamestate for backwards compatibility.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_list(value: Any) -> list[Any]:
    """Normalize GRE repeated fields that sometimes arrive as scalars."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    """Normalize repeated GRE dict fields that may arrive as a single dict."""
    return [item for item in _ensure_list(value) if isinstance(item, dict)]


def _ensure_int_list(value: Any) -> list[int]:
    """Normalize repeated GRE integer fields that may arrive as a scalar."""
    result: list[int] = []
    for item in _ensure_list(value):
        if item is None or isinstance(item, bool):
            continue
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            logger.debug("Skipping non-integer GRE list item: %r", item)
    return result


def _collapse_gre_value(value: Any) -> Any:
    """Collapse single-item repeated values while preserving true lists."""
    if isinstance(value, (list, tuple)):
        items = [item for item in value if item is not None]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return items
    return value


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort integer coercion for GRE scalar-or-list fields."""
    value = _collapse_gre_value(value)
    if isinstance(value, list):
        value = value[0] if value else default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(value: Any) -> int | None:
    """Best-effort optional integer coercion for GRE scalar-or-list fields."""
    value = _collapse_gre_value(value)
    if isinstance(value, list):
        value = value[0] if value else None
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str_list(value: Any) -> list[str]:
    """Normalize repeated GRE string/enum fields that may arrive as scalars."""
    return [str(item) for item in _ensure_list(value) if item not in (None, "")]


def _parse_attack_state(value: Any) -> bool:
    """True iff a GameObjectInfo.attackState value means "is an attacker".

    Protobuf JSON serializes the enum by name ("AttackState_Declared" /
    "AttackState_Attacking"); tolerate raw ints (1=Declared, 2=Attacking)
    in case a serializer emits numbers.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value in (1, 2)
    return str(value) in ("AttackState_Declared", "AttackState_Attacking")


def _parse_block_state(value: Any) -> bool:
    """True iff a GameObjectInfo.blockState value means "is a blocker".

    Blocked(3)/Unblocked(4) describe an *attacker*'s fate and must not set
    is_blocking; only Declared(1)/Blocking(2) mark the object as a blocker.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value in (1, 2)
    return str(value) in ("BlockState_Declared", "BlockState_Blocking")


def _bounded_gre_copy(
    value: Any,
    *,
    max_depth: int = 6,
    max_list_items: int = 24,
    max_dict_items: int = 32,
    max_string_length: int = 400,
    _depth: int = 0,
) -> tuple[Any, bool]:
    """Create a bounded copy of raw GRE payload data for snapshots/debugging."""
    if _depth >= max_depth:
        if value is None or isinstance(value, (bool, int, float)):
            return value, False
        text = value if isinstance(value, str) else repr(value)
        if len(text) > max_string_length:
            return text[: max_string_length - 3] + "...", True
        return text, True

    if value is None or isinstance(value, (bool, int, float)):
        return value, False

    if isinstance(value, str):
        if len(value) > max_string_length:
            return value[: max_string_length - 3] + "...", True
        return value, False

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        truncated = False
        items = list(value.items())
        for idx, (key, item) in enumerate(items):
            if idx >= max_dict_items:
                truncated = True
                break
            copied, child_truncated = _bounded_gre_copy(
                item,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            result[str(key)] = copied
            truncated = truncated or child_truncated
        return result, truncated or len(items) > max_dict_items

    if isinstance(value, (list, tuple)):
        result = []
        truncated = False
        items = list(value)
        for idx, item in enumerate(items):
            if idx >= max_list_items:
                truncated = True
                break
            copied, child_truncated = _bounded_gre_copy(
                item,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            result.append(copied)
            truncated = truncated or child_truncated
        return result, truncated or len(items) > max_list_items

    text = repr(value)
    if len(text) > max_string_length:
        return text[: max_string_length - 3] + "...", True
    return text, False


def _collect_text_fragments(value: Any, *, max_items: int = 12) -> list[str]:
    """Extract candidate human-readable text fragments from nested GRE payloads."""
    results: list[str] = []
    seen: set[str] = set()
    text_keys = {"text", "message", "label", "title", "header", "body", "prompt"}

    def visit(node: Any) -> None:
        if len(results) >= max_items:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if len(results) >= max_items:
                    return
                if isinstance(child, str) and key.lower() in text_keys:
                    text = child.strip()
                    if text and text not in seen:
                        seen.add(text)
                        results.append(text)
                else:
                    visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
                if len(results) >= max_items:
                    return

    visit(value)
    return results


def _detect_ui_message_kind(value: Any) -> str | None:
    """Best-effort classification of a raw UI GRE message."""
    blob = json.dumps(value, sort_keys=True, default=str).lower()
    if "onhover" in blob or '"hover"' in blob:
        return "hover"
    if "tooltip" in blob:
        return "tooltip"
    if "toast" in blob or "notification" in blob:
        return "toast"
    if "alert" in blob or "warning" in blob:
        return "alert"
    if "prompt" in blob:
        return "prompt"
    return None
