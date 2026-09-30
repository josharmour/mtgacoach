"""Tooth and Nail searches must expose card identities and the two-card limit."""

from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION
from arenamcp.decisions import build_pending_decision, submit_option
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from test_mac_bridge_adapter import FakeGame, enum, listing
from test_typed_decision_path import _engine, _planner_with, _TypedBridge

CARDS = {
    101: {"name": "Shang-Chi, Master of Kung Fu", "type_line": "Legendary Creature — Human Hero", "cmc": 4},
    102: {
        "name": "Ulamog, the Ceaseless Hunger",
        "type_line": "Legendary Creature — Eldrazi",
        "mana_cost": "{10}",
        "cmc": 10,
        "oracle_text": "When you cast this spell, exile two target permanents.\nIndestructible",
    },
    103: {
        "name": "Void Winnower",
        "type_line": "Creature — Eldrazi",
        "mana_cost": "{9}",
        "cmc": 9,
        "oracle_text": "Your opponents can't cast spells with even mana values.",
    },
}


def search_request():
    return {
        "$c": "GreClient.Rules.SearchRequest",
        "$h": 55,
        "Min": 0,
        "Max": 2,
        "Options": listing(537, 538, 539),
        "ZonesToSearch": listing(42),
    }


class SearchGame(FakeGame):
    def __init__(self):
        super().__init__(search_request(), {"Type": enum("Search", 0)})
        self.cards = {537: 101, 538: 102, 539: 103, 999: 9999}
        self.looked_up = []

    def send(self, command, timeout):
        result = super().send(command, timeout)
        if not result["ok"]:
            return result
        for index, op in enumerate(command["ops"]):
            if op.get("method") == "GetCardById":
                iid = op["args"][0]["uint"]
                self.looked_up.append(iid)
                result["results"][index] = self.cards.get(iid, 0)
            elif op.get("member") == "GrpId":
                result["results"][index] = result["results"][op["target"]["ref"]]
        return result


@pytest.fixture
def poll(monkeypatch):
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda grp: CARDS.get(grp, {}))
    return MacBridgeAdapter(SearchGame().send).handle({"action": "get_pending_actions"})


def test_native_search_resolves_only_offered_instances_and_preserves_bounds():
    game = SearchGame()
    response = MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"})
    assert response["select_n_min"] == 0
    assert response["select_n_max"] == 2
    assert response["search_candidates"] == [
        {"instanceId": 537, "grpId": 101},
        {"instanceId": 538, "grpId": 102},
        {"instanceId": 539, "grpId": 103},
    ]
    assert game.looked_up == [537, 538, 539]
    assert not any(op.get("method", "").startswith("Submit") for batch in game.batches for op in batch)


def test_search_identity_read_rejects_a_changed_request():
    game = SearchGame()
    game.requests = [search_request(), {**search_request(), "$h": 56}]
    result = MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"})
    assert result["ok"] is False
    assert "stale" in result["error"]
    assert game.looked_up == []


def test_truncated_search_does_not_hide_possible_choices():
    game = SearchGame()
    game.request["Options"]["$n"] = 4
    result = MacBridgeAdapter(game.send).handle({"action": "get_pending_actions"})
    assert result["ok"] is False
    assert "incomplete" in result["error"]
    assert game.looked_up == []


def test_real_card_named_unknown_shores_is_not_an_unresolved_choice(poll, monkeypatch):
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda grp: {"name": "Unknown Shores"})
    decision = build_pending_decision(poll)
    assert all(option.meta["identity_known"] for option in decision.options)


@pytest.mark.parametrize("ids", [[538, 539], [], [537, 538, 539], [538, 538], [999]])
def test_native_search_checks_the_offered_set_and_count_before_submission(ids):
    game = SearchGame()
    result = MacBridgeAdapter(game.send).handle({"action": "submit_selection", "ids": ids})
    valid = ids in ([538, 539], [])
    assert result["ok"] is valid
    calls = [op for batch in game.batches for op in batch if op.get("method") == "SubmitSelection"]
    assert len(calls) == int(valid)
    if valid:
        assert calls[0]["args"] == [{"list": [{"uint": iid} for iid in ids]}]


def test_two_creature_search_carries_card_rules_to_planner_and_submits_both(poll):
    decision = build_pending_decision(poll)
    assert (decision.min_select, decision.max_select) == (0, 2)
    planner = _planner_with('{"option_ids":["sel:538","sel:539"],"reasoning":"Two complementary threats."}')
    planner._backend.complete = Mock(wraps=planner._backend.complete)
    picked = planner.plan_decision_options(decision, {})
    assert picked == ["sel:538", "sel:539"]
    system, prompt = planner._backend.complete.call_args.args[:2]
    for card in CARDS.values():
        assert card["name"] in prompt
        if card.get("oracle_text"):
            assert card["oracle_text"].splitlines()[0] in prompt
    assert "Choose at least 0 and at most 2 option(s)." in prompt
    assert "does not trigger 'when you cast'" in system
    bridge = _TypedBridge(poll)
    assert submit_option(bridge, decision, picked)
    assert bridge.submitted == [("selection", [538, 539])]


def test_unidentified_search_is_not_sold_as_a_strategic_choice(monkeypatch):
    poll = {
        "has_pending": True,
        "request_type": "Search",
        "search_candidates": [537, 538],
        "select_n_min": 0,
        "select_n_max": 2,
    }
    planner = _planner_with('{"option_ids":["sel:537"],"reasoning":"Strongest creature."}')
    bridge = _TypedBridge(poll)
    engine = _engine(monkeypatch, bridge, planner)
    engine._pause_for_manual = Mock()
    assert engine._try_typed_decision_path({}, "decision_required") is True
    assert planner._backend.calls == 0
    assert bridge.submitted == []
    engine._pause_for_manual.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        "{}",
        '{"option_ids":[null]}',
        "not JSON",
        '{"option_ids":["sel:538","sel:999"]}',
        '{"option_ids":["sel:537","sel:538","sel:539"]}',
        '{"option_ids":["sel:538","sel:538"]}',
    ],
)
def test_bad_search_answer_does_not_fall_back_to_first_creature(poll, response):
    planner = _planner_with(response)
    assert planner.plan_decision_options(build_pending_decision(poll), {}) == [DECLINE_DECISION]


def test_explicit_fail_to_find_can_submit_empty_selection(poll, monkeypatch):
    planner = _planner_with('{"option_ids":[],"reasoning":"Avoid the search penalty."}')
    bridge = _TypedBridge(poll)
    engine = _engine(monkeypatch, bridge, planner)
    assert engine._try_typed_decision_path({}, "decision_required") is True
    assert bridge.submitted == [("selection", [])]


def test_empty_library_search_can_finish_without_guessing_a_card(poll):
    poll["search_candidates"] = []
    decision = build_pending_decision(poll)
    planner = _planner_with("must not be called")
    assert planner.plan_decision_options(decision, {}) == []
    bridge = _TypedBridge(poll)
    assert submit_option(bridge, decision, [])
    assert bridge.submitted == [("selection", [])]
