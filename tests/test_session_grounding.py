from arenamcp.session_facts import public_rules_notes, rules_block


def test_grounding_public_rules_not_invented_win():
    cards = [
        dict(
            instance_id=1,
            name="Attercop",
            controller_seat_id=2,
            oracle_text="Reach\nDeathtouch",
            keywords=["Reach"],
        ),
        dict(
            instance_id=2,
            name="Infestation Sage",
            controller_seat_id=2,
            oracle_text="When this creature dies, create a 1/1 black and green Insect creature token with flying.",
        ),
        dict(
            instance_id=3,
            name="Enduring Tenacity",
            controller_seat_id=2,
            oracle_text="When Enduring Tenacity dies, if it was a creature, return it to the battlefield under its owner's control. It's an enchantment.",
        ),
        dict(
            instance_id=4,
            name="Fleeting Effigy",
            controller_seat_id=1,
            oracle_text="At the beginning of your end step, return Fleeting Effigy to its owner's hand.",
        ),
    ]
    state = dict(local_seat_id=1, battlefield=cards, hand=[])
    notes = rules_block(state)
    assert "has reach" in notes and "does not deny that token" in notes
    assert "not its persistent engine" in notes and "cannot be held as a blocker" in notes
    cards[0]["is_tapped"] = True
    assert not any("has reach" in x for x in public_rules_notes(state))


def test_death_token_removal_vs_exile_and_suppression():
    from arenamcp.death_bodies import death_is_suppressed, removal_causes_death, token_specs_on_death

    sage = dict(
        name="Infestation Sage",
        oracle_text="When this creature dies, create a 1/1 black and green Insect creature token with flying.",
    )
    specs = token_specs_on_death(sage)
    assert specs and specs[0][:3] == (1, 1, 1) and "flying" in specs[0][3]
    assert removal_causes_death(dict(oracle_text="Deal 2 damage to target creature."))
    assert not removal_causes_death(dict(oracle_text="Exile target creature."))
    assert not removal_causes_death({}, bounce=True)
    assert death_is_suppressed(
        [dict(oracle_text="Creatures entering the battlefield or dying don't cause abilities to trigger.")]
    )
    assert not token_specs_on_death(
        dict(
            name="Maybe",
            oracle_text="When this creature dies, if you control an artifact, create a 1/1 Insect creature token.",
        )
    )


def test_line_transition_keeps_sages_replacement_flying_blocker():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves

    s = dict(
        local_seat_id=1,
        opponent_seat_id=2,
        turn=dict(turn_number=3, active_player=1, phase="Main1", step="None"),
        players=[
            dict(seat_id=1, is_local=True, life_total=20, lands_played=0),
            dict(seat_id=2, life_total=20, lands_played=0),
        ],
        battlefield=[
            dict(
                instance_id=1,
                name="Mountain",
                controller_seat_id=1,
                type_line="Basic Land — Mountain",
                oracle_text="{T}: Add {R}.",
            ),
            dict(
                instance_id=2,
                name="Infestation Sage",
                controller_seat_id=2,
                type_line="Creature",
                power=1,
                toughness=1,
                oracle_text="When this creature dies, create a 1/1 black and green Insect creature token with flying.",
            ),
        ],
        hand=[
            dict(
                instance_id=3,
                name="Shock",
                controller_seat_id=1,
                type_line="Instant",
                mana_cost="{R}",
                oracle_text="Shock deals 2 damage to any target.",
            )
        ],
        stack=[],
        graveyard=[],
    )
    moves = Moves(build_board_model(s), survival=False, lethal_now=False, hard_ms=100000, opponent=None)
    node = moves.root_node()
    var = moves.spells[0].plain
    mid = moves.begin(node, None, ((var, node.theirs[0]),))
    assert len(mid.theirs) == 1 and "Insect" in mid.theirs[0]["name"]
    assert "flying" in mid.theirs[0]["keywords"]


def test_known_false_rules_are_not_reused_as_announcer_strategy():
    from arenamcp.commentary.rationale import purpose
    from arenamcp.game_plan import decision_claim_notes

    state = {
        "local_seat_id": 1,
        "battlefield": [
            dict(
                name="Infestation Sage",
                controller_seat_id=2,
                type_line="Creature",
                power=1,
                toughness=1,
                oracle_text="When this creature dies, create a 1/1 black and green Insect creature token with flying.",
            )
        ],
        "hand": [],
    }
    notes = decision_claim_notes("Burn Infestation Sage to deny the token.", state)
    assert notes and "does not deny that token" in notes[0]
    assert not decision_claim_notes("Killing Infestation Sage does not deny the token.", state)
    assert not purpose("Casting Shock. Burn to deny the token. [checked: killing it makes a token]")


def test_exact_tenacity_recurrence_does_not_call_returned_engine_harmless():
    from arenamcp.game_plan import decision_claim_notes

    state = {
        "local_seat_id": 1,
        "battlefield": [
            dict(
                name="Enduring Tenacity",
                controller_seat_id=2,
                type_line="Enchantment Creature",
                power=4,
                toughness=3,
                oracle_text="Whenever you gain life, target opponent loses that much life. When Enduring Tenacity dies, if it was a creature, return it to the battlefield under its owner's control. It's an enchantment.",
            )
        ],
        "hand": [],
    }
    for text in (
        "Lightning Strike kills the Enduring Tenacity engine (it returns as a harmless enchantment), removing their biggest threat and lifegain-drain combo before developing more creatures.",
        "Kills the Enduring Tenacity lifegain-drain engine, their biggest threat, as intended.",
    ):
        assert any("not its persistent engine" in note for note in decision_claim_notes(text, state))
    assert not decision_claim_notes(
        "Killing Enduring Tenacity removes the body but does not stop its engine; it remains an enchantment.",
        state,
    )
