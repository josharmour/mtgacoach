"""Regression tests for neural response validation (task 02).

Exercises the reported failure modes end-to-end:
- NaN value mapped to 0.98 win probability by the old clamp logic,
- missing/extra/truncated rows accepted and indexed blindly,
- 127/129-wide or non-finite policy heads flowing into ranking,
- 503/504 and malformed MessagePack reaching ingestion.

The "server" is a real local HTTP server speaking the msgpack protocol, so the
client's transport + validation layers run for real. Evaluator ingestion is
tested through MCTSEvaluator._apply_magezero_lookahead with crafted batches.
Ephemeral localhost sockets only - the running RL fleet is untouched.
"""

from __future__ import annotations

import http.server
import math
import threading
from typing import Any

import msgpack
import pytest

from arenamcp import magezero_client as mc
from arenamcp.magezero_client import MageZeroClient
from arenamcp.mcts_evaluator import MCTSEvaluator

WIDTH = 128


def _policy(head: int = WIDTH, fill: float = 0.0, mutate: Any = None) -> list[Any]:
    row = [fill] * head
    if mutate is not None:
        row[0] = mutate
    return row


def _valid_row(
    request_index: int | None = 0,
    value: Any = 0.2,
    width: int = WIDTH,
    include_index: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "value": value,
        "policy_player": _policy(width),
        "policy_opponent": _policy(width),
    }
    if include_index and request_index is not None:
        row["request_index"] = request_index
    return row


class _EvalHandler(http.server.BaseHTTPRequestHandler):
    """Programmable /evaluate endpoint speaking msgpack."""

    behavior: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not self.path.startswith("/evaluate"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = self.behavior.get("status", 200)
        self.send_response(status)
        self.send_header("Content-Type", "application/x-msgpack")
        self.end_headers()
        if status == 200:
            self.wfile.write(self.behavior["body"](body))
        else:
            self.wfile.write(b"service unavailable")

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def eval_server():
    handler = type("EvalHandler2", (_EvalHandler,), {"behavior": {}})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url, handler.behavior
    srv.shutdown()


@pytest.fixture
def gated_client(monkeypatch: pytest.MonkeyPatch, eval_server):
    """Health-gated client pointed at the local /evaluate replica."""
    url, behavior = eval_server
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))
    monkeypatch.setattr(MageZeroClient, "_active_host", url)
    MageZeroClient._reject_reason = None
    return url, behavior


@pytest.fixture(autouse=True)
def _reset_state():
    MageZeroClient.reset_health_cache()
    MCTSEvaluator.reset_cache()
    yield
    MCTSEvaluator.reset_cache()


_GATED_STATE: dict[str, Any] = {
    "local_seat_id": 1,
    "turn": {"turn_number": 3, "phase": "Phase_Main1", "active_player": 1, "priority_player": 1},
    "players": [
        {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 0, "mana_pool": {"U": 2}},
        {"seat_id": 2, "is_local": False, "life_total": 18, "cards_in_hand": 3},
    ],
    "battlefield": [
        {"name": "Malcolm, Alluring Scoundrel", "controller_seat_id": 1, "owner_seat_id": 1,
         "power": 2, "toughness": 1, "is_tapped": False, "type_line": "Legendary Creature — Siren Pirate"},
        {"name": "Island", "controller_seat_id": 1, "owner_seat_id": 1,
         "type_line": "Basic Land — Island", "is_tapped": False},
    ],
    "hand": [
        {"name": "Island", "controller_seat_id": 1, "owner_seat_id": 1, "type_line": "Basic Land — Island"},
        {"name": "Spell Pierce", "controller_seat_id": 1, "owner_seat_id": 1,
         "type_line": "Instant", "mana_cost": "{U}"},
    ],
}


def test_nan_value_rejected_not_mapped_to_098(gated_client):
    """Regression: old client mapped NaN value -> win_probability 0.98."""
    _url, behavior = gated_client

    def body(raw: bytes) -> bytes:
        return msgpack.packb([_valid_row(value=float("nan"))])

    behavior["body"] = body
    result = MageZeroClient.evaluate(_GATED_STATE)
    assert result is None
    reason = MageZeroClient.last_reject_reason()
    assert reason is not None and "finite" in reason.lower() or "value" in reason, reason


def test_value_out_of_contract_rejected(gated_client):
    """Values outside [-1, 1] break the value contract and are rejected whole."""
    _url, behavior = gated_client
    behavior["body"] = lambda raw: msgpack.packb([_valid_row(value=1.5)])
    assert MageZeroClient.evaluate(_GATED_STATE) is None
    assert MageZeroClient.last_reject_reason() is not None


