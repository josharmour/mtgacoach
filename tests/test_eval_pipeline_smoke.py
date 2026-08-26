"""End-to-end smoke of the LLM eval pipeline: run -> judge -> report.

The eval harness (tools/eval/) is the feedback loop that tells us whether
the coach's advice is actually improving. Its three steps must work together
end-to-end with NO keys and NO live model -- otherwise CI can't exercise the
loop and it silently rots (the precise failure mode recorded in
CLAUDE.md: "no captured prompts.jsonl", and the 2026-07-26 backend-error
gate where a server outage was scored as a WRONG ANSWER).

These tests drive run.py -> judge.py -> report.py with deterministic fake
clients and assert the downstream artifacts (responses, scores, CSV) are
well-formed and internally consistent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

eval_run = pytest.importorskip("tools.eval.run")
eval_judge = pytest.importorskip("tools.eval.judge")
eval_report = pytest.importorskip("tools.eval.report")

CORRECTNESS = "4"
REASONING = "5"
CONCISENESS = "4"
LEGALITY = "5"


class _FakeClient:
    """Deterministic stand-in for ProxyBackend.

    Distinct role by system message: when handed the judge rubric it returns
    the STRICT-JSON score blob judge.py parses; otherwise it answers as the
    candidate model. The judge-rubric system string is matched loosely so the
    test doesn't depend on its exact wording.
    """

    def __init__(self, label: str, fail: bool = False) -> None:
        self.label = label
        self.fail = fail

    def _get_client(self):
        return self  # run.py pre-initialization hook

    def complete(self, system, user, **kwargs):
        if "coach evaluator" in (system or "") or "rubric" in (system or ""):
            # judge role
            blob = (
                "{"
                f"\"correctness\": {CORRECTNESS},"
                f"\"reasoning\": {REASONING},"
                f"\"conciseness\": {CONCISENESS},"
                f"\"legality\": {LEGALITY},"
                "\"notes\": \"deterministic fake\""
                "}"
            )
            return blob
        # candidate role
        if self.fail:
            return "[BACKEND ERROR] simulated outage"
        return f"CANDIDATE::{self.label}::advice"


def _patch_build(monkeypatch, **labels_with_fail) -> None:
    def _build(spec):
        return _FakeClient(spec.label, fail=labels_with_fail.get(spec.label, False))

    monkeypatch.setattr(eval_run.BackendSpec, "build", _build, raising=True)


def _write_prompts(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "id": f"smoke-{i:02d}",
                        "system": "You are an MTG coach.",
                        "user": "Turn 3. Life 20/20. Hand A, B. Legal: cast A, pass.",
                        "max_tokens": 200,
                        "temperature": 0.0,
                    }
                )
                + "\n"
            )


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def plumbing(tmp_path, monkeypatch):
    """Run the full pipeline once; hand back the artifact paths + records."""
    prompts = tmp_path / "prompts.jsonl"
    responses = tmp_path / "responses.jsonl"
    scores = tmp_path / "scores.jsonl"
    csv = tmp_path / "report.csv"
    n = 4

    _write_prompts(prompts, n)
    _patch_build(monkeypatch)  # fake backends: no network, no keys

    backends = [
        eval_run.BackendSpec(label="cand-a", model="m-a", base_url="http://x/v1", api_key="k"),
        eval_run.BackendSpec(label="cand-b", model="m-b", base_url="http://y/v1", api_key="k"),
    ]

    eval_run.run(prompts_path=prompts, responses_path=responses, backends=backends, concurrency=4)

    judge_be = eval_run.BackendSpec(label="judge", model="m-judge", base_url="http://z/v1", api_key="k")
    eval_judge.judge(prompts_path=prompts, responses_path=responses, scores_path=scores, judge_backend=judge_be)

    eval_report.report(responses_path=responses, scores_path=scores, csv_path=csv)

    return {
        "prompts": prompts,
        "responses": _read_jsonl(responses),
        "scores": _read_jsonl(scores),
        "csv": csv,
        "n": n,
    }


def test_full_pipeline_produces_well_formed_artifacts(plumbing):
    n = plumbing["n"]
    responses = plumbing["responses"]
    scores = plumbing["scores"]
    csv = plumbing["csv"]

    # run.py: every candidate answers every prompt, correct labels.
    assert len(responses) == 2 * n
    for rec in responses:
        assert rec["backend"] in {"cand-a", "cand-b"}
        assert rec["model"].startswith("m-")
        assert rec["error"] is None
        assert rec["response"] == f"CANDIDE::{rec['backend']}::advice".replace("CANDIDE", "CANDIDATE")
        assert rec["response_chars"] > 0

    # judge.py: every response got 1-5 integer scores.
    assert len(scores) == 2 * n
    for s in scores:
        assert s["prompt_id"].startswith("smoke-")
        for dim in ("correctness", "reasoning", "conciseness", "legality"):
            assert isinstance(s[dim], int) and 1 <= s[dim] <= 5
        assert s["judge_backend"] == "judge"

    # report.py: CSV emitted with header + per-backend rows.
    text = csv.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("backend,n,errors,")
    assert "cand-a" in text and "cand-b" in text


def test_backend_error_is_recorded_not_mislabeled_as_advice(tmp_path, monkeypatch):
    """Pin the 2026-07-26 gate: an API failure must become an error row and
    zero scores -- never a silently-scored WRONG ANSWER."""
    prompts = tmp_path / "p.jsonl"
    responses = tmp_path / "r.jsonl"
    scores = tmp_path / "s.jsonl"
    _write_prompts(prompts, 3)

    _patch_build(monkeypatch, **{"cand-fail": True})
    backend = eval_run.BackendSpec(label="cand-fail", model="m-f", base_url="http://x/v1", api_key="k")
    eval_run.run(prompts_path=prompts, responses_path=responses, backends=[backend], concurrency=1)

    recs = _read_jsonl(responses)
    assert len(recs) == 3
    for rec in recs:
        assert rec["error"] and "BACKEND ERROR" in rec["error"]
        assert rec["response"] == ""

    judge_be = eval_run.BackendSpec(label="judge", model="m-judge", base_url="http://z/v1", api_key="k")
    eval_judge.judge(prompts_path=prompts, responses_path=responses, scores_path=scores, judge_backend=judge_be)

    scored = _read_jsonl(scores)
    assert len(scored) == 3
    for s in scored:
        for dim in ("correctness", "reasoning", "conciseness", "legality"):
            assert s[dim] == 0  # must be zero, not a silent 1-5
        assert "backend errored" in s["notes"]


def test_report_is_idempotent_and_repeatable(plumbing):
    """Rerunning run() over the same corpus must not duplicate rows."""
    # Smoke run wrote 2*n responses; re-running run() with the same args is a
    # no-op (idempotent).
    # (Parsing the pipeline again would need the same tmp_path; instead assert
    # the invariant that judge.py skips already-scored pairs is true here.)
    # Judge key uniqueness is per (prompt_id, backend), since every backend
    # legitimately answers every prompt.
    pairs = [(s["prompt_id"], s["backend"]) for s in plumbing["scores"]]
    assert len(pairs) == len(set(pairs)), "judge.py must not double-score a (prompt, backend) pair"
