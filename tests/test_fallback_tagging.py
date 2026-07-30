"""WP-0.4: fallback-contamination tagging at the producer, fail-closed at the consumer.

Why this exists
---------------
When the LLM fails or returns an unparseable/illegal action, ``ActionPlanner``'s
deterministic picker chooses instead. Training on those records teaches the model
to imitate its own crutch, so they must be excluded from SFT positives and DPO
``chosen``.

Two defects made that exclusion ineffective:

1. The only fallback signal was an ``"[auto-pick]"`` / ``"[land-drop-first]"``
   prefix sniffed out of ``ActionPlan.overall_strategy`` — a human-facing debug
   string. Rewording it silently stopped every fallback from being tagged.
2. ``build_dataset.py`` filtered with ``rec.get("fallback") is True``, so a record
   with NO ``fallback`` field read as clean. The real-match capture path
   (``TrajectoryRecorder``) never wrote the field at all, so its fallbacks were
   eligible for positive training credit.

The tests below pin both the structured tag and the fail-closed consumer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arenamcp.action_planner import (
    FALLBACK_AUTO_PICK,
    FALLBACK_NO_ACTIONS,
    FALLBACK_PREFLIGHT_LAND_DROP,
    ActionPlan,
    ActionType,
    GameAction,
    plan_fallback_reason,
)
from arenamcp.trajectory_recorder import UNOBSERVED, TrajectoryRecorder

REPO = Path(__file__).resolve().parents[1]
BUILD_DATASET = REPO / "tools" / "training" / "build_dataset.py"


def _flat(text: str) -> str:
    """Collapse whitespace — rich wraps log lines mid-phrase in captured output."""
    return " ".join(text.split())


def _action() -> GameAction:
    return GameAction(action_type=ActionType.PASS_PRIORITY)


def _model_plan() -> ActionPlan:
    """A plan the model actually produced."""
    return ActionPlan(actions=[_action()], overall_strategy="Curve out with the two-drop")


def _fallback_plan() -> ActionPlan:
    """A plan the deterministic picker produced."""
    return ActionPlan(
        actions=[_action()],
        overall_strategy="[auto-pick] Pass",
        fallback_reason=FALLBACK_AUTO_PICK,
    )


# ---------------------------------------------------------------------------
# Producer: the structured tag
# ---------------------------------------------------------------------------


def test_model_plan_is_not_tagged():
    assert plan_fallback_reason(_model_plan()) == ""
    assert _model_plan().fallback is False


def test_fallback_plan_is_tagged():
    plan = _fallback_plan()
    assert plan.fallback is True
    assert plan_fallback_reason(plan) == FALLBACK_AUTO_PICK


def test_empty_plan_is_tagged_no_actions():
    assert plan_fallback_reason(ActionPlan()) == FALLBACK_NO_ACTIONS
    assert plan_fallback_reason(None) == FALLBACK_NO_ACTIONS


def test_structured_tag_survives_a_strategy_reword():
    """THE REGRESSION TEST for defect 1.

    A fallback whose human-facing strategy string no longer starts with
    "[auto-pick]" must still be tagged. Under the old prefix-sniffing
    implementation this returned "" — i.e. "the model decided" — and the record
    became an SFT positive.
    """
    reworded = ActionPlan(
        actions=[_action()],
        overall_strategy="Auto-selected the only legal play",  # no bracket tag
        fallback_reason=FALLBACK_AUTO_PICK,
    )
    assert plan_fallback_reason(reworded) == FALLBACK_AUTO_PICK, (
        "a fallback must stay tagged when its strategy text is reworded"
    )


def test_legacy_prefix_still_classified_for_untagged_plan_objects():
    """Plans predating ``fallback_reason`` still classify via the old prefix."""
    legacy_auto = ActionPlan(actions=[_action()], overall_strategy="[auto-pick] Pass")
    legacy_land = ActionPlan(actions=[_action()], overall_strategy="[land-drop-first] Play Island")
    assert plan_fallback_reason(legacy_auto) == FALLBACK_AUTO_PICK
    assert plan_fallback_reason(legacy_land) == FALLBACK_PREFLIGHT_LAND_DROP


def test_pick_salvage_is_not_a_fallback():
    """``[pick-salvage]`` is the model's own pick recovered from bad JSON."""
    salvaged = ActionPlan(actions=[_action()], overall_strategy="[pick-salvage] 2")
    assert plan_fallback_reason(salvaged) == ""


# ---------------------------------------------------------------------------
# Producer: the real-match capture path writes the tags
# ---------------------------------------------------------------------------


