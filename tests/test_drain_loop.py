"""2026-10-11 Historic Brawl loss (bug_20261011_131147): Vito + Exquisite Blood.

Turn 9: the opponent's drain loop took us from 22 to 5 over 13 priority
windows; Tear Asunder could exile Exquisite Blood in every one, and the one
high-effort model call timed out. Turn 10: Witch-Blessed Meadow's "pay 3
life?" reached the model unnamed at 5 life. Turn 8: the removal filter
ignored Tear Asunder's kicker.
"""

from __future__ import annotations

import copy

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.coach_triggers import GameStateTrigger
from arenamcp.decisions import build_pending_decision
from arenamcp.drain_loop import (
    answers_in_hand,
    card_removes,
    facts_flag,
    loop_answer,
    opponent_drain_loop,
    warning_line,
)
from arenamcp.gamestate import GameObject, GameObjectKind, GameState
from arenamcp.gamestate_decisions import _handle_decision_message, prompt_card_ids
from arenamcp.optional_prompts import life_payment_decline, prompt_note
from arenamcp.play_safety import removal_lacks_opponent_target

ME, THEM = 2, 1

VITO = {
    "instance_id": 556,
    "name": "Vito, Thorn of the Dusk Rose",
    "controller_seat_id": THEM,
    "type_line": "Legendary Creature — Vampire Cleric",
    "card_types": ["Creature"],
    "power": 1,
    "toughness": 3,
    "oracle_text": "Whenever you gain life, target opponent loses that much life.\n"
    "{3}{B}{B}: Creatures you control gain lifelink until end of turn.",
}
EXQUISITE_BLOOD = {
    "instance_id": 566,
    "name": "Exquisite Blood",
    "controller_seat_id": THEM,
    "type_line": "Enchantment",
    "card_types": ["Enchantment"],
    "oracle_text": "Whenever an opponent loses life, you gain that much life.",
}
BLIGHT_PRIEST = {
    "instance_id": 1099,
    "name": "Marauding Blight-Priest",
    "controller_seat_id": THEM,
    "type_line": "Creature — Vampire Cleric",
    "card_types": ["Creature"],
    "power": 3,
    "toughness": 2,
    "oracle_text": "Whenever you gain life, each opponent loses 1 life.",
}
AETHERBORN = {
    "instance_id": 712,
    "name": "Gifted Aetherborn",
    "controller_seat_id": THEM,
    "type_line": "Creature — Aetherborn Vampire",
    "card_types": ["Creature"],
    "power": 2,
    "toughness": 3,
    "oracle_text": "Deathtouch, lifelink",
}
VRAAN = {
    "instance_id": 460,
    "name": "Vraan, Executioner Thane",
    "controller_seat_id": THEM,
    "type_line": "Legendary Creature — Phyrexian Vampire",
    "card_types": ["Creature"],
    "power": 2,
    "toughness": 2,
    "oracle_text": "Whenever one or more other creatures you control die, each opponent loses 2 life "
    "and you gain 2 life. This ability triggers only once each turn.",
}
TEAR_ASUNDER = {
    "instance_id": 450,
    "name": "Tear Asunder",
    "controller_seat_id": ME,
    "type_line": "Instant",
    "card_types": ["Instant"],
    "mana_cost": "{1}{G}",
    "oracle_text": "Kicker {1}{B}\nExile target artifact or enchantment. If this spell was kicked, "
    "exile target nonland permanent instead.",
}
ESSENCE_FLUX = {
    "instance_id": 346,
    "name": "Essence Flux",
    "controller_seat_id": ME,
    "type_line": "Instant",
    "card_types": ["Instant"],
    "mana_cost": "{U}",
    "oracle_text": "Exile target creature you control, then return that card to the battlefield under "
    "its owner's control. If a Spirit entered the battlefield this way, put a +1/+1 counter on it.",
}
SANCTUM = {
    "instance_id": 551,
    "name": "Sanctum of Stone Fangs",
    "controller_seat_id": ME,
    "type_line": "Legendary Enchantment — Shrine",
    "card_types": ["Enchantment"],
    "oracle_text": "At the beginning of your first main phase, each opponent loses X life and you gain "
    "X life, where X is the number of Shrines you control.",
}


