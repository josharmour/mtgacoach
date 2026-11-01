"""#340: 1c28a6d6 T6, Belladonna286 + hand295 incorrectly forecast two bodies.

Source: task-2/monitoring/1c28a6d6-legend-candidate.json, requests99/125 and102/130.
CR704.5j applies before future attacks; it does not prevent casts or entry triggers.
"""

from copy import deepcopy

import pytest

from arenamcp.board_model import build_board_model
from arenamcp.legend_rule import cast_note, conflicts, resolve_legends
from arenamcp.line_search import search_lines
from arenamcp.line_search_moves import Moves
from arenamcp.play_safety import unsafe_play_reason

BELL = (
    "Whenever a token you control enters, you gain 1 life if this is the first time this ability "
    "has resolved this turn. If it's the second time, draw a card. If it's the third time, "
    "put a +1/+1 counter on each creature you control."
)


def creature(iid, name="Belladonna Took", seat=2, **extra):
    return dict(
        instance_id=iid,
        name=name,
        controller_seat_id=seat,
        owner_seat_id=seat,
        type_line="Legendary Creature — Halfling Citizen",
        card_types=["CardType_Creature"],
        power=2,
        toughness=2,
        oracle_text=BELL,
        mana_cost="{1}{W}",
        rarity="-",
        turn_entered_battlefield=4,
        **extra,
    )


def board():
    old, new = creature(286), creature(295)
    new["turn_entered_battlefield"] = -1
    voice = creature(219, "Voice of Victory")
    voice.update(
        type_line="Creature — Human Bard",
        power=1,
        toughness=3,
        oracle_text="Mobilize 2\nYour opponents can't cast spells during your turn.",
    )
    hydra = creature(300, "Mossborn Hydra", 1)
    hydra.update(type_line="Creature — Elemental Hydra", power=1, toughness=1, oracle_text="Trample")
    lands = [
        dict(
            instance_id=i,
            name=n,
            type_line="Basic Land — " + n,
            controller_seat_id=2,
            owner_seat_id=2,
            oracle_text="{T}: Add {" + c + "}.",
            is_tapped=False,
        )
        for i, n, c in [(285, "Plains", "W"), (281, "Mountain", "R")]
    ]
    return dict(
        match_id="1c28a6d6",
        local_seat_id=2,
        turn=dict(turn_number=6, active_player=2, priority_player=2, phase="Phase_Main1", step=""),
        players=[
            dict(seat_id=2, is_local=True, life_total=18, lands_played=1),
            dict(seat_id=1, is_local=False, life_total=20, lands_played=1),
        ],
        battlefield=[old, hydra, *lands],
        hand=[voice, new],
        stack=[],
        graveyard=[],
    )


def moves(state):
    model = build_board_model(state)
    assert model is not None
    return Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)


def cast(m, name="Belladonna Took"):
    hs = next(h for h in m.spells if h.name == name)
    return ((hs.variants[0], None),)


def test_exact_duplicate_cast_never_adds_a_future_body_or_extra_attack():
    state = board()
    before = deepcopy(state)
    m = moves(state)
    root = m.root_node()
    mid = m.begin(root, None, cast(m))
    assert [b["instance_id"] for b in mid.ours] == [286]
    assert 295 not in mid.entered
    assert m.choice_value(cast(m), root.theirs, ours=root.ours) == 0
    result = search_lines(m.model, survival=False, lethal_now=False, hard_ms=1e9)
    assert "Belladonna Took" not in result.best.steps[0].casts
    for line in [result.best, result.baseline, *result.lines]:
        for step in line.steps:
            assert sum(name == "Belladonna Took" for name in step.attack) <= 1
    assert state == before


def test_pending_legend_selection_and_stack_resolution_forecast_only_one_survivor():
    state = board()
    state["battlefield"].append(creature(296))
    assert [c["instance_id"] for c in build_board_model(state).ours] == [286]
    assert len(state["battlefield"]) == 5  # raw choices untouched
    state = board()
    state["stack"] = [state["hand"].pop()]
    root = moves(state).root_node()
    assert [c["instance_id"] for c in root.ours] == [286]
    assert not root.entered


def test_two_legendary_copies_from_hand_still_leave_one_body():
    state = board()
    state["battlefield"] = state["battlefield"][1:]
    state["hand"] = [creature(295), creature(296)]
    m = moves(state)
    both = tuple((h.variants[0], None) for h in m.spells)
    assert len(m.begin(m.root_node(), None, both).ours) == 1


@pytest.mark.parametrize(
    "kind", ["different_name", "different_controller", "nonlegendary", "nonlegendary_token", "phased_out"]
)
def test_same_name_and_current_legendary_status_and_control_are_required(kind):
    old, new = creature(286), creature(295)
    if kind == "different_name":
        new["name"] = "Belladonna, Another Card"
    if kind == "different_controller":
        new["controller_seat_id"] = 1
    if kind == "nonlegendary":
        new["type_line"] = "Creature — Halfling"
    if kind == "nonlegendary_token":
        new.update(is_legendary=False, object_kind="TOKEN")
    if kind == "phased_out":
        old["is_phased_out"] = True
    assert len(resolve_legends([old, new])) == 2
    assert not conflicts(new, [old])


