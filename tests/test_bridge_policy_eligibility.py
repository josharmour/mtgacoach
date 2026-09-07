#!/usr/bin/env python3
"""Task 10 — policy-label eligibility separated from game outcome.

An otherwise-eligible unknown-outcome priority decision must reach rendered
policy records under ``--outcome all`` (preserved as outcome="unknown",
policy_label="menu.pick", never relabeled), be dropped under ``--outcome
won_only``, and reconcile the manifest counters (raw, filtered, rendered).
Observation/action quality eligibility is tested separately and must stay
drop-reasoned even when outcome handling changes.

All records here are built from SYNTHETIC in-memory decision rows — no
MageZero log parse, no live services, no GPU work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.training import build_magezero_bridge as BRIDGE  # noqa: E402
from tools.training import magezero_filters as FILTERS  # noqa: E402
from tools.training import run_wp3_pipeline as PIPE  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic decision rows (MageZero decisions-JSONL schema)
# ---------------------------------------------------------------------------


def _row(**overrides) -> dict:
    row = {
        "game_id": "gameX.log:Thread-1:001",
        "turn": 5,
        "phase": "PRECOMBAT_MAIN",
        "active_life": 17,
        "opp_life": 14,
        "hand": ["Lightning Strike"],
        "battlefield_self": [{"name": "Mountain", "tapped": False}],
        "battlefield_opp": [],
        "menu": ["Cast Lightning Strike", "Pass"],
        "chosen": "Cast Lightning Strike",
        "mcts_counts": {"Cast Lightning Strike": 900, "Pass": 40},
        "actor": "PlayerA",
        "outcome": "unknown",
        "session": "sess_unknown_test",
        "decision_kind": "priority",
    }
    row.update(overrides)
    return row


def _won_row(**overrides) -> dict:
    return _row(outcome="won", game_id="gameY.log:Thread-1:001", **overrides)


# ---------------------------------------------------------------------------
# Bridge-level eligibility (build_record)
# ---------------------------------------------------------------------------


def test_unknown_outcome_reaches_record_under_all():
    record, reason = BRIDGE.build_record(_row(), outcome_mode="all")
    assert record is not None, f"unknown-outcome row must render under all, reason={reason}"
    meta = record["meta"]
    assert meta["outcome"] == "unknown"
    assert meta["policy_label"] == "menu.pick"


def test_unknown_outcome_dropped_under_won_only():
    record, reason = BRIDGE.build_record(_row(), outcome_mode="won_only")
    assert record is None
    assert reason == "outcome_unknown"


def test_unknown_outcome_never_renamed_won_or_lost():
    for mode in ("all", "won_only"):
        record, _ = BRIDGE.build_record(_row(), outcome_mode=mode)
        if record is not None:
            assert record["meta"]["outcome"] in ("unknown", "won", "lost")
            assert record["meta"]["outcome"] == "unknown"


def test_known_outcome_keeps_outcome_supervision_label():
    record, _ = BRIDGE.build_record(_won_row(), outcome_mode="all")
    assert record is not None
    assert record["meta"]["outcome"] == "won"
    assert record["meta"]["policy_label"] == "outcome.menu.pick"


def test_build_record_rejectsUnknown_mode_explicitly():
    with pytest.raises(ValueError):
        BRIDGE.build_record(_row(), outcome_mode="magic")


def test_unknown_rows_from_literal_row_without_outcome_key():
    """A row with NO outcome key at all defaults to unknown (parser gap) and
    follows the same eligibility rule."""
    row = _row()
    del row["outcome"]
    record, reason = BRIDGE.build_record(row, outcome_mode="all")
    assert record is not None, reason
    assert record["meta"]["outcome"] == "unknown"
    record, reason = BRIDGE.build_record(row, outcome_mode="won_only")
    assert record is None and reason == "outcome_unknown"


def test_bad_observation_rows_remain_rejected_under_all():
    """Observation/action quality eligibility is separate from outcome: a
    malformed menu must still be rejected under --outcome all."""
    bad_menu = _row(menu=["Cast Lightning Strike"])
    record, reason = BRIDGE.build_record(bad_menu, outcome_mode="all")
    assert record is None
    assert reason == "menu_too_small"

    bad_chosen = _row(chosen="Attack with Nothing")
    record, reason = BRIDGE.build_record(bad_chosen, outcome_mode="all")
    assert record is None
    assert reason == "chosen_not_in_menu"


def test_menu_header_guard_still_enforced_on_unknown_records():
    """R1 (production legal-menu shape) is an observation-quality gate and
    must stay enforced for unknown-outcome policy records."""
    row = _row()
    record, _ = BRIDGE.build_record(row, outcome_mode="all")
    assert record is not None
    assert record["user"].count("Legal: (pick by number)") == 1


def test_unknown_record_system_prompt_identity():
    record, _ = BRIDGE.build_record(_row(), outcome_mode="all")
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

    assert record["system"] == AUTOPILOT_SYSTEM_PROMPT


def test_unknown_record_mcts_counts_never_rendered():
    """Leak defense preserved: unknown-outcome policy records must not contain
    mcts counts in the prompt."""
    import re

    record, _ = BRIDGE.build_record(_row(mcts_counts={"Cast Lightning Strike": 4242}), outcome_mode="all")
    nums = set(int(m) for m in re.findall(r"\b\d+\b", record["user"]))
    assert 4242 not in nums
    assert "score:" not in record["user"] and "count:" not in record["user"]


# ---------------------------------------------------------------------------
# Filter-level consistency
# ---------------------------------------------------------------------------


def test_outcome_filter_all_keeps_unknown_rows():
    rows = [_row(), _won_row()]
    kept, dropped = FILTERS.outcome_filter(rows, "all")
    assert dropped == 0 and len(kept) == 2


def test_outcome_filter_won_only_excludes_unknown():
    rows = [_row(), _won_row()]
    kept, dropped = FILTERS.outcome_filter(rows, "won_only")
    assert dropped == 1 and len(kept) == 1 and kept[0]["outcome"] == "won"


# ---------------------------------------------------------------------------
# Render-stage consistency (pipeline-level, no files touched)
# ---------------------------------------------------------------------------


def test_stage_render_unknown_rows_render_under_all():
    splits = {"train": [_row(), _won_row()]}
    drop_counts: dict = {}
    rendered = PIPE.stage_render(splits, drop_counts, {}, include_combat=False, outcome_mode="all")
    assert len(rendered["train"]) == 2
    meta_by_outcome = {r["meta"]["outcome"]: r for r in rendered["train"]}
    assert meta_by_outcome["unknown"]["meta"]["policy_label"] == "menu.pick"
    assert meta_by_outcome["unknown"]["meta"]["outcome"] == "unknown"
    assert meta_by_outcome["won"]["meta"]["policy_label"] == "outcome.menu.pick"


def test_stage_render_unknown_rows_dropped_under_won_only():
    splits = {"train": [_row(), _won_row()]}
    drop_counts: dict = {}
    rendered = PIPE.stage_render(splits, drop_counts, {}, include_combat=False, outcome_mode="won_only")
    assert len(rendered["train"]) == 1
    assert rendered["train"][0]["meta"]["outcome"] == "won"
    assert dict(drop_counts["train"])["outcome_unknown"] == 1


def test_stage_render_rejects_unknown_outcome_mode():
    with pytest.raises(ValueError):
        PIPE.stage_render({"train": [_row()]}, {}, {}, include_combat=False, outcome_mode="nonsense")


# ---------------------------------------------------------------------------
# Manifest counters reconcile raw/filtered/rendered
# ---------------------------------------------------------------------------


def test_render_accounting_reconciles():
    """rendered + dropped_at_render must equal filtered rows; raw = filtered +
    filter_dropped."""
    from collections import Counter

    split_drops = {"train": Counter({"outcome_unknown": 1, "menu_too_small": 1})}
    rendered = {"train": [{} for _ in range(3)]}
    filter_counts = {"drop_single_option": 2, "outcome_filter": 0, "dedupe": 1}
    raw = 10
    acct = PIPE._render_accounting(rendered, split_drops, filter_counts, raw)
    assert acct["rendered_records"] == 3
    assert acct["dropped_at_render"] == 2
    # filter drops = 2 + 0 + 1 = 3; filtered (retained) = raw - filter drops = 7
    assert acct["dropped_by_filters"] == 3


def test_manifest_outcome_mode_and_render_accounting_written(tmp_path):
    outdir = tmp_path / "wp3out"
    outdir.mkdir()
    rendered = {
        "train": [
            {
                "system": "SYS",
                "user": "U",
                "response": "{}",
                "meta": {"outcome": "unknown", "decision_kind": "priority", "session": "s", "menu_size": 2, "policy_label": "menu.pick"},
            }
        ]
    }
    from collections import Counter

    filter_counts = {"drop_single_option": 1, "outcome_filter": 0, "dedupe": 0}
    splits = {"train": [_row()]}
    manifest = PIPE.stage_write(
        rendered,
        outdir,
        filter_counts,
        splits,
        raw_count=2,
        pass_rate=0.0,
        attackers_blockers_total=0,
        elapsed=0.01,
        outcome_mode="all",
        render_drop_counts={"train": Counter({"outcome_unknown": 1})},
    )
    assert manifest["outcome_mode"] == "all"
    assert manifest["render_accounting"]["rendered_records"] == 1
    assert manifest["render_accounting"]["render_drop_reasons"]["outcome_unknown"] == 1
    assert manifest["splits"]["train"]["by_policy_label"] == {"menu.pick": 1}
    # Re-read from disk — immutable on-disk artifact check
    on_disk = json.loads((outdir / "manifest.json").read_text())
    assert on_disk["outcome_mode"] == "all"
    assert on_disk["render_accounting"]["rendered_records"] == 1