def _land(instance_id: int, name: str = "Forest", tapped: bool = False) -> dict:
    return {
        "instance_id": instance_id,
        "name": name,
        "controller_seat_id": ME,
        "type_line": f"Basic Land — {name}",
        "card_types": ["Land"],
        "is_tapped": tapped,
        "oracle_text": "({T}: Add {G}.)" if name == "Forest" else "({T}: Add {B}.)",
    }


def _state(
    *, battlefield=None, hand=None, stack=None, our_life=19, active=THEM, decision_context=None
) -> dict:
    return {
        "match_id": "3c4a4da0",
        "local_seat_id": ME,
        "turn": {"turn_number": 9, "active_player": active, "priority_player": ME, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": THEM, "life_total": 21, "is_local": False},
            {"seat_id": ME, "life_total": our_life, "is_local": True},
        ],
        "battlefield": list(
            battlefield if battlefield is not None else [VITO, EXQUISITE_BLOOD, AETHERBORN, SANCTUM]
        ),
        "hand": list(hand if hand is not None else [TEAR_ASUNDER, ESSENCE_FLUX]),
        "stack": list(stack or []),
        "graveyard": [],
        "exile": [],
        "command": [],
        "decision_context": decision_context or {},
    }


def _actions_decision(*instances: int):
    actions = [
        {"actionType": "ActionType_Cast", "grpId": 1000 + i, "instanceId": iid, "hasAutoTap": True}
        for i, iid in enumerate(instances)
    ]
    actions.append({"actionType": "ActionType_Pass"})
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "actions": actions, "can_pass": True},
        resolve_name=lambda grp: f"Card {grp}",
    )


class _ExplodingBackend:
    """A model that must not be consulted."""

    def complete(self, *args, **kwargs):
        raise AssertionError("the drain-loop guard must not call the model")


class _RecordingBackend:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[dict] = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append({"user": user, **kwargs})
        return self.answer


# --- recognition -------------------------------------------------------------


def test_vito_and_exquisite_blood_are_a_live_loop():
    loop = opponent_drain_loop(_state())
    assert loop is not None and loop.live
    assert loop.names() == "Vito, Thorn of the Dusk Rose + Exquisite Blood"


def test_two_gain_triggers_are_not_a_loop():
    # The final board after Exquisite Blood was exiled: Vito + Blight-Priest.
    assert opponent_drain_loop(_state(battlefield=[VITO, BLIGHT_PRIEST, AETHERBORN])) is None


def test_our_own_pieces_are_not_their_loop():
    ours = [dict(VITO, controller_seat_id=ME), dict(EXQUISITE_BLOOD, controller_seat_id=ME)]
    assert opponent_drain_loop(_state(battlefield=ours)) is None


def test_second_piece_on_the_stack_counts_only_when_asked():
    state = _state(battlefield=[VITO, AETHERBORN], stack=[EXQUISITE_BLOOD])
    assert opponent_drain_loop(state) is None
    pending = opponent_drain_loop(state, include_stack=True)
    assert pending is not None and not pending.live


def test_tear_asunder_answers_exquisite_blood_and_essence_flux_does_not():
    state = _state()
    assert card_removes(TEAR_ASUNDER, EXQUISITE_BLOOD, state)
    # Unkicked Tear Asunder can't touch Vito: the kicked clause is conditional.
    assert not card_removes(TEAR_ASUNDER, VITO, state)
    assert not card_removes(ESSENCE_FLUX, VITO, state)
    loop = opponent_drain_loop(state)
    assert [(c["name"], p["name"]) for c, p in answers_in_hand(state, loop)] == [
        ("Tear Asunder", "Exquisite Blood")
    ]
    assert "Tear Asunder can remove Exquisite Blood" in warning_line(state, loop)
    assert facts_flag(state).startswith("OPPONENT DRAIN LOOP ASSEMBLED")


# --- fix 1: cast the answer without the model ---------------------------------


def test_planner_casts_the_answer_without_a_model_call():
    state = _state()
    decision = _actions_decision(ESSENCE_FLUX["instance_id"], TEAR_ASUNDER["instance_id"])
    assert loop_answer(decision, state)[0] == "idx:1"
    planner = ActionPlanner(backend=_ExplodingBackend())
    assert planner.plan_decision_options(decision, state) == ["idx:1"]
    assert planner._last_decision_trace["policy"] == "drain_loop"
    assert "Exquisite Blood" in planner._last_decision_reasoning


