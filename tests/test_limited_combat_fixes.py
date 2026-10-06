"""In-match fixes from the 2026-10-05 sealed and Premier Draft runs."""

from __future__ import annotations

from arenamcp.action_planner import linked_cast_note, power_only_note
from arenamcp.combat_keywords import ability_keywords, has_combat_keyword
from arenamcp.combat_solver import evaluate_attack
from arenamcp.gamestate import GameState
from arenamcp.play_safety import power_only_debuff_wasted, unsafe_play_reason

ICY_RECEPTION = {
    "instance_id": 296,
    "name": "Icy Reception",
    "type_line": "Instant",
    "oracle_text": "Choose one — \n•Counter target creature or legendary spell unless its controller pays {o3}. \n"
    "•Target creature gets -5/-0 until end of turn.\nChoose one<nobr> —</nobr> \n"
    "•<indent=4%>Counter target creature or legendary spell unless its controller pays {o3}. </indent>\n"
    "•Target creature gets -5/-0 until end of turn.",
}


def board(**extra):
    return {
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [
            {
                "instance_id": 228,
                "name": "Carnivorous Cultivator",
                "type_line": "Creature",
                "power": 2,
                "toughness": 3,
                "controller_seat_id": 2,
                **extra,
            }
        ],
        "stack": [],
    }


def test_power_only_shrink_is_held_until_combat():
    # 2026-10-05: cast in main phase 1 "to kill" a 2/3; nothing attacked.
    reason = unsafe_play_reason(board(), ICY_RECEPTION, "ActionType_Cast", {})
    assert "never kills" in reason
    assert power_only_debuff_wasted(ICY_RECEPTION, board(is_attacking=True)) == ""
    assert power_only_debuff_wasted(ICY_RECEPTION, board(is_blocking=True)) == ""
    countering = {**board(), "stack": [{"name": "Bear", "controller_seat_id": 2}]}
    assert power_only_debuff_wasted(ICY_RECEPTION, countering) == ""
    cantrip = {**ICY_RECEPTION, "oracle_text": "Target creature gets -2/-0 until end of turn. Draw a card."}
    assert power_only_debuff_wasted(cantrip, board()) == ""
    shrink_both = {**ICY_RECEPTION, "oracle_text": "Target creature gets -2/-2 until end of turn."}
    assert power_only_debuff_wasted(shrink_both, board()) == ""


def test_power_only_effects_are_flagged_to_the_model():
    assert "never kills" in power_only_note("Mode 1: Target creature gets -5/-0 until end of turn.")
    assert power_only_note("Target creature gets -3/-3 until end of turn.") == ""


def test_gre_abilities_grant_combat_keywords():
    # Titanbones wearing the opponent's Medic's Kitesail: reach (13) printed,
    # flying (8) granted by the equipment.
    state = GameState()
    state._update_game_object(
        {
            "instanceId": 233,
            "grpId": 106511,
            "type": "GameObjectType_Card",
            "cardTypes": ["CardType_Creature"],
            "power": {"value": 13},
            "toughness": {"value": 11},
            "uniqueAbilities": [{"id": 160, "grpId": 13}, {"id": 177, "grpId": 8}, {"id": 178, "grpId": 189117}],
        }
    )
    assert state.game_objects[233].keywords == ["reach", "flying"]
    assert state.game_objects[233].to_dict()["keywords"] == ["reach", "flying"]
    state._update_game_object({"instanceId": 233, "isTapped": True})  # a diff without abilities keeps them
    assert state.game_objects[233].keywords == ["reach", "flying"]
    assert ability_keywords([8, 8, 999]) == ["flying"]


def test_granted_flying_counts_in_combat_math():
    titanbones = {
        "instance_id": 233,
        "name": "Titanbones",
        "power": 13,
        "toughness": 11,
        "oracle_text": "Reach",
        "keywords": ["reach", "flying"],
    }
    chumps = [
        {"instance_id": 290, "name": "Kiora", "power": 2, "toughness": 2, "oracle_text": ""},
        {"instance_id": 332, "name": "Cadet", "power": 2, "toughness": 2, "oracle_text": ""},
    ]
    assert has_combat_keyword(titanbones, "flying")
    flyers = [
        {"instance_id": 263, "name": "Fateseer", "power": 3, "toughness": 4, "oracle_text": "Flying"},
        {"instance_id": 298, "name": "Dragon", "power": 6, "toughness": 5, "oracle_text": "Flying"},
    ]
    # 2026-10-05 turn 15 at 4 life: both flyers attacked, leaving only ground
    # 2/2s against a 13-power flyer.
    plan = evaluate_attack(flyers, chumps, [], 23, 4, [titanbones])
    assert plan.worst_case_crackback >= 4
    held = evaluate_attack(flyers[:1], chumps + flyers[1:], [], 23, 4, [titanbones])
    assert held.worst_case_crackback < 4
    printed_only = {key: value for key, value in titanbones.items() if key != "keywords"}
    assert evaluate_attack(flyers, chumps, [], 23, 4, [printed_only]).worst_case_crackback == 0


def test_linked_cast_note_names_a_prepared_spell():
    state = {"battlefield": [{"instance_id": 263, "grp_id": 106385, "name": "Prudent Fateseer"}]}
    spells = {
        555: {
            "name": "Peer Review",
            "mana_cost": "{2}{W/U}",
            "type_line": "Sorcery",
            "oracle_text": "Create a 2/2 Cadet creature token. Surveil 1.",
        }
    }
    note = linked_cast_note(state, {"grpId": 555, "instanceId": 263}, lookup=spells.get)
    assert '"casts": "Peer Review"' in note and '"from": "Prudent Fateseer"' in note and "Surveil 1" in note
    assert linked_cast_note(state, {"grpId": 106385, "instanceId": 263}, lookup=spells.get) == ""
