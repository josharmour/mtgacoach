"""Offline regression tests for Batch 3: T1 correlation IDs, B2 model runner, H3 split."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arenamcp.action_planner import ActionPlanner, LLMUnavailableError
from arenamcp.backends.proxy import ProxyBackend, _CallContext, _CallHandle, _valid_correlation_id
from arenamcp.decisions import DecisionOption, PendingDecision


def _load_tool(name: str):
    path = Path(__file__).resolve().parent.parent / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tools.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db = _load_tool("decision_benchmark")
h3 = _load_tool("h3_model_comparison")
um = _load_tool("upgrade_metrics")
br = _load_tool("benchmark_report")
rep = _load_tool("benchmark_repeats")


def _actions_case(case_hex: str) -> dict:
    return {
        "case_id": case_hex,
        "snapshot": {
            "version": 1,
            "state": {
                "local_seat_id": 1,
                "players": [
                    {"seat_id": 1, "is_local": True, "life_total": 20, "mana_pool": {"R": 2}},
                    {"seat_id": 2, "is_local": False, "life_total": 20},
                ],
                "hand": [
                    {
                        "name": "Sorcery One",
                        "instance_id": 10,
                        "grp_id": 101,
                        "mana_cost": "{R}",
                        "type_line": "Sorcery",
                        "oracle_text": "Draw two cards.",
                    },
                    {
                        "name": "Sorcery Two",
                        "instance_id": 11,
                        "grp_id": 102,
                        "mana_cost": "{1}{R}",
                        "type_line": "Sorcery",
                        "oracle_text": "Sorcery Two deals 3 damage to target opponent.",
                    },
                ],
                "battlefield": [],
                "zones": {},
                "turn": {"turn_number": 2, "active_player": 1, "phase": "Phase_Main1"},
            },
        },
        "decision": {
            "request_id": [1, 2],
            "request_type": "ActionsAvailable",
            "min_select": 1,
            "max_select": 1,
            "can_pass": True,
            "can_cancel": False,
            "source_label": "",
            "options": [
                {
                    "option_id": "idx:0",
                    "label": "Cast Sorcery One",
                    "payable": True,
                    "meta": {"actionType": "ActionType_Cast", "instanceId": 10, "grpId": 101},
                },
                {
                    "option_id": "idx:1",
                    "label": "Cast Sorcery Two",
                    "payable": True,
                    "meta": {"actionType": "ActionType_Cast", "instanceId": 11, "grpId": 102},
                },
                {
                    "option_id": "pass",
                    "label": "Pass",
                    "payable": True,
                    "meta": {"actionType": "ActionType_Pass"},
                },
            ],
            "slots": [],
        },
        "chosen": ["idx:1"],
        "result": "loss",
    }


def _hex(ch: str) -> str:
    return ch * 64


# ---------------------------------------------------------------------------
# T1: correlation IDs, cancel sources, hedge isolation & union metrics
# ---------------------------------------------------------------------------


def test_proxy_records_call_id_correlation_id_and_cancel_source():
    assert _valid_correlation_id("req_abc123-xyz:1.2") == "req_abc123-xyz:1.2"
    assert _valid_correlation_id("bad player name with spaces") is None
    assert _valid_correlation_id("x" * 97) is None

    backend = ProxyBackend(model="current", api_key="k", base_url="http://127.0.0.1:1")
    ext = threading.Event()
    ext._cancel_source = "request_changed"
    call = _CallContext(
        call_class="decision.typed",
        max_tokens=100,
        budget_s=10.0,
        handle=_CallHandle("decision.typed", ext),
        correlation_id="req_112233",
    )
    out = backend._finish_cancelled(
        "cancelled",
        call,
        {"messages": [{"role": "user", "content": "hi"}]},
        request_started=time.perf_counter(),
        attempt=1,
        raise_on_error=False,
    )
    assert "request cancelled (cancelled)" in out
    metrics = backend.last_request_metrics
    assert metrics["call_id"] == call.call_id and len(call.call_id) == 16
    assert metrics["correlation_id"] == "req_112233"
    assert metrics["cancel_reason"] == "cancelled"
    assert metrics["cancel_source"] == "request_changed"


def test_proxy_rejects_unlisted_cancel_source_and_preserves_handle_reason():
    backend = ProxyBackend(model="current", api_key="k", base_url="http://127.0.0.1:1")
    ext = threading.Event()
    ext._cancel_source = "untrusted free text"
    call = _CallContext(
        call_class="decision.typed",
        max_tokens=100,
        budget_s=10.0,
        handle=_CallHandle("decision.typed", ext),
        correlation_id="bad id",
    )
    backend._finish_cancelled(
        "cancelled",
        call,
        {"messages": [{"role": "user", "content": "hi"}]},
        request_started=time.perf_counter(),
        attempt=1,
        raise_on_error=False,
    )
    assert backend.last_request_metrics["cancel_source"] is None
    assert _valid_correlation_id(call.correlation_id) is None


def test_planner_forwards_correlation_id_and_tags_hedge_vs_request_changed():
    captured = []

    class ModernBackend:
        def complete(self, sys_p, usr_p, max_tokens=100, **kwargs):
            captured.append(kwargs)
            if kwargs.get("reasoning_effort") == "high":
                time.sleep(0.08)
                return '{"option_ids": ["mull:keep"], "reasoning": "ok"}'
            return '{"option_ids": ["mull:keep"], "reasoning": "ok"}'

    planner = ActionPlanner(ModernBackend())
    planner.set_decision_correlation_id("req_hedged_1")
    _, chosen = planner._hedged_decision(
        lambda msg, effort=None, budget=5.0, cancel_event=None: planner._call_llm(
            "sys",
            msg,
            50,
            timeout_s=budget,
            call_class="decision.typed",
            reasoning_effort=effort,
            cancel_event=cancel_event,
        ),
        lambda raw: (json.loads(raw), json.loads(raw)["option_ids"]),
        "choose",
        normal_budget=2.0,
        high_budget=2.0,
    )
    assert chosen == ["mull:keep"]
    assert captured and all(c.get("correlation_id") == "req_hedged_1" for c in captured)
    high_call = next(c for c in captured if c.get("reasoning_effort") == "high")
    assert getattr(high_call["cancel_event"], "_cancel_source", None) == "hedge_loser"

    # Outer request change tags remaining leg events as "request_changed".
    outer = threading.Event()
    outer.set()
    planner2 = ActionPlanner(ModernBackend())
    planner2.set_decision_cancel_event(outer)
    with pytest.raises(LLMUnavailableError, match="request changed"):
        planner2._hedged_declaration(
            lambda cc, eff, cancel_event=None: "unused",
            deadline=time.monotonic() + 1.0,
        )

    # Legacy backend without correlation_id parameter stays clean via _accepted_extras.
    class LegacyBackend:
        def complete(self, sys_p, usr_p):
            return "legacy"

    legacy = ActionPlanner(LegacyBackend())
    legacy.set_decision_correlation_id("req_legacy")
    assert legacy._call_llm("sys", "usr", 10, timeout_s=1.0, call_class="decision.plan") == "legacy"


def test_upgrade_metrics_interval_union_and_hedge_separation():
    lines = [
        "[PROXY] Request metrics: "
        + repr(
            {
                "call_id": "c_high",
                "call_class": "decision.typed",
                "correlation_id": "req_001",
                "status": "cancelled",
                "cancel_reason": "cancelled",
                "cancel_source": "request_changed",
                "request_started_unix_s": 1000.0,
                "total_ms": 2000.0,
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }
        ),
        "[PROXY] Request metrics: "
        + repr(
            {
                "call_id": "c_low",
                "call_class": "decision.typed",
                "correlation_id": "req_001",
                "status": "cancelled",
                "cancel_reason": "cancelled",
                "cancel_source": "request_changed",
                "request_started_unix_s": 1000.5,
                "total_ms": 1700.0,
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }
        ),
        "[PROXY] Request metrics: "
        + repr(
            {
                "call_id": "c_hedge",
                "call_class": "decision.typed",
                "correlation_id": "req_002",
                "status": "cancelled",
                "cancel_reason": "cancelled",
                "cancel_source": "hedge_loser",
                "request_started_unix_s": 1010.0,
                "total_ms": 900.0,
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }
        ),
        "[PROXY] Request metrics: "
        + repr(
            {
                "call_id": "c_legacy",
                "call_class": "background.game_plan",
                "status": "cancelled",
                "total_ms": 3500.0,
                "input_tokens": 500,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
            }
        ),
    ]
    # Duplicate the exact bundle lines across two bundles with overlapping call_ids.
    b1 = {"match_id": "m_a", "coach_log": {"lines": lines}, "packet": {"decisions": []}}
    b2 = {"match_id": "m_a", "coach_log": {"lines": lines}, "packet": {"decisions": []}}
    agg = um.aggregate([um.summarize_bundle(b1), um.summarize_bundle(b2)])
    corr = agg["t1"]["correlated_cancellations"]
    # Interval union of [1000.0, 1002.0] and [1000.5, 1002.2] = 2.2s, NOT 3.7s or 7.4s!
    assert corr["unique_requests"] == 1
    assert corr["request_changed_calls"] == 2
    assert corr["attributable_duration_s"] == pytest.approx(2.2)
    assert corr["hedge_loser_calls"] == 1
    assert corr["hedge_loser_total_s"] == pytest.approx(0.9)
    assert corr["uncorrelated_proxy_cancelled_by_class"] == {
        "background.game_plan": {"calls": 1, "total_s": 3.5}
    }
    text = um.render_report(agg)
    assert (
        "correlated request-change cancellations: 1 unique requests (2 calls, 2.2 s wall-clock union)" in text
    )
    assert "wasting" not in text


def test_autopilot_sets_correlation_id_around_typed_decision():
    from arenamcp.autopilot import AutopilotEngine
    from arenamcp.autopilot_models import AutopilotConfig

    seen_corr = []
    seen_cancels = []

    class FakePlanner:
        def set_decision_cancel_event(self, ev):
            seen_cancels.append(ev)

        def set_decision_correlation_id(self, cid):
            seen_corr.append(cid)

        def plan_decision_options(self, decision, game_state):
            return ["mull:keep"]

    decision = PendingDecision(
        request_id=(1, 2),
        request_type="Mulligan",
        min_select=1,
        max_select=1,
        can_pass=False,
        can_cancel=False,
        source_label="LondonMulligan",
        options=(
            DecisionOption(option_id="mull:keep", label="Keep", payable=None, meta={}),
            DecisionOption(option_id="mull:mull", label="Mulligan", payable=None, meta={}),
        ),
        slots=(),
    )

    class FakeBridge:
        connected = True

        def get_pending_actions(self):
            return {
                "has_pending": True,
                "request_type": "Mulligan",
                "game_state_id": 1,
                "msg_id": 2,
                "options": [],
            }

    planner = FakePlanner()
    bridge = FakeBridge()
    with patch("arenamcp.autopilot.get_bridge", return_value=bridge):
        engine = AutopilotEngine(
            planner=planner,
            get_game_state=lambda: {"match_id": "m", "_log_game_state_id": 1, "turn": {"turn_number": 1}},
            config=AutopilotConfig(dry_run=False),
        )
    with (
        patch.object(engine, "_await_log_catch_up", side_effect=lambda poll, gs: gs),
        patch("arenamcp.autopilot.enrich_snapshot_from_pending_response"),
        patch.object(engine, "_wait_for_sideboard", return_value=False),
        patch.object(engine, "_observe_decision_progress", return_value=False),
        patch.object(engine, "_wait_for_search_identities", return_value=False),
        patch.object(engine, "_refresh_game_plan"),
        patch.object(engine, "_cancel_on_request_change", return_value=lambda: None),
        patch("arenamcp.decisions.build_pending_decision", return_value=decision),
        patch("arenamcp.request_tracker.decision_fingerprint", return_value="fp"),
        patch.object(engine, "_note_typed_fallback"),
        patch("arenamcp.autopilot.AutopilotEngine._progress_bridge", return_value=SimpleNamespace()),
        patch("arenamcp.decisions.submit_option", return_value=True),
        patch.object(engine, "_record_autopilot_decision"),
        patch.object(engine, "_log_execution_path"),
    ):
        engine._try_typed_decision_path(
            {"match_id": "m", "_log_game_state_id": 1, "turn": {"turn_number": 1}}, "test"
        )

    assert len(seen_corr) == 2
    assert isinstance(seen_corr[0], str) and seen_corr[0].startswith("req_")
    assert seen_corr[1] is None
    assert len(seen_cancels) == 2
    assert getattr(seen_cancels[0], "_cancel_source", None) == "request_changed"
    assert seen_cancels[1] is None


# ---------------------------------------------------------------------------
# B2: labels, card facts loading, BenchmarkModelBackend, fail-closed errors
# ---------------------------------------------------------------------------


def test_b2_labels_and_card_facts_loader(tmp_path):
    labels_path = tmp_path / "labels.json"
    labels_payload = {
        "schema_version": 1,
        "labels": {
            _hex("1"): {"status": "labelled", "acceptable": [["mull:keep"]], "blunders": [["mull:mull"]]},
            _hex("2"): {"status": "abstain", "acceptable": [], "blunders": []},
        },
    }
    labels_path.write_text(json.dumps(labels_payload))
    active, sha = db.load_labels(labels_path)
    assert set(active) == {_hex("1")}
    assert sha == hashlib.sha256(labels_path.read_bytes()).hexdigest()

    empty_recs, empty_sha = db.load_card_facts_dir(None)
    assert empty_recs == {}
    assert empty_sha == hashlib.sha256(b"{}").hexdigest()

    # Valid wrapped record on disk loads with non-empty hash; status!="ok" ignored.
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    valid_hash = _hex("a")
    valid_rec = {
        "schema_version": 1,
        "rules_hash": valid_hash,
        "name": "Shock",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Shock deals 2 damage to any target.",
        "mana": None,
        "effects": [],
        "keywords": [],
        "limited": {"roles": [], "notes": ""},
        "body": "Shock deals 2 damage to any target.",
        "confidence": "high",
        "notes": "",
        "compiled_by": "test",
        "compiled_at": "2026-11-16T00:00:00Z",
    }
    (facts_dir / f"{valid_hash}.json").write_text(json.dumps({"fetched_at": 1.0, "record": valid_rec}))
    (facts_dir / f"{_hex('b')}.json").write_text(json.dumps({"status": "missing", "record": valid_rec}))
    recs, fsha = db.load_card_facts_dir(facts_dir)
    assert list(recs) == [valid_hash]
    assert fsha != empty_sha


def test_b2_model_backend_fail_closed_and_repeats_smoke(tmp_path):
    c1, c2 = _hex("1"), _hex("2")
    cases = [_actions_case(c1), _actions_case(c2)]
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "labels": {
                    c1: {"status": "labelled", "acceptable": [["idx:1"]], "blunders": [["pass"]]},
                    c2: {"status": "labelled", "acceptable": [["idx:1"]], "blunders": [["pass"]]},
                }
            }
        )
    )
    labels, labels_sha = db.load_labels(labels_path)
    corpus_sha = hashlib.sha256(b"test-corpus").hexdigest()

    seen_calls = []

    class RecordingFakeBackend:
        model = "current"

        def available(self):
            return True

        def degraded(self):
            return False

        def complete(self, *args, **kwargs):
            seen_calls.append((dict(kwargs), os.environ.get("ARENAMCP_THINKING_ROWS")))
            return json.dumps({"option_ids": ["idx:1"], "reasoning": "cast bolt"})

    class FailingBackend:
        model = "current"

        def available(self):
            return True

        def degraded(self):
            return False

        def complete(self, *args, **kwargs):
            raise LLMUnavailableError("gateway down")

    os.environ.pop("ARENAMCP_THINKING_ROWS", None)
    runs_ok, report_ok, repeats_ok = db.run_benchmark(
        cases,
        corpus_sha256=corpus_sha,
        labels=labels,
        labels_sha256=labels_sha,
        backend_mode="proxy",
        model="current",
        thinking="low",
        priority=10,
        repeats=2,
        workers=2,
        inner_backend_factory=lambda: RecordingFakeBackend(),
    )
    assert "ARENAMCP_THINKING_ROWS" not in os.environ
    assert all(row["status"] == "answered" for row in runs_ok[0])
    assert br.summary(report_ok)["scores"] == {"acceptable": 2}
    assert repeats_ok["spread"]["acceptable_rate"]["mean"] == pytest.approx(1.0)
    assert all(kw["priority"] == 10 and kw["reasoning_effort"] == "low" for kw, _ in seen_calls)
    assert all(json.loads(env)["*"]["decision"] == "low" for _, env in seen_calls)

    # Contrary control: unavailable LLM must become status="error", score="not_scored", NOT fallback acceptable!
    runs_err, report_err, repeats_err = db.run_benchmark(
        [cases[0]],
        corpus_sha256=corpus_sha,
        labels=labels,
        labels_sha256=labels_sha,
        backend_mode="proxy",
        model="current",
        priority=10,
        repeats=2,
        inner_backend_factory=lambda: FailingBackend(),
    )
    assert runs_err[0][0]["status"] == "error"
    assert runs_err[0][0]["error"] == "ModelUnavailable"
    assert report_err["results"][0]["score"] == "not_scored"
    assert repeats_err["per_repeat"][0]["error"] == 1


# ---------------------------------------------------------------------------
# H3: split-by-who-answers, only-GLM math, and mismatch guards
# ---------------------------------------------------------------------------


def _v1_for(rows, *, corpus=_hex("c"), labels=_hex("d"), cfg=_hex("e"), model_layer="run"):
    return {
        "version": 1,
        "corpus_sha256": corpus,
        "configuration_sha256": cfg,
        "facts_sha256": _hex("f"),
        "labels_sha256": labels,
        "model_layer": model_layer,
        "results": rows,
    }


def _row(cid, status, sel, score, secs=1.0, gen=1):
    return {
        "case_id": cid,
        "status": status,
        "selection_sha256": _hex(sel),
        "seconds": secs,
        "generation_attempts": gen,
        "score": score,
    }


def test_h3_split_by_who_answers_and_mismatch_guards():
    c1, c2, c3, c4 = _hex("1"), _hex("2"), _hex("3"), _hex("4")
    code_rep = _v1_for(
        [
            _row(c1, "answered", "a", "acceptable", 0.01, gen=0),
            _row(c2, "withheld", "0", "not_scored", 0.01, gen=1),
            _row(c3, "withheld", "0", "not_scored", 0.01, gen=1),
            _row(c4, "withheld", "0", "not_scored", 0.01, gen=1),
        ],
        model_layer="not_run",
    )
    glm_rep = _v1_for(
        [
            _row(c1, "answered", "a", "acceptable", 0.5),
            _row(c2, "answered", "a", "acceptable", 1.0),
            _row(c3, "answered", "b", "blunder", 1.5),
            _row(c4, "error", "0", "not_scored", 2.0),
        ],
        cfg=_hex("a"),
    )
    small_rep = _v1_for(
        [
            _row(c1, "answered", "a", "acceptable", 0.4),
            _row(c2, "answered", "c", "unlabelled_choice", 0.8),
            _row(c3, "answered", "a", "acceptable", 1.2),  # beats GLM!
            _row(c4, "answered", "c", "blunder", 1.6),
        ],
        cfg=_hex("b"),
    )
    out = h3.compare_models(
        code_report=code_rep,
        glm_report=glm_rep,
        candidates=[("small_test", small_rep)],
    )
    split = out["split_by_who_answers"]
    assert split["settled_by_code"]["cases"] == 1
    assert split["model_required_cases"] == 3
    assert split["glm_acceptable"] == 1
    assert split["any_candidate_acceptable"] == 1
    assert split["all_candidates_acceptable"] == 1
    assert split["only_glm_acceptable"] == 1
    assert split["only_glm_share_of_all"] == pytest.approx(0.25)
    assert split["only_glm_share_of_model_required"] == pytest.approx(1 / 3)
    assert split["candidate_beats_glm_acceptable"] == 1
    assert split["neither_acceptable"] == 1

    small_row = next(r for r in out["models"] if r["name"] == "small_test")
    assert small_row["vs_glm"]["model_required"]["only_glm_acceptable"] == 1
    assert small_row["vs_glm"]["model_required"]["only_candidate_acceptable"] == 1

    # Zero case_id leakage in JSON or table.
    rendered = json.dumps(out) + "\n" + h3.render_table(out)
    for cid in (c1, c2, c3, c4):
        assert cid not in rendered

    # Hash / cohort mismatch must raise ValueError.
    with pytest.raises(ValueError, match="corpus_sha256 mismatch"):
        h3.compare_models(
            code_report=code_rep,
            glm_report=glm_rep,
            candidates=[("bad", _v1_for(small_rep["results"], corpus=_hex("9")))],
        )


def test_unhedged_plan_decision_options_preserves_request_changed_and_timeout_cancel_sources():
    case = _actions_case(_hex("7"))
    decision = db.typed_decision(case["decision"])
    state = case["snapshot"]["state"]

    # 1. Request changed mid-call on single-leg _llm_decision_options -> ask()
    seen_events = []

    class SlowCancelBackend:
        def complete(self, sys_p, usr_p, max_tokens=100, **kwargs):
            seen_events.append((kwargs.get("correlation_id"), kwargs.get("cancel_event")))
            outer.set()
            time.sleep(0.25)
            return '{"option_ids": ["pass"], "reasoning": "late"}'

    outer = threading.Event()
    outer._cancel_source = "request_changed"
    planner = ActionPlanner(SlowCancelBackend(), timeout=2.0)
    planner.set_decision_cancel_event(outer)
    planner.set_decision_correlation_id("req_unhedged_1")
    planner.plan_decision_options(decision, state)
    assert seen_events and seen_events[0][0] == "req_unhedged_1"
    ev = seen_events[0][1]
    assert ev is not None and ev is not outer and ev.is_set()
    assert getattr(ev, "_cancel_source", None) == "request_changed"

    # 2. Timeout on single-leg _llm_decision_options -> ask()
    timeout_events = []

    class TimeoutBackend:
        def complete(self, sys_p, usr_p, max_tokens=100, **kwargs):
            timeout_events.append(kwargs.get("cancel_event"))
            time.sleep(0.4)
            return '{"option_ids": ["pass"], "reasoning": "late"}'

    tplanner = ActionPlanner(TimeoutBackend(), timeout=0.05)
    tplanner._NORMAL_DECISION_BUDGET_S = 0.05
    tplanner._CRITICAL_DECISION_BUDGET_S = 0.05
    tplanner.set_decision_correlation_id("req_timeout_1")
    tplanner.plan_decision_options(decision, state)
    assert timeout_events and timeout_events[0] is not None and timeout_events[0].is_set()
    assert getattr(timeout_events[0], "_cancel_source", None) == "timeout"


def test_unhedged_plan_actions_bounded_complete_preserves_cancel_sources():
    state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "mana_pool": {"R": 2}},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [],
        "hand": [{"name": "Lightning Bolt", "instance_id": 20, "grp_id": 102, "type_line": "Instant"}],
    }

    # 1. Request changed mid-call on routine plan_actions -> _bounded_complete
    rc_events = []

    class ReqChangePlanBackend:
        def complete(self, sys_p, usr_p, max_tokens=100, **kwargs):
            rc_events.append((kwargs.get("correlation_id"), kwargs.get("cancel_event")))
            outer.set()
            time.sleep(0.25)
            return '{"actions": []}'

    outer = threading.Event()
    outer._cancel_source = "request_changed"
    planner = ActionPlanner(ReqChangePlanBackend(), timeout=2.0, land_drop_first=False)
    planner.set_decision_cancel_event(outer)
    planner.set_decision_correlation_id("req_plan_rc")
    planner.plan_actions(state, "decision_required", legal_actions=["Cast Lightning Bolt", "Pass priority"])
    assert rc_events and rc_events[0][0] == "req_plan_rc"
    ev = rc_events[0][1]
    assert ev is not None and ev.is_set() and getattr(ev, "_cancel_source", None) == "request_changed"

    # 2. Timeout on routine plan_actions -> _bounded_complete
    to_events = []

    class TimeoutPlanBackend:
        def complete(self, sys_p, usr_p, max_tokens=100, **kwargs):
            to_events.append(kwargs.get("cancel_event"))
            time.sleep(0.4)
            return '{"actions": []}'

    tplanner = ActionPlanner(TimeoutPlanBackend(), timeout=0.05, land_drop_first=False)
    tplanner.set_decision_correlation_id("req_plan_to")
    tplanner.plan_actions(state, "decision_required", legal_actions=["Cast Lightning Bolt", "Pass priority"])
    assert to_events and to_events[0] is not None and to_events[0].is_set()
    assert getattr(to_events[0], "_cancel_source", None) == "timeout"
