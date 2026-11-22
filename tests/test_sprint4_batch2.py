"""Sprint 4 Batch 2 regression tests: generic X-cost creature-token spell modelling (S4-3)."""

from __future__ import annotations

from arenamcp.board_assessment import assess
from arenamcp.line_search_moves import _text_x_token_spell, make_tokens, unmodelled_effect


def _mountain(iid: int, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "card_types": ["CardType_Land"],
        "oracle_text": "{T}: Add {R}.",
        "controller_seat_id": 1,
        "is_tapped": tapped,
        "turn_entered_battlefield": 1,
    }


def _plains(iid: int, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "card_types": ["CardType_Land"],
        "oracle_text": "{T}: Add {W}.",
        "controller_seat_id": 1,
        "is_tapped": tapped,
        "turn_entered_battlefield": 1,
    }


def _creature(
    iid: int,
    name: str,
    power: int,
    toughness: int,
    *,
    seat: int = 1,
    mana_cost: str = "{1}{R}",
    cmc: int = 2,
    oracle_text: str = "",
    turn_entered: int = 1,
) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "mana_cost": mana_cost,
        "cmc": cmc,
        "type_line": "Creature — Warrior",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle_text,
        "controller_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": turn_entered,
    }


def _x_haste_swarm(iid: int = 100) -> dict:
    return {
        "instance_id": iid,
        "name": "Chorus of Embers",
        "mana_cost": "{X}{R}",
        "cmc": 1,
        "type_line": "Sorcery",
        "card_types": ["CardType_Sorcery"],
        "oracle_text": (
            'Create X 1/1 black Rat creature tokens with "This creature can\'t block." '
            "Creatures you control gain haste until end of turn."
        ),
        "controller_seat_id": 1,
    }


def _x_plain_tokens(iid: int = 101) -> dict:
    return {
        "instance_id": iid,
        "name": "Call the Militia",
        "mana_cost": "{X}{W}",
        "cmc": 1,
        "type_line": "Instant",
        "card_types": ["CardType_Instant"],
        "oracle_text": "Create X 1/1 white Warrior creature tokens.",
        "controller_seat_id": 1,
    }


def _state(*, our_life: int, opp_life: int, battlefield: list[dict], hand: list[dict], turn: int = 4) -> dict:
    return {
        "turn": {
            "turn_number": turn,
            "active_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "life_total": our_life, "is_local": True},
            {"seat_id": 2, "life_total": opp_life, "is_local": False},
        ],
        "battlefield": battlefield,
        "hand": hand,
        "stack": [],
        "graveyard": [],
        "exile": [],
        "command": [],
        "local_seat_id": 1,
    }


# ---------------------------------------------------------------------------
# Unit tests for _text_x_token_spell & unmodelled_effect
# ---------------------------------------------------------------------------


def test_text_x_token_spell_parses_haste_swarm_with_cant_block():
    card = _x_haste_swarm()
    parsed = _text_x_token_spell(card)
    assert parsed == (1, 1, ("_cant_block", "haste"), "Rat token", True)
    assert unmodelled_effect(card) == ""

    bodies = make_tokens([(2, *parsed[:4])], turn=4, rules={}, first_id=900_000_000)
    assert len(bodies) == 2
    assert bodies[0]["_can_block"] is False
    assert "haste" in [k.lower() for k in bodies[0]["keywords"]]


def test_text_x_token_spell_parses_plain_and_keyworded_variants():
    plain = _x_plain_tokens()
    assert _text_x_token_spell(plain) == (1, 1, (), "Warrior token", False)
    assert unmodelled_effect(plain) == ""

    vigilant = {
        "name": "Banner of Dawn",
        "mana_cost": "{X}{W}{W}",
        "type_line": "Sorcery",
        "oracle_text": "Create X 2/2 white Soldier creature tokens with vigilance.",
    }
    assert _text_x_token_spell(vigilant) == (2, 2, ("vigilance",), "Soldier token", False)
    assert unmodelled_effect(vigilant) == ""

    mixed = {
        "name": "Feral Stampede",
        "mana_cost": "{X}{R}",
        "type_line": "Sorcery",
        "oracle_text": 'Create X 1/1 red Imp creature tokens with flying and "This creature can\'t block."',
    }
    assert _text_x_token_spell(mixed) == (1, 1, ("_cant_block", "flying"), "Imp token", False)

    token_haste = {
        "name": "Spark Rally",
        "mana_cost": "{X}{R}",
        "type_line": "Sorcery",
        "oracle_text": "Create X 1/1 red Goblin creature tokens. They gain haste until end of turn.",
    }
    assert _text_x_token_spell(token_haste) == (1, 1, ("haste",), "Goblin token", False)


