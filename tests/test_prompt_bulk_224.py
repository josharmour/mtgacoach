"""Regression tests for ledger #224 (prompt bulk / prefix caching), #199(e) and #219.

#224: the typed-decision prompt's call-to-call STABLE text (deck reference,
deck strategy, playbook decision rules) leads the message so vLLM's prefix
cache matches through it; the per-turn plan facts stay in the volatile tail.
The backend's cached-input count surfaces in the decision trace.
#199(e): the decision trace stores the plan block only when it changed.
#219: limited formats ahead on board get a deterministic aggression nudge.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision


class Backend:
    def __init__(self, reply='{"option_ids":["pass"]}', metrics=None):
        self.reply = reply
        # The proxy exposes the last request's usage as an attribute.
        self.last_request_metrics = metrics
        self.calls = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user))
        return self.reply


WIN_ENGINE = (
    "Win through an engine; hold removal as needed. Establish the engine early "
    "and protect it; spend removal only on real threats."
)

PASS_DECISION = {
    "has_pending": True,
    "request_type": "ActionsAvailable",
    "can_pass": True,
    "actions": [{"actionType": "ActionType_Pass"}],
}


def plan(planner_backend):
    return ActionPlanner(planner_backend, deck_strategy_fn=lambda: WIN_ENGINE)


def creature(instance_id, name, seat, power, toughness):
    return {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "card_types": ["CardType_Creature"],
        "type_line": "Creature — Test",
        "power": power,
        "toughness": toughness,
    }


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda gid, epoch: {
            "name": f"Card {gid}",
            "type_line": "Instant",
            "mana_cost": "{G}",
            "cmc": 1,
            "oracle_text": f"Distinct rules for {gid}.",
        },
    )
    return {
        "match_id": "bulk-224",
        "deck_cards": list(range(100, 140)),
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": 20},
            {"is_local": False, "seat_id": 2, "life_total": 20},
        ],
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Phase_Main1"},
        "hand": [
            {
                "instance_id": 1,
                "grp_id": 100,
                "name": "Card 100",
                "owner_seat_id": 1,
                "type_line": "Instant",
                "mana_cost": "{G}",
                "oracle_text": "Distinct rules for 100.",
            }
        ],
        "battlefield": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 39, "opponent_hand_count": 5},
    }


# --- #224: stable sections first ----------------------------------------------------


def test_stable_strategy_leads_the_typed_decision_prompt(state):
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, state)
    user = backend.calls[-1][1]
    head, tail = user.split("PENDING DECISION:", 1)
    # The stable strategy text sits between the deck reference and the volatile
    # window, so the prefix cache matches from the deck reference through it.
    assert head.startswith("DECK REFERENCE")
    assert WIN_ENGINE in head
    # The per-turn plan facts stay after the volatile window.
    assert ("STRATEGIC ROLE" in tail) or ("FACTS:" in tail)


def test_stable_prefix_is_unchanged_when_only_the_window_changes(state):
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, state)
    first = backend.calls[-1][1]
    changed = deepcopy(state)
    changed["players"][0]["life_total"] = 5
    changed["turn"]["turn_number"] = 5
    planner._llm_decision_options(decision, changed)
    second = backend.calls[-1][1]
    assert first.split("PENDING DECISION:", 1)[0] == second.split("PENDING DECISION:", 1)[0]
    assert first != second


def test_representative_prompt_prefix_measurement(state, capsys):
    """Measure the stable-prefix share on a representative built prompt (#224).

    Prints chars/tokens for the report; asserts the strategy joined the cached
    prefix and that the prefix is a substantial share of the message.
    """
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, state)
    user = backend.calls[-1][1]
    system = backend.calls[-1][0]
    head_chars = user.find("PENDING DECISION:")
    total_chars = len(user)
    share = head_chars / total_chars
    print(
        f"\n#224 representative prompt: system={len(system)} chars (~{len(system) // 4} tok), "
        f"user={total_chars} chars (~{total_chars // 4} tok), "
        f"stable prefix={head_chars} chars (~{head_chars // 4} tok, {share:.0%})"
    )
    assert WIN_ENGINE in user[:head_chars]
    assert share >= 0.15


# --- #224: cached-token visibility --------------------------------------------------


def test_cached_input_tokens_surface_in_the_decision_trace(state, caplog):
    backend = Backend(metrics={"input_tokens": 10240, "cached_input_tokens": 8192})
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    with caplog.at_level("INFO", logger="arenamcp.action_planner"):
        planner._llm_decision_options(decision, state)
    trace = planner.get_last_decision_trace()
    assert trace["llm_usage"] == {"input_tokens": 10240, "cached_input_tokens": 8192}
    assert any("prefix cache hit" in record.message for record in caplog.records)


def test_missing_usage_reports_nothing(state):
    backend = Backend(metrics=None)
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, state)
    assert "llm_usage" not in planner.get_last_decision_trace()


# --- #199(e): plan block stored as a delta ------------------------------------------


def test_plan_block_stored_in_full_once_then_as_a_marker(state):
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, state)
    first_block = planner.get_last_decision_trace()["plan_block"]
    assert isinstance(first_block, str) and first_block and not first_block.startswith("unchanged")
    planner._llm_decision_options(decision, state)
    marker = planner.get_last_decision_trace()["plan_block"]
    assert marker.startswith("unchanged (") and str(len(first_block)) in marker
    # A changed plan (new life/turn -> new facts) is stored in full again.
    changed = deepcopy(state)
    changed["players"][0]["life_total"] = 5
    changed["turn"]["turn_number"] = 5
    planner._llm_decision_options(decision, changed)
    again = planner.get_last_decision_trace()["plan_block"]
    assert again and not again.startswith("unchanged")


# --- #219: limited-format aggression nudge -----------------------------------------


@pytest.fixture
def ahead_limited(state):
    ahead = deepcopy(state)
    ahead["deck_cards"] = list(range(200, 240))  # a 40-card limited pool
    ahead["battlefield"] = [
        creature(11, "Vanquisher", 1, 3, 2),
        creature(12, "Ambusher", 1, 2, 2),
        creature(21, "Sentinel", 2, 2, 4),
        {"instance_id": 31, "name": "Plains", "owner_seat_id": 1, "controller_seat_id": 1},
        {"instance_id": 32, "name": "Island", "owner_seat_id": 2, "controller_seat_id": 2},
    ]
    return ahead


# --- #219: the nudge in the built prompt --------------------------------------------


def test_limited_ahead_on_board_gets_the_aggression_nudge(state, ahead_limited, monkeypatch):
    monkeypatch.delenv("ARENAMCP_LIMITED_AGGRESSION", raising=False)
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, ahead_limited)
    user = backend.calls[-1][1]
    assert "LIMITED AGGRESSION: you are ahead on board (2 creatures/5 power vs their 1/2)" in user
    assert "Do NOT hold value creatures behind blockers" in user


def test_not_ahead_or_constructed_gets_no_nudge(state, monkeypatch):
    monkeypatch.delenv("ARENAMCP_LIMITED_AGGRESSION", raising=False)
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)

    # Even board: no nudge.
    even = deepcopy(state)
    even["deck_cards"] = list(range(200, 240))
    even["battlefield"] = [creature(11, "Ours", 1, 3, 2), creature(21, "Theirs", 2, 3, 2)]
    planner._llm_decision_options(decision, even)
    assert "LIMITED AGGRESSION" not in backend.calls[-1][1]

    # Ahead on board but a 60-card constructed deck: no nudge.
    constructed = deepcopy(state)
    constructed["deck_cards"] = list(range(300, 360))
    constructed["battlefield"] = [
        creature(11, "Ours", 1, 3, 2),
        creature(12, "Ours2", 1, 3, 2),
        creature(21, "Theirs", 2, 2, 2),
    ]
    planner._llm_decision_options(decision, constructed)
    assert "LIMITED AGGRESSION" not in backend.calls[-1][1]

    # Behind on power in limited: no nudge.
    behind = deepcopy(state)
    behind["deck_cards"] = list(range(200, 240))
    behind["battlefield"] = [
        creature(11, "Ours", 1, 1, 2),
        creature(12, "Ours2", 1, 1, 2),
        creature(21, "Theirs", 2, 5, 5),
    ]
    planner._llm_decision_options(decision, behind)
    assert "LIMITED AGGRESSION" not in backend.calls[-1][1]


def test_aggression_nudge_is_reversible_via_config(state, ahead_limited, monkeypatch):
    monkeypatch.setenv("ARENAMCP_LIMITED_AGGRESSION", "0")
    backend = Backend()
    planner = plan(backend)
    decision = build_pending_decision(PASS_DECISION)
    planner._llm_decision_options(decision, ahead_limited)
    assert "LIMITED AGGRESSION" not in backend.calls[-1][1]


# --- #199(d): Standard Brawl waits for the commander like Historic Brawl ------------


def test_standard_brawl_deck_analysis_waits_for_command_zone_commander(monkeypatch):
    from arenamcp.coach import CoachEngine
    from arenamcp.standalone_deck import _DeckAnalysisMixin

    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(Mock())
    runtime._auto_deck_strategy = True

    # A 59-card Standard Brawl list before the command zone is populated: wait.
    pre_gsm = {
        "match_id": "sb-match-1",
        "deck_cards": list(range(1000, 1059)),
        "commander_grp_ids": [],
        "players": [],
    }
    assert not runtime._maybe_analyze_deck(pre_gsm)
    assert workers == []

    # Commander grp ids arrive: analyse.
    with_commander = {**pre_gsm, "commander_grp_ids": [1001]}
    assert runtime._maybe_analyze_deck(with_commander)
    assert len(workers) == 1

    # A Brawl event id is evidence on its own (59 cards, no grp ids yet).
    by_event = {**pre_gsm, "match_id": "sb-match-2", "event_id": "StandardBrawl"}
    assert not runtime._maybe_analyze_deck(by_event)

    # A plain 60-card constructed deck with no commander evidence is not a
    # Brawl deck: never waited on.
    plain = {"match_id": "std-match-1", "deck_cards": [1002] * 30 + [1003] * 30, "commander_grp_ids": [], "players": []}
    assert runtime._maybe_analyze_deck(plain)
