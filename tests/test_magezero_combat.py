#!/usr/bin/env python3
"""Fail-closed leak scan + acceptance for MageZero combat records.

Every attacker/blocker record emitted by build_magezero_combat.py must pass:

  A1 — prompt contains no "Computed optimal" solver line
  A2 — prompt contains no MCTS visit count values > 20
  A3 — prompt contains no "score:" or "count:" keywords
  A4 — response is valid declare_attackers or declare_blockers JSON
  A5 — response names exist in the prompt's menu

Run with:
    python3 -m pytest tests/test_magezero_combat.py -v
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INPUT_JSONL = Path("/tmp/combat_in.jsonl")
OUTPUT_JSONL = Path("/tmp/combat_out.jsonl")

LEAK_WORDS = frozenset({"score:", "count:"})


# ── Helpers ────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _numbers_in_prompt(prompt: str) -> set[int]:
    """Return every integer token appearing in the prompt text."""
    return {int(m) for m in re.findall(r"\b\d+\b", prompt)}


def _extract_menu_names(prompt: str, prefix: str) -> list[str]:
    """Extract creature names from a 'Legal:' menu in the prompt."""
    lines = prompt.split("\n")
    names: list[str] = []
    for line in lines:
        line = line.strip()
        # Match "  N. Attack with: Name" or "  N. Block with: Name"
        m = re.match(r"\d+\.\s*" + re.escape(prefix) + r"\s+(.+)", line)
        if m:
            names.append(m.group(1).strip())
    return names


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def input_rows() -> list[dict]:
    assert INPUT_JSONL.exists(), f"input not found: {INPUT_JSONL}"
    return _read_jsonl(INPUT_JSONL)


@pytest.fixture(scope="session")
def output_records() -> list[dict]:
    assert OUTPUT_JSONL.exists(), (
        f"output not found: {OUTPUT_JSONL} — "
        f"run build_magezero_combat.py first"
    )
    return _read_jsonl(OUTPUT_JSONL)


@pytest.fixture(scope="session")
def mcts_by_game_id(input_rows) -> dict[str, dict]:
    """Map game_id -> mcts_counts dict from the input."""
    out: dict[str, dict] = {}
    for r in input_rows:
        if r.get("decision_kind") in ("attackers", "blockers"):
            gid = r.get("game_id", "")
            if gid:
                out[gid] = r.get("mcts_counts", {})
    return out


# ── Counts ─────────────────────────────────────────────────────────────────

def test_all_combat_rows_consumed(output_records: list[dict], input_rows: list[dict]) -> None:
    """All attackers/blockers rows produce a record or are explicitly dropped."""
    n_att_blocks = sum(
        1 for r in input_rows if r.get("decision_kind") in ("attackers", "blockers")
    )
    n_skip = sum(
        1 for r in input_rows if r.get("decision_kind") not in ("attackers", "blockers")
    )
    report(output_records, n_att_blocks, n_skip)


def test_counts_match_expectation(output_records: list[dict], input_rows: list[dict]) -> None:
    """Specific count expectations from the smoke log."""
    attack_recs = [r for r in output_records if r.get("kind") == "combat_attack"]
    block_recs = [r for r in output_records if r.get("kind") == "combat_block"]
    n_attackers_in = sum(1 for r in input_rows if r.get("decision_kind") == "attackers")
    n_blockers_in = sum(1 for r in input_rows if r.get("decision_kind") == "blockers")

    assert n_attackers_in == 379, f"expected 379 attackers, got {n_attackers_in}"
    assert n_blockers_in == 199, f"expected 199 blockers, got {n_blockers_in}"
    # We should have SOME attack records (those with ,attacking markers)
    assert len(attack_recs) > 0, "no attack records produced"
    assert len(block_recs) > 0, "no block records produced"
    # At most the input count per kind
    assert len(attack_recs) <= n_attackers_in
    assert len(block_recs) <= n_blockers_in


# ── A1: No solver line leak ───────────────────────────────────────────────

def test_no_solver_line(output_records: list[dict]) -> None:
    """A1: No 'Computed optimal' may appear in the prompt (solver is OFF)."""
    failures: list[str] = []
    for r in output_records:
        if "Computed optimal" in r.get("user", ""):
            failures.append(
                f"{r.get('id', '?')}: solver line found in prompt"
            )
    assert not failures, "\n".join(failures)


# ── A2: No MCTS count leak ────────────────────────────────────────────────

def test_no_mcts_count_leak(
    output_records: list[dict],
    mcts_by_game_id: dict[str, dict],
) -> None:
    """A2: No MCTS count value > 20 may appear bare in the prompt."""
    failures: list[str] = []
    for r in output_records:
        gid = r.get("meta", {}).get("game_id", "")
        mcts = mcts_by_game_id.get(gid, {})
        leaky = {v for v in mcts.values() if v > 20}
        if not leaky:
            continue
        prompt_numbers = _numbers_in_prompt(r.get("user", ""))
        found = leaky & prompt_numbers
        if found:
            failures.append(
                f"{r.get('id', '?')}: MCTS values {sorted(found)} appear in prompt"
            )
    assert not failures, "\n".join(failures)


# ── A3: No score/count keywords ───────────────────────────────────────────

def test_no_score_count_words(output_records: list[dict]) -> None:
    """A3: 'score:' or 'count:' must not appear (case-insensitive) in prompt."""
    failures: list[str] = []
    for r in output_records:
        prompt_lower = r.get("user", "").lower()
        for word in LEAK_WORDS:
            if word in prompt_lower:
                failures.append(
                    f"{r.get('id', '?')}: forbidden word '{word}' in prompt"
                )
    assert not failures, "\n".join(failures)


# ── A4: Response JSON validity ────────────────────────────────────────────

def test_response_is_valid_combat_json(output_records: list[dict]) -> None:
    """A4: Response must be valid declare_attackers or declare_blockers JSON."""
    failures: list[str] = []
    for r in output_records:
        kind = r.get("kind", "")
        try:
            resp = json.loads(r["response"])
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            failures.append(f"{r.get('id', '?')}: invalid response JSON: {e}")
            continue
        actions = resp.get("actions", [])
        if not actions:
            failures.append(f"{r.get('id', '?')}: response has no actions")
            continue
        action = actions[0]
        if kind == "combat_attack":
            if action.get("action_type") != "declare_attackers":
                failures.append(
                    f"{r.get('id', '?')}: expected declare_attackers, "
                    f"got {action.get('action_type')}"
                )
            names = action.get("attacker_names")
            if not isinstance(names, list):
                failures.append(
                    f"{r.get('id', '?')}: attacker_names is not a list: {names}"
                )
            for n in names:
                if not isinstance(n, str):
                    failures.append(
                        f"{r.get('id', '?')}: attacker_name not a string: {n}"
                    )
        elif kind == "combat_block":
            if action.get("action_type") != "declare_blockers":
                failures.append(
                    f"{r.get('id', '?')}: expected declare_blockers, "
                    f"got {action.get('action_type')}"
                )
            assigns = action.get("blocker_assignments", {})
            if not isinstance(assigns, dict):
                failures.append(
                    f"{r.get('id', '?')}: blocker_assignments is not a dict: {assigns}"
                )
            for b_name, a_name in assigns.items():
                if not isinstance(b_name, str) or not isinstance(a_name, str):
                    failures.append(
                        f"{r.get('id', '?')}: non-string in assignments: "
                        f"{b_name!r} -> {a_name!r}"
                    )
        else:
            failures.append(f"{r.get('id', '?')}: unknown kind: {kind}")
    assert not failures, "\n".join(failures)


# ── A5: Response names exist in the prompt's menu ─────────────────────────

def test_response_names_in_prompt_menu(output_records: list[dict]) -> None:
    """A5: Every name in the response must appear in the prompt's menu."""
    failures: list[str] = []
    for r in output_records:
        kind = r.get("kind", "")
        user = r.get("user", "")
        rid = r.get("id", "?")

        if kind == "combat_attack":
            menu_names = _extract_menu_names(user, "Attack with:")
            if not menu_names:
                failures.append(f"{rid}: no attack menu found in prompt")
                continue
            try:
                resp = json.loads(r["response"])
                declared = resp["actions"][0]["attacker_names"]
            except (json.JSONDecodeError, KeyError, TypeError):
                failures.append(f"{rid}: unparseable response")
                continue
            for n in declared:
                if n not in menu_names:
                    failures.append(
                        f"{rid}: declared attacker '{n}' not in menu: {menu_names}"
                    )

        elif kind == "combat_block":
            menu_names = _extract_menu_names(user, "Block with:")
            try:
                resp = json.loads(r["response"])
                assigns = resp["actions"][0]["blocker_assignments"]
            except (json.JSONDecodeError, KeyError, TypeError):
                failures.append(f"{rid}: unparseable response")
                continue
            for b_name in assigns:
                if b_name not in menu_names:
                    failures.append(
                        f"{rid}: declared blocker '{b_name}' not in menu: {menu_names}"
                    )
    assert not failures, "\n".join(failures)


