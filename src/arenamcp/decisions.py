"""Typed PendingDecision pipeline (fable-improvements.md item 1).

One structured object flows from the bridge poll to the planner to the
executor. The planner chooses among ``option_id``s; submission happens by
id; display strings are rendered *from* the structure and never parsed
back *into* it.

Option-id scheme (family-agnostic; the executor dispatches on prefix):

    idx:<n>        — ActionsAvailable action at index n (submit_action_by_index)
    tgt:<iid>      — SelectTargets candidate instance id (submit_targets)
    sel:<id>       — SelectN / Search id (submit_selection, multi-select)
    mull:keep|mull — Mulligan decision (submit_mulligan)
    pass           — pass priority (submit_pass)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionOption:
    option_id: str
    label: str  # display only — NEVER parsed for semantics
    payable: Optional[bool] = None  # casts: autotap solution exists; else None
    meta: dict = field(default_factory=dict)  # prompt enrichment only


@dataclass(frozen=True)
class PendingDecision:
    request_id: tuple[int, int]  # (gameStateId, msgId); zeros when unknown
    request_type: str  # bridge enum/class name ("SelectTargets", ...)
    options: tuple[DecisionOption, ...]
    min_select: int = 1
    max_select: int = 1
    can_pass: bool = False
    can_cancel: bool = False
    source_label: str = ""

    def option_ids(self) -> set[str]:
        return {o.option_id for o in self.options}

    def find(self, option_id: str) -> Optional[DecisionOption]:
        for o in self.options:
            if o.option_id == option_id:
                return o
        return None


def _default_name_resolver(grp_id: int) -> str:
    try:
        from arenamcp import server

        info = server.get_card_info(grp_id)
        return str(info.get("name") or "")
    except Exception:
        return ""


_ACTIONS_AVAILABLE_TYPES = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}
_SELECT_TARGETS_TYPES = {"SelectTargets", "SelectTargetsRequest"}
_SELECT_N_TYPES = {"SelectN", "SelectNRequest", "Search", "SearchRequest"}
_MULLIGAN_TYPES = {"Mulligan", "MulliganReq", "MulliganRequest"}


def build_pending_decision(
    poll: Optional[dict[str, Any]],
    *,
    resolve_name: Callable[[int], str] = _default_name_resolver,
) -> Optional[PendingDecision]:
    """Build a PendingDecision from a raw get_pending_actions() response.

    Returns None when nothing is pending or the request family isn't
    structurally mapped yet (callers keep their legacy path as fallback —
    fable-improvements.md migration note).
    """
    if not poll or not poll.get("has_pending"):
        return None

    request_type = str(poll.get("request_type") or "")
    request_class = str(poll.get("request_class") or "")
    rtype = request_type or request_class
    request_id = (
        int(poll.get("game_state_id") or 0),
        int(poll.get("msg_id") or 0),
    )
    can_pass = bool(poll.get("can_pass"))
    can_cancel = bool(poll.get("can_cancel"))
    source_label = str(poll.get("source_card") or poll.get("prompt") or "")

    if rtype in _ACTIONS_AVAILABLE_TYPES or (
        not rtype and poll.get("actions")
    ):
        return _build_actions_available(
            poll, request_id, can_pass, can_cancel, source_label, resolve_name
        )
    if rtype in _SELECT_TARGETS_TYPES or request_class in _SELECT_TARGETS_TYPES:
        return _build_select_targets(
            poll, request_id, can_cancel, source_label, resolve_name
        )
    if rtype in _SELECT_N_TYPES or request_class in _SELECT_N_TYPES:
        return _build_select_n(
            poll, request_id, rtype, can_cancel, source_label, resolve_name
        )
    if rtype in _MULLIGAN_TYPES or request_class in _MULLIGAN_TYPES:
        return PendingDecision(
            request_id=request_id,
            request_type="Mulligan",
            options=(
                DecisionOption("mull:keep", "Keep this hand"),
                DecisionOption("mull:mull", "Mulligan"),
            ),
            can_pass=False,
            can_cancel=False,
        )
    return None


def _build_actions_available(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_pass: bool,
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
) -> Optional[PendingDecision]:
    options: list[DecisionOption] = []
    saw_pass = False
    for i, action in enumerate(poll.get("actions") or []):
        atype = str(action.get("actionType") or "")
        grp_id = int(action.get("grpId") or 0)
        name = resolve_name(grp_id) if grp_id else ""
        payable: Optional[bool] = None
        if atype == "ActionType_Pass":
            saw_pass = True
            options.append(DecisionOption("pass", "Pass"))
            continue
        if atype == "ActionType_Cast":
            payable = action.get("autoTapSolution") is not None
            label = f"Cast {name or 'spell'}" + (
                "" if payable else " (cannot auto-pay)"
            )
        elif atype == "ActionType_Play":
            label = f"Play land: {name or 'land'}"
        elif atype == "ActionType_Activate":
            label = f"Activate: {name or 'ability'}"
        else:
            label = atype.replace("ActionType_", "") or f"Action {i}"
        options.append(
            DecisionOption(
                option_id=f"idx:{i}",
                label=label,
                payable=payable,
                meta={
                    "actionType": atype,
                    "grpId": grp_id,
                    "instanceId": int(action.get("instanceId") or 0),
                },
            )
        )
    if can_pass and not saw_pass:
        options.append(DecisionOption("pass", "Pass"))
    if not options:
        return None
    return PendingDecision(
        request_id=request_id,
        request_type="ActionsAvailable",
        options=tuple(options),
        can_pass=can_pass or saw_pass,
        can_cancel=can_cancel,
        source_label=source_label,
    )


def _build_select_targets(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
) -> Optional[PendingDecision]:
    options: list[DecisionOption] = []
    seen: set[int] = set()
    for cand in poll.get("target_candidates") or []:
        iid = int(cand.get("targetInstanceId") or cand.get("instanceId") or 0)
        if not iid or iid in seen:
            continue
        seen.add(iid)
        grp_id = int(cand.get("grpId") or 0)
        name = resolve_name(grp_id) if grp_id else ""
        options.append(
            DecisionOption(
                option_id=f"tgt:{iid}",
                label=name or f"Target #{iid}",
                meta={"grpId": grp_id, "targetIdx": cand.get("targetIdx")},
            )
        )
    if not options:
        return None
    min_sel, max_sel = 1, 1
    selections = poll.get("target_selections") or []
    if selections:
        first = selections[0] or {}
        min_sel = int(first.get("minTargets") or 1)
        max_sel = int(first.get("maxTargets") or 1)
    return PendingDecision(
        request_id=request_id,
        request_type="SelectTargets",
        options=tuple(options),
        min_select=min_sel,
        max_select=max_sel,
        can_cancel=can_cancel,
        source_label=source_label,
    )


def _build_select_n(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    rtype: str,
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
) -> Optional[PendingDecision]:
    ids = poll.get("select_n_ids") or poll.get("search_candidates") or []
    options: list[DecisionOption] = []
    for raw in ids:
        if isinstance(raw, dict):
            oid = int(raw.get("id") or raw.get("instanceId") or raw.get("grpId") or 0)
            grp_id = int(raw.get("grpId") or 0)
        else:
            try:
                oid = int(raw)
            except (TypeError, ValueError):
                continue
            grp_id = 0
        if not oid:
            continue
        name = resolve_name(grp_id) if grp_id else ""
        options.append(
            DecisionOption(
                option_id=f"sel:{oid}",
                label=name or f"Option {oid}",
                meta={"grpId": grp_id},
            )
        )
    if not options:
        return None
    return PendingDecision(
        request_id=request_id,
        request_type="Search" if "Search" in rtype else "SelectN",
        options=tuple(options),
        min_select=int(poll.get("select_n_min") or 1),
        max_select=int(poll.get("select_n_max") or 1),
        can_cancel=can_cancel,
        source_label=source_label,
    )


# ---------------------------------------------------------------------------
# (De)serialization — used by the stall corpus (fable item 5)
# ---------------------------------------------------------------------------


def decision_to_dict(decision: PendingDecision) -> dict[str, Any]:
    return {
        "request_id": list(decision.request_id),
        "request_type": decision.request_type,
        "options": [
            {
                "option_id": o.option_id,
                "label": o.label,
                "payable": o.payable,
                "meta": o.meta,
            }
            for o in decision.options
        ],
        "min_select": decision.min_select,
        "max_select": decision.max_select,
        "can_pass": decision.can_pass,
        "can_cancel": decision.can_cancel,
        "source_label": decision.source_label,
    }


def decision_from_dict(data: dict[str, Any]) -> PendingDecision:
    return PendingDecision(
        request_id=tuple(data.get("request_id") or (0, 0)),  # type: ignore[arg-type]
        request_type=str(data.get("request_type") or ""),
        options=tuple(
            DecisionOption(
                option_id=str(o.get("option_id") or ""),
                label=str(o.get("label") or ""),
                payable=o.get("payable"),
                meta=o.get("meta") or {},
            )
            for o in (data.get("options") or [])
        ),
        min_select=int(data.get("min_select") or 1),
        max_select=int(data.get("max_select") or 1),
        can_pass=bool(data.get("can_pass")),
        can_cancel=bool(data.get("can_cancel")),
        source_label=str(data.get("source_label") or ""),
    )


# ---------------------------------------------------------------------------
# Submission by option id
# ---------------------------------------------------------------------------


def submit_option(
    bridge: Any,
    decision: PendingDecision,
    option_ids: list[str],
) -> bool:
    """Submit the chosen option(s) through the bridge by id.

    Mechanical validation: ids outside the decision's option set are
    rejected here — there is no string matching and no legality heuristic.
    """
    valid = decision.option_ids()
    chosen = [oid for oid in option_ids if oid in valid]
    if not chosen:
        logger.warning(
            "submit_option: none of %s are valid for %s (valid: %s)",
            option_ids,
            decision.request_type,
            sorted(valid),
        )
        return False
    if len(chosen) < len(option_ids):
        logger.info(
            "submit_option: dropped invalid ids %s",
            [o for o in option_ids if o not in valid],
        )

    first = chosen[0]
    if first == "pass":
        return bool(bridge.submit_pass())
    if first.startswith("mull:"):
        return bool(bridge.submit_mulligan(first == "mull:keep"))
    if first.startswith("idx:"):
        return bool(bridge.submit_action_by_index(int(first.split(":", 1)[1])))
    if first.startswith("tgt:"):
        return bool(bridge.submit_targets(int(first.split(":", 1)[1])))
    if first.startswith("sel:"):
        ids = [int(o.split(":", 1)[1]) for o in chosen if o.startswith("sel:")]
        return bool(bridge.submit_selection(ids))
    logger.warning("submit_option: unknown option id scheme %r", first)
    return False
