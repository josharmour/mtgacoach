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
class TargetSlot:
    """One TargetSelection slot of a SelectTargetsRequest.

    MTGA target requests can carry MULTIPLE slots (e.g. an Aura that
    enchants your creature AND exiles an opponent's permanent on enter).
    Each slot has its own legal-candidate set; a submit that fills only
    one slot when several are required is silently rejected and the
    request re-presents — the multi-target wedge (Sheltered by Ghosts,
    Ethereal Armor). Submission must cover every unsatisfied slot.
    """

    target_idx: int
    min_targets: int
    max_targets: int
    selected: int
    candidate_ids: tuple[int, ...]

    @property
    def needs(self) -> int:
        """How many more targets this slot still requires (>=0)."""
        return max(0, self.min_targets - self.selected)


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
    # SelectTargets only: per-slot structure (empty for other families and
    # for single-slot requests built from older plugin builds).
    slots: tuple[TargetSlot, ...] = ()

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
_GROUP_TYPES = {"Group", "GroupReq", "GroupRequest"}


def build_pending_decision(
    poll: Optional[dict[str, Any]],
    *,
    resolve_name: Callable[[int], str] = _default_name_resolver,
    resolve_instance: Optional[Callable[[int], str]] = None,
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
    if rtype in _GROUP_TYPES or request_class in _GROUP_TYPES:
        return _build_group(poll, request_id, can_cancel, source_label, resolve_instance)
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

    # Reconstruct per-slot structure so submission can cover EVERY slot a
    # multi-target request requires (not just the first). The flat
    # ``options`` list above stays for prompt/LLM presentation.
    slots: list[TargetSlot] = []
    for sel in poll.get("target_selections") or []:
        sel = sel or {}
        cand_ids: list[int] = []
        cseen: set[int] = set()
        for t in sel.get("targets") or []:
            iid = int(t.get("targetInstanceId") or t.get("instanceId") or 0)
            if iid and iid not in cseen:
                cseen.add(iid)
                cand_ids.append(iid)
        slots.append(
            TargetSlot(
                target_idx=int(sel.get("targetIdx") or 0),
                min_targets=int(sel.get("minTargets") or 1),
                max_targets=int(sel.get("maxTargets") or 1),
                selected=int(sel.get("selectedTargets") or 0),
                candidate_ids=tuple(cand_ids),
            )
        )

    if slots:
        # Pick enough across all unsatisfied slots; single-slot stays 1/1.
        min_sel = sum(s.needs for s in slots) or 1
        max_sel = sum(max(0, s.max_targets - s.selected) for s in slots) or min_sel
        max_sel = max(max_sel, min_sel)
    else:
        min_sel, max_sel = 1, 1
    return PendingDecision(
        request_id=request_id,
        request_type="SelectTargets",
        options=tuple(options),
        min_select=min_sel,
        max_select=max_sel,
        can_cancel=can_cancel,
        source_label=source_label,
        slots=tuple(slots),
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


def _build_group(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_instance: Optional[Callable[[int], str]],
) -> Optional[PendingDecision]:
    """GroupRequest — London mulligan bottoming and ordering windows.

    Option semantics: each option is a card; CHOSEN options go to the
    bottom group (Library/Bottom), the rest keep (Hand/Top) — mirroring
    MTGA's LondonWorkflow response shape. Only bottoming-shaped requests
    (a bottom spec with a positive bound) are mapped; pure ordering
    windows fall back to the legacy safe-default handler.
    """
    payload = poll.get("request_payload") or {}
    raw_ids = poll.get("group_instance_ids") or payload.get("instanceIds") or []
    instance_ids: list[int] = []
    for v in raw_ids:
        try:
            iid = int(v)
        except (TypeError, ValueError):
            continue
        if iid:
            instance_ids.append(iid)
    if not instance_ids:
        return None

    specs = poll.get("group_specs") or payload.get("groupSpecs") or []
    context = str(poll.get("group_context") or payload.get("context") or "")

    bottom_count = 0
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        zone = str(spec.get("zoneType") or spec.get("zone") or "")
        sub = str(spec.get("subZoneType") or spec.get("subZone") or "")
        if "Bottom" in sub or "Library" in zone:
            for key in ("lowerBound", "upperBound", "lower_bound", "upper_bound"):
                try:
                    b = int(spec.get(key) or 0)
                except (TypeError, ValueError):
                    b = 0
                if b > 0:
                    bottom_count = max(bottom_count, b)
    if bottom_count <= 0 and "LondonMulligan" in context:
        bottom_count = max(0, len(instance_ids) - 7)
    if bottom_count <= 0 or bottom_count > len(instance_ids):
        return None  # ordering/unknown shape → legacy safe-default path

    options = []
    for iid in instance_ids:
        name = ""
        if resolve_instance is not None:
            try:
                name = resolve_instance(iid) or ""
            except Exception:
                name = ""
        options.append(
            DecisionOption(
                option_id=f"grp:{iid}",
                label=(f"Bottom {name}" if name else f"Bottom card #{iid}"),
                meta={"instance_id": iid},
            )
        )
    return PendingDecision(
        request_id=request_id,
        request_type="Group",
        options=tuple(options),
        min_select=bottom_count,
        max_select=bottom_count,
        can_cancel=can_cancel,
        source_label=source_label or context,
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
        "slots": [
            {
                "target_idx": s.target_idx,
                "min_targets": s.min_targets,
                "max_targets": s.max_targets,
                "selected": s.selected,
                "candidate_ids": list(s.candidate_ids),
            }
            for s in decision.slots
        ],
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
        slots=tuple(
            TargetSlot(
                target_idx=int(s.get("target_idx") or 0),
                min_targets=int(s.get("min_targets") or 1),
                max_targets=int(s.get("max_targets") or 1),
                selected=int(s.get("selected") or 0),
                candidate_ids=tuple(int(i) for i in (s.get("candidate_ids") or [])),
            )
            for s in (data.get("slots") or [])
        ),
    )


# ---------------------------------------------------------------------------
# Submission by option id
# ---------------------------------------------------------------------------


def _tgt_iid(option_id: str) -> Optional[int]:
    if not option_id.startswith("tgt:"):
        return None
    try:
        return int(option_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def expand_target_selection(
    decision: PendingDecision, chosen_ids: list[str]
) -> list[int]:
    """Resolve a SelectTargets pick into one legal instance id per slot.

    The planner chooses from the flat option list and may only name one
    target even when the request has several slots (e.g. enchant-your-
    creature + exile-opponent's-permanent). Submitting a single id leaves
    the other slot empty → MTGA rejects → the request re-presents and the
    autopilot wedges (the Sheltered by Ghosts / Ethereal Armor loop).

    For every slot that still needs a target, prefer one of the planner's
    chosen ids that is legal there; otherwise fall back to that slot's own
    first candidate. Each id is used at most once so two slots can't
    collapse onto the same target. Slots already satisfied are skipped.
    """
    preferred = [iid for iid in (_tgt_iid(o) for o in chosen_ids) if iid]

    # No per-slot data (older plugin / single flat slot): preserve the
    # historical single-target behavior.
    if not decision.slots:
        return preferred[:1]

    used: set[int] = set()
    out: list[int] = []
    for slot in decision.slots:
        if slot.needs <= 0:
            continue
        legal = set(slot.candidate_ids)
        pick = next(
            (iid for iid in preferred if iid in legal and iid not in used), None
        )
        if pick is None:
            pick = next((iid for iid in slot.candidate_ids if iid not in used), None)
        if pick is None:
            continue  # slot has no free legal candidate; let the plugin decide
        used.add(pick)
        out.append(pick)
    # If structure analysis produced nothing usable, don't silently submit
    # empty — fall back to the planner's pick so single-target still works.
    return out or preferred[:1]


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
        # Cover every required slot, not just the first chosen target.
        target_ids = expand_target_selection(decision, chosen)
        if not target_ids:
            return False
        return bool(bridge.submit_targets(target_ids))
    if first.startswith("sel:"):
        ids = [int(o.split(":", 1)[1]) for o in chosen if o.startswith("sel:")]
        return bool(bridge.submit_selection(ids))
    if first.startswith("grp:"):
        # Chosen = bottom; the rest of the option set keeps (LondonWorkflow
        # response shape: [Hand/Top keep group, Library/Bottom group]).
        bottom = [int(o.split(":", 1)[1]) for o in chosen if o.startswith("grp:")]
        all_ids = [
            int(o.option_id.split(":", 1)[1])
            for o in decision.options
            if o.option_id.startswith("grp:")
        ]
        keep = [i for i in all_ids if i not in bottom]
        groups = [
            {"ids": keep, "zone": "Hand", "sub_zone": "Top"},
            {"ids": bottom, "zone": "Library", "sub_zone": "Bottom"},
        ]
        return bool(bridge.submit_group(groups))
    logger.warning("submit_option: unknown option id scheme %r", first)
    return False
