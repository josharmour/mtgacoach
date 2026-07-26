"""Regression: tools.eval.run must never mislabel a response's backend.

`run()` loops over backends and, for each one, hands a nested `_process_one`
to a ThreadPoolExecutor. That nested function used to *close over* the loop
variables `be` and `client` (ruff B023), so the label written into each record
was resolved at call time from whatever the loop variable happened to point at
— not from the backend whose client actually produced the text.

Today the loop drains `executor.map` before advancing, so the mis-binding is
latent rather than live. That is exactly why it needs a test: the invariant
("the `backend`/`model` on a record identifies the client that produced its
`response`") is currently upheld by an incidental property of the control
flow, and any refactor that hoists the executor out of the loop, or overlaps
backends, silently corrupts every eval number instead of failing.

These tests pin the invariant end-to-end for both the concurrent and the
serial path.
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


class _FakeClient:
    """Stands in for ProxyBackend; echoes which backend answered."""

    def __init__(self, label: str) -> None:
        self.label = label

    def _get_client(self):  # pre-initialization hook called by run()
        return self

    def complete(self, system, user, **kwargs):
        return f"answered-by::{self.label}"


def _write_prompts(path: Path, n: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"id": f"p{i:02d}", "system": "sys", "user": f"user {i}"}) + "\n")


def _read_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _run(tmp_path: Path, monkeypatch, concurrency: int, n_prompts: int = 24):
    monkeypatch.setattr(
        eval_run.BackendSpec,
        "build",
        lambda self: _FakeClient(self.label),
        raising=True,
    )

    prompts = tmp_path / "prompts.jsonl"
    responses = tmp_path / "responses.jsonl"
    _write_prompts(prompts, n_prompts)

    backends = [
        eval_run.BackendSpec(label="be-alpha", model="m-alpha", base_url="http://a/v1", api_key="k"),
        eval_run.BackendSpec(label="be-beta", model="m-beta", base_url="http://b/v1", api_key="k"),
        eval_run.BackendSpec(label="be-gamma", model="m-gamma", base_url="http://c/v1", api_key="k"),
    ]

    eval_run.run(
        prompts_path=prompts,
        responses_path=responses,
        backends=backends,
        concurrency=concurrency,
    )
    return _read_records(responses), backends, n_prompts


@pytest.mark.parametrize("concurrency", [8, 1])
def test_record_backend_label_matches_the_client_that_answered(tmp_path, monkeypatch, concurrency):
    records, backends, n_prompts = _run(tmp_path, monkeypatch, concurrency)

    assert len(records) == len(backends) * n_prompts

    for rec in records:
        # The fake client embeds its own label in the response body, so a
        # record whose `backend` field disagrees with the body was labelled
        # from a stale loop variable.
        assert rec["response"] == f"answered-by::{rec['backend']}", (
            f"record for prompt {rec['prompt_id']} claims backend "
            f"{rec['backend']!r} but was answered by {rec['response']!r}"
        )


@pytest.mark.parametrize("concurrency", [8, 1])
def test_model_field_matches_its_backend(tmp_path, monkeypatch, concurrency):
    records, backends, _ = _run(tmp_path, monkeypatch, concurrency)
    expected = {b.label: b.model for b in backends}

    for rec in records:
        assert rec["model"] == expected[rec["backend"]], (
            f"record for prompt {rec['prompt_id']} pairs backend {rec['backend']!r} "
            f"with model {rec['model']!r}"
        )


def test_process_one_does_not_close_over_the_backend_loop_variables():
    """The discriminating test: `be`/`client` must be bound, not captured.

    The end-to-end tests above pass on the buggy code too, because the loop
    currently drains each backend's executor before advancing — the race has
    no window to open. This one fails on the buggy code: it asserts the
    structural property that ruff B023 encodes, namely that the loop variables
    are *parameters* of the nested function (bound once per iteration) rather
    than free variables resolved from the enclosing scope at call time.
    """
    nested = [
        c for c in eval_run.run.__code__.co_consts if hasattr(c, "co_name") and c.co_name == "_process_one"
    ]
    assert len(nested) == 1, "expected exactly one nested _process_one in run()"
    code = nested[0]

    for name in ("be", "client"):
        assert name not in code.co_freevars, (
            f"_process_one closes over the loop variable {name!r} "
            f"(co_freevars={code.co_freevars}). Threads would resolve it at "
            f"call time, so responses can be recorded under the wrong backend. "
            f"Bind it as a default argument instead."
        )
        assert name in code.co_varnames, (
            f"_process_one no longer binds {name!r} as a parameter; the "
            f"per-iteration binding that prevents backend mislabelling is gone."
        )


def test_every_backend_covers_every_prompt_exactly_once(tmp_path, monkeypatch):
    records, backends, n_prompts = _run(tmp_path, monkeypatch, concurrency=8)

    seen: dict[str, list[str]] = {b.label: [] for b in backends}
    for rec in records:
        seen[rec["backend"]].append(rec["prompt_id"])

    for label, pids in seen.items():
        assert len(pids) == n_prompts, f"{label} produced {len(pids)} records, want {n_prompts}"
        assert len(set(pids)) == n_prompts, f"{label} produced duplicate prompt ids"
