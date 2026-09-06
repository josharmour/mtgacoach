"""Prompt-payload compaction and fallback advice helpers for the coach engine.

Extracted from arenamcp.coach (pure move, no behavior change).
Re-exported from arenamcp.coach for backwards compatibility."""

import json
from typing import Any

from arenamcp.backend_health import LOCAL_FALLBACK_PREFIX


def _compact_gre_target(target: Any) -> Any:
    """Reduce GRE target payloads to compact prompt-friendly fields."""
    if not isinstance(target, dict):
        return target

    compact = {}
    for key in ("targetType", "instanceId", "grpId", "zoneId", "seatId", "selection", "index"):
        value = target.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact or target


def _compact_legal_action_for_prompt(action: Any) -> Any:
    """Reduce raw GRE legal actions to the fields most useful to the model."""
    if not isinstance(action, dict):
        return action

    compact = {}
    for key in (
        "actionType",
        "grpId",
        "instanceId",
        "abilityGrpId",
        "sourceId",
        "alternativeGrpId",
        "selectionType",
        "selection",
        "shouldStop",
        "maxActivations",
        "isBatchable",
        "highlight",
    ):
        value = action.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value

    targets = action.get("targets")
    if isinstance(targets, list) and targets:
        compact["targets"] = [_compact_gre_target(t) for t in targets[:4]]

    mana_options = action.get("manaPaymentOptions")
    if isinstance(mana_options, list) and mana_options:
        compact["manaPaymentOptionsCount"] = len(mana_options)

    costs = action.get("costs")
    if isinstance(costs, list) and costs:
        compact["costCount"] = len(costs)

    return compact or action


def _format_legal_actions_raw_for_prompt(
    actions: list[dict[str, Any]],
    max_actions: int = 12,
) -> str:
    """Format raw GRE legal actions compactly for prompt context."""
    if not actions:
        return "[]"

    compact_actions = [_compact_legal_action_for_prompt(action) for action in actions[:max_actions]]
    suffix = " …" if len(actions) > max_actions else ""
    return json.dumps(compact_actions, separators=(",", ":")) + suffix


_ACTIONS_AVAILABLE_BRIDGE_REQUESTS = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}


