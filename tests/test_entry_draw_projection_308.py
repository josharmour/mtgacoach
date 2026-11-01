"""G04 mechanics reconstruction; the original complete decision state is absent.

Retained audit: d1bad78e, requests346/533 and356/551; Lavamancer entered,
Innocence drew at351, Bowmasters pinged at355 and Army grew2->3 at356.
The mana/remaining board below is a test fixture, not the historical full board.
"""

from copy import deepcopy

import pytest
from tests.test_tactical_timing_backlog import card

from arenamcp.board_assessment import _assess
from arenamcp.board_model import build_board_model
from arenamcp.entry_draw_projection import model_reasons
from arenamcp.line_guard import _usable
from arenamcp.line_search import evaluate_plan, search_lines


def position():
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 12, "active_player": 1, "phase": "Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 8, "lands_played": 1},
            {"seat_id": 2, "life_total": 20},
        ],
        "battlefield": [
            card(1, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain"),
            card(
                70,
                "Enduring Innocence",
                "Lifelink\nWhenever one or more other creatures you control with power 2 or less enter, draw a card. This ability triggers only once each turn.",
                "Enchantment Creature",
                power=2,
                toughness=1,
            ),
            card(
                320,
                "Orcish Bowmasters",
                "Flash\nWhen Orcish Bowmasters enters and whenever an opponent draws a card except the first one they draw in each of their draw steps, Orcish Bowmasters deals 1 damage to any target. Then amass Orcs 1.",
                "Creature",
                seat=2,
                power=1,
                toughness=1,
            ),
            card(324, "Orc Army", "", "Token Creature — Orc Army", seat=2, power=2, toughness=2),
        ],
        "hand": [
            card(
                394,
                "Grim Lavamancer",
                "{R}, {T}, Exile two cards from your graveyard: Grim Lavamancer deals 2 damage to any target.",
                "Creature",
                "{R}",
                power=1,
                toughness=1,
            )
        ],
        "stack": [],
    }


def search(state):
    return search_lines(build_board_model(state), survival=True, lethal_now=False, hard_ms=1e9)


def test_retained_entry_chain_cannot_be_a_complete_survival_forecast():
    state = position()
    original = deepcopy(state)
    result = search(state)
    assert result.unsupported and "entry-draw continuation" in result.unsupported[0]
    assert "may trigger" in result.unsupported[0]
    assert not result.first_action and not result.lines and not _usable(result)
    assert not result.exact_first_attack and not result.only_survivor and not result.all_dead_at_first
    assert result.best.outcome == "unknown" and not result.best.complete
    assert result.dead_in is None and result.now_life is None
    evaluation = evaluate_plan(result, [{"cast": ["Grim Lavamancer"], "attack": "none"}])
    assert evaluation.line is None and evaluation.unmodelled
    assert state == original


@pytest.mark.parametrize("enabled", [True, False])
def test_legacy_fallback_cannot_restore_false_combat_certainty(monkeypatch, enabled):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "1" if enabled else "0")
    assessment = _assess(position())
    assert assessment is not None
    assert assessment.race == "unknown"
    assert not assessment.lethal_now and not assessment.lethal_next_turn and not assessment.all_in
    assert assessment.dead_in is None and assessment.our_clock is None
    assert any("entry-draw continuation unsupported" in note for note in assessment.unknowns)


@pytest.mark.parametrize(
    "change",
    [
        {"toughness": 2},
        {"oracle_text": "Hexproof"},
        {"oracle_text": "Indestructible"},
    ],
)
def test_body_protection_does_not_model_opponent_face_ping_or_amass(change):
    state = position()
    state["hand"][0].update(change)
    assert model_reasons(build_board_model(state))


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("change", ["removed", "phased", "controller"])
def test_absent_or_wrong_controller_engine_does_not_block_forecast(index, change):
    state = position()
    if change == "removed":
        state["battlefield"].pop(index)
    elif change == "phased":
        state["battlefield"][index]["is_phased_out"] = True
    else:
        state["battlefield"][index]["controller_seat_id"] = (
            3 - state["battlefield"][index]["controller_seat_id"]
        )
    assert not model_reasons(build_board_model(state))
    assert not search(state).unsupported


@pytest.mark.parametrize("change", ["no entry", "above cap", "GRE noncreature"])
def test_irrelevant_cast_does_not_block_forecast(change):
    state = position()
    if change == "no entry":
        state["hand"] = []
    elif change == "above cap":
        state["hand"][0]["modified_power"] = 3
    else:
        state["hand"][0]["card_types"] = ["CardType_Enchantment"]
    assert not model_reasons(build_board_model(state))


def test_current_modified_power_can_put_printed_large_creature_under_cap():
    state = position()
    state["hand"][0].update(power=3, modified_power=1)
    assert model_reasons(build_board_model(state))


def test_pending_creature_entry_is_also_unsupported():
    state = position()
    state["stack"] = state.pop("hand")
    model = build_board_model(state)
    assert model.our_stack_bodies
    assert model_reasons(model)
    assert search(state).unsupported


def test_pending_removal_does_not_erase_unresolved_entry_order():
    state = position()
    creature = state["hand"].pop()
    creature["oracle_text"] = "Flash\n" + creature["oracle_text"]
    state["stack"] = [
        card(500, "Shock", "Shock deals 2 damage to any target.", "Instant", "{R}", targeting=[320]),
        creature,
    ]
    model = build_board_model(state)
    assert 320 in model.our_stack_doomed and model.our_stack_bodies
    result = search(state)
    assert result.unsupported and not result.best.complete


def test_engine_itself_does_not_trigger_its_other_creature_clause():
    state = position()
    state["battlefield"].pop(1)
    state["hand"] = [position()["battlefield"][1]]
    assert not model_reasons(build_board_model(state))


def test_missing_object_ids_do_not_prove_entry_is_the_engine_itself():
    state = position()
    state["hand"][0].pop("instance_id")
    state["battlefield"][1].pop("instance_id")
    assert model_reasons(build_board_model(state))
