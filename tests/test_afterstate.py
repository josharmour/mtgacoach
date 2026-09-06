"""Golden unit tests for the 1-ply Synergy Afterstate Engine."""

from arenamcp.ability_synthesizer import AbilitySynthesizer
from arenamcp.afterstate import (
    Action,
    ActivateAbility,
    AfterstateSimulator,
    BoardState,
    CastSpell,
    Permanent,
    PlayLand,
    score_board_state,
)
from arenamcp.format_profile import FormatEvaluatorConfig


def test_samwise_and_notary_hobbits_cascade():
    # 1. Samwise Gamgee is on battlefield
    samwise_oracle = (
        "Whenever another nontoken creature enters the battlefield under your control, create a Food token.\n"
        "{2}, Sacrifice three Foods: Return target historic card from your graveyard to your hand."
    )
    sam_abs = AbilitySynthesizer.parse_card("Samwise Gamgee", samwise_oracle)

    samwise_perm = Permanent(
        name="Samwise Gamgee",
        power=2,
        toughness=2,
        is_commander=True,
        abilities=sam_abs.abilities,
        oracle_text=samwise_oracle,
    )

    board = BoardState(
        hero_life=25,
        opp_life=25,
        available_mana=4,
        permanents=(samwise_perm,),
        hand=("The Notary Hobbits",),
        config=FormatEvaluatorConfig(life_norm=25, token_synergy_weight=1.0),
    )

    # Pre-parse The Notary Hobbits into AbilitySynthesizer cache
    hobbit_oracle = (
        "When The Notary Hobbits enters the battlefield, if it's not a token, "
        "create two tokens that are copies of The Notary Hobbits, except the tokens aren't legendary.\n"
        "{T}: Add {C} for each Halfling you control."
    )
    AbilitySynthesizer.parse_card("The Notary Hobbits", hobbit_oracle)

    # 2. Hero casts The Notary Hobbits
    action = CastSpell(
        card_name="The Notary Hobbits",
        from_zone="hand",
        cmc=4,
        oracle_text=hobbit_oracle,
    )

    afterstate = AfterstateSimulator.apply(board, action)

    # 3. Verify bodies and tokens
    # Should have: Samwise (1) + Notary Hobbits (1) + 2 Notary Hobbit Tokens (2) + 1 Food Token (1) = 5 permanents!
    assert len(afterstate.board.permanents) == 5

    # Exactly 1 Food token created (Samwise only triggers on the NONTOKEN original Notary Hobbits)
    assert afterstate.delta.tokens_created.get("Food") == 1
    assert afterstate.delta.tokens_created.get("The Notary Hobbits") == 2
    assert afterstate.delta.bodies_added >= 3

    # Verify trigger trace captured both events
    trace_str = " | ".join(afterstate.trigger_trace)
    assert "Notary Hobbits" in trace_str
    assert "Food" in trace_str

    # 4. Value function score should be higher than root
    root_val = score_board_state(board)
    after_val = score_board_state(afterstate.board)
    assert after_val > root_val
    delta_v = round(after_val - root_val, 3)
    assert delta_v > 0.05


def test_food_sacrifice_at_low_life():
    food_perm = Permanent(name="Food", is_token=True)
    board = BoardState(
        hero_life=6,
        opp_life=18,
        available_mana=2,
        permanents=(food_perm,),
        config=FormatEvaluatorConfig(life_norm=25, lethal_zone=8),
    )

    act = ActivateAbility(
        permanent_name="Food",
        is_food_sacrifice=True,
    )

    afterstate = AfterstateSimulator.apply(board, act)

    # Life total went from 6 -> 9
    assert afterstate.board.hero_life == 9
    assert afterstate.delta.life_gained == 3
    assert len(afterstate.board.permanents) == 0  # Food sacrificed

    # Value should jump significantly out of danger
    root_v = score_board_state(board)
    after_v = score_board_state(afterstate.board)
    assert after_v > root_v


def test_play_land_mana_development():
    board = BoardState(
        hero_life=20,
        opp_life=20,
        available_mana=2,
        lands_played_this_turn=0,
        hand=("Forest",),
    )

    act = PlayLand(land_name="Forest")
    afterstate = AfterstateSimulator.apply(board, act)

    assert afterstate.board.lands_played_this_turn == 1
    assert afterstate.board.available_mana == 3
    assert "Forest" in [p.name for p in afterstate.board.permanents]
