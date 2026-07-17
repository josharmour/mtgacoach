import arenamcp.server as server_module
from arenamcp.autopilot import AutopilotEngine
from arenamcp.gamestate import GameObject, GameObjectKind, GameState, create_game_state_handler
from arenamcp.rules_engine import RulesEngine


def _fake_card_info(grp_id: int) -> dict:
    mapping = {
        97452: {
            "name": "Earthbender Ascension",
            "oracle_text": (
                "When this enchantment enters, earthbend 2. Then search your library "
                "for a basic land card, put it onto the battlefield tapped, then shuffle.\n"
                "Landfall — Whenever a land you control enters, put a quest counter on "
                "this enchantment. When you do, if it has four or more quest counters on "
                "it, put a +1/+1 counter on target creature you control. It gains trample "
                "until end of turn."
            ),
            "type_line": "Enchantment",
            "mana_cost": "{2}{G}",
        },
        100652: {
            "name": "Forest",
            "oracle_text": "({T}: Add {G}.)",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
        },
        75451: {
            "name": "Impassioned Orator",
            "oracle_text": "Whenever another creature you control enters, you gain 1 life.",
            "type_line": "Creature — Human Cleric",
            "mana_cost": "{1}{W}",
        },
    }
    return mapping.get(grp_id, {"name": f"Card#{grp_id}"})


def _fake_enrich_with_oracle_text(grp_id: int) -> dict:
    if grp_id == 192762:
        return {
            "grp_id": grp_id,
            "name": "Ability (ID: 192762)",
            "oracle_text": "Earthbend 2.",
            "type_line": "Ability",
            "mana_cost": "",
        }
    result = _fake_card_info(grp_id)
    return {
        "grp_id": grp_id,
        "name": result.get("name", f"Card#{grp_id}"),
        "oracle_text": result.get("oracle_text", ""),
        "type_line": result.get("type_line", ""),
        "mana_cost": result.get("mana_cost", ""),
    }


def test_select_targets_req_captures_source_context(monkeypatch):
    monkeypatch.setattr(server_module, "get_card_info", _fake_card_info)
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", _fake_enrich_with_oracle_text)

    state = GameState()
    state.local_seat_id = 1

    parent = GameObject(
        instance_id=301,
        grp_id=97452,
        zone_id=1,
        owner_seat_id=1,
        controller_seat_id=1,
        object_kind=GameObjectKind.CARD,
    )
    ability = GameObject(
        instance_id=307,
        grp_id=192762,
        zone_id=2,
        owner_seat_id=1,
        controller_seat_id=1,
        object_kind=GameObjectKind.ABILITY,
        parent_instance_id=301,
    )
    state.game_objects[parent.instance_id] = parent
    state.game_objects[ability.instance_id] = ability
    state.stack.append(ability)

    handler = create_game_state_handler(state)
    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": {
                    "type": "GREMessageType_SelectTargetsReq",
                    "systemSeatIds": 1,
                    "selectTargetsReq": {"sourceId": 307},
                }
            }
        }
    )

    assert state.pending_decision == "Select Targets"
    assert state.decision_context["source_card"] == "Earthbender Ascension"
    assert state.decision_context["source_parent_instance_id"] == 301
    assert "earthbend 2" in state.decision_context["source_oracle_text"].lower()


def test_rules_engine_earthbend_targets_your_lands_only():
    game_state = {
        "decision_context": {
            "type": "target_selection",
            "source_card": "Earthbender Ascension",
            "source_oracle_text": "Earthbend 2.",
        },
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
            },
            {
                "instance_id": 11,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": True,
            },
            {
                "instance_id": 20,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
            },
            {
                "instance_id": 21,
                "name": "Impassioned Orator",
                "type_line": "Creature — Human Cleric",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "power": 2,
                "toughness": 2,
                "is_tapped": False,
            },
        ],
        "stack": [],
    }

    assert RulesEngine.get_legal_actions(game_state) == [
        "Select target: Forest (YOURS) #1",
        "Select target: Forest (YOURS) #2",
    ]


def test_autopilot_target_helpers_prefer_local_lands_and_resolve_ordinals():
    engine = AutopilotEngine.__new__(AutopilotEngine)
    battlefield = [
        {"instance_id": 20, "name": "Forest", "owner_seat_id": 2},
        {"instance_id": 10, "name": "Forest", "owner_seat_id": 1},
        {"instance_id": 11, "name": "Forest", "owner_seat_id": 1},
    ]
    game_state = {
        "decision_context": {
            "type": "target_selection",
            "source_oracle_text": "Earthbend 2.",
        }
    }

    assert AutopilotEngine._get_target_owner_order(game_state, 1, 2) == [1, 2]
    assert engine._find_instance_id("Forest #2", battlefield, 1) == 11


def test_rules_engine_prefers_explicit_bridge_targets_for_follow_up_trigger():
    game_state = {
        "decision_context": {
            "type": "target_selection",
            "source_card": "Sheltered by Ghosts",
            "source_oracle_text": (
                "Enchant creature you control\n"
                "When this Aura enters, exile target nonland permanent an opponent controls "
                "until this Aura leaves the battlefield."
            ),
            "validTargets": [{"instanceId": 20}],
        },
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Wonderweave Aerialist",
                "type_line": "Creature — Spider Human Hero",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "power": 2,
                "toughness": 2,
            },
            {
                "instance_id": 20,
                "name": "Treefolk",
                "type_line": "Creature — Treefolk",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "power": 3,
                "toughness": 4,
            },
        ],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }

    assert RulesEngine.get_legal_actions(game_state) == ["Select target: Treefolk (OPP)"]