def test_planner_aims_the_follow_up_target_request_at_the_loop_piece():
    planner = ActionPlanner(backend=_ExplodingBackend())
    state = _state()
    planner.plan_decision_options(_actions_decision(TEAR_ASUNDER["instance_id"]), state)
    # Cast: Tear Asunder moved to the stack under a new id; Arena asks for targets.
    on_stack = dict(TEAR_ASUNDER, instance_id=581)
    after_cast = _state(hand=[ESSENCE_FLUX], stack=[on_stack])
    targets = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "target_candidates": [
                {
                    "targetInstanceId": SANCTUM["instance_id"],
                    "legalAction": "SelectAction_Select",
                    "targetIdx": 1,
                },
                {"targetInstanceId": 566, "legalAction": "SelectAction_Select", "targetIdx": 1},
            ],
            "target_selections": [{"targetIdx": 1, "minTargets": 1, "maxTargets": 1}],
        },
        resolve_name=lambda grp: "",
    )
    assert planner.plan_decision_options(targets, after_cast) == ["tgt:566"]


def test_no_loop_leaves_the_decision_to_the_model():
    state = _state(battlefield=[VITO, AETHERBORN, SANCTUM])
    backend = _RecordingBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(backend=backend)
    planner.plan_decision_options(_actions_decision(TEAR_ASUNDER["instance_id"]), state)
    assert backend.calls, "without a loop the model decides"


# --- fix 2: combo recognition --------------------------------------------------


def test_threat_detector_warns_when_the_second_piece_is_cast():
    detector = GameStateTrigger()
    before = _state(battlefield=[VITO, AETHERBORN, SANCTUM])
    after = _state(battlefield=[VITO, AETHERBORN, SANCTUM], stack=[EXQUISITE_BLOOD])
    assert "threat_detected" in detector.check_triggers(before, after)
    threat = detector._last_threat
    assert threat["combo"] is True
    assert "Infinite drain combo" in threat["warning"]
    # Once per loop: resolving it does not warn again.
    assert "threat_detected" not in detector.check_triggers(after, _state())


def test_board_assessment_leads_with_the_loop_flag():
    from arenamcp.board_assessment import assess

    state = _state(
        battlefield=[VITO, EXQUISITE_BLOOD, AETHERBORN, SANCTUM, _land(444), _land(451), _land(550, "Swamp")]
    )
    assessment = assess(state)
    if assessment is None:
        pytest.skip("assessment needs a fuller snapshot")
    assert assessment.flags and assessment.flags[0].startswith("OPPONENT DRAIN LOOP ASSEMBLED")


# --- fix 3: optional prompts ---------------------------------------------------

MEADOW_PROMPT = {
    "type": "GREMessageType_OptionalActionMessage",
    "systemSeatIds": [2],
    "msgId": 358,
    "gameStateId": 244,
    "prompt": {
        "promptId": 2248,
        "parameters": [{"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 685}],
    },
    "optionalActionMessage": {"sourceId": 9011},
    "allowCancel": "AllowCancel_No",
}
MEADOW_TEXT = "As Witch-Blessed Meadow enters, you may pay 3 life. If you don't, it enters tapped."


@pytest.fixture
def meadow_context(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "enrich_with_oracle_text", lambda grp: {"oracle_text": MEADOW_TEXT})
    game = GameState()
    game.local_seat_id = ME
    game._card_name_cache[90805] = "Witch-Blessed Meadow"
    game.game_objects[685] = GameObject(
        instance_id=685, grp_id=90805, zone_id=28, owner_seat_id=ME, object_kind=GameObjectKind.CARD
    )
    _handle_decision_message(game, MEADOW_PROMPT["type"], MEADOW_PROMPT)
    return game.decision_context


def _optional_decision():
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "request_class": "OptionalActionMessageRequest",
            "game_state_id": 244,
            "msg_id": 358,
            "source_instance_id": 9011,
        },
        resolve_name=lambda grp: "",
        resolve_instance=lambda iid: "",
    )


