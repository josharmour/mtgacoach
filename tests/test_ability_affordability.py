"""Regression tests for issue #483 — the coach repeatedly told the player to
activate Wolfwillow Haven's {4}{G} ability with nowhere near that much mana.

Root causes, from the field report (bug_20260813_205508.json):
  1. `_handle_actions_available` tagged `Cast` actions with an affordability
     check but `Activate` actions with nothing — the GRE lists an activation
     as legal to *announce*, not as payable.
  2. `RulesEngine._get_mana_pool` counted only lands and mana creatures, so
     mana artifacts (Talisman of Resilience) were invisible.
  3. The same helper matched only Scryfall-style `{T}` symbols, so cards whose
     oracle text came from MTGA's DB in the `{oT}` dialect (Paradise Druid)
     were invisible too.
"""

from arenamcp.gamestate_decisions import _braced_mana_cost
from arenamcp.rules_engine import RulesEngine, _normalize_mana_symbols

WOLFWILLOW_ABILITY_COST = [
    {"color": ["ManaColor_Generic"], "count": 4, "abilityGrpId": 136588},
    {"color": ["ManaColor_Green"], "count": 1, "abilityGrpId": 136588},
]


def _pool(battlefield, turn_number=5, floating=None):
    snapshot = {"battlefield": battlefield, "turn": {"turn_number": turn_number}}
    if floating is not None:
        snapshot["floating_mana"] = floating
    return RulesEngine._get_mana_pool(snapshot, 1)


def test_normalize_mtga_mana_symbols():
    assert _normalize_mana_symbols("{oT}: Add {oG}.") == "{T}: Add {G}."
    assert _normalize_mana_symbols("{oT}: Add {oB} or {oR}.") == "{T}: Add {B} or {R}."
    # MTGA packs a whole cost into one brace (Wolfwillow Haven, Bre of Clan
    # Stoutarm) — each symbol has to come out separately.
    assert _normalize_mana_symbols("{o4oG}, Sacrifice this Aura:") == "{4}{G}, Sacrifice this Aura:"
    assert _normalize_mana_symbols("{o1oW}, {oT}:") == "{1}{W}, {T}:"
    assert _normalize_mana_symbols("{oT}: Add {oGoG}.") == "{T}: Add {G}{G}."
    assert _normalize_mana_symbols("{o10}") == "{10}"
    # Hybrid keeps its slash.
    assert _normalize_mana_symbols("{oB/oR}") == "{B/R}"
    # Scryfall-form text is left untouched.
    assert _normalize_mana_symbols("{T}: Add {G}.") == "{T}: Add {G}."
    assert _normalize_mana_symbols("") == ""
    assert _normalize_mana_symbols(None) == ""


def test_land_tapping_for_two_mana_in_mtga_dialect():
    """`{oT}: Add {oGoG}` must register both the ability and the colour."""
    battlefield = [
        {
            "name": "Nykthos Shrine",
            "type_line": "Land",
            "oracle_text": "{oT}: Add {oGoG}.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 1,
        }
    ]
    pool = _pool(battlefield)
    assert pool["G"] == 1  # one source (it counts sources, not mana produced)
    assert pool["total"] == 1


