"""77a434ba: TNN's GRE player link must survive into legal combat/removal forecasts."""

from __future__ import annotations

import json

import pytest

from arenamcp import board_assessment as ba
from arenamcp.combat_keywords import can_be_blocked_by
from arenamcp.combat_solver import _card_key, _resolve_attacker, evaluate_attack, optimal_blocks
from arenamcp.gamestate import GameState
from arenamcp.player_protection import protected_from_player_source


def link(*, ability=90240, player=1):
    # Actual persistent annotation 375, gsid120. Repeated at gsid394 and 416.
    return {
        "id": 375,
        "type": ["AnnotationType_LinkInfo"],
        "affectorId": 231,
        "affectedIds": [player],
        "details": [
            {"key": "ChooseLinkType", "valueString": ["GamePlayer"]},
            {"key": "sourceAbilityGRPID", "valueInt32": [ability]},
            {"key": "LinkType", "valueInt32": [3]},
        ],
    }


def replay(*, ability=90240, player=1):
    state = GameState()
    state.local_seat_id = 1
    state.match_id = "protection-regression"
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 120,
            "gameObjects": [
                {
                    "instanceId": 231,
                    "grpId": 55607,
                    "zoneId": 28,
                    "ownerSeatId": 2,
                    "controllerSeatId": 2,
                    "power": {"value": 3},
                    "toughness": {"value": 1},
                    "cardTypes": ["CardType_Creature"],
                    "uniqueAbilities": [{"grpId": i} for i in [90240, 101226, 45307]],
                }
            ],
            "persistentAnnotations": [link(ability=ability, player=player)],
        }
    )
    return state


def creature(iid, power, toughness, controller, **extra):
    return {
        "instance_id": iid,
        "name": f"Creature {iid}",
        "power": power,
        "toughness": toughness,
        "controller_seat_id": controller,
        "owner_seat_id": controller,
        "type_line": "Creature",
        "keywords": [],
        **extra,
    }


def nemesis(**extra):
    return creature(231, 3, 1, 2, name="True-Name Nemesis", protection_from_players=[1], **extra)


def test_real_persistent_link_survives_diff_full_refresh_and_checkpoint():
    state = replay()
    assert state.game_objects[231].to_dict()["protection_from_players"] == [1]
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 394,
            "gameObjects": [{"instanceId": 231, "controllerSeatId": 1}],
        }
    )
    changed = state.game_objects[231].to_dict()
    assert changed["controller_seat_id"] == 1
    assert changed["protection_from_players"] == [1]  # choice does not follow controller
    restored = GameState()
    assert restored.restore_checkpoint(json.loads(json.dumps(state.export_checkpoint())))
    assert restored.game_objects[231].to_dict()["protection_from_players"] == [1]


def test_ability_loss_suspends_protection_and_zone_change_clears_choice():
    state = replay()
    state._update_game_object({"instanceId": 231, "uniqueAbilities": []})
    assert "protection_from_players" not in state.game_objects[231].to_dict()
    state._update_game_object({"instanceId": 231, "uniqueAbilities": [101226]})
    assert state.game_objects[231].to_dict()["protection_from_players"] == [1]
    state._update_game_object({"instanceId": 231, "zoneId": 29})
    assert state.game_objects[231].chosen_player_links == {}
    state._update_game_object({"instanceId": 231, "zoneId": 28})
    assert "protection_from_players" not in state.game_objects[231].to_dict()


def test_arbitrary_choose_player_link_is_not_protection():
    assert replay(ability=99999).game_objects[231].chosen_player_links == {}
    unlinked = creature(231, 3, 1, 2, name="True-Name Nemesis")
    assert can_be_blocked_by(unlinked, creature(340, 3, 1, 1))


def test_all_server_serializers_preserve_protection(monkeypatch):
    from arenamcp import server

    info = {
        "name": "True-Name Nemesis",
        "oracle_text": "Protection from the chosen player.",
        "type_line": "Creature — Merfolk Rogue",
        "mana_cost": "{1}{U}{U}",
    }
    monkeypatch.setattr(server, "enrich_with_oracle_text", lambda *args: info)
    monkeypatch.setattr(server, "card_object_info", lambda *args: info)
    obj = replay().game_objects[231]
    raw = obj.to_dict()
    for serialized in (
        server._serialize_game_object(obj),
        server._serialize_snapshot_obj(raw),
        server._serialize_bridge_card({"instance_id": 231, "grp_id": 55607}, {231: raw}),
    ):
        assert serialized["protection_from_players"] == [1]
        assert serialized["chosen_player_links"] == {90240: 1}


def test_chosen_player_cannot_block_even_after_nemesis_changes_controller():
    goblin = creature(340, 3, 1, 1)
    assert not can_be_blocked_by(nemesis(), goblin)
    assert not can_be_blocked_by({**nemesis(), "controller_seat_id": 1}, goblin)
    # A creature stolen from the chosen player is controlled by somebody else.
    stolen = {**goblin, "controller_seat_id": 2}
    assert can_be_blocked_by(nemesis(), stolen)
    plan = optimal_blocks([nemesis()], [goblin], 4)
    assert plan.assignments == {} and plan.damage_through == 3
    # Holding the Goblin back cannot reduce the recorded T15 crackback.
    hold = evaluate_attack([], [goblin], [], 20, 4, [nemesis()])
    attack = evaluate_attack([goblin], [], [], 20, 4, [nemesis()])
    assert hold.worst_case_crackback == attack.worst_case_crackback == 3