def test_short_policy_head_rejected(gated_client):
    """127-wide heads must not reach ranking (old code indexed up to 128 blindly)."""
    _url, behavior = gated_client
    behavior["body"] = lambda raw: msgpack.packb([_valid_row(width=127)])
    assert MageZeroClient.evaluate(_GATED_STATE) is None
    assert "policy" in MageZeroClient.last_reject_reason()


def test_nonfinite_policy_head_rejected(gated_client):
    _url, behavior = gated_client
    head = _policy(mutate=float("inf"))
    behavior["body"] = lambda raw: msgpack.packb(
        [{"value": 0.2, "policy_player": head, "policy_opponent": _policy()}]
    )
    assert MageZeroClient.evaluate(_GATED_STATE) is None


def test_batch_7_of_8_roots_rejected(gated_client):
    """A truncated batch (7 of 8 requested items) is rejected as a whole."""
    _url, behavior = gated_client
    n_items = 9  # 8 root + 1 afterstate row set

    def body(raw: bytes) -> bytes:
        req = msgpack.unpackb(raw, raw=False)
        n_req = len(req["indices"])  # any non-empty request
        del n_req
        # one row short on purpose
        return msgpack.packb([_valid_row(request_index=i) for i in range(n_items - 1)])

    behavior["body"] = body
    items = [(_GATED_STATE, None) for _ in range(n_items)]
    out = MageZeroClient.evaluate_batch(items)
    assert out is None
    assert "count" in MageZeroClient.last_reject_reason()


def test_batch_extra_row_rejected_not_dropped(gated_client):
    """An extra response row must be rejected, not silently dropped."""
    _url, behavior = gated_client
    n_items = 9

    behavior["body"] = lambda raw: msgpack.packb(
        [_valid_row(request_index=i) for i in range(n_items + 1)]
    )
    items = [(_GATED_STATE, None) for _ in range(n_items)]
    assert MageZeroClient.evaluate_batch(items) is None
    assert "count" in MageZeroClient.last_reject_reason()


def test_batch_reorder_with_request_index_rejected(gated_client):
    """Rows echoing wrong request_index (secret reorder) are rejected."""
    _url, behavior = gated_client
    n_items = 9
    rows = [_valid_row(request_index=i) for i in range(n_items)]
    rows[0], rows[1] = rows[1], rows[0]  # swap echoes

    behavior["body"] = lambda raw: msgpack.packb(rows)
    items = [(_GATED_STATE, None) for _ in range(n_items)]
    assert MageZeroClient.evaluate_batch(items) is None
    assert "order" in MageZeroClient.last_reject_reason()


def test_http_503_and_504_rejected_with_reason(gated_client):
    _url, behavior = gated_client
    behavior["status"] = 503
    assert MageZeroClient.evaluate(_GATED_STATE) is None
    assert "503" in (MageZeroClient.last_reject_reason() or "")
    behavior["status"] = 504
    assert MageZeroClient.evaluate(_GATED_STATE) is None
    assert "504" in (MageZeroClient.last_reject_reason() or "")


def test_malformed_msgpack_rejected(gated_client):
    _url, behavior = gated_client
    behavior["body"] = lambda raw: b"\xc3this is not msgpack"
    items = [(_GATED_STATE, None)]
    assert MageZeroClient.evaluate_batch(items) is None
    reason = MageZeroClient.last_reject_reason() or ""
    assert ("transport-error" in reason) or ("not-a-list" in reason), reason


def test_empty_results_rejected(gated_client):
    _url, behavior = gated_client
    behavior["body"] = lambda raw: msgpack.packb([])
    assert MageZeroClient.evaluate(_GATED_STATE) is None
    assert "count" in MageZeroClient.last_reject_reason()


def test_valid_single_result_accepted(gated_client):
    _url, behavior = gated_client
    behavior["body"] = lambda raw: msgpack.packb([_valid_row(value=-0.4)])
    result = MageZeroClient.evaluate(_GATED_STATE)
    assert result is not None
    assert result["value"] == -0.4
    assert abs(result["win_probability"] - 0.3) < 0.011  # (v+1)/2
    assert len(result["policy_player"]) == 128
    assert MageZeroClient.last_reject_reason() is None


def test_valid_batch_accepted_with_request_index(gated_client):
    _url, behavior = gated_client
    n = 9

    def body(raw: bytes) -> bytes:
        req = msgpack.unpackb(raw, raw=False)
        n_req = len(req["offsets"])
        return msgpack.packb([_valid_row(request_index=i, value=0.1) for i in range(n_req)])

    behavior["body"] = body
    items = [(_GATED_STATE, None) for _ in range(n)]
    out = MageZeroClient.evaluate_batch(items)
    assert out is not None and len(out) == n
    assert [r["request_index"] for r in out] == list(range(n))


