"""E1 offline contract tests; not the separate twenty-real-match acceptance gate."""

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from tests.test_decision_layer_20261019 import _Model, _planner, _reply
from tests.test_planner_micro_wins_226 import _our_main

from arenamcp import line_guard as lg
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search import ALIVE


def menu(kind="ActionsAvailable", **kw):
    return PendingDecision((1, 1), kind, (DecisionOption("pass", "Pass"),), min_select=1, max_select=1, **kw)


@pytest.mark.parametrize(
    "kind",
    [
        "ActionsAvailable",
        "OptionalAction",
        "Group",
        "SelectTargets",
        "SelectN",
        "Mulligan",
        "CastingTimeOptions",
    ],
)
def test_direct_model_entry_forced_choice(kind):
    model = _Model(_reply("pass", "pass"))
    planner = _planner(model)
    d = menu(kind)
    if kind == "CastingTimeOptions":
        d = replace(
            d,
            options=(DecisionOption("pass", "mode", meta={"choiceKind": "modal", "minSel": 1, "maxSel": 1}),),
        )
    assert planner._llm_decision_options(d, _our_main()) == ["pass"]
    assert not model.efforts


def test_optional_empty_cancel_repeated_and_invalid_abstain():
    p = _planner(_Model(_reply("pass", "pass")))
    for d in [
        menu(can_cancel=True),
        replace(menu(), min_select=0),
        replace(menu(), options=(DecisionOption("x", "x", payable=False),)),
        replace(menu(), options=(DecisionOption("x", "x", meta={"repeatedSelectAllowed": True}),)),
    ]:
        assert p._single_option_answer(d) is None


def test_cache_same_size_hand_and_metadata_change_are_new_questions():
    p = _planner(_Model(_reply("pass", "pass")))
    s = _our_main()
    before = p._reuse_state_signature(s)
    s["hand"][0]["instance_id"] += 1
    assert p._reuse_state_signature(s) != before
    d = menu()
    assert p._decision_cache_key(d) != p._decision_cache_key(replace(d, can_cancel=True))
    assert p._decision_cache_key(d) != p._decision_cache_key(
        replace(d, options=(DecisionOption("pass", "Pass", meta={"cost": 2}),))
    )


def certainty(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.board_assessment.assess",
        lambda s: NS(lethal_now=False, survival_mode=False, unknown_bodies=[]),
    )
    monkeypatch.setattr(lg, "action_key", lambda o, s: (o.option_id,))
    monkeypatch.setattr(lg, "_unmodelled_finisher", lambda *a: False)

    def line(v):
        return NS(
            v=v,
            timing=0,
            cls=ALIVE,
            complete=True,
            unsupported=(),
            dead_at=None,
            win_at=None,
            ranked=(ALIVE, 0, v),
            score=(ALIVE, 0, v),
        )

    result = NS(
        truncated=False,
        dead_now=False,
        unsupported=(),
        choice_caveats=(),
        bounded=False,
        dead_in=None,
        dead_in_greedy=None,
        all_dead_at_first=False,
        cast_trigger_risk=False,
        posture="hold",
        model=NS(our_turn=True, stack_nonempty=False, unknown_bodies=[]),
        first_action={("a",): line(10), ("b",): line(1)},
    )
    d = replace(menu(), options=(DecisionOption("a", "a"), DecisionOption("b", "b")))
    return result, d


def test_certainty_all_evaluated_clear_survivor_only(monkeypatch):
    r, d = certainty(monkeypatch)
    assert lg.planner_certain_pick(r, d, {}) == "a"
    del r.first_action[("b",)]
    assert lg.planner_certain_pick(r, d, {}) is None


@pytest.mark.parametrize(
    "attr,value",
    [
        ("truncated", True),
        ("bounded", True),
        ("dead_now", True),
        ("dead_in", 1),
        ("cast_trigger_risk", True),
        ("posture", "lethal"),
        ("choice_caveats", ("unknown",)),
    ],
)
def test_certainty_abstains_on_risk(monkeypatch, attr, value):
    r, d = certainty(monkeypatch)
    setattr(r, attr, value)
    assert lg.planner_certain_pick(r, d, {}) is None


def test_certainty_shadow_default_and_on_bypasses_model(monkeypatch):
    from arenamcp.guard_settings import GUARD_DEFAULTS

    assert GUARD_DEFAULTS["planner_certain"] == "shadow"
    model = _Model(_reply("b", "choose b"))
    p = _planner(model)
    r, d = certainty(monkeypatch)
    monkeypatch.setenv("ARENAMCP_PLANNER_CERTAIN", "on")
    monkeypatch.setattr(p, "_line_prompt_context", lambda *a: (r, "", {}))
    assert p._llm_decision_options(d, _our_main()) == ["a"]
    assert not model.efforts


def test_shadow_keeps_model_choice_and_records_disagreement(monkeypatch):
    model = _Model(_reply("pass", "hold"))
    p = _planner(model)
    d = replace(menu(), options=(DecisionOption("pass", "Pass"), DecisionOption("other", "other")))
    monkeypatch.setenv("ARENAMCP_PLANNER_CERTAIN", "shadow")
    monkeypatch.setattr(lg, "planner_certain_pick", lambda *a: "other")
    assert p._llm_decision_options(d, _our_main()) == ["pass"]
    assert len(model.efforts) == 1
    assert p._last_decision_trace["planner_certain"]["agreement"] is False


def test_shadow_report_requires_real_twenty_match_observations():
    import importlib.util
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    try:
        spec = importlib.util.spec_from_file_location("e1_shadow_metrics", tools / "e1_shadow_metrics.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.summarize([])["minimum_match_gate_met"] is False
        assert module.summarize([])["observed_before_after_savings"] is None
    finally:
        sys.path.remove(str(tools))
