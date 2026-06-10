"""Replay stall-corpus fixtures offline (fable-improvements.md item 5).

Each fixture holds a full PendingDecision plus the planner's answer and
the live outcome. Replaying asserts the *mechanics* hold with no live
game: answers validate against the option set, the deterministic fallback
picks from the same set (never an unpayable cast), and the submission
dispatch builds a bridge call.

Usage:
    python -m tools.eval.replay_stalls                  # ~/.arenamcp/stall_corpus
    python -m tools.eval.replay_stalls <dir-or-file>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import decision_from_dict, submit_option


class RecordingBridge:
    """Accepts every submission and records the dispatch."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit_pass(self):
        self.calls.append(("pass",))
        return True

    def submit_mulligan(self, keep):
        self.calls.append(("mulligan", keep))
        return True

    def submit_action_by_index(self, idx):
        self.calls.append(("action", idx))
        return True

    def submit_targets(self, iid):
        self.calls.append(("targets", iid))
        return True

    def submit_selection(self, ids):
        self.calls.append(("selection", ids))
        return True


def replay_fixture(data: dict[str, Any]) -> dict[str, Any]:
    """Replay one fixture; returns {ok, errors, picked, dispatch}."""
    errors: list[str] = []
    decision = decision_from_dict(data["pending_decision"])
    if not decision.options:
        return {"ok": False, "errors": ["fixture has no options"], "picked": []}

    valid = decision.option_ids()
    answered = [
        oid
        for oid in (data.get("planner_answer") or {}).get("option_ids") or []
        if oid in valid
    ]
    picked = answered or ActionPlanner.deterministic_option_pick(decision)

    if not picked:
        errors.append("no pick produced from a non-empty option set")
    if any(oid not in valid for oid in picked):
        errors.append(f"pick {picked} escaped the option set")
    for oid in picked:
        opt = decision.find(oid)
        if opt is not None and opt.payable is False:
            errors.append(f"picked unpayable option {oid} ({opt.label})")

    bridge = RecordingBridge()
    if picked and not submit_option(bridge, decision, picked):
        errors.append("submit_option dispatch failed")
    if picked and not bridge.calls:
        errors.append("dispatch produced no bridge call")

    return {
        "ok": not errors,
        "errors": errors,
        "picked": picked,
        "dispatch": bridge.calls,
        "request_type": decision.request_type,
    }


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path.home() / ".arenamcp" / "stall_corpus"
    files = [target] if target.is_file() else sorted(target.glob("*.json"))
    if not files:
        print(f"no fixtures under {target}")
        return 0
    failures = 0
    for f in files:
        try:
            result = replay_fixture(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"FAIL {f.name}: unreadable ({e})")
            failures += 1
            continue
        if result["ok"]:
            print(f"ok   {f.name}: {result['request_type']} -> {result['picked']}")
        else:
            failures += 1
            print(f"FAIL {f.name}: {'; '.join(result['errors'])}")
    print(f"\n{len(files) - failures}/{len(files)} fixtures replay clean")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
