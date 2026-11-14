"""A cache-only second lookup cannot discard a captured valid strategy."""

import pytest
from tests.test_decision_layer_20261019 import _Model, _planner, _reply
from tests.test_planner_micro_wins_226 import _our_main

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.deck_strategy import DeckPlaybook
from deck_fakes import playbook_for


@pytest.mark.parametrize(
    "failure", ["provider", "renderer", "invalid_parts", "none_fields", "nonstring_field", "invalid_indices"]
)
def test_optional_partition_failure_preserves_actual_prompt(monkeypatch, failure):
    model = _Model(_reply("pass", "hold"))
    planner = _planner(model)
    captured = "Primary plan: captured safe plan\n  Unless: preserve this whole qualifier\nRULE entire captured condition"
    monkeypatch.setattr(planner, "_stable_strategy_context", lambda *a, **k: captured)

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic optional partition failure")

    if failure == "provider":
        monkeypatch.setattr(planner, "_deck_playbook", broken)
    else:
        monkeypatch.setattr(planner, "_deck_playbook", lambda: playbook_for("replay"))
        malformed = {
            "invalid_parts": (),
            "none_fields": (None, set()),
            "nonstring_field": ([captured, None], {0}),
            "invalid_indices": ([captured], {True}),
        }
        monkeypatch.setattr(
            DeckPlaybook,
            "_decision_context_lines",
            broken if failure == "renderer" else lambda *a, **k: malformed[failure],
        )
    assert planner._playbook_context_parts(captured, {}, "ActionsAvailable") == (captured, "")
    menu = PendingDecision(
        (1, 2), "ActionsAvailable", (DecisionOption("pass", "Pass"),), min_select=1, max_select=1
    )
    assert planner._llm_decision_options(menu, _our_main()) == ["pass"]
    assert len(model.prompts) == 1
    assert model.prompts[0].count(captured) == 1
    assert "PENDING DECISION: ActionsAvailable" in model.prompts[0]
    assert "pass: Pass" in model.prompts[0]


def test_custom_subclass_partition_is_not_trusted(monkeypatch):
    class CustomBook(DeckPlaybook):
        def decision_context_parts(self, *args, **kwargs):
            return captured, "invented replacement", ""

    book = playbook_for("replay")
    book.__class__ = CustomBook
    planner = _planner(_Model(_reply("pass", "hold")))
    captured = "Primary plan: keep duplicate\nUnless: preserve qualifier\nUnless: preserve qualifier"
    monkeypatch.setattr(planner, "_deck_playbook", lambda: book)
    assert planner._playbook_context_parts(captured, {}, "ActionsAvailable") == (captured, "")


def test_native_field_partition_ignores_overridden_parts_method(monkeypatch):
    book = playbook_for("replay")
    planner = _planner(_Model(_reply("pass", "hold")))
    captured = book.decision_context({}, request_type="ActionsAvailable")
    expected = book.decision_context_parts({}, request_type="ActionsAvailable")
    monkeypatch.setattr(planner, "_deck_playbook", lambda: book)
    monkeypatch.setattr(DeckPlaybook, "decision_context_parts", lambda *a, **k: (captured, "", ""))
    assert planner._playbook_context_parts(captured, {}, "ActionsAvailable") == expected[1:]
