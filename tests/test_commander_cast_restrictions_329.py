"""#329: exact GRE evidence with a bounded, explicitly reconstructed planner board."""

import copy
import json
from pathlib import Path

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model

HOBBITS = "The Notary Hobbits"
DRANNITH = "Your opponents can't cast spells from anywhere other than their hands."
EVIDENCE = json.loads((Path(__file__).parent / "fixtures/live_329/9376b418-drannith.json").read_text())


def board():
    # IDs, controller, zones and prohibition come from the retained packet. Five basic
    # Forests replace the historical mixed mana base; this is not a complete game replay.
    return {
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {"turn_number": 13, "active_player": 2, "priority_player": 2, "phase": "Main1"},
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 26, "commander_ids": [245]},
            {"seat_id": 1, "life_total": 25},
        ],
        "commander_casts": {103511: 0},
        "commander_grp_ids": [103511],
        "command": [
            {
                "instance_id": 245,
                "grp_id": 103511,
                "name": HOBBITS,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "mana_cost": "{3}{G}{G}",
                "type_line": "Legendary Creature — Halfling Advisor",
                "power": 1,
                "toughness": 1,
                "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens that are copies of them, except the tokens aren't legendary.\n{T}: Add {C} for each Halfling you control.",
            }
        ],
        "battlefield": [
            {
                "instance_id": 666,
                "grp_id": 98617,
                "name": "Drannith Magistrate",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "type_line": "Creature — Human Wizard",
                "power": 1,
                "toughness": 3,
                "mana_cost": "{1}{W}",
                "oracle_text": DRANNITH,
                "is_tapped": True,
            },
        ]
        + [
            {
                "instance_id": 100 + i,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "oracle_text": "{T}: Add {G}.",
            }
            for i in range(5)
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
    }


def assess(state):
    ba._CACHE.clear()
    return ba.assess(copy.deepcopy(state))


def test_retained_gre_explicitly_disqualifies_commander_by_opposing_drannith():
    objects = {row["object"]["instanceId"]: row["object"] for row in EVIDENCE["objects"]}
    assert (objects[245]["zoneId"], objects[245]["controllerSeatId"]) == (26, 2)
    assert (objects[666]["zoneId"], objects[666]["controllerSeatId"]) == (28, 1)
    assert {z["zoneId"]: z["type"] for z in EVIDENCE["initial_zones"]} == {
        26: "ZoneType_Command",
        28: "ZoneType_Battlefield",
    }
    for message in EVIDENCE["requests"]:
        menu = message["actionsAvailableReq"]
        assert not any(a.get("instanceId") == 245 for a in menu["actions"])
        blocked = next(a for a in menu["inactiveActions"] if a.get("instanceId") == 245)
        assert blocked["disqualifyingSourceId"] == 666


def test_drannith_blocks_command_cast_in_search_and_explains_the_restriction():
    state = board()
    result = assess(state)
    search = result.line_search
    assert search is not None
    assert all(HOBBITS not in step.casts for line in search.lines + [search.baseline] for step in line.steps)
    facts = "\n".join(ba._commander_facts(build_board_model(state), result.lookahead, search))
    assert "Drannith Magistrate" in facts and "cannot cast from command" in facts
    assert "castable this turn" not in facts


def test_controller_not_owner_decides_whose_casts_drannith_prohibits():
    state = board()
    drannith = state["battlefield"][0]
    drannith["owner_seat_id"] = 2
    assert build_board_model(state).commanders[0].blocked_by == ("Drannith Magistrate",)
    drannith["owner_seat_id"] = 1
    drannith["controller_seat_id"] = 2
    result = assess(state)
    assert any(HOBBITS in step.casts for step in result.line_search.best.steps)
    assert "castable this turn" in result.commander_line()


def test_hand_cast_and_already_resolving_permanent_are_not_zone_casts():
    state = board()
    commander = state.pop("command")[0]
    state["hand"] = [commander]
    result = assess(state)
    assert any(HOBBITS in step.casts for step in result.line_search.best.steps)
    state["hand"] = []
    state["stack"] = [commander]
    model = build_board_model(state)
    assert model.our_stack_bodies  # no counter/removal invented by the prohibition


def test_restriction_tracks_fresh_zone_and_phase_state():
    for change in ("removed", "phased", "ability_removed", "unknown_controller"):
        state = board()
        if change == "removed":
            state["graveyard"] = [state["battlefield"].pop(0)]
        elif change == "phased":
            state["battlefield"][0]["is_phased_out"] = True
        elif change == "ability_removed":
            state["battlefield"][0]["removed_abilities"] = ["all"]
        else:
            state["battlefield"][0].pop("controller_seat_id")
        assert not build_board_model(state).commanders[0].blocked_by


