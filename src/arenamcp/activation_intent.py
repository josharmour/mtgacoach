"""Carry a selected activation's purpose into its own nested legal requests.

Separate from cast/search target memory: an equip or loyalty activation has
no inferred spell line target. Identity, ability, turn and lifetime gate the
hint; it never chooses or adds a target to the current menu.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from arenamcp.board_model import canonical_phase_step
from arenamcp.play_safety import find_source

_FOLLOWUPS = frozenset(
    {
        "SelectTargets",
        "SelectN",
        "Search",
        "SearchFromGroups",
        "SelectFromGroups",
        "Group",
        "CastingTimeOptions",
        "OptionalAction",
    }
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _turn_key(state: dict) -> tuple:
    turn = _dict(state.get("turn"))
    return (
        str(state.get("match_id") or ""),
        _int(turn.get("turn_number")),
        _int(turn.get("active_player")),
        _int(state.get("local_seat_id")),
    )


@dataclass(frozen=True)
class ActivationIntent:
    source_id: int
    ability_id: int
    name: str
    reason: str
    turn_key: tuple
    phase: str
    request_id: tuple
    at: float


def _matches(intent: ActivationIntent | None, decision: Any, state: dict) -> bool:
    if intent is None or decision.request_type not in _FOLLOWUPS:
        return False
    if time.monotonic() - intent.at > 60 or _turn_key(state) != intent.turn_key:
        return False
    request_id = tuple(decision.request_id)
    if request_id[0] and intent.request_id[0] and request_id <= intent.request_id:
        return False
    payload = _dict(state.get("_bridge_request_payload"))
    context = _dict(state.get("decision_context"))
    source_id = (
        _int(payload.get("sourceId") or payload.get("source_id"))
        or _int(getattr(decision, "source_instance_id", 0))
        or _int(context.get("sourceId") or context.get("source_id"))
    )
    if not source_id:
        return False  # a display-name match cannot distinguish another copy
    source = next(
        (
            card
            for zone in ("stack", "pending_selection_objects")
            for card in state.get(zone) or []
            if isinstance(card, dict) and _int(card.get("instance_id")) == source_id
        ),
        {},
    )
    parent_id = _int(source.get("parent_instance_id"))
    if (parent_id or source_id) != intent.source_id:
        return False
    selections = list(payload.get("targetSelections") or payload.get("target_selections") or [])
    context_id = _int(context.get("sourceId") or context.get("source_id"))
    if context_id == source_id:
        selections += list(context.get("targets") or _dict(context.get("raw")).get("targets") or [])
    ability_ids = {
        _int(item.get("targetingAbilityGrpId")) for item in selections if isinstance(item, dict)
    } - {0}
    return not ability_ids or not intent.ability_id or intent.ability_id in ability_ids


def observe_activation(
    previous: ActivationIntent | None, decision: Any, state: dict, chosen: list[str], reason: str
) -> ActivationIntent | None:
    if any(str(option).startswith("__") for option in chosen or []):
        return None  # cancellation/no-target sentinel
    if decision.request_type != "ActionsAvailable":
        # The target/group choice completes this hint. Do not lend it to a
        # later triggered ability from the same permanent before priority.
        if decision.request_type in ("SelectTargets", "Group") and decision.selection_is_valid(chosen):
            if _matches(previous, decision, state):
                return None
        return previous
    if len(chosen) != 1 or not decision.selection_is_valid(chosen):
        return None
    option = decision.find(chosen[0])
    meta = option.meta or {}
    if meta.get("actionType") != "ActionType_Activate" or option.payable is False:
        return None
    source_id = _int(meta.get("instanceId"))
    if not source_id or not _turn_key(state)[1]:
        return None
    source = find_source(state, meta)
    phase, step = canonical_phase_step(state.get("turn"))
    return ActivationIntent(
        source_id,
        _int(meta.get("abilityGrpId")),
        str(source.get("name") or f"source #{source_id}"),
        " ".join(str(reason or "").split()[:60]),
        _turn_key(state),
        " ".join(filter(None, (phase, step))),
        tuple(decision.request_id),
        time.monotonic(),
    )


def activation_intent_note(intent: ActivationIntent | None, decision: Any, state: dict) -> str:
    if not _matches(intent, decision, state):
        return ""
    phase, step = canonical_phase_step(state.get("turn"))
    now = " ".join(filter(None, (phase, step))) or "unknown phase"
    return (
        f"ACTIVATION INTENT: you activated {intent.name} (source {intent.source_id}, "
        f'ability {intent.ability_id}) during {intent.phase or "unknown phase"}, saying: "{intent.reason}". '
        f"Current timing: {now}. Recheck the purpose against this board and only the current legal options; "
        "the earlier explanation is conditional guidance, not a target selection. If its intended target "
        "or follow-up is no longer legal or useful, explain the changed choice."
    )