def test_mana_artifact_counts_as_a_source():
    """Talisman of Resilience was worth 0 mana before this fix."""
    battlefield = [
        {
            "name": "Talisman of Resilience",
            "type_line": "Artifact",
            # MTGA dialect, exactly as it arrives in the field report.
            "oracle_text": "{oT}: Add {oC}.\n{oT}: Add {oB} or {oG}. This artifact deals 1 damage to you.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        }
    ]
    pool = _pool(battlefield)
    assert pool["total"] == 1
    assert pool["G"] == 1
    assert pool["B"] == 1


def test_mana_creature_in_mtga_dialect_counts():
    """Paradise Druid's {oT} ability failed the {T} regex before this fix."""
    battlefield = [
        {
            "name": "Paradise Druid",
            "type_line": "Creature — Elf Druid",
            "oracle_text": (
                "This creature has hexproof as long as it's untapped.\n"
                "{oT}: Add one mana of any color."
            ),
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        }
    ]
    pool = _pool(battlefield, turn_number=5)
    assert pool["total"] == 1
    assert pool["Any"] == 1


def test_summoning_sick_mana_creature_still_excluded():
    battlefield = [
        {
            "name": "Paradise Druid",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{oT}: Add one mana of any color.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        }
    ]
    assert _pool(battlefield, turn_number=5)["total"] == 0


def test_artifact_is_not_summoning_sick():
    """Artifacts can tap for mana the turn they land."""
    battlefield = [
        {
            "name": "Sol Ring",
            "type_line": "Artifact",
            "oracle_text": "{T}: Add {C}{C}.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        }
    ]
    assert _pool(battlefield, turn_number=5)["total"] == 1


def test_tapped_sources_still_excluded():
    battlefield = [
        {
            "name": "Talisman of Resilience",
            "type_line": "Artifact",
            "oracle_text": "{oT}: Add {oC}.",
            "owner_seat_id": 1,
            "is_tapped": True,
            "turn_entered_battlefield": 1,
        }
    ]
    assert _pool(battlefield)["total"] == 0


def test_floating_mana_is_spendable():
    """Sources tapped for a partially-paid cost must not vanish from the pool."""
    battlefield = [
        {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "owner_seat_id": 1,
            "is_tapped": True,
            "turn_entered_battlefield": 1,
        }
    ]
    pool = _pool(battlefield, floating={"G": 2})
    assert pool["total"] == 2
    assert pool["G"] == 2
    assert RulesEngine._can_afford("{1}{G}", pool)


def test_braced_mana_cost_from_gre_structure():
    assert _braced_mana_cost(WOLFWILLOW_ABILITY_COST) == "{4}{G}"
    assert _braced_mana_cost([{"color": ["ManaColor_Red"], "count": 2}]) == "{R}{R}"
    assert _braced_mana_cost([{"color": ["ManaColor_Black", "ManaColor_Green"], "count": 1}]) == "{B/G}"
    # ManaColor_Any is payable by anything → generic.
    assert _braced_mana_cost([{"color": ["ManaColor_Any"], "count": 2}]) == "{2}"


def test_braced_mana_cost_empty_for_free_abilities():
    """A tap/sacrifice ability (fetch land) has no mana cost and must stay
    unflagged — an empty string means 'free', never 'unaffordable'."""
    assert _braced_mana_cost([]) == ""
    assert _braced_mana_cost([{"count": 0}]) == ""


def test_wolfwillow_haven_activation_unaffordable_in_reported_state():
    """The exact board from the field report: everything relevant is tapped,
    so {4}{G} is not payable and the coach must not recommend it."""
    battlefield = [
        {
            "name": "Talisman of Resilience",
            "type_line": "Artifact",
            "oracle_text": "{oT}: Add {oC}.\n{oT}: Add {oB} or {oG}.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        },
        {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "owner_seat_id": 1,
            "is_tapped": True,
            "turn_entered_battlefield": 5,
        },
        {
            "name": "Paradise Druid",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{oT}: Add one mana of any color.",
            "owner_seat_id": 1,
            "is_tapped": True,
            "turn_entered_battlefield": 3,
        },
        {
            "name": "White Lotus Hideout",
            "type_line": "Land",
            "oracle_text": "{oT}: Add {oC}.\n{oT}: Add one mana of any color.",
            "owner_seat_id": 1,
            "is_tapped": True,
            "turn_entered_battlefield": 3,
        },
    ]
    pool = _pool(battlefield, turn_number=6)
    cost = _braced_mana_cost(WOLFWILLOW_ABILITY_COST)
    assert pool["total"] == 1  # only the untapped Talisman
    assert not RulesEngine._can_afford(cost, pool)


def test_wolfwillow_haven_activation_affordable_when_untapped():
    """The same board untapped affords {4}{G} — the check must not become a
    blanket suppression of activated abilities."""
    battlefield = [
        {
            "name": "Talisman of Resilience",
            "type_line": "Artifact",
            "oracle_text": "{oT}: Add {oC}.\n{oT}: Add {oB} or {oG}.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        },
        {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 5,
        },
        {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 1,
        },
        {
            "name": "Paradise Druid",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{oT}: Add one mana of any color.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "name": "White Lotus Hideout",
            "type_line": "Land",
            "oracle_text": "{oT}: Add {oC}.\n{oT}: Add one mana of any color.",
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
    ]
    pool = _pool(battlefield, turn_number=6)
    assert pool["total"] == 5
    assert RulesEngine._can_afford(_braced_mana_cost(WOLFWILLOW_ABILITY_COST), pool)


# ---------------------------------------------------------------------------
# Coach-side filter: a [NEED:] activation must never reach the model
# ---------------------------------------------------------------------------


def _make_coach():
    from arenamcp.coach import CoachEngine

    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def test_unaffordable_activation_stripped_from_legal_line():
    coach = _make_coach()
    moves = ["Activate Ability: Wolfwillow Haven [NEED:{4}{G}]", "Pass"]
    lines = [
        "=== GAME ===",
        "Legal: " + ", ".join(moves),
        'LegalGRE: [{"actionType":"ActionType_Activate","grpId":70716,"_unaffordable":true},'
        '{"actionType":"ActionType_Pass"}]',
    ]
    raw = [
        {
            "actionType": "ActionType_Activate",
            "grpId": 70716,
            "instanceId": 642,
            "abilityGrpId": 136588,
            "_unaffordable": True,
        },
        {"actionType": "ActionType_Pass"},
    ]
    coach._post_filter_uncastable_legal_moves(lines, moves, raw, set(), set(), {})

    assert "Wolfwillow Haven" not in lines[1]
    assert lines[1] == "Legal: Pass"
    assert "136588" not in lines[2]


def test_free_activation_survives_the_filter():
    """Tap/sacrifice abilities cost no mana and must stay in the legal list."""
    coach = _make_coach()
    moves = ["Activate Ability: Verdant Catacombs", "Pass"]
    lines = ["=== GAME ===", "Legal: " + ", ".join(moves), "LegalGRE: []"]
    raw = [{"actionType": "ActionType_Activate", "grpId": 95845}]
    coach._post_filter_uncastable_legal_moves(lines, moves, raw, set(), set(), {})

    assert "Verdant Catacombs" in lines[1]


# ---------------------------------------------------------------------------
# End-to-end: the real GRE ActionsAvailableReq from the field report
# ---------------------------------------------------------------------------


def _report_game_state(tapped: bool):
    """Rebuild the reported board (grp_ids straight from the bug report)."""
    from arenamcp.gamestate import GameObject, GameState, Player, Zone, ZoneType

    gs = GameState()
    gs.local_seat_id = 1
    gs.players[1] = Player(seat_id=1, life_total=26)
    gs.turn_info.turn_number = 6
    # `battlefield` is a computed property over the zone map — objects have to
    # be registered in both to be visible.
    gs.zones[28] = Zone(zone_id=28, zone_type=ZoneType.BATTLEFIELD)
    board = [
        # instance, grp, name,                    tapped,  entered
        (647, 71575, "Talisman of Resilience", False, 5),
        (642, 70716, "Wolfwillow Haven", False, 5),
        (552, 75553, "Forest", tapped, 5),
        (538, 69622, "Paradise Druid", tapped, 3),
        (537, 97562, "White Lotus Hideout", tapped, 3),
        (440, 75553, "Forest", tapped, 1),
    ]
    for instance_id, grp_id, _name, is_tapped, entered in board:
        gs.game_objects[instance_id] = GameObject(
            instance_id=instance_id,
            grp_id=grp_id,
            zone_id=28,
            owner_seat_id=1,
            controller_seat_id=1,
            is_tapped=is_tapped,
            turn_entered_battlefield=entered,
        )
        gs.zones[28].object_instance_ids.append(instance_id)
    assert len(gs.battlefield) == len(board)
    return gs


ACTIONS_AVAILABLE_MSG = {
    "actionsAvailableReq": {
        "actions": [
            {
                "actionType": "ActionType_Activate",
                "grpId": 70716,
                "instanceId": 642,
                "abilityGrpId": 136588,
                "shouldStop": True,
                "manaCost": WOLFWILLOW_ABILITY_COST,
            },
            {"actionType": "ActionType_Pass"},
        ]
    }
}


def test_reported_activation_is_tagged_need_end_to_end():
    """#483: with the board as reported, the legal list must warn that the
    {4}{G} activation is unpayable instead of presenting it as a free option."""
    from arenamcp.gamestate_decisions import _handle_actions_available

    gs = _report_game_state(tapped=True)
    _handle_actions_available(gs, ACTIONS_AVAILABLE_MSG)

    assert gs.legal_actions == ["Activate Ability: Wolfwillow Haven [NEED:{4}{G}]", "Pass"]
    activate_raw = [a for a in gs.legal_actions_raw if a.get("actionType") == "ActionType_Activate"][0]
    assert activate_raw["_unaffordable"] is True


def test_affordable_activation_is_tagged_ok_end_to_end():
    """Same board with mana untapped: the activation is offered normally."""
    from arenamcp.gamestate_decisions import _handle_actions_available

    gs = _report_game_state(tapped=False)
    _handle_actions_available(gs, ACTIONS_AVAILABLE_MSG)

    assert gs.legal_actions == ["Activate Ability: Wolfwillow Haven [OK]", "Pass"]
    activate_raw = [a for a in gs.legal_actions_raw if a.get("actionType") == "ActionType_Activate"][0]
    assert not activate_raw.get("_unaffordable")


def test_free_activation_untagged_end_to_end():
    """A tap/sacrifice ability with no manaCost stays untagged (not [NEED:])."""
    from arenamcp.gamestate_decisions import _handle_actions_available

    gs = _report_game_state(tapped=True)
    msg = {
        "actionsAvailableReq": {
            "actions": [
                {
                    "actionType": "ActionType_Activate",
                    "grpId": 95845,  # Verdant Catacombs — {T}, pay 1 life, sac
                    "instanceId": 900,
                    "shouldStop": True,
                }
            ]
        }
    }
    _handle_actions_available(gs, msg)

    assert gs.legal_actions == ["Activate Ability: Verdant Catacombs"]
    assert not gs.legal_actions_raw[0].get("_unaffordable")


def test_floating_mana_keeps_activation_affordable_end_to_end():
    """Lands tapped for a partially-paid cost leave floating mana; the check
    must not call the ability unaffordable just because they are now tapped."""
    from arenamcp.gamestate_decisions import _handle_actions_available

    gs = _report_game_state(tapped=True)
    gs.players[1].mana_pool = {"G": 4, "C": 1}
    _handle_actions_available(gs, ACTIONS_AVAILABLE_MSG)

    assert gs.legal_actions == ["Activate Ability: Wolfwillow Haven [OK]", "Pass"]
