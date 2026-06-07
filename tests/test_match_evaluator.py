"""Unit tests for arenamcp.match_evaluator.MatchEvaluator."""

from __future__ import annotations

import json

from arenamcp.match_evaluator import MatchEvaluator, _extract_json


SAMPLE_DECISIONS = [
    {"match_id": "m1", "turn": 1, "phase": "Main1", "request_type": "ActionsAvailable",
     "planned_action": "play Mountain"},
    {"match_id": "m1", "turn": 2, "phase": "Main1", "request_type": "ActionsAvailable",
     "planned_action": "cast Monastery Swiftspear"},
    {"match_id": "m1", "turn": 3, "phase": "Combat", "request_type": "Attacker",
     "planned_action": "attack with Monastery Swiftspear"},
]


class _FakeClient:
    """Records the last complete() call and returns a canned response."""

    def __init__(self, response: str):
        self.response = response
        self.model = "fake-model"
        self.calls = []

    def complete(self, system_prompt, user_message, max_tokens=600, temperature=0.2):
        self.calls.append((system_prompt, user_message, max_tokens, temperature))
        return self.response


def _read_jsonl(path):
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


def test_evaluate_writes_record_with_required_fields(tmp_path):
    out = tmp_path / "evals.jsonl"
    payload = {
        "summary": "Aggressive start closed it out.",
        "key_mistakes": ["Held Swiftspear a turn too long"],
        "improvements": ["Deploy one-drops on curve"],
        "self_score": 4,
    }
    client = _FakeClient("```json\n" + json.dumps(payload) + "\n```")
    ev = MatchEvaluator(client, out_path=out)

    rec = ev.evaluate(
        match_id="m1",
        decisions=SAMPLE_DECISIONS,
        result="win",
        winner="local",
        deck_name="Mono Red",
    )

    assert rec is not None
    rows = _read_jsonl(out)
    assert len(rows) == 1
    row = rows[0]
    for field in (
        "match_id", "result", "winner", "deck_name", "n_decisions",
        "summary", "key_mistakes", "improvements", "self_score", "model",
    ):
        assert field in row
    assert row["match_id"] == "m1"
    assert row["result"] == "win"
    assert row["winner"] == "local"
    assert row["deck_name"] == "Mono Red"
    assert row["n_decisions"] == 3
    assert row["self_score"] == 4
    assert row["key_mistakes"] == ["Held Swiftspear a turn too long"]
    assert row["model"] == "fake-model"
    # The user message should reference the deck and a planned action.
    _, user_msg, _, _ = client.calls[0]
    assert "Mono Red" in user_msg
    assert "Swiftspear" in user_msg


def test_evaluate_handles_unparseable_response(tmp_path):
    out = tmp_path / "evals.jsonl"
    client = _FakeClient("Sorry, I cannot produce JSON right now.")
    ev = MatchEvaluator(client, out_path=out)

    rec = ev.evaluate(
        match_id="m2",
        decisions=SAMPLE_DECISIONS,
        result="loss",
        winner="opp",
    )

    # Parse failure must NOT raise and must still write a record with raw text.
    assert rec is not None
    rows = _read_jsonl(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["match_id"] == "m2"
    assert row["self_score"] is None
    assert row["summary"] == ""
    assert "raw" in row
    assert "cannot produce JSON" in row["raw"]


def test_evaluate_no_decisions_returns_none(tmp_path):
    out = tmp_path / "evals.jsonl"
    client = _FakeClient("{}")
    ev = MatchEvaluator(client, out_path=out)
    assert ev.evaluate("m3", [], "win", "local") is None
    assert not out.exists()


def test_evaluate_client_exception_returns_none(tmp_path):
    out = tmp_path / "evals.jsonl"

    class _Boom:
        model = "boom"

        def complete(self, *a, **k):
            raise RuntimeError("backend down")

    ev = MatchEvaluator(_Boom(), out_path=out)
    # Must swallow the backend error and not write anything.
    assert ev.evaluate("m4", SAMPLE_DECISIONS, "loss", "opp") is None
    assert not out.exists()


def test_extract_json_tolerates_prose_and_fences():
    assert _extract_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('prefix {"b": 2} suffix') == {"b": 2}
    assert _extract_json("no json here") is None