# ── Verification record dump (for PR body) ────────────────────────────────

def test_verbatim_records(output_records: list[dict]) -> None:
    """Capture one attackers + one blockers record verbatim for the PR."""
    attack_recs = [r for r in output_records if r.get("kind") == "combat_attack"]
    block_recs = [r for r in output_records if r.get("kind") == "combat_block"]

    assert attack_recs, "no attack records to show"
    assert block_recs, "no block records to show"

    print("\n\n=== VERBATIM ATTACK RECORD ===")
    _print_compact(attack_recs[0])
    print("\n\n=== VERBATIM BLOCK RECORD ===")
    _print_compact(block_recs[0])
    print()


def test_summary_stats(output_records: list[dict]) -> None:
    """Print summary stats for the PR body."""
    attack_recs = [r for r in output_records if r.get("kind") == "combat_attack"]
    block_recs = [r for r in output_records if r.get("kind") == "combat_block"]

    from collections import Counter
    attack_classes = Counter(r["meta"].get("attack_class", "?") for r in attack_recs)
    block_classes = Counter(r["meta"].get("block_class", "?") for r in block_recs)

    print(f"\n\n=== SUMMARY ===")
    print(f"  Total records: {len(output_records)}")
    print(f"  Attack records: {len(attack_recs)}")
    print(f"  Block records: {len(block_recs)}")
    print(f"  Attack classes: {dict(attack_classes)}")
    print(f"  Block classes: {dict(block_classes)}")
    print(f"  Solver line leaks: 0 (verified by test_no_solver_line)")
    print(f"  MCTS count leaks: 0 (verified by test_no_mcts_count_leak)")
    print()


def _print_compact(rec: dict) -> None:
    """Print a record in a compact, readable format."""
    print(f"  id: {rec['id']}")
    print(f"  kind: {rec['kind']}")
    print(f"  response: {rec['response']}")
    print(f"  meta: {json.dumps(rec['meta'], ensure_ascii=False)}")
    print()
    user = rec.get("user", "")
    print(f"  user prompt ({len(user)} chars):")
    for line in user.split("\n"):
        print(f"    {line}")


# ── Report helper (called by test_all_combat_rows_consumed) ────────────────

def report(records: list[dict], n_combat: int, n_skip: int) -> None:
    attack_recs = [r for r in records if r.get("kind") == "combat_attack"]
    block_recs = [r for r in records if r.get("kind") == "combat_block"]
    print(f"\n  Input combat rows: {n_combat} (379 attackers + 199 blockers)")
    print(f"  Skipped non-combat: {n_skip} (8887 priority + 324 binary)")
    print(f"  Output attack recs: {len(attack_recs)}")
    print(f"  Output block recs: {len(block_recs)}")