@pytest.mark.parametrize(
    "text,seat,expected",
    [
        ('The "legend rule" doesn\'t apply.', 1, 2),
        ('The "legend rule" doesn\'t apply to permanents you control.', 2, 2),
        ('The "legend rule" doesn\'t apply to permanents you control.', 1, 1),
        ('The "legend rule" doesn\'t apply to tokens you control.', 2, 1),
        ('If you control a Dragon, the "legend rule" doesn\'t apply.', 2, 1),
    ],
)
def test_explicit_exceptions_respect_scope_and_do_not_invent_conditions(text, seat, expected):
    source = dict(
        instance_id=50, name="Exception", type_line="Artifact", controller_seat_id=seat, oracle_text=text
    )
    assert len(resolve_legends([creature(286), creature(295)], battlefield=[source])) == expected


def test_exception_source_on_board_allows_search_to_keep_both_bodies():
    state = board()
    state["battlefield"].append(
        dict(
            instance_id=50,
            name="Exception",
            type_line="Artifact",
            controller_seat_id=2,
            oracle_text='The "legend rule" doesn\'t apply to permanents you control.',
        )
    )
    m = moves(state)
    root = m.root_node()
    assert len(m.begin(root, None, cast(m)).ours) == 2
    assert m.choice_value(cast(m), root.theirs, ours=root.ours) > 0
    assert not cast_note(state, state["hand"][-1])


def test_creature_exception_does_not_outlive_its_source_in_projected_board():
    source = creature(50, "Exception")
    source["oracle_text"] = 'The "legend rule" doesn\'t apply to permanents you control.'
    old, new = creature(286), creature(295)
    assert len(resolve_legends([source, old, new], battlefield=[source])) == 3
    assert len(resolve_legends([old, new], battlefield=[source])) == 1


@pytest.mark.parametrize("replacement", ["stronger", "damaged", "disabled"])
def test_useful_replacements_remain_available_and_keep_the_better_body(replacement):
    state = board()
    old, new = state["battlefield"][0], state["hand"][-1]
    if replacement == "stronger":
        new.update(power=4, toughness=4)
    if replacement == "damaged":
        old["damage"] = 1
    if replacement == "disabled":
        state["battlefield"].append(
            dict(
                instance_id=600,
                name="Arrest",
                type_line="Enchantment — Aura",
                controller_seat_id=1,
                attached_to_id=286,
                oracle_text="Enchanted creature can't attack or block.",
            )
        )
    m = moves(state)
    mid = m.begin(m.root_node(), None, cast(m))
    assert [b["instance_id"] for b in mid.ours] == [295]
    assert mid.entered[295] == 6
    assert not unsafe_play_reason(state, new, "cast")  # never a blanket ban


def test_losing_new_copy_does_not_delete_its_real_entry_effect():
    state = board()
    for c in [state["battlefield"][0], state["hand"][-1]]:
        c.update(
            name="Legendary Recruiter",
            oracle_text="When this creature enters, create two 1/1 white Soldier creature tokens.",
        )
    m = moves(state)
    mid = m.begin(m.root_node(), None, cast(m, "Legendary Recruiter"))
    assert len(mid.ours) == 3
    assert sum(b["name"] == "Legendary Recruiter" for b in mid.ours) == 1
    assert sum(b["is_token"] for b in mid.ours) == 2
    assert m.choice_value(cast(m, "Legendary Recruiter"), m.root_node().theirs, ours=m.root_node().ours) > 0
    assert not unsafe_play_reason(state, state["hand"][-1], "cast")


def test_tactical_note_corrects_add_a_body_claim_without_banning_entry_or_death_value():
    state = board()
    note = cast_note(state, state["hand"][-1])
    assert "286" in note and "does NOT add a lasting second body" in note
    assert "entry/cast/death payoff or useful replacement" in note
    assert not unsafe_play_reason(state, state["hand"][-1], "cast")
    assert not cast_note(state, state["hand"][0])


def test_legend_loss_is_not_destruction_indestructible_does_not_keep_two():
    old, new = creature(286), creature(295)
    old["keywords"] = new["keywords"] = ["indestructible"]
    assert len(resolve_legends([old, new])) == 1


def test_current_supertype_and_name_override_printed_card_identity():
    old, new = creature(286), creature(295)
    new["super_types"] = []
    assert len(resolve_legends([old, new])) == 2
    new["super_types"] = ["SuperType_Legendary"]
    new["modified_name"] = "A different permanent"
    assert len(resolve_legends([old, new])) == 2
    new["modified_name"] = "Belladonna Took"
    assert len(resolve_legends([old, new])) == 1


