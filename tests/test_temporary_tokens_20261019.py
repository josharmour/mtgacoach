"""Ledger #185: opposing tokens sacrificed at the next end step are not kills worth blocking.

Match 4d934402 (2026-10-19 T9 and T11): Lagomos, Hand of Hatred and Urabrask's Forge
made 2/1 trample haste tokens that their creating abilities sacrifice "at the beginning
of the next end step". The tokens carry only "Trample / Haste", so nothing read the
sacrifice: the block solver valued killing them like real creatures and traded Rabbits
for them (T9: Rabbit 489 for Elemental 502; T11: two Rabbits into Vein Ripper's drain).

The creator is in Player.log: the token's ``objectSourceGrpId`` (82257 Lagomos,
83853 Urabrask's Forge) and the TokenCreated annotation's affector, the creating
ability (gsid 289: ability 541, grpId 152901, parentId 451 = Lagomos).
"""

from __future__ import annotations

from arenamcp.combat_solver import (
    _material,
    can_attack_next_turn,
    leaves_at_our_end_step,
    optimal_blocks,
)
from arenamcp.combat_strategy import free_blocks
from arenamcp.gamestate import GameState
from arenamcp.token_lifetime import token_expiry

US, THEM = 2, 1
LAGOMOS = (
    "At the beginning of combat on your turn, create a 2/1 red Elemental creature token with trample and "
    "haste. Sacrifice it at the beginning of the next end step.\n{oT}: Search your library for a card, put it "
    "into your hand, then shuffle. Activate only if five or more creatures died this turn."
)
LAGOMOS_ABILITY = (
    "At the beginning of combat on your turn, create a 2/1 red Elemental creature token with trample and "
    "haste. Sacrifice it at the beginning of the next end step."
)
FORGE_ABILITY = (
    "At the beginning of combat on your turn, put an oil counter on CARDNAME, then create an X/1 red Phyrexian "
    "Horror creature token with trample and haste, where X is the number of oil counters on CARDNAME. "
    "Sacrifice that token at the beginning of the next end step."
)
HARE = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)


def test_the_creating_texts_read_as_sacrificed_at_end_step():
    assert token_expiry(LAGOMOS) == "sacrifice"
    assert token_expiry(LAGOMOS_ABILITY) == "sacrifice"
    assert token_expiry(FORGE_ABILITY) == "sacrifice"
    assert (
        token_expiry(
            "Create a 3/3 Elemental creature token. At the beginning of the next end step, exile it."
        )
        == "exile"
    )
    assert token_expiry(HARE) == ""
    # A delayed sacrifice of something other than the token made is not the token's lifetime.
    assert token_expiry("Create a Treasure token. Sacrifice it at the beginning of the next end step.") == ""


# --- the log facts -------------------------------------------------------------------------------

LAGOMOS_OBJECT = {
    "instanceId": 451, "grpId": 82257, "type": "GameObjectType_Card", "zoneId": 28, "visibility": "Visibility_Public",
    "ownerSeatId": 1, "controllerSeatId": 1, "cardTypes": ["CardType_Creature"],
    "power": {"value": 1}, "toughness": {"value": 3},
}  # fmt: skip
ABILITY_OBJECT = {
    "instanceId": 541, "grpId": 152901, "type": "GameObjectType_Ability", "zoneId": 27,
    "visibility": "Visibility_Public", "ownerSeatId": 1, "controllerSeatId": 1, "parentId": 451,
    "objectSourceGrpId": 82257,
}  # fmt: skip
TOKEN_OBJECT = {
    "instanceId": 542, "grpId": 82400, "type": "GameObjectType_Token", "zoneId": 28,
    "visibility": "Visibility_Public", "ownerSeatId": 1, "controllerSeatId": 1,
    "cardTypes": ["CardType_Creature"], "subtypes": ["SubType_Elemental"], "power": {"value": 2},
    "toughness": {"value": 1}, "parentId": 541, "objectSourceGrpId": 82257, "hasSummoningSickness": True,
}  # fmt: skip


def _creation_message():
    """Player.log gsid 289 (2026-10-19 16:07:3x), trimmed to the objects and the annotation."""
    return {
        "type": "GameStateType_Diff",
        "gameStateId": 289,
        "gameObjects": [LAGOMOS_OBJECT, ABILITY_OBJECT, TOKEN_OBJECT],
        "annotations": [
            {"id": 851, "affectorId": 541, "affectedIds": [542], "type": ["AnnotationType_TokenCreated"]}
        ],
    }


def test_the_log_records_the_tokens_creator():
    state = GameState()
    state.update_from_message(_creation_message())
    token = state.game_objects[542]
    assert token.token_source_grp_id == 82257
    assert token.token_creator_ability_grp_id == 152901
    assert token.token_creator_id == 451
    facts = token.to_dict()
    assert facts["token_source_grp_id"] == 82257
    assert facts["token_creator_ability_grp_id"] == 152901
    assert facts["token_creator_id"] == 451
    # Cards and abilities carry no token facts.
    assert "token_source_grp_id" not in state.game_objects[451].to_dict()


