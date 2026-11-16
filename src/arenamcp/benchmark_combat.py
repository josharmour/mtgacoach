"""In-memory comparisons of recorded native combat declarations with today's solver."""

import gzip
import hashlib
import json
from collections import Counter

from arenamcp.combat_receipt import CombatReceipt
from arenamcp.combat_strategy import combat_choice, safe_default_blocks
from arenamcp.combat_targets import recipient_key
from arenamcp.decision_snapshot import game_state_from_snapshot
from arenamcp.snapshot_transport import expand_bundle_snapshots

KINDS = {"DeclareAttackers": "DeclareAttackerRequest", "DeclareBlockers": "DeclareBlockersRequest"}
RECIPIENT_FIELDS = {
    "Player": "playerSystemSeatId",
    "PlanesWalker": "planeswalkerInstanceId",
    "Planeswalker": "planeswalkerInstanceId",
    "Battle": "battleInstanceId",
}


def _identity(value):
    if type(value) is not int or not 0 < value <= 0xFFFFFFFF:
        raise ValueError("missing native identity")
    return value


def _request(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("missing request pair")
    return tuple(_identity(v) for v in value)


def _menu(state, kind):
    ctx = state["decision_context"]
    if (
        ctx.get("_bridge_source") is not True
        or ctx.get("requestType") != kind
        or state.get("_bridge_request_type") != kind
        or ctx.get("requestClass") != KINDS[kind]
        or state.get("_bridge_request_class") != KINDS[kind]
    ):
        raise ValueError("snapshot request mismatch")
    attack = kind == "DeclareAttackers"
    rows = ctx["raw_attackers" if attack else "raw_blockers"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("incomplete native actors")
    actors = set()
    edges = []
    for row in rows:
        actor = _identity(row["attackerInstanceId" if attack else "blockerInstanceId"])
        if actor in actors:
            raise ValueError("duplicate native actor")
        actors.add(actor)
        targets = row["legalDamageRecipients" if attack else "attackerInstanceIds"]
        if not isinstance(targets, list) or len(targets) > 64:
            raise ValueError("invalid native target collection")
        if attack:
            if row.get("selectedDamageRecipient") is not None:
                raise ValueError("preexisting attacker selection")
            for r in targets:
                field = RECIPIENT_FIELDS.get(r.get("type"))
                if field is None or set(r) != {"type", field}:
                    raise ValueError("ambiguous recipient")
                target = _identity(r[field])
                if field == "playerSystemSeatId" and target not in (1, 2):
                    raise ValueError("invalid seat")
                edges.append((actor, r["type"].lower(), target))
        else:
            if row.get("selectedAttackerInstanceIds", []) != []:
                raise ValueError("preexisting blocker selection")
            edges.extend((actor, "attacker", _identity(v)) for v in targets)
    if not edges or len(edges) > 128 or len(edges) != len(set(edges)):
        raise ValueError("empty or duplicate native edge")
    return set(edges)


def compare(row):
    p = row["pending_decision"]
    kind = p["request_type"]
    offered = _request(p["request_id"])
    submitted = _request(row["submitted_request_id"])
    chosen = row["chosen_options"]
    if row.get("outcome") != "executed" or not isinstance(chosen, list):
        raise ValueError("missing executed selection")
    if chosen:
        if submitted[0] != offered[0] + 1 or submitted[1] <= offered[1]:
            raise ValueError("unwitnessed updated selection")
    elif submitted != offered:
        raise ValueError("unwitnessed empty submission")
    snap = row["state_snapshot"]
    if type(snap.get("version")) is not int or snap["version"] != 1:
        raise ValueError("unsupported snapshot")
    state = game_state_from_snapshot(snap)
    state_id = state.get("_bridge_game_state_id")
    if state_id is not None and (type(state_id) is not int or state_id not in (0, offered[0])):
        raise ValueError("snapshot belongs to another request")
    menu = _menu(state, kind)
    receipt = CombatReceipt(kind, offered, submitted, (), ())
    ids = {receipt.option_id(edge): edge for edge in menu}
    options = p["options"]
    if not isinstance(options, list) or len(options) != len(ids):
        raise ValueError("incomplete receipt")
    observed = set()
    for option in options:
        edge = (
            _identity(option["actor_instance_id"]),
            option["recipient_type"],
            _identity(option["recipient_id"]),
        )
        key = option["option_id"]
        if ids.get(key) != edge or key in observed:
            raise ValueError("receipt conflicts with native menu")
        observed.add(key)
    selected = [ids[key] for key in chosen]
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate selection")
    if kind == "DeclareAttackers" and len({edge[0] for edge in selected}) != len(selected):
        raise ValueError("multiple targets for one actor")

    if kind == "DeclareAttackers":
        pick = combat_choice(state)
        if pick is None:
            return "no_forecast"
        forecast = {(actor, *recipient_key(target)) for actor, target in pick.assignments.items()}
    else:
        pick = safe_default_blocks(state)
        if pick is None:
            return "no_forecast"
        forecast = {(actor, "attacker", target) for actor, target in pick[0].items()}
    if not forecast <= menu:
        return "forecast_outside_menu"
    # Only the recorded selection has a native submission witness. Individual
    # offered edges cannot establish that a different joint declaration was legal.
    return "same_submission" if forecast == set(selected) else "different_offered_selection"


def aggregate(paths):
    """Only emit fixed counters. Distinct duplicate archives must agree on the witnessed decision."""
    counts = {kind: Counter() for kind in KINDS}
    seen = {}
    for path in sorted(paths):
        with gzip.open(path) as source:
            bundle = expand_bundle_snapshots(json.load(source))
        packet = bundle.get("packet") or {}
        match = bundle.get("arena_match_id") or bundle.get("match_id") or packet.get("match_id")
        for row in packet.get("decisions") or []:
            p = row.get("pending_decision") or {}
            kind = p.get("request_type")
            if kind not in KINDS:
                continue
            try:
                if p.get("option_family") != "combat_assignment_v1":
                    counts[kind]["missing_native_receipt"] += 1
                    continue
                if not isinstance(match, str) or not match:
                    raise ValueError("missing match")
                state = row["state_snapshot"]["state"]
                if state.get("match_id") != match:
                    raise ValueError("snapshot belongs to another match")
                key = (match, _identity(state["game_number"]), kind, _request(p["request_id"]))
                signature = (
                    p,
                    row.get("chosen_options"),
                    row.get("submitted_request_id"),
                    row.get("state_snapshot"),
                    row.get("outcome"),
                )
                signature = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).digest()
                if key in seen:
                    prev, verdict = seen[key]
                    if signature != prev and verdict != "conflicting_receipts":
                        counts[kind][verdict] -= 1
                        counts[kind]["conflicting_receipts"] += 1
                        seen[key] = (prev, "conflicting_receipts")
                    continue
                try:
                    verdict = compare(row)
                except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                    verdict = "unverified_receipt"
                seen[key] = (signature, verdict)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                verdict = "unverified_receipt"
            counts[kind][verdict] += 1
    return {kind: dict(+count) for kind, count in counts.items()}