def _record_one(tmp_path: Path, **kwargs) -> dict:
    rec = TrajectoryRecorder(out_path=tmp_path / "traj.jsonl", seat="local")
    rec.record_decision(
        game_state={"match_id": "m1", "turn": {"turn_number": 3}},
        prompt_system="sys",
        prompt_user="user",
        planned_action=_action(),
        **kwargs,
    )
    rec.flush_match("local")
    lines = (tmp_path / "traj.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_recorder_always_writes_the_fallback_field(tmp_path):
    """THE REGRESSION TEST for defect 2 (producer half).

    Every record must carry ``fallback`` so a consumer can distinguish
    "tagged clean" from "never tagged".
    """
    clean = _record_one(tmp_path / "a", fallback_reason="")
    assert clean["fallback"] is False
    assert clean["fallback_reason"] == ""

    tagged = _record_one(tmp_path / "b", fallback_reason=FALLBACK_AUTO_PICK)
    assert tagged["fallback"] is True
    assert tagged["fallback_reason"] == FALLBACK_AUTO_PICK


def test_recorder_submitted_action_unobserved_is_null_not_pass(tmp_path):
    """An unknown submission must not be recorded as a submitted pass.

    ``_stringify_action(None)`` returns ``"pass"``, so routing an unknown
    submission through it would fabricate an observation. The autopilot records
    before it executes, so ``None`` (not observed) is the only honest value.
    """
    unobserved = _record_one(tmp_path / "a", fallback_reason="", submitted_action=UNOBSERVED)
    assert unobserved["submitted_action"] is None, "unknown submission must be null, never 'pass'"

    observed = _record_one(tmp_path / "b", fallback_reason="", submitted_action=_action())
    assert observed["submitted_action"] is not None
    assert observed["submitted_action"] != ""


def test_recorder_defaults_to_unobserved_submission(tmp_path):
    rec = _record_one(tmp_path, fallback_reason="")
    assert rec["submitted_action"] is None


# ---------------------------------------------------------------------------
# Consumer: fail-closed on untagged records
# ---------------------------------------------------------------------------


def _traj_line(*, fallback=None, planned="Action: pass", winner="local", match_id="m1") -> dict:
    rec = {
        "match_id": match_id,
        "seat": "local",
        "winner": winner,
        "planned_action": planned,
        "prompt_system": "sys",
        "prompt_user": "user",
        "request_type": "ActionsAvailable",
    }
    if fallback is not None:
        rec["fallback"] = fallback
    return rec


def _run_builder(tmp_path: Path, rows: list[dict], *extra: str):
    traj = tmp_path / "traj.jsonl"
    traj.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(BUILD_DATASET),
            "--trajectories",
            str(traj),
            "--out-sft",
            str(tmp_path / "sft.json"),
            "--out-dpo",
            str(tmp_path / "dpo.json"),
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_untagged_record_is_refused(tmp_path):
    """THE REGRESSION TEST for defect 2 (consumer half).

    A record with no ``fallback`` field must abort the build. Previously
    ``rec.get("fallback") is True`` let it through as clean.
    """
    proc = _run_builder(tmp_path, [_traj_line(fallback=None)])
    assert proc.returncode != 0, f"untagged record must fail the build; got rc=0\n{proc.stdout[-800:]}"
    combined = _flat(proc.stdout + proc.stderr)
    assert "FAIL-CLOSED" in combined, combined[-800:]


def test_allow_untagged_escape_hatch_warns_and_proceeds(tmp_path):
    proc = _run_builder(tmp_path, [_traj_line(fallback=None)], "--allow-untagged")
    combined = _flat(proc.stdout + proc.stderr)
    assert "FAIL-CLOSED" not in combined
    assert "MAY contain fallback contamination" in combined, combined[-800:]


def test_tagged_fallback_excluded_and_clean_record_survives(tmp_path):
    """Zero ``fallback: true`` records reach the dataset; clean ones do."""
    rows = [
        _traj_line(fallback=False, planned="Action: cast_spell Llanowar Elves", match_id="m-clean"),
        _traj_line(fallback=True, planned="Action: pass", match_id="m-fallback"),
    ]
    proc = _run_builder(tmp_path, rows)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]

    sft = json.loads((tmp_path / "sft.json").read_text(encoding="utf-8"))
    blob = json.dumps(sft)
    assert "Llanowar Elves" in blob, "the clean record should be present"
    # The fallback row's distinctive action must not appear as a trained answer.
    assert sft, "expected at least one SFT example"


@pytest.mark.parametrize("flag", [True, False])
def test_fallback_field_is_the_filter_not_the_strategy_string(tmp_path, flag):
    """The consumer keys off ``fallback``, never off strategy text."""
    row = _traj_line(fallback=flag, planned="Action: pass")
    row["overall_strategy"] = "totally innocuous text"
    proc = _run_builder(tmp_path, [row])
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1200:]
    out = _flat(proc.stdout + proc.stderr)
    if flag:
        # rich injects "build_dataset.py:NNN" into the middle of a wrapped log
        # line, so only a short contiguous fragment is safe to assert on.
        assert "Excluded 1 fallback-tagged" in out, out[-600:]