# ------------------- Evaluator intake -------------------


def _mock_batch_factory(row_builder):
    """Build an evaluate_batch side_effect producing len(items) rows."""

    def _side(items, model_id=None):
        return [row_builder(i, items) for i in range(len(items))]

    return _side


def test_evaluator_nan_preserves_heuristic(monkeypatch: pytest.MonkeyPatch):
    """NaN rows in a mocked batch: heuristic result preserved, no exception."""
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))
    monkeypatch.setattr(
        MageZeroClient, "evaluate_batch",
        classmethod(lambda cls, items, model_id=None: [
            _valid_row(request_index=i, value=float("nan")) for i in range(len(items))
        ]),
    )
    payload = MCTSEvaluator.evaluate(dict(_GATED_STATE), force=True)
    assert payload.eval_source == "Tactical Heuristic Lookahead"
    assert payload.expected_opponent_actions == []


def test_evaluator_missing_afterstate_rows_preserves_heuristic(monkeypatch: pytest.MonkeyPatch):
    """Fewer rows than requested items -> reject entire batch, keep heuristic."""
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))

    def short_batch(items, model_id=None):
        return [_valid_row(request_index=i, value=0.2) for i in range(len(items) - 8)]

    monkeypatch.setattr(MageZeroClient, "evaluate_batch", classmethod(lambda cls, *a, **kw: short_batch(*a, **kw)))
    payload = MCTSEvaluator.evaluate(dict(_GATED_STATE), force=True)
    assert payload.eval_source == "Tactical Heuristic Lookahead"


def test_evaluator_gated_zero_rows_preserves_heuristic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))
    monkeypatch.setattr(MageZeroClient, "evaluate_batch", classmethod(lambda cls, *a, **kw: []))
    payload = MCTSEvaluator.evaluate(dict(_GATED_STATE), force=True)
    assert payload.eval_source == "Tactical Heuristic Lookahead"


def test_evaluator_short_heads_preserve_heuristic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))

    def bad_head_rows(items, model_id=None):
        out = []
        for i in range(len(items)):
            row = _valid_row(request_index=i, value=0.2)
            row["policy_player"] = [0.0] * 127  # truncated head
            out.append(row)
        return out

    monkeypatch.setattr(MageZeroClient, "evaluate_batch", classmethod(lambda cls, *a, **kw: bad_head_rows(*a, **kw)))
    payload = MCTSEvaluator.evaluate(dict(_GATED_STATE), force=True)
    assert payload.eval_source == "Tactical Heuristic Lookahead"


def test_evaluator_valid_batch_applies_rl(monkeypatch: pytest.MonkeyPatch):
    """A fully valid batch flows through: RL label, root value from rows, priors."""
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, **kw: True))

    def valid_batch(items, model_id=None):
        out = []
        for i, (state, _hand) in enumerate(items):
            bf_islands = sum(
                1 for c in (state.get("battlefield") or [])
                if isinstance(c, dict) and c.get("name") == "Island"
            )
            val = 0.35 if bf_islands >= 2 else 0.20
            row = _valid_row(request_index=i, value=val)
            row["policy_player"] = _policy(mutate=1.0)
            row["policy_opponent"] = _policy(mutate=2.5)
            out.append(row)
        return out

    from dataclasses import replace
    from arenamcp.model_zoo import ModelZooClient, ModelSelection, ModelSpec
    from test_model_zoo import _v2_manifest
    spec = replace(ModelSpec.from_manifest(_v2_manifest()), is_resident=True, promotion_status="certified")
    selection = ModelSelection(
        model_spec=spec,
        similarity=1.0,
        label="MageZero UWTempo v2",
        is_resident=True,
    )
    monkeypatch.setattr(ModelZooClient, "select", classmethod(lambda cls, *a, **kw: selection))
    monkeypatch.setattr(MageZeroClient, "evaluate_batch", classmethod(lambda cls, *a, **kw: valid_batch(*a, **kw)))
    payload = MCTSEvaluator.evaluate(dict(_GATED_STATE), force=True)
    assert "MageZero UWTempo v2" in payload.eval_source
    # Root rows all 0.20 -> win probability (0.2+1)/2 = 0.60
    assert abs(payload.root_win_probability - 0.60) < 0.02
    land_branch = next((b for b in payload.branches if b.action_type == "land"), None)
    assert land_branch is not None
    assert land_branch.prior_probability > 0.0
    assert land_branch.value_delta > 0.0
    assert abs(land_branch.win_probability - 0.675) < 0.02
    assert len(payload.expected_opponent_actions) > 0
