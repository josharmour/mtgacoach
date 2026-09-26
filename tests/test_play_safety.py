"""Regressions from the Nature's Rhythm and Haywire Mite reports, 2026-09-24."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import filter_play_options, tutor_has_target, unsafe_play_reason
from arenamcp.rules_engine import RulesEngine


def _card(name, instance_id, type_line, seat=1, **fields):
    return dict(
        name=name,
        instance_id=instance_id,
        grp_id=instance_id,
        type_line=type_line,
        owner_seat_id=seat,
        controller_seat_id=seat,
        **fields,
    )


@pytest.fixture
def state(monkeypatch):
    cards = {
        10: SimpleNamespace(name="Haywire Mite", type_line="Artifact Creature", cmc=1, colors=[]),
        20: SimpleNamespace(name="Forest", type_line="Basic Land — Forest", cmc=0, colors=[]),
        30: SimpleNamespace(
            name="Dryad Arbor", type_line="Land Creature — Forest Dryad", cmc=0, colors=["G"]
        ),
    }
    import arenamcp.card_db as card_db

    monkeypatch.setattr(card_db, "get_card_database", lambda: SimpleNamespace(get_card_by_arena_id=cards.get))
    return {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
        "_bridge_connected": True,
        "_bridge_request_type": "ActionsAvailable",
        "turn": {"turn_number": 3, "active_player": 1, "phase": "Main1"},
        "deck_cards": [10, 20],
        "hand": [
            _card(
                "Nature's Rhythm",
                346,
                "Sorcery",
                mana_cost="{X}{G}{G}",
                oracle_text="Search your library for a creature card with mana value X or less, "
                "put it onto the battlefield, then shuffle. Harmonize {oXoGoGoGoG}",
            )
        ],
        "battlefield": [
            _card("Forest", 544, "Basic Land — Forest"),
            _card("Forest", 553, "Basic Land — Forest"),
        ],
    }


def _cast_decision(state, can_pass=True):
    card = state["hand"][0]
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": can_pass,
            "actions": [
                {
                    "actionType": "Cast",
                    "instanceId": card["instance_id"],
                    "grpId": card["grp_id"],
                    "hasAutoTap": True,
                    "manaCost": [{"color": '[ "X" ]', "count": 1}, {"color": '[ "Green" ]', "count": 2}],
                }
            ],
        },
        resolve_name=lambda grp: card["name"],
    )


def test_natures_rhythm_payable_does_not_mean_useful(state):
    decision = _cast_decision(state)
    assert decision.options[0].payable is True
    backend = Mock()
    backend.complete.return_value = '{"option_ids": ["idx:0"]}'
    planner = ActionPlanner(backend=backend)
    assert planner.plan_decision_options(decision, state) == ["pass"]
    prompt = backend.complete.call_args.args[1]
    assert "idx:0" not in prompt
    assert filter_play_options(decision, state).option_ids() == {"pass"}


def test_no_safe_options_are_not_restored(state):
    planner = ActionPlanner(backend=Mock())
    assert planner._filter_legal_actions_for_planning(state, ["Cast Nature's Rhythm [OK]"]) == []
    assert planner.plan_actions(state, "decision_required", ["Cast Nature's Rhythm [OK]"]).actions == []
    assert planner.plan_decision_options(_cast_decision(state, can_pass=False), state) == [DECLINE_DECISION]
    planner._backend.complete.assert_not_called()


def test_legacy_and_typed_share_x_tutor_preflight(state):
    planner = ActionPlanner(backend=Mock())
    assert planner._filter_legal_actions_for_planning(state, ["Cast Nature's Rhythm [OK]", "Pass"]) == [
        "Pass"
    ]
    state["battlefield"].append(_card("Forest", 554, "Basic Land — Forest"))
    assert unsafe_play_reason(state, state["hand"][0], "Cast") == ""
    assert "idx:0" in filter_play_options(_cast_decision(state), state).option_ids()


def test_x_zero_is_allowed_when_tutor_can_find_a_zero_cost_creature(state):
    state["deck_cards"].append(30)
    assert tutor_has_target(state, state["hand"][0], 0) is True
    assert unsafe_play_reason(state, state["hand"][0], "Cast") == ""


def test_visible_last_copy_is_not_a_library_target(state):
    state["deck_cards"] = [30]
    state["battlefield"].append(_card("Dryad Arbor", 900, "Land Creature"))
    state["battlefield"][-1]["grp_id"] = 30
    assert tutor_has_target(state, state["hand"][0], 0) is False
    state["deck_cards"].append(30)
    assert tutor_has_target(state, state["hand"][0], 0) is True


def test_color_restricted_x_tutor_does_not_count_colorless_zero(state):
    state["hand"][0]["oracle_text"] = (
        "Search your library for a green creature card with mana value X or less."
    )
    state["deck_cards"] = [10]
    assert tutor_has_target(state, state["hand"][0], 1) is False
    state["deck_cards"] = [30]
    assert tutor_has_target(state, state["hand"][0], 0) is True


@pytest.mark.parametrize(
    "oracle", ["Creatures you control gain trample.", "Put X +1/+1 counters on target creature."]
)
def test_other_x_spells_are_not_blanket_banned_at_zero(state, oracle):
    state["hand"][0]["oracle_text"] = oracle
    assert unsafe_play_reason(state, state["hand"][0], "Cast") == ""


def test_actual_action_cost_handles_alternative_cost_and_reduction(state):
    card = state["hand"][0]
    state["battlefield"].append(_card("Forest", 554, "Basic Land — Forest"))
    assert unsafe_play_reason(
        state, card, "Cast", {"manaCost": [{"color": "X", "count": 1}, {"color": "Green", "count": 4}]}
    )
    card["mana_cost"] = "{X}{G}{G}{G}{G}"
    state["_bridge_actions"] = [
        {
            "actionType": "Cast",
            "instanceId": card["instance_id"],
            "manaCost": [{"color": "X", "count": 1}, {"color": "Green", "count": 2}],
        }
    ]
    assert unsafe_play_reason(state, card, "Cast") == ""
    assert (
        unsafe_play_reason(
            state, card, "Cast", {"manaCost": [{"color": "X", "count": 1}, {"color": "Green", "count": 2}]}
        )
        == ""
    )


def _mite_board(state):
    mite = _card(
        "Haywire Mite",
        545,
        "Artifact Creature — Insect",
        oracle_text="When this creature dies, you gain 2 life.\n"
        "{oG}, Sacrifice this creature: Exile target noncreature artifact or noncreature enchantment.",
    )
    state["battlefield"].extend(
        [
            mite,
            _card("Lumbering Worldwagon", 652, "Artifact — Vehicle"),
            _card("Dusk Legion Duelist", 700, "Creature — Vampire Soldier", seat=2),
        ]
    )
    return mite


def test_mite_not_activated_into_own_worldwagon(state):
    mite = _mite_board(state)
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [{"actionType": "Activate", "instanceId": 545, "grpId": 545, "abilityGrpId": 153387}],
        },
        resolve_name=lambda grp: "Haywire Mite",
    )
    assert unsafe_play_reason(state, mite, "Activate")
    assert unsafe_play_reason(state, mite, "Cast") == ""
    assert filter_play_options(decision, state).option_ids() == {"pass"}
    assert ActionPlanner(backend=Mock())._filter_legal_actions_for_planning(
        state, ["Activate: Haywire Mite", "Pass"]
    ) == ["Pass"]
    state["battlefield"].append(_card("Enemy Mite", 701, "Artifact Creature", seat=2))
    assert unsafe_play_reason(state, mite, "Activate")
    state["battlefield"].append(_card("Enemy Enchantment", 702, "Enchantment", seat=2))
    assert unsafe_play_reason(state, mite, "Activate") == ""


def test_other_abilities_and_optional_removal_are_not_disabled(state):
    mite = _mite_board(state)
    mite["oracle_text"] += "\n{T}: Add {G}."
    assert unsafe_play_reason(state, mite, "Activate") == ""
    mite["oracle_text"] = "{G}: You may exile target artifact."
    assert unsafe_play_reason(state, mite, "Activate") == ""


def test_removal_disjunction_accepts_either_type_and_blink(state):
    card = {"oracle_text": "Destroy target artifact or enchantment."}
    state["battlefield"].append(_card("Opp enchantment", 700, "Enchantment", seat=2))
    assert unsafe_play_reason(state, card, "Cast") == ""
    card["oracle_text"] = "Exile target artifact or creature, then return it to the battlefield."
    state["battlefield"] = []
    assert unsafe_play_reason(state, card, "Cast") == ""


def test_mana_uses_controller_not_owner(state):
    stolen = deepcopy(state["battlefield"][0])
    stolen["controller_seat_id"] = 2
    state["battlefield"] = [stolen]
    assert RulesEngine._get_mana_pool(state, 1)["total"] == 0
    stolen["owner_seat_id"], stolen["controller_seat_id"] = 2, 1
    assert RulesEngine._get_mana_pool(state, 1)["total"] == 1


def test_typed_fallback_plays_land_then_passes_not_first_payable_spell(state):
    decision = _cast_decision(state)
    assert ActionPlanner.deterministic_option_pick(decision) == ["pass"]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [{"actionType": "Cast", "hasAutoTap": True}, {"actionType": "Play"}],
        }
    )
    assert ActionPlanner.deterministic_option_pick(decision) == ["idx:1"]


def test_executor_cannot_submit_a_filtered_cast_even_if_planner_returns_it(state):
    planner = Mock()
    planner.plan_decision_options.return_value = ["idx:0"]
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [{"actionType": "Cast", "instanceId": 346, "hasAutoTap": True}],
    }
    assert engine._try_typed_decision_path(state, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
    assert planner.plan_decision_options.call_args.args[0].option_ids() == {"pass"}


def test_cost_change_invalidates_request_fingerprint(state):
    from dataclasses import replace

    from arenamcp.request_tracker import decision_fingerprint

    decision = _cast_decision(state)
    option = replace(
        decision.options[0], meta={**decision.options[0].meta, "manaCost": [{"color": "Green", "count": 4}]}
    )
    changed = replace(decision, options=(option, *decision.options[1:]))
    assert decision_fingerprint(decision) != decision_fingerprint(changed)


def _ask_for_x(state):
    state["_bridge_request_type"] = "CastingTimeOptions"
    state["decision_context"] = {
        "type": "casting_time_options",
        "raw": {
            "castingTimeOptionReq": [
                {
                    "castingTimeOptionType": "CastingTimeOptionType_ChooseX",
                    "affectedId": 346,
                    "numericInputReq": {"numericInputType": "NumericInputType_ChooseX", "sourceId": 346},
                }
            ]
        },
    }


def test_x_choice_does_not_undo_tutor_preflight(state):
    _ask_for_x(state)
    state["battlefield"].append(_card("Forest", 554, "Basic Land — Forest"))
    assert RulesEngine.get_legal_actions(state) == ["X = 1"]
    planner = ActionPlanner(backend=Mock())
    assert not planner._is_action_legal(
        GameAction(ActionType.NUMERIC_INPUT, numeric_value=0),
        ["X = 1"],
        state["decision_context"],
        "CastingTimeOptions",
    )
    state["deck_cards"].append(30)
    assert RulesEngine.get_legal_actions(state) == ["X = 0", "X = 1"]


def test_unusable_x_does_not_turn_into_generic_casting_options(state):
    _ask_for_x(state)
    assert RulesEngine.get_legal_actions(state) == ["No useful X values"]
    planner = ActionPlanner(backend=Mock())
    assert (
        planner.plan_actions(state, "decision_required", RulesEngine.get_legal_actions(state)).actions == []
    )
    planner._backend.complete.assert_not_called()


def test_executor_and_emergency_fallback_do_not_submit_useless_x_zero(state):
    _ask_for_x(state)
    engine = AutopilotEngine(planner=Mock(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = Mock(connected=True)
    engine._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
    }
    engine._pause_for_manual = Mock()
    result = engine._try_gre_bridge(GameAction(ActionType.NUMERIC_INPUT, numeric_value=0), state)
    assert result.success is False
    engine._gre_bridge.submit_x.assert_not_called()
    engine._gre_bridge.submit_numeric.assert_not_called()
    assert engine._try_interactive_safe_default(state, "decision_required") is False
    assert engine._try_auto_respond_escape(state, "stuck X chooser") is False
    engine._gre_bridge.auto_respond.assert_not_called()