def test_ban_reads_exact_rules_not_name_or_conditional_text():
    from arenamcp.cast_zones import outside_hand_ban, zone_blockers

    source = board()["battlefield"][0]
    source["name"] = "Copy of a Magistrate"
    assert outside_hand_ban(source, ours=False)
    assert not outside_hand_ban(source, ours=True)
    assert not outside_hand_ban(source, ours=None)
    assert zone_blockers([source], 2, "hand") == ()
    assert zone_blockers([source], 2, "exile") == ("Copy of a Magistrate",)
    source["oracle_text"] = "At the beginning of your turn, " + DRANNITH.lower()
    assert not outside_hand_ban(source, ours=False)
    source["oracle_text"] = "Your opponents can cast spells from anywhere other than their hands."
    assert not outside_hand_ban(source, ours=False)
    source["oracle_text"] = "You can't cast spells from anywhere other than your hand."
    assert outside_hand_ban(source, ours=True)
    assert not outside_hand_ban(source, ours=False)


def test_removal_inside_search_unlocks_later_command_cast_without_sticky_ban():
    state = board()
    state["hand"] = [
        {
            "name": "Clear the Way",
            "instance_id": 777,
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "mana_cost": "{G}",
            "type_line": "Sorcery",
            "oracle_text": "Destroy target creature.",
        }
    ]
    result = assess(state)
    lines = result.line_search.lines + [result.line_search.baseline]
    unlocked = [line for line in lines if any(HOBBITS in s.casts for s in line.steps)]
    assert unlocked
    for line in unlocked:
        removal = next(i for i, s in enumerate(line.steps) if "Clear the Way" in s.casts)
        commander = next(i for i, s in enumerate(line.steps) if HOBBITS in s.casts)
        assert commander > removal
        assert any("Drannith Magistrate" in str(t) for t in line.steps[removal].targets)


def test_unknown_commander_cost_or_flash_does_not_bypass_restriction():
    state = board()
    state["command"][0]["mana_cost"] = ""
    state["command"][0]["oracle_text"] = "Flash"
    result = assess(state)
    assert "cannot cast from command" in result.commander_line()
    model = build_board_model(state)
    assert HOBBITS not in ba._unmodelled_castable(model, result.line_search, result.lookahead)[0]
    state = board()
    state["turn"]["active_player"] = 1
    state["command"][0]["oracle_text"] = "Flash"
    result = assess(state)
    assert "castable now at instant speed" not in result.commander_line()


def test_plan_replay_and_validation_do_not_reintroduce_blocked_commander():
    from arenamcp.game_plan import _validate_turns
    from arenamcp.line_search import evaluate_plan

    state = board()
    result = assess(state)
    raw = [{"turn": "T", "label": "T", "cast": [HOBBITS], "attack": "none"}]
    replay = evaluate_plan(result.line_search, raw, state)
    assert any("cannot cast from command" in issue for issue in replay.issues)
    assert HOBBITS not in replay.line.steps[0].casts
    issues = []
    turns = _validate_turns(raw, result, state, issues)
    assert not turns[0]["cast"]
    assert any("cannot cast from command" in issue for issue in issues)


def test_flash_and_face_damage_projections_obey_current_zone_restriction():
    state = board()
    state["command"][0]["oracle_text"] = "Flash"
    result = assess(state)
    search = result.line_search._search
    root = search.root_node()
    held = search._held(root.hand, (search.model.sources_all, 0, ()), 26, root.theirs, root.ours)
    assert not held[1]
    assert search._held(root.hand, (search.model.sources_all, 0, ()), 26, (), root.ours)[1]
    state["command"][0].update(type_line="Sorcery", oracle_text="Deal 5 damage to each opponent.")
    result = assess(state)
    search = result.line_search._search
    root = search.root_node()
    assert search._next_turn_spells(root.hand, search.model.sources_all, root.ours, root.theirs) == (0, ())
    assert search._next_turn_spells(root.hand, search.model.sources_all, root.ours, ())[0] == 5


def test_current_plan_guard_also_works_when_search_is_unavailable():
    from arenamcp.game_plan import _validate_turns

    state = board()
    result = assess(state)
    result.line_search = None
    issues = []
    turns = _validate_turns([{"turn": "T", "cast": [HOBBITS]}], result, state, issues)
    assert not turns[0]["cast"]
    assert any("Drannith Magistrate" in issue for issue in issues)


def test_plan_can_remove_the_source_then_cast_commander_next_turn():
    from arenamcp.game_plan import _validate_turns

    state = board()
    state["hand"] = [
        {
            "name": "Clear the Way",
            "instance_id": 777,
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "mana_cost": "{G}",
            "type_line": "Sorcery",
            "oracle_text": "Destroy target creature.",
        }
    ]
    result = assess(state)
    issues = []
    turns = _validate_turns(
        [
            {"turn": "T", "cast": ["Clear the Way (on Drannith Magistrate)"]},
            {"turn": "T+1", "cast": [HOBBITS]},
        ],
        result,
        state,
        issues,
    )
    assert HOBBITS in turns[1]["cast"], issues
    assert not any("cannot cast" in issue for issue in issues)