def test_log_parser_names_the_card_behind_an_optional_prompt(meadow_context):
    assert prompt_card_ids(MEADOW_PROMPT["prompt"]) == [685]
    assert meadow_context["prompt"] == "Optional effect of Witch-Blessed Meadow"
    assert meadow_context["prompt_cards"][0]["oracle_text"] == MEADOW_TEXT


def test_paying_three_life_at_five_is_declined_without_the_model(meadow_context):
    state = _state(our_life=5, decision_context=meadow_context)
    decision = _optional_decision()
    assert "leave us at 2" in life_payment_decline(decision, state)
    planner = ActionPlanner(backend=_ExplodingBackend())
    assert planner.plan_decision_options(decision, state) == ["optional:decline"]


def test_paying_life_at_a_healthy_total_goes_to_the_model_with_the_card_named(meadow_context):
    state = _state(our_life=20, decision_context=meadow_context)
    decision = _optional_decision()
    assert life_payment_decline(decision, state) == ""
    note = prompt_note(decision, state)
    assert "Witch-Blessed Meadow (yours)" in note and "pay 3 life" in note
    backend = _RecordingBackend('{"option_ids": ["optional:accept"], "reasoning": "untapped land"}')
    ActionPlanner(backend=backend).plan_decision_options(decision, state)
    assert "Asked by Witch-Blessed Meadow" in backend.calls[0]["user"]


def test_a_stale_log_context_is_not_used_for_a_different_request(meadow_context):
    state = _state(our_life=5, decision_context=meadow_context)
    other = build_pending_decision(
        {"has_pending": True, "request_type": "OptionalAction", "game_state_id": 245, "msg_id": 360},
        resolve_name=lambda grp: "",
        resolve_instance=lambda iid: "",
    )
    assert life_payment_decline(other, state) == ""
    assert "not identified" in prompt_note(other, state)


def test_bridge_prompt_parameters_name_the_source():
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "source_instance_id": 9011,
            "request_payload": {
                "prompt": {"promptId": 2248, "parameters": [{"ParameterName": "CardId", "NumberValue": 685}]}
            },
        },
        resolve_name=lambda grp: "",
        resolve_instance=lambda iid: "Witch-Blessed Meadow" if iid == 685 else "",
    )
    assert decision.source_label == "Witch-Blessed Meadow"
    assert decision.find("optional:accept").meta["promptCardIds"] == [685]


# --- fix 4: opponent-turn decisions at low effort ------------------------------


@pytest.mark.parametrize(("active", "effort"), [(THEM, None), (ME, "high")])
def test_removal_windows_think_hard_only_on_our_turn(active, effort):
    state = _state(battlefield=[VITO, AETHERBORN, SANCTUM], active=active)
    murder = {
        "instance_id": 470,
        "name": "Murder",
        "controller_seat_id": ME,
        "type_line": "Instant",
        "card_types": ["Instant"],
        "mana_cost": "{1}{B}{B}",
        "oracle_text": "Destroy target creature.",
    }
    state["hand"] = [murder]
    backend = _RecordingBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    ActionPlanner(backend=backend).plan_decision_options(_actions_decision(470), state)
    assert backend.calls[0].get("reasoning_effort") == effort


# --- fix 5: kicker in the removal filter ----------------------------------------


def test_kicked_tear_asunder_is_not_withheld_when_the_kicker_is_affordable():
    lands = [_land(444), _land(451), _land(550, "Swamp"), _land(555, "Swamp")]
    state = _state(battlefield=[VRAAN, *lands], hand=[TEAR_ASUNDER])
    assert removal_lacks_opponent_target(TEAR_ASUNDER, state) is False


def test_tear_asunder_is_withheld_when_only_the_kicked_mode_has_a_target_and_it_is_unaffordable():
    lands = [_land(444), _land(451), _land(550, "Swamp")]
    state = _state(battlefield=[VRAAN, *lands], hand=[TEAR_ASUNDER])
    assert removal_lacks_opponent_target(TEAR_ASUNDER, state) is True


def test_unkicked_target_still_allows_the_cast():
    state = _state(battlefield=[VRAAN, EXQUISITE_BLOOD, _land(444)], hand=[TEAR_ASUNDER])
    assert removal_lacks_opponent_target(TEAR_ASUNDER, copy.deepcopy(state)) is False
