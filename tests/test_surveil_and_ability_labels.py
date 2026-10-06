"""Scry/surveil GroupRequests and activation labels (field report 2026-10-06).

Player.log showed every autopilot surveil answered
``[{Hand/Top: []}, {Library/Bottom: [card]}]`` against the request's specs
``[Library/Top ub=1, Graveyard ub=1]``. Arena reads the groups positionally,
so the second group was the graveyard slot: 7 of 7 surveilled cards were
binned (Sphinx of False Conclusions twice). The option list also offered
only "Bottom card #298", so "keep on top" could not be chosen and the model
never learned the card was a Sphinx — even though Player.log had revealed it.

Separately, Undulating Witness's Basic landcycling from hand was labelled
"Activate: Undulating Witness"; the model called it impossible "since it's
still in hand", a pump, or a graveyard ability.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import arenamcp.autopilot as autopilot_module
from arenamcp import decisions
from arenamcp.action_planner import ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import (
    build_pending_decision,
    default_group_choice,
    is_london_group,
    reasoning_choice_conflict,
    submit_option,
)
from arenamcp.gamestate import GameState
from arenamcp.gamestate_models import GameObject, Zone, ZoneType

SPHINX = {
    "grp_id": 106267,
    "name": "Sphinx of False Conclusions",
    "type_line": "Creature — Sphinx Illusion",
    "mana_cost": "{2}{U}{U}",
    "cmc": 4,
}
MOUNTAIN = {
    "grp_id": 106533,
    "name": "Mountain",
    "type_line": "Basic Land — Mountain",
    "mana_cost": "",
    "cmc": 0,
}
DUELIST = {
    "grp_id": 106255,
    "name": "Divining Duelist",
    "type_line": "Creature — Human Wizard",
    "mana_cost": "{1}{U}",
    "cmc": 2,
}
CARDS = {298: SPHINX, 303: MOUNTAIN, 300: DUELIST}

# GroupReq gameStateId 127 as Player.log recorded it (protobuf OriginalNames).
SURVEIL_LOG_POLL = {
    "has_pending": True,
    "request_type": "Group",
    "request_class": "GroupRequest",
    "game_state_id": 127,
    "msg_id": 180,
    "group_instance_ids": [298],
    "group_specs": [
        {"upperBound": 1, "zoneType": "ZoneType_Library", "subZoneType": "SubZoneType_Top"},
        {"upperBound": 1, "zoneType": "ZoneType_Graveyard"},
    ],
    "group_context": "GroupingContext_Surveil",
}
# The same request as the bridges serialize it (enum names, explicit None subzone).
SURVEIL_BRIDGE_POLL = {
    **SURVEIL_LOG_POLL,
    "group_specs": [
        {"lowerBound": 0, "upperBound": 1, "zoneType": "Library", "subZoneType": "Top", "isFacedown": False},
        {"lowerBound": 0, "upperBound": 1, "zoneType": {"e": "Graveyard", "v": 4}, "subZoneType": "None"},
    ],
    "group_context": "Surveil",
}


def _scry_poll(*ids: int) -> dict:
    return {
        "has_pending": True,
        "request_type": "Group",
        "group_instance_ids": list(ids),
        "group_specs": [
            {"upperBound": len(ids), "zoneType": "ZoneType_Library", "subZoneType": "SubZoneType_Top"},
            {"upperBound": len(ids), "zoneType": "ZoneType_Library", "subZoneType": "SubZoneType_Bottom"},
        ],
        "group_context": "GroupingContext_Scry",
    }


class _GroupBridge:
    def __init__(self, poll=None):
        self.connected = True
        self.poll_resp = poll
        self.groups = []

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.poll_resp

    def submit_group(self, groups):
        self.groups.append(groups)
        return True


def _surveil(poll=SURVEIL_LOG_POLL):
    return build_pending_decision(poll, resolve_card=CARDS.get)


# --- surveil -----------------------------------------------------------------


def test_surveil_offers_keep_on_top_and_graveyard_naming_the_card():
    for poll in (SURVEIL_LOG_POLL, SURVEIL_BRIDGE_POLL):
        decision = _surveil(poll)
        assert decision is not None and decision.request_type == "Group"
        assert decision.min_select == decision.max_select == 1
        assert [o.option_id for o in decision.options] == ["grp:298:top", "grp:298:graveyard"]
        assert decision.find("grp:298:top").label == (
            "Keep Sphinx of False Conclusions on top of your library ({2}{U}{U} Creature — Sphinx Illusion)"
        )
        assert decision.find("grp:298:graveyard").label.startswith(
            "Put Sphinx of False Conclusions into your graveyard"
        )
        assert decision.source_label.startswith("Surveil 1")


def test_surveil_keep_on_top_answers_each_spec_in_order():
    bridge = _GroupBridge()
    assert submit_option(bridge, _surveil(), ["grp:298:top"]) is True
    # MTGA's SurveilWorkflow shape: [Library/Top kept, Graveyard (empty)].
    assert bridge.groups == [
        [
            {"ids": [298], "zone": "Library", "sub_zone": "Top"},
            {"ids": [], "zone": "Graveyard", "sub_zone": None},
        ]
    ]


def test_surveil_to_graveyard_uses_the_graveyard_slot_not_hand_and_bottom():
    bridge = _GroupBridge()
    assert submit_option(bridge, _surveil(SURVEIL_BRIDGE_POLL), ["grp:298:graveyard"]) is True
    groups = bridge.groups[0]
    assert groups == [
        {"ids": [], "zone": "Library", "sub_zone": "Top"},
        {"ids": [298], "zone": "Graveyard", "sub_zone": None},
    ]
    # The shape that binned every card on 2026-10-06 is gone.
    assert all(group["zone"] != "Hand" and group["sub_zone"] != "Bottom" for group in groups)


def test_unrevealed_card_is_labelled_by_instance_and_destination():
    decision = build_pending_decision(SURVEIL_LOG_POLL, resolve_card=lambda iid: {})
    assert decision.find("grp:298:top").label == "Keep card #298 on top of your library"
    assert decision.find("grp:298:graveyard").label == "Put card #298 into your graveyard"


def test_surveilled_card_is_named_from_the_log_fed_game_state(monkeypatch):
    # Player.log revealed instance 298 as grpId 106267 (Visibility_Private,
    # viewers [2]) in zone 36, the local library, in the GroupReq's own event.
    state = GameState()
    state.zones[36] = Zone(zone_id=36, zone_type=ZoneType.LIBRARY, owner_seat_id=2, object_instance_ids=[298])
    state.game_objects[298] = GameObject(instance_id=298, grp_id=106267, zone_id=36, owner_seat_id=2)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    monkeypatch.setattr(
        decisions, "_default_card_resolver", lambda grp: SPHINX if grp == 106267 else {"error": "x"}
    )
    decision = build_pending_decision(SURVEIL_LOG_POLL)
    assert "Sphinx of False Conclusions" in decision.find("grp:298:top").label
    assert decisions._default_instance_zone(298) == "Library"


# --- scry --------------------------------------------------------------------


def test_scry_one_top_or_bottom():
    decision = build_pending_decision(_scry_poll(303), resolve_card=CARDS.get)
    assert [o.option_id for o in decision.options] == ["grp:303:top", "grp:303:bottom"]
    assert decision.find("grp:303:bottom").label.startswith("Put Mountain on the bottom of your library")
    bridge = _GroupBridge()
    assert submit_option(bridge, decision, ["grp:303:bottom"]) is True
    assert bridge.groups[0] == [
        {"ids": [], "zone": "Library", "sub_zone": "Top"},
        {"ids": [303], "zone": "Library", "sub_zone": "Bottom"},
    ]


def test_multi_card_scry_needs_one_destination_per_card_and_keeps_listed_order():
    decision = build_pending_decision(_scry_poll(298, 303, 300), resolve_card=CARDS.get)
    assert decision.min_select == decision.max_select == 3
    assert "first = top" in decision.source_label
    # Destination-major: taking the first N options leaves the library as is.
    assert [o.option_id for o in decision.options[:3]] == ["grp:298:top", "grp:303:top", "grp:300:top"]
    assert decision.selection_is_valid(["grp:300:top", "grp:303:bottom", "grp:298:top"])
    assert not decision.selection_is_valid(
        ["grp:298:top", "grp:298:bottom", "grp:300:top"]
    )  # 298 twice, 303 never
    bridge = _GroupBridge()
    assert submit_option(bridge, decision, ["grp:298:top", "grp:298:bottom", "grp:300:top"]) is False
    assert bridge.groups == []
    assert submit_option(bridge, decision, ["grp:300:top", "grp:303:bottom", "grp:298:top"]) is True
    assert bridge.groups[0] == [
        {"ids": [300, 298], "zone": "Library", "sub_zone": "Top"},
        {"ids": [303], "zone": "Library", "sub_zone": "Bottom"},
    ]


def test_london_mulligan_shape_is_unchanged():
    london = {
        "has_pending": True,
        "request_type": "Group",
        "group_instance_ids": [245, 244, 243, 242, 241, 240, 239],
        "group_specs": [
            {"lowerBound": 5, "upperBound": 5, "zoneType": "ZoneType_Hand", "subZoneType": "SubZoneType_Top"},
            {
                "lowerBound": 2,
                "upperBound": 2,
                "zoneType": "ZoneType_Library",
                "subZoneType": "SubZoneType_Bottom",
            },
        ],
    }
    assert is_london_group(london["group_specs"], "")  # shape alone, no context
    assert not is_london_group(SURVEIL_LOG_POLL["group_specs"], "GroupingContext_Surveil")
    assert not is_london_group(_scry_poll(1, 2)["group_specs"], "Scry")
    decision = build_pending_decision(london)
    assert decision.min_select == decision.max_select == 2
    assert decision.find("grp:243").label == "Bottom card #243"
    bridge = _GroupBridge()
    assert submit_option(bridge, decision, ["grp:243", "grp:239"]) is True
    keep, bottom = bridge.groups[0]
    assert bottom == {"ids": [243, 239], "zone": "Library", "sub_zone": "Bottom"}
    assert keep["zone"] == "Hand" and len(keep["ids"]) == 5


# --- safe default ------------------------------------------------------------


def _board(lands_in_play: int, hand: list[dict]) -> dict:
    land = {"type_line": "Basic Land — Island", "controller_seat_id": 2}
    return {
        "local_seat_id": 2,
        "battlefield": [dict(land, instance_id=900 + i) for i in range(lands_in_play)],
        "hand": hand,
    }


def test_default_keeps_a_castable_spell_on_top():
    # T7 13:54:49 — four lands out: the Sphinx is castable and must stay.
    choice = default_group_choice(_surveil(), _board(4, [MOUNTAIN]))
    assert choice == ["grp:298:top"]


def test_default_bins_an_excess_land_and_keeps_needed_ones():
    decision = build_pending_decision(
        {**SURVEIL_LOG_POLL, "group_instance_ids": [303]}, resolve_card=CARDS.get
    )
    witness = {"type_line": "Creature — Serpent", "mana_cost": "{4}{U}", "cmc": 5}
    assert default_group_choice(decision, _board(6, [witness])) == ["grp:303:graveyard"]
    assert default_group_choice(decision, _board(2, [witness])) == ["grp:303:top"]


def test_default_bins_a_spell_far_above_the_mana_in_reach():
    assert default_group_choice(_surveil(), _board(1, [])) == ["grp:298:graveyard"]


def test_default_leaves_unknown_cards_and_unknown_boards_on_top():
    unknown = build_pending_decision(SURVEIL_LOG_POLL, resolve_card=lambda iid: {})
    assert default_group_choice(unknown, _board(9, [])) == ["grp:298:top"]
    assert default_group_choice(_surveil(), None) == ["grp:298:top"]


def test_default_scry_choice_is_a_valid_assignment():
    decision = build_pending_decision(_scry_poll(298, 303, 300), resolve_card=CARDS.get)
    choice = default_group_choice(decision, _board(7, []))
    assert decision.selection_is_valid(choice)
    assert set(choice) == {"grp:298:top", "grp:300:top", "grp:303:bottom"}


def test_legacy_safe_default_no_longer_bins_the_surveilled_card(monkeypatch):
    from tests.test_autopilot_safe_default_group import _engine as legacy_engine

    monkeypatch.setattr(decisions, "_default_instance_card", CARDS.get)
    bridge = _GroupBridge(SURVEIL_BRIDGE_POLL)
    engine, _ = legacy_engine(bridge)
    result = engine._try_gre_bridge_group_default(_board(4, []), SURVEIL_BRIDGE_POLL)
    assert result is not None and result.success
    assert bridge.groups == [
        [
            {"ids": [298], "zone": "Library", "sub_zone": "Top"},
            {"ids": [], "zone": "Graveyard", "sub_zone": None},
        ]
    ]


# --- typed path end to end ---------------------------------------------------


class _Backend:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.response


def test_typed_path_lets_the_model_keep_the_sphinx(monkeypatch):
    monkeypatch.setattr(decisions, "_default_instance_card", CARDS.get)
    bridge = _GroupBridge(SURVEIL_BRIDGE_POLL)
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._backend = _Backend('{"option_ids": ["grp:298:top"], "reasoning": "Sphinx is my best threat."}')
    planner._timeout = 5.0
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    engine = AutopilotEngine(
        planner=planner,
        mapper=MagicMock(),
        controller=MagicMock(),
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=False),
    )
    state = {
        "turn": {"turn_number": 7, "phase": "Phase_Main1"},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "_bridge_connected": True,
        "battlefield": [],
        "hand": [],
    }
    assert engine._try_typed_decision_path(state, "decision_required") is True
    prompt = next(p for p in planner._backend.prompts if "PENDING DECISION: Group" in p)
    assert "- grp:298:top: Keep Sphinx of False Conclusions on top of your library" in prompt
    assert "- grp:298:graveyard: Put Sphinx of False Conclusions into your graveyard" in prompt
    assert bridge.groups[-1][0] == {"ids": [298], "zone": "Library", "sub_zone": "Top"}


# --- activation labels -------------------------------------------------------

LANDCYCLING, PUMP, UNEARTH, SURVEIL, DRAW = 170331, 153236, 9001, 208424, 208425
ABILITIES = {
    LANDCYCLING: ("", "Basic landcycling {o2}"),
    PUMP: ("", "{o2}: this gets +1/-1 until end of turn."),
    UNEARTH: ("", "Unearth {o1oR}"),
    SURVEIL: ("-1", "Surveil 1."),
    DRAW: ("-3", "Draw a card."),
}


def _activations(*actions, zones=None):
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": list(actions)},
        resolve_name={106272: "Undulating Witness", 106555: "Jace", 5000: "Dregscape Zombie"}.get,
        resolve_zone=(zones or {}).get,
    )


def _activate(grp, iid, ability):
    return {"actionType": "ActionType_Activate", "grpId": grp, "instanceId": iid, "abilityGrpId": ability}


def test_landcycling_from_hand_names_ability_zone_and_effect(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ABILITIES.get(aid, ("", "")))
    decision = _activations(_activate(106272, 241, LANDCYCLING), zones={241: "Hand"})
    label = decision.find("idx:0").label
    assert label == (
        "Activate: Undulating Witness [from hand: Basic landcycling {2} — "
        "discard it to search for a basic land card and put it into your hand]"
    )
    # autopilot's repeat-activation guard still reads the bare source name.
    assert AutopilotEngine._activation_source_name(label) == "Undulating Witness"


def test_keyword_implies_the_zone_when_the_log_cannot_place_the_card(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ABILITIES.get(aid, ("", "")))
    decision = _activations(_activate(106272, 241, LANDCYCLING), _activate(5000, 77, UNEARTH))
    assert decision.find("idx:0").label.startswith(
        "Activate: Undulating Witness [from hand: Basic landcycling {2}"
    )
    assert decision.find("idx:1").label.startswith(
        "Activate: Dregscape Zombie [from graveyard: Unearth {1}{R} — "
    )


def test_battlefield_ability_shows_text_without_a_zone(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ABILITIES.get(aid, ("", "")))
    decision = _activations(_activate(106272, 371, PUMP), zones={371: "Battlefield"})
    assert (
        decision.find("idx:0").label
        == "Activate: Undulating Witness [{2}: this gets +1/-1 until end of turn.]"
    )


def test_sibling_abilities_are_left_for_the_sibling_labeler(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ABILITIES.get(aid, ("", "")))
    decision = _activations(
        _activate(106555, 355, SURVEIL), _activate(106555, 355, DRAW), zones={355: "Battlefield"}
    )
    assert [o.label for o in decision.options[:2]] == ["Activate: Jace", "Activate: Jace"]
    labelled = AutopilotEngine._label_sibling_activations(decision, describe=ABILITIES.get)
    assert labelled.find("idx:1").label == "Activate: Jace [-3: Draw a card.]"  # not double-labelled
    lone = _activations(_activate(106555, 355, SURVEIL), zones={355: "Battlefield"})
    assert lone.find("idx:0").label == "Activate: Jace [-1: Surveil 1.]"


def test_unknown_ability_text_keeps_the_plain_label(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ("", ""))
    assert _activations(_activate(106272, 371, PUMP)).find("idx:0").label == "Activate: Undulating Witness"
    hand = _activations(_activate(106272, 241, PUMP), zones={241: "Hand"})
    assert hand.find("idx:0").label == "Activate: Undulating Witness [from hand]"


def test_arena_mana_text_is_cleaned():
    assert decisions.clean_rules_text("Flashback—{o1o(U/R)o(U/R)}, Discard a card.") == (
        "Flashback—{1}{U/R}{U/R}, Discard a card."
    )
    assert decisions.clean_rules_text("{oT}: Add {oU}. <i>CARDNAME</i>", "Island") == "{T}: Add {U}. Island"


def test_activation_submission_carries_the_ability_identity(monkeypatch):
    monkeypatch.setattr(decisions, "_default_ability_description", lambda aid: ABILITIES.get(aid, ("", "")))
    calls = []

    class _Bridge:
        def submit_action_by_index(self, index, expected=None):
            calls.append((index, expected))
            return True

    decision = _activations(_activate(106272, 241, LANDCYCLING))
    assert submit_option(_Bridge(), decision, ["idx:0"]) is True
    assert calls == [(0, {"instanceId": 241, "grpId": 106272, "abilityGrpId": LANDCYCLING})]


# --- reasoning vs chosen option (G1 T8) ---------------------------------------


def _t8_decision():
    # T8 13:51:18 menu: Witness cast/landcycle ×2, Tam's Resistance, ...
    names = {106272: "Undulating Witness", 106394: "Tam's Resistance", 106399: "Keeper of the Quiet Hour"}
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 106272, "instanceId": 240},
            {"actionType": "ActionType_Cast", "grpId": 106394, "instanceId": 242},
            {"actionType": "ActionType_Cast", "grpId": 106399, "instanceId": 293},
            _activate(106272, 240, LANDCYCLING),
            _activate(106272, 364, LANDCYCLING),
            {"actionType": "ActionType_Pass"},
        ],
    }
    return build_pending_decision(poll, resolve_name=names.get, resolve_zone=lambda iid: "Hand"), names.get


def test_reasoning_that_cycles_the_witness_flags_a_tams_resistance_pick():
    decision, names = _t8_decision()
    reasoning = (
        "Cycling Undulating Witness for {2} digs for a land drop and Arni, matching the "
        "stalled-development plan; Tam's Resistance has no creature to buff."
    )
    assert reasoning_choice_conflict(decision, ["idx:1"], reasoning, names) == ["idx:3", "idx:4"]


def test_consistent_or_unclear_reasoning_is_not_flagged():
    decision, names = _t8_decision()
    for chosen, reasoning in (
        (["idx:1"], "Casting Tam's Resistance banks Empower; cycling Undulating Witness can wait."),
        (["idx:3"], "Cycling Undulating Witness finds a land drop."),
        (["idx:1"], "Instead of cycling Undulating Witness, cast Tam's Resistance for Empower."),
        (["idx:1"], "Holding the Witness; nothing else to do."),
        (["pass"], "Cycling Undulating Witness is too slow; pass."),
    ):
        assert reasoning_choice_conflict(decision, chosen, reasoning, names) == [], reasoning


def test_planner_follows_reasoning_when_it_names_a_different_card(monkeypatch):
    """G1 T8 end to end: reasoning cycles the Witness, the answer said Tam's Resistance."""
    import dataclasses

    from arenamcp import decisions as decisions_module
    from arenamcp.action_planner import ActionPlanner

    decision, names = _t8_decision()
    # In the logged window Tam's Resistance was payable (autotap solution present).
    decision = dataclasses.replace(
        decision,
        options=tuple(
            dataclasses.replace(o, payable=True) if o.option_id == "idx:1" else o for o in decision.options
        ),
    )
    monkeypatch.setattr(
        decisions_module, "_default_card_resolver", lambda grp_id: {"name": names(grp_id) or ""}
    )
    planner = ActionPlanner.__new__(ActionPlanner)

    def answer(decision, game_state):
        planner._last_decision_reasoning = "Cycling Undulating Witness for {2} digs for a land drop; Tam's Resistance has no creature to buff."
        return ["idx:1"]

    monkeypatch.setattr(planner, "_llm_decision_options", answer, raising=False)
    monkeypatch.setattr("arenamcp.action_planner.filter_play_options", lambda d, s: d)
    assert planner.plan_decision_options(decision, {"turn": {}}) == ["idx:3"]


def test_typed_path_surveil_uses_the_safe_default_when_the_model_answer_is_unusable(monkeypatch):
    from tests.test_typed_decision_path import _engine, _planner_with, _state, _TypedBridge

    bridge = _TypedBridge(SURVEIL_BRIDGE_POLL)
    bridge.groups = None
    bridge.submit_group = lambda groups: (setattr(bridge, "groups", groups), True)[1]
    monkeypatch.setattr(
        "arenamcp.decisions.default_group_choice", lambda decision, state=None: ["grp:298:top"]
    )
    engine = _engine(monkeypatch, bridge, _planner_with("nonsense"))
    assert engine._try_typed_decision_path(_state(), "decision_required") is True
    assert bridge.groups[0]["ids"] == [298] and bridge.groups[1]["ids"] == []
