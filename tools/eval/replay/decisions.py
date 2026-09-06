"""Extract decision points + ground-truth player actions from a replay.

Pairs each `IN`-direction request message with its matching `OUT`-direction
response by matching `respId` against the request's `msgId`. This is the
authoritative pairing per the GRE protocol; the "next OUT message"
heuristic in `reader.py` is an approximation we no longer rely on.

For each decision we extract a compact `GroundTruth` description: a
backend-agnostic action label that can be compared directly to what a
coach recommended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .reader import ReplayMessage


@dataclass
class GroundTruth:
    """Compact description of what the player actually did at a decision.

    Fields are populated based on the request type. Always non-empty
    `kind` and `summary`; other fields are optional.
    """

    kind: str  # request type stripped of GREMessageType_ prefix
    summary: str  # human-readable one-liner ("Cast Lightning Strike", "Pass", ...)
    action_type: Optional[str] = None  # ActionType_Cast / ActionType_Pass / ActionType_PlayLand / ...
    grp_ids: list[int] = field(default_factory=list)  # cards played/cast/searched
    instance_ids: list[int] = field(default_factory=list)  # board entities targeted
    keep: Optional[bool] = None  # mulligan answer
    raw: dict = field(default_factory=dict)


@dataclass
class Decision:
    """One (request, response, ground_truth) tuple."""

    index: int  # decision number within the replay (0-based)
    request: ReplayMessage
    response: Optional[ReplayMessage]
    ground_truth: Optional[GroundTruth]


# ---------------------------------------------------------------------------
# Per-request-type extractors
#
# Each function receives the request's payload (the full proto JSON) and the
# response's payload (or None) and returns a ``GroundTruth`` or ``None`` if
# the response shape doesn't carry a usable answer (e.g. an ActionsAvailable
# request answered by an empty PerformActionResp). The kind label is set by
# the dispatcher.
# ---------------------------------------------------------------------------


def _extract_actions_available(req: dict, resp: dict) -> Optional[GroundTruth]:
    actions = ((resp.get("performActionResp") or {}).get("actions")) or []
    if not actions:
        # Some responses are pure ack with no action; treat as Pass-equivalent.
        return GroundTruth(
            kind="ActionsAvailable", summary="(empty action)", action_type="ActionType_Pass", raw={}
        )
    # Player can submit multiple actions in one response (rare); summarize.
    parts = []
    grp_ids: list[int] = []
    instance_ids: list[int] = []
    primary_type = actions[0].get("actionType") or "?"
    for a in actions:
        atype = (a.get("actionType") or "?").replace("ActionType_", "")
        gid = a.get("grpId")
        iid = a.get("instanceId")
        if gid is not None:
            grp_ids.append(int(gid))
        if iid is not None:
            instance_ids.append(int(iid))
        if gid is not None:
            parts.append(f"{atype}({gid})")
        else:
            parts.append(atype)
    summary = ", ".join(parts)
    return GroundTruth(
        kind="ActionsAvailable",
        summary=summary,
        action_type=primary_type,
        grp_ids=grp_ids,
        instance_ids=instance_ids,
        raw={"actions": actions},
    )


def _extract_declare_attackers(req: dict, resp: dict) -> Optional[GroundTruth]:
    """Ground truth = the `attackers` set on the request itself.

    The wire-level flow: the player toggles attackers via PerformActionResp
    BEFORE this request fires. By the time DeclareAttackersReq arrives,
    `attackers` already reflects the player's intended set; the response is
    a thin commit (autoDeclare or empty SubmitAttackersReq).
    """
    da = req.get("declareAttackersReq") or {}
    chosen = da.get("attackers") or []
    qualified = da.get("qualifiedAttackers") or []
    instance_ids = sorted(
        int(a.get("attackerInstanceId")) for a in chosen if a.get("attackerInstanceId") is not None
    )
    qual_ids = sorted(
        int(a.get("attackerInstanceId")) for a in qualified if a.get("attackerInstanceId") is not None
    )
    if instance_ids:
        summary = f"Attack with {instance_ids}"
    else:
        summary = "No attack"
    raw = {"chosen": chosen, "qualified_ids": qual_ids}
    return GroundTruth(
        kind="DeclareAttackers",
        summary=summary,
        action_type="ActionType_Attack" if instance_ids else "ActionType_NoAttack",
        instance_ids=instance_ids,
        raw=raw,
    )


def _extract_declare_blockers(req: dict, resp: dict) -> Optional[GroundTruth]:
    """Ground truth = the `blockers` field on the request.

    Each entry: {blockerInstanceId, attackerInstanceIds, maxAttackers}. By
    the time the request arrives, the player has finished toggling block
    assignments — the response is a thin commit.
    """
    db = req.get("declareBlockersReq") or {}
    blockers = db.get("blockers") or []
    instance_ids = sorted(
        {int(b["blockerInstanceId"]) for b in blockers if b.get("blockerInstanceId") is not None}
    )
    if not blockers:
        summary = "No blocks"
    else:
        pairs = []
        for b in blockers:
            bid = b.get("blockerInstanceId")
            atks = b.get("attackerInstanceIds") or []
            if atks:
                pairs.append(f"{bid}->{atks[0]}")
            else:
                pairs.append(f"{bid}->none")
        summary = "Blocks: " + ", ".join(pairs)
    return GroundTruth(
        kind="DeclareBlockers",
        summary=summary,
        action_type="ActionType_Block" if blockers else "ActionType_NoBlock",
        instance_ids=instance_ids,
        raw={"blockers": blockers},
    )


def _extract_mulligan(req: dict, resp: dict) -> Optional[GroundTruth]:
    sub = resp.get("mulliganResp") or {}
    decision = sub.get("decision") or ""
    keep = decision == "MulliganOption_AcceptHand"
    summary = "Keep" if keep else "Mulligan"
    return GroundTruth(
        kind="Mulligan",
        summary=summary,
        keep=keep,
        action_type="ActionType_Keep" if keep else "ActionType_Mulligan",
        raw=sub,
    )


def _extract_search(req: dict, resp: dict) -> Optional[GroundTruth]:
    sub = resp.get("searchResp") or {}
    found = [int(x) for x in (sub.get("itemsFound") or [])]
    summary = f"Search found {found}" if found else "Search: nothing"
    return GroundTruth(kind="Search", summary=summary, grp_ids=found, raw=sub)


def _extract_select_targets(req: dict, resp: dict) -> Optional[GroundTruth]:
    sub = resp.get("selectTargetsResp") or {}
    # SelectTargetsResp has a single 'target' (singular) with .targets[].
    targets = ((sub.get("target") or {}).get("targets")) or []
    instance_ids = [int(t.get("targetInstanceId")) for t in targets if t.get("targetInstanceId") is not None]
    summary = f"Target {instance_ids}" if instance_ids else "No targets"
    return GroundTruth(kind="SelectTargets", summary=summary, instance_ids=instance_ids, raw=sub)


def _extract_optional_action(req: dict, resp: dict) -> Optional[GroundTruth]:
    sub = resp.get("optionalActionResp") or {}
    decision = sub.get("decision")
    chose_yes = decision in ("OptionalActionDecision_Yes", "Yes", True, 1)
    summary = "Yes" if chose_yes else "No"
    return GroundTruth(
        kind="OptionalAction",
        summary=summary,
        action_type="ActionType_OptionalYes" if chose_yes else "ActionType_OptionalNo",
        raw=sub,
    )


def _extract_select_n(req: dict, resp: dict) -> Optional[GroundTruth]:
    sub = resp.get("selectNresp") or resp.get("selectNResp") or {}
    selections = sub.get("selections") or sub.get("selectionIds") or []
    summary = f"SelectN: {selections}" if selections else "SelectN: empty"
    return GroundTruth(kind="SelectN", summary=summary, grp_ids=[int(x) for x in selections], raw=sub)


_EXTRACTORS = {
    "GREMessageType_ActionsAvailableReq": _extract_actions_available,
    "GREMessageType_DeclareAttackersReq": _extract_declare_attackers,
    "GREMessageType_DeclareBlockersReq": _extract_declare_blockers,
    "GREMessageType_MulliganReq": _extract_mulligan,
    "GREMessageType_SearchReq": _extract_search,
    "GREMessageType_SelectTargetsReq": _extract_select_targets,
    "GREMessageType_OptionalActionMessage": _extract_optional_action,
    "GREMessageType_SelectNReq": _extract_select_n,
}


def pair_request_response(messages: list[ReplayMessage]) -> dict[int, ReplayMessage]:
    """Build a {msg_id -> response} map from OUT messages with `respId`.

    Each OUT message except for client-initiated UI traffic carries a
    `respId` that points at the request it answered. We use that as the
    canonical pairing.
    """
    by_resp_id: dict[int, ReplayMessage] = {}
    for m in messages:
        if m.direction != "OUT":
            continue
        rid = m.payload.get("respId")
        if rid is None:
            continue
        try:
            by_resp_id[int(rid)] = m
        except (TypeError, ValueError):
            continue
    return by_resp_id


def extract_decisions(messages: list[ReplayMessage]) -> list[Decision]:
    """Walk the replay and produce a Decision per request message."""
    out: list[Decision] = []
    by_resp = pair_request_response(messages)
    idx = 0
    for m in messages:
        if not m.is_request:
            continue
        msg_id = m.msg_id
        resp = by_resp.get(msg_id) if msg_id is not None else None
        truth = None
        extractor = _EXTRACTORS.get(m.msg_type)
        if extractor is not None and resp is not None:
            try:
                truth = extractor(m.payload, resp.payload)
            except Exception as exc:  # noqa: BLE001
                truth = GroundTruth(
                    kind=m.msg_type.replace("GREMessageType_", ""), summary=f"<extract error: {exc}>"
                )
        out.append(Decision(index=idx, request=m, response=resp, ground_truth=truth))
        idx += 1
    return out


def main():
    """CLI: dump decisions + ground truth for a single replay."""
    import argparse
    from pathlib import Path

    from .reader import parse_replay_path

    p = argparse.ArgumentParser(description="Dump decision + ground truth")
    p.add_argument("path", type=Path)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument(
        "--kinds",
        default=None,
        help="Comma-separated request types to keep (e.g. ActionsAvailable,DeclareAttackers)",
    )
    args = p.parse_args()

    meta, messages = parse_replay_path(args.path)
    keeps = set((args.kinds or "").split(",")) - {""}
    decisions = extract_decisions(messages)
    if keeps:
        decisions = [d for d in decisions if d.ground_truth and d.ground_truth.kind in keeps]
    print(f"=== {args.path.name} === local={meta.local_screen_name} opp={meta.opponent_screen_name}")
    print(f"decisions: {len(decisions)} (showing first {args.limit})\n")
    for d in decisions[: args.limit]:
        gt = d.ground_truth
        kind = gt.kind if gt else "<no-truth>"
        sm = gt.summary if gt else "<no-response>"
        print(f"[{d.index:3d}] msgId={d.request.msg_id:>4}  {kind:18s} -> {sm}")


if __name__ == "__main__":
    main()
