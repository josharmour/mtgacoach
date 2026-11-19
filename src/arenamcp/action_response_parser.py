"""LLM JSON cleanup, payload normalization, ActionPlan & TurnPlan response parsing."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from arenamcp.action_types import ActionPlan, ActionType, TurnPlan, TurnPlanStep

logger = logging.getLogger("arenamcp.action_planner")


def strip_json_fences(text: str, *, extract_object: bool = False) -> str | None:
    """Strip markdown code fences and trailing commas from JSON model output."""
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    if extract_object:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        cleaned = match.group(0)
    return re.sub(r",\s*([\]}])", r"\1", cleaned)


def normalize_action_payload(data: Any) -> dict[str, Any]:
    """Normalize list/single-action LLM JSON responses into ``{"actions": [...]}``."""
    if isinstance(data, list):
        return {"actions": data}
    if not isinstance(data, dict):
        return {}
    if "actions" not in data and ("pick" in data or "action_type" in data):
        out = {**data, "actions": [data]}
        if not out.get("overall_strategy"):
            out["overall_strategy"] = str(out.get("reasoning", "") or "")
        return out
    return data


def parse_turn_plan_response(
    response: str,
    current_turn: int,
    visible_actions: Iterable[str],
) -> TurnPlan | None:
    """Defensively parse ``{"turn_plan": {"steps": [...]}}`` or top-level actions."""
    if not response:
        return None
    text = strip_json_fences(response) or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("turn plan: response was not valid JSON")
        return None

    if not isinstance(data, dict):
        return None

    block = data.get("turn_plan")
    steps_data = block.get("steps", []) if isinstance(block, dict) else data.get("steps", [])

    if (not isinstance(steps_data, list) or not steps_data) and isinstance(data.get("actions"), list):
        steps_data = [
            {**a, "rationale": a.get("rationale") or a.get("reasoning", "")}
            for a in data["actions"]
            if isinstance(a, dict)
        ]

    if not isinstance(steps_data, list):
        return None

    allowed = set(visible_actions)
    steps: list[TurnPlanStep] = []
    for item in steps_data:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("action_type") or "").strip().lower()
        if not action_type or action_type not in allowed:
            continue
        card_name = str(item.get("card_name") or "").strip()
        target_names_raw = item.get("target_names") or []
        if isinstance(target_names_raw, list):
            target_names = [str(t).strip() for t in target_names_raw if str(t).strip()]
        else:
            target_names = []
        rationale = str(item.get("rationale") or "").strip()
        steps.append(
            TurnPlanStep(
                action_type=action_type,
                card_name=card_name,
                target_names=target_names,
                rationale=rationale,
                status="pending",
            )
        )

    if not steps:
        return None
    return TurnPlan(turn_number=current_turn, steps=steps)


def parse_action_plan_response(
    planner: Any,
    response: str,
    legal_actions: list[str],
    decision_context: dict[str, Any] | None = None,
    bridge_request: str | None = None,
    game_state: dict[str, Any] | None = None,
) -> ActionPlan:
    """Parse an LLM action-plan JSON reply into a validated :class:`ActionPlan`."""
    plan = ActionPlan()
    json_str = strip_json_fences(response) or ""

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        from arenamcp.action_diagnostics import record_parse_failure
        from arenamcp.action_json import recover_action_json

        decode_error = e

        def record_failure(recovery: str) -> None:
            state = game_state if isinstance(game_state, dict) else {}
            request = state.get("_bridge_request_payload")
            request_id = (
                (request.get("gameStateId"), request.get("msgId")) if isinstance(request, dict) else ()
            )
            record_parse_failure(
                response,
                parse_input=json_str,
                error=decode_error,
                recovery=recovery,
                legal_action_count=len(legal_actions or []),
                request_type=bridge_request,
                request_id=request_id,
                origin_logger=logger,
            )

        recovered = recover_action_json(json_str)
        if recovered is not None:
            record_failure("action_payload")
            return parse_action_plan_response(
                planner, json.dumps(recovered), legal_actions, decision_context, bridge_request, game_state
            )
        m = re.search(r'"pick"\s*:\s*(\d+)', json_str)
        if m and planner._last_menu:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(planner._last_menu):
                action = planner._legal_action_to_action(planner._last_menu[idx])
                if action is not None:
                    record_failure("pick")
                    plan.actions = [action]
                    plan.overall_strategy = f"[pick-salvage] {planner._last_menu[idx]}"
                    plan.voice_advice = planner._humanize_legal_action(planner._last_menu[idx])
                    return plan
        record_failure("none")
        return plan

    data = normalize_action_payload(data)
    plan.overall_strategy = data.get("overall_strategy", "")
    plan.voice_advice = data.get("voice_advice", "")

    for action_data in data.get("actions", []):
        action = None
        pick = action_data.get("pick") if isinstance(action_data, dict) else None
        if pick is not None and planner._structured_combat_declaration(action_data):
            pick = None
        if pick is not None:
            try:
                idx = int(pick) - 1
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(planner._last_menu):
                entry = planner._last_menu[idx]
                if entry.lower().startswith("block with:"):
                    if not action_data.get("blocker_assignments"):
                        logger.warning("Blocker menu pick %s omitted its attacker assignments", pick)
                        continue
                    action = planner._parse_action({**action_data, "action_type": "declare_blockers"})
                else:
                    action = planner._menu_entry_action(entry, action_data)
                if action is not None:
                    action.reasoning = str(action_data.get("reasoning", "") or "")
                    logger.debug(f"Menu pick {pick} → {planner._last_menu[idx]!r}")
            if action is None:
                logger.warning(
                    f"Planner pick {pick!r} "
                    + (
                        f"({planner._last_menu[idx]!r}) has no executable mapping"
                        if 0 <= idx < len(planner._last_menu)
                        else f"out of menu range (1..{len(planner._last_menu)})"
                    )
                    + "; trying structured fields"
                )
        if action is None:
            if (
                isinstance(action_data, dict)
                and str(action_data.get("action_type") or "").strip().lower() == "declare_blockers"
                and not (action_data.get("blocker_assignments") or {})
                and (
                    str((decision_context or {}).get("type") or "").lower() == "declare_attackers"
                    or str(bridge_request or "") == "DeclareAttackers"
                    or any(
                        str(a or "").lower().strip().startswith(("attack with:", "declare attackers:"))
                        for a in legal_actions
                    )
                )
            ):
                action_data = {
                    **action_data,
                    "action_type": "declare_attackers",
                    "attacker_names": list(action_data.get("attacker_names") or []),
                    "attacker_targets": dict(action_data.get("attacker_targets") or {}),
                }
            action = planner._parse_action(action_data)
        if action is not None and action.action_type == ActionType.DECLARE_ATTACKERS:
            from arenamcp.action_legality import normalize_attack_declaration

            context = decision_context or {}
            ids = context.get("legal_attacker_ids") or [
                a.get("attackerInstanceId") for a in context.get("raw_attackers") or [] if isinstance(a, dict)
            ]
            repaired = normalize_attack_declaration(action, legal_actions, ids)
            if repaired:
                logger.info("Attack declaration repaired: %s", repaired)
        if action and game_state and game_state.get("battlefield"):
            try:
                planner._bind_combat_identities(action, game_state, decision_context or {})
            except (ValueError, TypeError, KeyError) as error:
                logger.warning("Rejecting ambiguous combat assignment: %s", error)
                continue
        if action and planner._is_action_legal(action, legal_actions, decision_context, bridge_request):
            plan.actions.append(action)
        elif action:
            logger.warning(
                "Dropping illegal planner action: %s (%s) bridge_request=%r decision=%r not in %s",
                action.action_type.value,
                action.card_name,
                bridge_request,
                (decision_context or {}).get("type"),
                legal_actions,
            )
            if action.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS):
                logger.warning(
                    "Dropped declaration: attackers=%s targets=%s blocks=%s; answer: %s",
                    action.attacker_names,
                    action.attacker_targets,
                    action.blocker_assignments,
                    (response or "")[:4000],
                )

    if any(
        action.action_type == ActionType.DECLARE_ATTACKERS
        and (action.attacker_targets or action.target_names)
        for action in plan.actions
    ):
        plan.voice_advice = plan.spoken_actions()
        return plan
    if plan.actions and plan.voice_advice:
        accepted_names = []
        for action in plan.actions:
            accepted_names.extend(action.select_card_names)
            accepted_names.extend(action.target_names)
            accepted_names.extend(action.attacker_names)
            accepted_names.extend(action.blocker_assignments.keys())
            accepted_names.extend(action.blocker_assignments.values())
            if action.card_name:
                accepted_names.append(action.card_name)
        advice_low = plan.voice_advice.casefold()
        if any(n and n.casefold() in advice_low for n in accepted_names):
            return plan
    plan.voice_advice = plan.spoken_actions()
    return plan