def test_legendary_token_creation_counts_one_survivor_and_token_exception_allows_both():
    state = board()
    state["hand"] = [
        dict(
            creature(500, "Summoning"),
            type_line="Sorcery",
            card_types=["CardType_Sorcery"],
            oracle_text="Create two 2/2 legendary white Soldier creature tokens named Captain.",
        )
    ]
    m = moves(state)
    mid = m.begin(m.root_node(), None, cast(m, "Summoning"))
    assert sum(b["name"] == "Captain" for b in mid.ours) == 1
    state["battlefield"].append(
        dict(
            instance_id=600,
            name="Token exception",
            type_line="Artifact",
            controller_seat_id=2,
            oracle_text='The "legend rule" does not apply to tokens you control.',
        )
    )
    m = moves(state)
    mid = m.begin(m.root_node(), None, cast(m, "Summoning"))
    assert sum(b["name"] == "Captain" for b in mid.ours) == 2


def test_raw_supertype_survives_diff_snapshot_and_checkpoint_and_can_be_removed():
    from arenamcp.gamestate import GameState
    from arenamcp.legend_rule import legendary

    state = GameState()
    full = dict(
        instanceId=286,
        grpId=103371,
        zoneId=28,
        ownerSeatId=2,
        controllerSeatId=2,
        type="GameObjectType_Card",
        cardTypes=["CardType_Creature"],
        superTypes=["SuperType_Legendary"],
        power={"value": 2},
        toughness={"value": 2},
    )
    state._update_game_object(full)
    state._update_game_object({"instanceId": 286, "isTapped": True})
    snapshot = state.game_objects[286].to_dict()
    assert snapshot["super_types"] == ["SuperType_Legendary"]
    assert legendary(dict(snapshot, name="Belladonna Took", type_line="Creature"))
    restored = GameState()
    assert restored.restore_checkpoint(state.export_checkpoint())
    assert restored.game_objects[286].super_types == ["SuperType_Legendary"]
    # A full GRE nonlegendary copy omits protobuf's empty repeated superTypes.
    state._update_game_object({k: v for k, v in full.items() if k != "superTypes"})
    assert state.game_objects[286].to_dict()["super_types"] == []
    assert not legendary(dict(state.game_objects[286].to_dict(), type_line="Legendary Creature"))
    state._update_game_object({"instanceId": 286, "superTypes": ["SuperType_Legendary"]})
    state._update_game_object({"instanceId": 286, "superTypes": []})
    assert state.game_objects[286].super_types == []


def test_actual_tactical_prompt_labels_duplicate_option_but_keeps_it_selectable():
    from tests.test_burn_targeting_20261019 import _planner

    from arenamcp.decisions import DecisionOption, PendingDecision

    state = board()
    decision = PendingDecision(
        (99, 125),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Voice of Victory",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 219},
            ),
            DecisionOption(
                "idx:6",
                "Cast Belladonna Took",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 295},
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )
    planner = _planner('{"option_ids": ["idx:0"], "reasoning": "Voice adds a lasting body."}')
    assert planner._llm_decision_options(decision, state) == ["idx:0"]
    prompt = planner._backend.prompts[-1]
    line = next(line for line in prompt.splitlines() if line.startswith("- idx:6:"))
    assert "LEGEND RULE" in line and "does NOT add a lasting second body" in line


def test_board_counted_entry_trigger_counts_survivors_after_legend_rule():
    state = board()
    for card in [state["battlefield"][0], state["hand"][-1]]:
        card["name"] = "Legendary Musterer"
        card["oracle_text"] = (
            "When this creature enters, create a number of 1/1 white Soldier creature tokens "
            "equal to the number of creatures you control named Legendary Musterer."
        )
    m = moves(state)
    mid = m.begin(m.root_node(), None, cast(m, "Legendary Musterer"))
    assert sum(b["name"] == "Legendary Musterer" for b in mid.ours) == 1
    assert sum(b["is_token"] for b in mid.ours) == 1


def test_legend_keep_prompt_uses_both_raw_instances_with_damage_and_readiness():
    from arenamcp.decisions import DecisionOption, PendingDecision
    from arenamcp.legend_rule import keep_note

    state = board()
    state["battlefield"][0].update(damage=1, is_tapped=True)
    fresh = creature(296)
    fresh.update(turn_entered_battlefield=6, summoning_sickness=True)
    state["battlefield"].append(fresh)
    decision = PendingDecision(
        (102, 130),
        "SelectN",
        (DecisionOption("sel:286", "Belladonna Took"), DecisionOption("sel:296", "Belladonna Took")),
        prompt_text="Choose the legendary permanent that you wish to keep. All others will go to their owner's graveyard.",
    )
    note = keep_note(state, decision)
    assert "sel:286" in note and "sel:296" in note
    assert '"damage": 1' in note and '"summoning_sickness": true' in note
    assert "not a sacrifice or destruction" in note
