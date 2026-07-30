"""Regression tests for the three silent failures in run_wp3_pipeline.py.

All three shared a shape: the pipeline reported success while doing the wrong
thing, which is the worst failure mode for a training pipeline because a broken
run is indistinguishable from a good one.

1. The leak scan integer-hunted the whole prompt, so the rendered menu's own
   numbering ("  31. Pass") collided with MCTS counts and the scan rejected the
   entire corpus. The menu numbering is the ANSWER MECHANISM — the model must see
   it to emit {"pick": N} — so it cannot be a leak.
2. ``--seed`` was parsed and never plumbed; ``split_by_game`` was called with a
   hardcoded ``seed=7``, so every seed produced a byte-identical split.
3. A typo'd ``--log`` was silently discarded and the run printed
   "=== PIPELINE COMPLETE ===" with 0 decisions and empty split files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "training" / "run_wp3_pipeline.py"

pytest.importorskip("tools.training.run_wp3_pipeline", reason="pipeline module unavailable")

from tools.training import run_wp3_pipeline as P  # noqa: E402

# ---------------------------------------------------------------------------
# 1. Leak scan must not flag the rendered menu numbering
# ---------------------------------------------------------------------------


def _record(user: str) -> dict:
    return {"user": user, "system": "SYS", "response": '{"actions":[{"pick":1}]}'}


def test_leak_scan_ignores_menu_numbering():
    """THE regression: a menu index equal to an MCTS count is not a leak."""
    # MCTS count 31 exists, and the menu happens to have 31 entries.
    raw = [{"mcts_counts": {"Pass": 31, "Play Island": 700}}]
    menu_lines = "\n".join(f"  {i}. Option {i}" for i in range(1, 32))
    rendered = {"test": [_record(f"Legal actions:\n{menu_lines}\n")]}
    P.stage_leak_scan(rendered, raw)  # must not raise


def test_leak_scan_still_catches_a_real_mcts_leak():
    """A high MCTS count OUTSIDE the menu must still fail the scan."""
    raw = [{"mcts_counts": {"Play Island": 700}}]
    rendered = {"test": [_record("Board state: the search visited this line 700 times.\n")]}
    with pytest.raises(SystemExit) as exc:
        P.stage_leak_scan(rendered, raw)
    assert exc.value.code == 42


@pytest.mark.parametrize("word", ["score:", "count:"])
def test_leak_scan_still_catches_forbidden_words(word):
    raw = [{"mcts_counts": {"Pass": 50}}]
    rendered = {"test": [_record(f"Pass {word} 50\n")]}
    with pytest.raises(SystemExit) as exc:
        P.stage_leak_scan(rendered, raw)
    assert exc.value.code == 42


# ---------------------------------------------------------------------------
# 2. --seed must reach split_by_game
# ---------------------------------------------------------------------------


def test_run_pipeline_accepts_and_forwards_seed():
    """Signature-level guard: the seed parameter must exist to be forwardable."""
    import inspect

    params = inspect.signature(P.run_pipeline).parameters
    assert "seed" in params, "run_pipeline must take a seed; a hardcoded 7 made --seed a no-op"
    assert "balance" in params


def test_split_by_game_call_is_not_hardcoded():
    """AST guard: no literal seed= on the split call inside run_pipeline."""
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != "split_by_game":
            continue
        for kw in node.keywords:
            if kw.arg == "seed" and isinstance(kw.value, ast.Constant):
                bad.append(f"seed={kw.value.value!r} hardcoded at line {node.lineno}")
    assert not bad, "; ".join(bad)


# ---------------------------------------------------------------------------
# 3. An unmatched --log must fail, not yield a silently empty corpus
# ---------------------------------------------------------------------------


def test_expand_globs_reports_unmatched(tmp_path):
    real = tmp_path / "real.log"
    real.write_text("x", encoding="utf-8")
    paths, unmatched = P.expand_globs([str(real), str(tmp_path / "typo.log")])
    assert [p.name for p in paths] == ["real.log"]
    assert unmatched == [str(tmp_path / "typo.log")]


def test_cli_refuses_a_typod_log_path(tmp_path):
    """THE regression: this used to print PIPELINE COMPLETE with 0 records."""
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--log",
            str(tmp_path / "definitely-not-here.log"),
            "--outdir",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"expected rc=2, got {proc.returncode}\n{combined[-600:]}"
    assert "matched nothing" in combined
    assert "PIPELINE COMPLETE" not in combined, "a failed run must not report completion"
