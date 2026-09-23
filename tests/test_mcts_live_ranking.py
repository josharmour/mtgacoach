"""Regressions for the tactical search's live rankings (real bug-report states).

The prompt tells the LLM to follow the search's BEST LINE, and autopilot acts
on it, so a wrong ranking directly produces wrong plays:
- bug_20260905_220901: 5-mana commander ranked BEST with 1 mana open because
  any GRE "Cast X" listing (not just "[OK]") counted as payable;
- bug_20260813_233045: Pass outranked "Play Forest" because the land drop was
  scored by a different value function than Pass and had no mana-development term;
- bug_20260901_195513: "Cast Birds" outranked "Play Forest -> Cast Birds"
  because single casts discarded the simulated afterstate for a flat guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.magezero_client import MageZeroClient
from arenamcp.mcts_evaluator import MCTSEvaluator


@pytest.fixture(autouse=True)
def _heuristic_only(monkeypatch):
    monkeypatch.setattr(MageZeroClient, "check_health", classmethod(lambda cls, *a, **k: False))
    MCTSEvaluator.reset_cache()
    yield
    MCTSEvaluator.reset_cache()


def _forest(tapped: bool = False, turn_entered: int = 1) -> dict[str, Any]:
    return {
        "name": "Forest",
        "type_line": "Basic Land — Forest",
        "oracle_text": "({T}: Add {G}.)",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "is_tapped": tapped,
        "turn_entered_battlefield": turn_entered,
    }


def _state(*, hand, battlefield, legal_actions, lands_played=0, command=None, turn=4) -> dict[str, Any]:
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": turn, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": lands_played},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": battlefield,
        "hand": hand,
        "command": command or [],
        "legal_actions": legal_actions,
    }


def _actions(tree) -> list[str]:
    return [b.action for b in tree.branches]


def test_gre_listed_but_unpayable_cast_is_not_a_candidate():
    state = _state(
        hand=[
            {"name": "Ancient Behemoth", "type_line": "Creature — Beast", "mana_cost": "{4}{G}{G}",
             "power": 9, "toughness": 9},
            {"name": "Llanowar Elves", "type_line": "Creature — Elf Druid", "mana_cost": "{G}",
             "power": 1, "toughness": 1, "oracle_text": "{T}: Add {G}."},
        ],
        battlefield=[_forest(turn_entered=1), _forest(tapped=True)],
        legal_actions=["Cast Ancient Behemoth", "Cast Llanowar Elves [OK]", "Pass"],
        lands_played=1,
    )
    tree = MCTSEvaluator.evaluate(state, force=True)
    assert not any("Ancient Behemoth" in a for a in _actions(tree))
    assert "Llanowar Elves" in tree.branches[0].action


def test_spell_gre_does_not_offer_is_not_a_candidate():
    state = _state(
        hand=[{"name": "Shock", "type_line": "Instant", "mana_cost": "{R}",
               "oracle_text": "Shock deals 2 damage to any target."}],
        battlefield=[_forest()],
        legal_actions=["Cast Llanowar Elves [OK]", "Pass"],
        lands_played=1,
    )
    tree = MCTSEvaluator.evaluate(state, force=True)
    assert not any("Shock" in a for a in _actions(tree))


def test_land_drop_outranks_pass():
    state = _state(
        hand=[_forest() | {"owner_seat_id": 1}, {"name": "Giant Growth", "type_line": "Instant",
                                                 "mana_cost": "{G}", "oracle_text": "Target creature gets +3/+3."}],
        battlefield=[],
        legal_actions=[],
        turn=1,
    )
    tree = MCTSEvaluator.evaluate(state, force=True)
    by_type = {b.action_type: b.win_probability for b in tree.branches}
    assert "land" in by_type and "pass" in by_type
    assert by_type["land"] > by_type["pass"]
    assert tree.branches[0].action_type in ("land", "sequence")


def test_land_then_cast_scores_at_least_the_bare_cast():
    birds = {"name": "Birds of Paradise", "type_line": "Creature — Bird", "mana_cost": "{G}",
             "power": 0, "toughness": 1, "oracle_text": "Flying\n{T}: Add one mana of any color."}
    state = _state(
        hand=[birds, _forest() | {"owner_seat_id": 1}],
        battlefield=[_forest()],
        legal_actions=[],
        turn=2,
    )
    tree = MCTSEvaluator.evaluate(state, force=True)
    single = max(b.win_probability for b in tree.branches if b.action_type == "cast" and "Birds" in b.action)
    seq = max(b.win_probability for b in tree.branches if b.action_type == "sequence" and "Birds" in b.action)
    assert seq >= single