@pytest.mark.parametrize("keywords", [[], ["first strike"], ["double strike"], ["deathtouch"]])
def test_protected_blocker_prevents_damage_and_lifelink(keywords):
    attack = creature(100, 5, 5, 1, keywords=[*keywords, "lifelink"])
    result = _resolve_attacker(attack, [nemesis()])
    assert result.blockers_died == []
    assert result.life_gained == result.damage_through == 0
    assert not result.attacker_died


def test_trample_assigns_lethal_before_prevention_but_only_gains_dealt_damage():
    attack = creature(100, 5, 5, 1, keywords=["trample", "lifelink"])
    result = _resolve_attacker(attack, [nemesis()])
    assert result.blockers_died == []
    assert result.damage_through == result.life_gained == 4


def test_prevented_blocker_damage_has_no_deathtouch_or_lifelink():
    # Test damage independently of blocking legality (already-declared combat).
    blocker = creature(100, 5, 5, 1, keywords=["double strike", "deathtouch", "lifelink"])
    result = _resolve_attacker(nemesis(), [blocker])
    assert not result.attacker_died
    assert result.blocker_life_gained == 0


def test_protection_targeting_and_untargeted_damage_but_not_board_wipe():
    body = ba._body(nemesis(), 7, {})

    def spell(text, controller=1):
        return {"oracle_text": text, "type_line": "Sorcery", "controller_seat_id": controller}

    assert not ba._kills(spell("Destroy target creature."), body)
    assert not ba._kills(spell("Deal 3 damage to each creature."), body)
    # General board wipes are outside _kills' parsed targeted-removal subset;
    # the protection predicate must still distinguish their non-damage effect.
    assert not ba._protected_from(body, spell("Destroy all creatures."), targeted=False, damage=False)
    assert not ba._protected_from(
        body, spell("All creatures get -2/-2 until end of turn."), targeted=False, damage=False
    )
    assert ba._kills(spell("Destroy target creature.", 2), body)
    assert not protected_from_player_source(body, {"controller_seat_id": 2, "owner_seat_id": 1})
    assert not protected_from_player_source(body, {"owner_seat_id": 1})


def test_chainsaw_projected_etb_keeps_source_controller():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves

    chainsaw = {
        "instance_id": 300,
        "name": "Chainsaw",
        "type_line": "Artifact — Equipment",
        "mana_cost": "{1}{R}",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "oracle_text": "When Chainsaw enters, it deals 3 damage to target creature an opponent controls.\nEquip {3}",
    }
    state = {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {"turn_number": 7, "active_player": 1, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "battlefield": [nemesis()],
        "hand": [chainsaw],
    }
    model = build_board_model(state)
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1000, opponent=None)
    reaches = [var.reach for hs in moves.spells for var in hs.variants if var.reach]
    assert reaches
    assert all(not ba._kills(reach, model.theirs[0]) for reach in reaches)
    assert any(ba._kills({**reach, "_source_controller_seat_id": 2}, model.theirs[0]) for reach in reaches)


def test_protection_and_source_control_changes_invalidate_cached_combat():
    card = nemesis()
    assert _card_key(card) != _card_key({**card, "protection_from_players": [2]})
    assert _card_key(card) != _card_key({**card, "controller_seat_id": 1})
    state = {"battlefield": [card]}
    assert ba._signature(state) != ba._signature({"battlefield": [{**card, "protection_from_players": [2]}]})


@pytest.mark.parametrize("keywords", [[], ["first strike"], ["double strike"]])
def test_negative_power_blocker_neither_gains_life_nor_offsets_other_blockers(keywords):
    attacker = creature(100, 1, 4, 1)
    negative = creature(200, -2, 4, 2, keywords=[*keywords, "lifelink", "deathtouch"])
    positive = creature(201, 4, 4, 2)
    result = _resolve_attacker(attacker, [negative, positive])
    assert result.blocker_life_gained == 0
    assert result.attacker_died  # Four actual damage is not reduced by -2 power.
    alone = _resolve_attacker(attacker, [negative])
    assert not alone.attacker_died
    assert alone.blocker_life_gained == 0


@pytest.mark.parametrize("keywords", [[], ["first strike"], ["double strike"]])
def test_nontrampling_lifelink_overassigns_to_unprotected_blocker(keywords):
    attacker = creature(100, 8, 8, 1, keywords=[*keywords, "lifelink"])
    ordinary = creature(200, 3, 3, 2)
    result = _resolve_attacker(attacker, [nemesis(), ordinary])
    assert result.blockers_died == [ordinary]
    assert result.damage_through == 0
    # All eight can be assigned to the ordinary blocker in one step. For
    # double strike it then dies; the second step only has protected Nemesis.
    assert result.life_gained == 8


@pytest.mark.parametrize(
    "keywords,through,gain", [([], 4, 7), (["first strike"], 4, 7), (["double strike"], 11, 14)]
)
def test_trampling_lifelink_still_assigns_lethal_to_protected_blocker(keywords, through, gain):
    attacker = creature(100, 8, 8, 1, keywords=[*keywords, "trample", "lifelink"])
    ordinary = creature(200, 3, 3, 2)
    result = _resolve_attacker(attacker, [nemesis(), ordinary])
    assert result.blockers_died == [ordinary]
    assert result.damage_through == through
    assert result.life_gained == gain


def test_negative_power_attacker_deals_no_damage_or_lifelink():
    attacker = creature(100, -2, 4, 1, keywords=["double strike", "lifelink"])
    result = _resolve_attacker(attacker, [])
    assert result.damage_through == result.life_gained == 0