def test_text_x_token_spell_fails_closed_on_complex_or_non_token_x_spells():
    controls = [
        # X damage spell
        {
            "name": "Ember X",
            "mana_cost": "{X}{R}",
            "type_line": "Sorcery",
            "oracle_text": "Ember X deals X damage to any target.",
        },
        # Double X cost
        {
            "name": "Double X Swarm",
            "mana_cost": "{X}{X}{B}",
            "type_line": "Instant",
            "oracle_text": "Create X 2/2 black Zombie creature tokens.",
        },
        # Conditional board wipe bonus
        {
            "name": "Coup of Iron",
            "mana_cost": "{X}{W}{W}",
            "type_line": "Sorcery",
            "oracle_text": "Create X 1/1 white Soldier creature tokens. If X is 5 or more, destroy all other creatures.",
        },
        # Convoke mechanic
        {
            "name": "Convoke March",
            "mana_cost": "{X}{G}{W}{W}",
            "type_line": "Instant",
            "oracle_text": "Convoke\nCreate X 1/1 white Soldier creature tokens with lifelink.",
        },
        # Unmodelled keyword (toxic 1)
        {
            "name": "Mite Surge",
            "mana_cost": "{X}{W}",
            "type_line": "Sorcery",
            "oracle_text": 'Create X 1/1 colorless Mite creature tokens with toxic 1 and "This creature can\'t block."',
        },
        # Extra life gain clause
        {
            "name": "Twilight Swarm",
            "mana_cost": "{X}{W}{W}",
            "type_line": "Sorcery",
            "oracle_text": "You gain X life. Create X 1/1 white Soldier creature tokens.",
        },
        # Legendary tokens (legend rule would kill X-1 of them)
        {
            "name": "Legend Clones",
            "mana_cost": "{X}{W}",
            "type_line": "Sorcery",
            "oracle_text": "Create X 2/2 legendary white Knight creature tokens.",
        },
        # Permanent with X in cost
        {
            "name": "X Creature",
            "mana_cost": "{X}{G}",
            "type_line": "Creature — Hydra",
            "oracle_text": "Create X 1/1 green Saproling creature tokens.",
        },
    ]
    for card in controls:
        assert _text_x_token_spell(card) is None, card["name"]
        assert unmodelled_effect(card) == "its X cost", card["name"]


# ---------------------------------------------------------------------------
# End-to-end assess() / line_search tests
# ---------------------------------------------------------------------------


def test_line_search_finds_lethal_with_haste_x_token_sorcery():
    swarm = _x_haste_swarm(100)
    state = _state(
        our_life=10,
        opp_life=3,
        battlefield=[_mountain(1), _mountain(2), _mountain(3), _mountain(4)],
        hand=[swarm],
    )
    assessment = assess(state)
    assert assessment is not None
    res = assessment.line_search
    assert res is not None
    assert unmodelled_effect(swarm, res) == ""
    assert res.best.win_at == 1
    assert "Chorus of Embers" in res.best.steps[0].casts
    assert ("Chorus of Embers", "X=3") in res.best.steps[0].modes


def test_line_search_sequences_creature_before_haste_x_token_spell_for_lethal():
    """With 4 mana, casting a 4/4 for {1}{R} BEFORE the {X}{R} haste swarm (X=1) deals 5 lethal."""
    big_two_drop = _creature(99, "Iron Bruiser", 4, 4, mana_cost="{1}{R}", cmc=2)
    swarm = _x_haste_swarm(100)
    state = _state(
        our_life=10,
        opp_life=5,
        battlefield=[_mountain(1), _mountain(2), _mountain(3), _mountain(4)],
        hand=[swarm, big_two_drop],
    )
    assessment = assess(state)
    assert assessment is not None
    res = assessment.line_search
    assert res is not None
    assert unmodelled_effect(swarm, res) == ""
    assert res.best.win_at == 1
    casts = res.best.steps[0].casts
    assert "Iron Bruiser" in casts and "Chorus of Embers" in casts
    assert casts.index("Iron Bruiser") < casts.index("Chorus of Embers")
    assert res.best.steps[0].post_combat == ()


