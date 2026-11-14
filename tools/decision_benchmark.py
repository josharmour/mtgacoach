"""Local-only decision corpus and offline planner replay. Never uploads cases."""

from __future__ import annotations

import argparse
import collections
import dataclasses
import gzip
import hashlib
import json
import time
from pathlib import Path

PRIVATE = {
    "match_id",
    "arena_match_id",
    "event_id",
    "install_id",
    "account_id",
    "user_id",
    "player_name",
    "opponent_name",
    "display_name",
    "email",
    "username",
    "screen_name",
}


def scrub(value, identities=(), *, player=False):
    if isinstance(value, dict):
        result = {}
        private_keys = {k.replace("_", "") for k in PRIVATE}
        for key, item in value.items():
            normalized = key.lower().replace("_", "").replace("-", "")
            if normalized in private_keys or (player and normalized in {"name", "id"}):
                continue
            result[key] = scrub(item, identities, player=key == "players")
        return result
    if isinstance(value, list):
        return [scrub(v, identities, player=player) for v in value]
    if isinstance(value, str):
        for identity in identities:
            if identity and len(identity) > 3:
                value = value.replace(identity, "[redacted]")
    return value


def build_cases(paths):
    cases = []
    manifest = []
    counts = collections.Counter()
    for path in sorted(paths):
        raw = path.read_bytes()
        bundle = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        packet = bundle.get("packet") or {}
        match = bundle.get("arena_match_id") or bundle.get("match_id") or packet.get("match_id")
        if not match:
            counts["missing_match"] += 1
            continue
        group = hashlib.sha256(str(match).encode()).hexdigest()
        identities = [
            str(v)
            for obj in (bundle, packet)
            for k, v in obj.items()
            if k.lower() in PRIVATE and isinstance(v, str)
        ]
        for index, decision in enumerate(packet.get("decisions", [])):
            counts["decisions"] += 1
            snapshot = decision.get("state_snapshot")
            pending = decision.get("pending_decision")
            chosen = decision.get("chosen_options")
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("version") != 1
                or not isinstance(snapshot.get("state"), dict)
            ):
                counts["no_v1_snapshot"] += 1
                continue
            if not isinstance(pending, dict) or not isinstance(chosen, list):
                counts["bad_decision"] += 1
                continue
            ids = {o.get("option_id") for o in pending.get("options", []) if isinstance(o, dict)}
            if any(c not in ids for c in chosen):
                counts["unmatched_chosen"] += 1
                continue
            payload = {
                "snapshot": scrub(snapshot, identities),
                "decision": scrub(pending, identities),
                "chosen": chosen,
                "result": bundle.get("result"),
            }
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            case_id = hashlib.sha256((group + digest).encode()).hexdigest()
            cases.append(
                dict(
                    case_id=case_id,
                    match_group=group,
                    split=("test" if int(group[:8], 16) % 5 == 0 else "train"),
                    **payload,
                )
            )
            manifest.append(
                dict(
                    case_id=case_id,
                    source=str(path),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    decision_index=index,
                )
            )
            counts["eligible"] += 1
    unique = {c["case_id"]: c for c in cases}
    counts["duplicates"] = len(cases) - len(unique)
    return list(unique.values()), manifest, dict(counts)


class ModelRequired(BaseException):
    """Escapes production fallback handling rather than manufacturing a reply."""


class NoModel:
    def complete(self, *args, **kwargs):
        raise ModelRequired()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise ModelRequired()


def replay(case):
    from unittest.mock import patch

    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decision_snapshot import game_state_from_snapshot
    from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

    fields = {f.name for f in dataclasses.fields(PendingDecision)}
    data = {k: v for k, v in case["decision"].items() if k in fields}
    data["options"] = tuple(DecisionOption(**o) for o in data["options"])
    data["slots"] = tuple(TargetSlot(**s) for s in data.get("slots", []))
    data["request_id"] = tuple(data["request_id"])
    decision = PendingDecision(**data)
    start = time.perf_counter()
    result = {"case_id": case["case_id"], "status": "withheld", "chosen": []}
    # No cached or remote facts silently change the benchmark input.
    with (
        patch("arenamcp.card_facts.client.facts_for", return_value=None),
        patch("arenamcp.line_search_moves.facts_for", return_value=None),
    ):
        try:
            planner = ActionPlanner(NoModel())
            chosen = planner.plan_decision_options(decision, game_state_from_snapshot(case["snapshot"]))
            result.update(
                status="answered" if decision.selection_is_valid(chosen) else "invalid", chosen=chosen
            )
        except ModelRequired:
            pass
        except Exception as error:
            result.update(status="error", error=type(error).__name__)
    result["seconds"] = time.perf_counter() - start
    return result


def score(chosen, label):
    """A label enumerates acceptable selections, not individual interchangeable IDs."""
    key = sorted(chosen)
    if key in [sorted(selection) for selection in label.get("blunders", [])]:
        return "blunder"
    if key in [sorted(selection) for selection in label.get("acceptable", [])]:
        return "acceptable"
    return "unlabelled_choice"


def diff_reports(before, after):
    old = {r["case_id"]: r for r in before["results"]}
    new = {r["case_id"]: r for r in after["results"]}
    return {
        "only_before": sorted(old.keys() - new.keys()),
        "only_after": sorted(new.keys() - old.keys()),
        "changed": [
            key
            for key in sorted(old.keys() & new.keys())
            if (old[key]["status"], old[key]["chosen"]) != (new[key]["status"], new[key]["chosen"])
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--code-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.bundles:
        cases, manifest, counts = build_cases(args.bundles.glob("*.json.gz"))
        args.cases.mkdir(parents=True, exist_ok=True)
        (args.cases / "cases.json").write_text(json.dumps(cases))
        (args.cases / "private-source-manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(counts))
    if args.code_only:
        cases = json.loads((args.cases / "cases.json").read_text())
        results = [replay(case) for case in cases]
        report = {
            "results": results,
            "counts": dict(collections.Counter(r["status"] for r in results)),
            "seconds": sum(r["seconds"] for r in results),
            "model_layer": "not_run",
        }
        if args.report:
            args.report.write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "results"}))


if __name__ == "__main__":
    main()