def _compact_prompt_value(
    value: Any,
    *,
    max_depth: int = 4,
    max_list_items: int = 10,
    max_dict_items: int = 16,
    max_string_length: int = 240,
    _depth: int = 0,
) -> Any:
    """Compact nested JSON-like data into a bounded prompt-friendly structure."""
    if _depth >= max_depth:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        text = value if isinstance(value, str) else repr(value)
        return text if len(text) <= max_string_length else text[: max_string_length - 3] + "..."

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return value if len(value) <= max_string_length else value[: max_string_length - 3] + "..."

    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for idx, (key, child) in enumerate(value.items()):
            if idx >= max_dict_items:
                compact["_truncated"] = True
                break
            compact[str(key)] = _compact_prompt_value(
                child,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
        return compact

    if isinstance(value, (list, tuple)):
        compact = [
            _compact_prompt_value(
                child,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for child in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            compact.append({"_truncated": True})
        return compact

    text = repr(value)
    return text if len(text) <= max_string_length else text[: max_string_length - 3] + "..."


def _format_bounded_json_for_prompt(
    value: Any,
    *,
    max_chars: int = 5000,
) -> str:
    """Format bounded JSON data into a single prompt line."""
    text = json.dumps(_compact_prompt_value(value), separators=(",", ":"))
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _format_raw_gre_events_for_prompt(
    events: list[dict[str, Any]],
    *,
    max_events: int = 4,
) -> str:
    """Format a bounded tail of raw GRE events for richer online prompts."""
    if not events:
        return "[]"

    compact_events: list[dict[str, Any]] = []
    for event in events[-max_events:]:
        compact: dict[str, Any] = {}
        for key in ("seq", "type", "turn", "phase", "seat_id", "message_index", "payload_truncated"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value
        payload = event.get("payload")
        if payload not in (None, "", [], {}):
            compact["payload"] = _compact_prompt_value(
                payload, max_depth=3, max_list_items=8, max_dict_items=12
            )
        if compact:
            compact_events.append(compact)

    return json.dumps(compact_events, separators=(",", ":"))


def _build_bridge_context_lines(
    game_state: dict[str, Any],
    raw_legal_actions: list[dict[str, Any]],
    *,
    for_planner: bool = False,
) -> list[str]:
    """Render bounded bridge/GRE context into prompt lines.

    Args:
        for_planner: If True, omit heavy raw GRE JSON dumps (LegalGRE,
            GRE_RequestPayload, GRE_Recent). The planner only needs the
            request type to route decisions; the bridge action matcher
            consumes raw legal actions out-of-band, not via the prompt.
    """
    lines: list[str] = []
    bridge_req = game_state.get("_bridge_request_type")
    bridge_request_class = game_state.get("_bridge_request_class")
    bridge_request_payload = game_state.get("_bridge_request_payload")
    raw_gre_events = game_state.get("raw_gre_events") or []

    if raw_legal_actions and not for_planner:
        lines.append("LegalGRE: " + _format_legal_actions_raw_for_prompt(raw_legal_actions))
    if bridge_req:
        lines.append(f"GRE_Request: {bridge_req}")
    if bridge_request_class and bridge_request_class != bridge_req:
        lines.append(f"GRE_RequestClass: {bridge_request_class}")
    if bridge_request_payload and not for_planner:
        lines.append("GRE_RequestPayload: " + _format_bounded_json_for_prompt(bridge_request_payload))
    if raw_gre_events and not for_planner:
        lines.append("GRE_Recent: " + _format_raw_gre_events_for_prompt(raw_gre_events))

    # X-cost range. Small and decisive, so the planner gets it too — it is
    # told to choose "within shown min/max" and otherwise sees no bounds.
    numeric_line = _format_numeric_constraints(game_state)
    if numeric_line:
        lines.append(numeric_line)

    return lines


def _format_numeric_constraints(game_state: dict[str, Any]) -> str:
    """Render the pending X/numeric input's bounds as one prompt line."""
    minimum = game_state.get("_bridge_numeric_min")
    maximum = game_state.get("_bridge_numeric_max")
    if minimum is None and maximum is None:
        return ""

    parts = [
        f"min={minimum if minimum is not None else '?'}",
        f"max={maximum if maximum is not None else '?'}",
    ]
    step = game_state.get("_bridge_numeric_step")
    if step:
        parts.append(f"step={step}")
    suggested = game_state.get("_bridge_numeric_suggested")
    if suggested:
        parts.append("suggested=" + ",".join(str(v) for v in suggested))
    disallowed = game_state.get("_bridge_numeric_disallowed")
    if disallowed:
        parts.append("disallowed=" + ",".join(str(v) for v in disallowed))
    if game_state.get("_bridge_numeric_disallow_even"):
        parts.append("odd values only")
    if game_state.get("_bridge_numeric_disallow_odd"):
        parts.append("even values only")
    return "X_RANGE: " + ", ".join(parts)


_NON_PASSABLE_REQUEST_CLASSES = {
    "SelectTargetsRequest",
    "SelectNRequest",
    "SearchRequest",
    "GroupRequest",
    "DistributionRequest",
    "CastingTimeOptionRequest",
    "CastingTimeOption_ModalRequest",
    "CastingTimeOption_ChooseOrCostRequest",
    "CastingTimeOption_NumericInputRequest",
    "CastingTimeOption_Replicate",
    "CastingTimeOption_SelectNRequest",
    "CastingTimeOption_SpecializeRequest",
    "CastingTimeOption_KickerRequest",
    "CastingTimeOption_AdditionalCostRequest",
    "CastingTimeOption_CostKeywordRequest",
    "PayCostsRequest",
    "MulliganRequest",
}


_NON_PASSABLE_REQUEST_TYPES = {
    "SelectTargets",
    "SelectN",
    "Search",
    "Group",
    "Distribution",
    "PayCosts",
    "Mulligan",
}


def _fallback_non_action_advice(game_state: dict[str, Any]) -> str:
    """Pick a sensible fallback advice when legal_actions is empty.

    `pass priority` is the right answer for an idle Action request, but
    non-passable requests (target/mode/search/cast-time) need manual
    intervention instead. Surface that clearly rather than issuing a
    literal "pass priority" the user can't actually submit.

    Output is tagged [LOCAL FALLBACK] — it is generated locally without
    the LLM and must never be mistaken for model advice.
    """
    req_type = str(game_state.get("_bridge_request_type") or "")
    req_class = str(game_state.get("_bridge_request_class") or "")
    in_intermission = bool(game_state.get("_bridge_in_intermission"))
    can_pass = game_state.get("_bridge_can_pass")

    if in_intermission:
        msg = "Match ending — no action needed."
    else:
        non_passable = (
            req_class in _NON_PASSABLE_REQUEST_CLASSES
            or req_type in _NON_PASSABLE_REQUEST_TYPES
            or (can_pass is False and req_class)
        )
        if non_passable:
            if "Target" in req_class or req_type == "SelectTargets":
                msg = "Pick a target manually."
            elif "Search" in req_class:
                msg = "Pick a card manually."
            elif req_class.startswith("CastingTimeOption") or "Modal" in req_class:
                msg = "Choose a mode manually."
            elif "PayCosts" in req_class or req_type == "PayCosts":
                msg = "Confirm the mana payment."
            elif "Mulligan" in req_class:
                msg = "Make the mulligan call."
            else:
                msg = "Make this decision manually."
        else:
            msg = "pass priority"
    return f"{LOCAL_FALLBACK_PREFIX} {msg}"