def test_the_creator_survives_later_diffs_of_the_token():
    state = GameState()
    state.update_from_message(_creation_message())
    attacking = {**TOKEN_OBJECT, "attackState": "AttackState_Attacking"}
    state.update_from_message({"type": "GameStateType_Diff", "gameStateId": 295, "gameObjects": [attacking]})
    token = state.game_objects[542]
    assert (token.token_creator_ability_grp_id, token.token_creator_id) == (152901, 451)


def test_the_snapshot_marks_the_token_as_leaving(monkeypatch):
    from arenamcp import server

    class FakeDb:
        @staticmethod
        def get_ability_text(ability_id):
            return {152901: LAGOMOS_ABILITY}.get(ability_id)

    monkeypatch.setattr(server, "_get_card_db", lambda: FakeDb())
    monkeypatch.setattr(
        server,
        "enrich_with_oracle_text",
        lambda grp: {82257: {"name": "Lagomos, Hand of Hatred", "oracle_text": LAGOMOS}}.get(grp, {}),
    )
    facts = server._token_creator_facts(
        {"token_source_grp_id": 82257, "token_creator_ability_grp_id": 152901, "token_creator_id": 451}
    )
    assert facts == {
        "token_creator": "Lagomos, Hand of Hatred",
        "token_creator_id": 451,
        "token_expires": "sacrifice",
    }
    # Without the ability (an older checkpoint): the creator card's text decides.
    assert server._token_creator_facts({"token_source_grp_id": 82257})["token_expires"] == "sacrifice"
    assert server._token_creator_facts({}) == {}


# --- the solver ----------------------------------------------------------------------------------


def creature(iid, name, power, toughness, seat, text="", *, token=False, **extra):
    return {
        "instance_id": iid, "name": name, "type_line": "Token Creature" if token else "Creature",
        "card_types": ["CardType_Creature"], "object_kind": "TOKEN" if token else "CARD", "is_token": token,
        "power": power, "toughness": toughness, "owner_seat_id": seat, "controller_seat_id": seat,
        "oracle_text": text, "is_tapped": False, **extra,
    }  # fmt: skip


def elemental(iid=502, **extra):
    return creature(
        iid, "Elemental", 2, 1, THEM, "Trample\nHaste", token=True, is_attacking=True,
        token_expires="sacrifice", token_creator="Lagomos, Hand of Hatred", **extra,
    )  # fmt: skip


def test_killing_a_leaving_token_is_worth_nothing():
    assert _material(elemental()) == 0
    assert _material({**elemental(), "token_expires": ""}) == 5
    assert can_attack_next_turn(elemental()) is False
    assert leaves_at_our_end_step(elemental()) is True


def test_t9_no_rabbit_trade_for_lagomoss_elemental():
    """T9 (gsid 218): Rabbit 489 blocked Elemental 502; 1 trampled over and both died."""
    rabbit = creature(489, "Rabbit", 1, 1, US, token=True)
    before = optimal_blocks([{**elemental(), "token_expires": ""}], [rabbit], 23)
    assert before.assignments == {489: 502}  # the recorded block, scored as a kill
    plan = optimal_blocks([elemental()], [rabbit], 23)
    assert plan.assignments == {}
    assert plan.damage_through == 2


def test_the_chump_stays_when_its_damage_matters():
    """At 2 life the 2/1 trampler is lethal unblocked; the Rabbit stops 1 of it."""
    rabbit = creature(489, "Rabbit", 1, 1, US, token=True)
    plan = optimal_blocks([elemental()], [rabbit], 2)
    assert plan.assignments == {489: 502}
    assert plan.damage_through == 1


def test_a_free_block_on_a_leaving_token_still_stops_its_damage():
    board = {
        "turn": {"turn_number": 9, "active_player": THEM, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": THEM, "life_total": 18, "is_local": False, "mana_pool": {}},
            {"seat_id": US, "life_total": 23, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": [elemental(), creature(700, "Wall of Roots", 0, 5, US)],
        "hand": [],
        "graveyard": [],
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [700],
            "raw_blockers": [{"blockerInstanceId": 700, "attackerInstanceIds": [502], "maxAttackers": 1}],
            "attacker_ids": [502],
        },
    }
    # A 0-power Wall stops the damage without killing it: not a free block (that rule is about kills).
    assert free_blocks(board) == {}
    board["battlefield"][1] = creature(700, "Ogre", 3, 3, US)
    [(blocker, (attacker, why))] = free_blocks(board).items()
    assert (blocker, attacker) == (700, 502)
    assert "Lagomos, Hand of Hatred sacrifices it at end step anyway" in why