def test_line_search_x_zero_haste_lethal_for_sick_board_creature():
    """With 1 Mountain and a summoning-sick 4/4 on board, casting X=0 grants haste for lethal."""
    sick_bruiser = _creature(98, "Iron Bruiser", 4, 4, turn_entered=4)
    swarm = _x_haste_swarm(100)
    state = _state(
        our_life=10,
        opp_life=4,
        battlefield=[_mountain(1), sick_bruiser],
        hand=[swarm],
        turn=4,
    )
    assessment = assess(state)
    assert assessment is not None and assessment.line_search is not None
    res = assessment.line_search
    assert res.best.win_at == 1
    assert ("Chorus of Embers", "X=0") in res.best.steps[0].modes


def test_line_search_mid_range_x_not_skipped_with_hand_lands():
    """With 5 Mountains on board and 2 tapped lands in hand, X=4 must not be skipped for Turn-1 lethal."""
    tapped_land = {
        "instance_id": 201,
        "name": "Cinder Crag",
        "type_line": "Land",
        "card_types": ["CardType_Land"],
        "oracle_text": "Cinder Crag enters tapped.\n{T}: Add {R}.",
        "controller_seat_id": 1,
    }
    tapped_land_2 = {**tapped_land, "instance_id": 202}
    swarm = _x_haste_swarm(100)
    state = _state(
        our_life=10,
        opp_life=4,
        battlefield=[_mountain(1), _mountain(2), _mountain(3), _mountain(4), _mountain(5)],
        hand=[swarm, tapped_land, tapped_land_2],
        turn=5,
    )
    assessment = assess(state)
    assert assessment is not None and assessment.line_search is not None
    res = assessment.line_search
    assert res.best.win_at == 1
    assert ("Chorus of Embers", "X=4") in res.best.steps[0].modes


def test_line_search_main2_skips_x_zero_haste_only_cast():
    """In Main Phase 2, X=0 haste-only cast is useless and must not be chosen."""
    sick_bruiser = _creature(98, "Iron Bruiser", 4, 4, turn_entered=4)
    swarm = _x_haste_swarm(100)
    state = _state(
        our_life=10,
        opp_life=4,
        battlefield=[_mountain(1), sick_bruiser],
        hand=[swarm],
        turn=4,
    )
    state["turn"]["phase"] = "Phase_Main2"
    assessment = assess(state)
    assert assessment is not None and assessment.line_search is not None
    res = assessment.line_search
    assert ("Chorus of Embers", "X=0") not in res.best.steps[0].modes


def test_line_search_respects_cant_block_on_x_tokens_vs_blocking_control():
    """Tokens with 'can't block' cannot stop a lethal 10/10 crackback; normal X tokens can."""
    opp_giant = _creature(50, "Colossal Titan", 10, 10, seat=2, turn_entered=1)

    # Case 1: tokens can't block -> we die on opponent's turn
    cant_block_state = _state(
        our_life=10,
        opp_life=20,
        battlefield=[_mountain(1), _mountain(2), _mountain(3), opp_giant],
        hand=[_x_haste_swarm(100)],
    )
    a_cant = assess(cant_block_state)
    assert a_cant is not None and a_cant.line_search is not None
    assert a_cant.line_search.best.dead_at == 1

    # Case 2: control — plain X-token instant creates chump blockers -> we survive
    can_block_state = _state(
        our_life=10,
        opp_life=20,
        battlefield=[_plains(1), _plains(2), _plains(3), opp_giant],
        hand=[_x_plain_tokens(101)],
    )
    a_can = assess(can_block_state)
    assert a_can is not None and a_can.line_search is not None
    assert a_can.line_search.best.dead_at != 1


def test_line_search_keeps_conditional_x_token_spell_unmodelled():
    conditional_x = {
        "instance_id": 102,
        "name": "Coup of Iron",
        "mana_cost": "{X}{W}{W}",
        "cmc": 2,
        "type_line": "Sorcery",
        "card_types": ["CardType_Sorcery"],
        "oracle_text": "Create X 1/1 white Soldier creature tokens. If X is 5 or more, destroy all other creatures.",
        "controller_seat_id": 1,
    }
    state = _state(
        our_life=10,
        opp_life=10,
        battlefield=[_plains(1), _plains(2), _plains(3), _plains(4)],
        hand=[conditional_x],
    )
    assessment = assess(state)
    assert assessment is not None
    res = assessment.line_search
    assert res is not None
    assert unmodelled_effect(conditional_x, res) == "its X cost"
    assert "Coup of Iron" not in res.best.steps[0].casts
