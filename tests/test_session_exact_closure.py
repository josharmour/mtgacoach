"""Exact session misconceptions and their observable corrected paths, offline."""

from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_model import build_board_model
from arenamcp.line_search_moves import Moves
from arenamcp.persistent_returns import returned_enchantment

TENACITY = "Whenever you gain life, target opponent loses that much life.\nWhen Enduring Tenacity dies, if it was a creature, return it to the battlefield under its owner's control. It's an enchantment."


def card(i, name, text, kind="Creature", power=1, toughness=1, owner=2, **extra):
    return dict(
        instance_id=i,
        name=name,
        oracle_text=text,
        type_line=kind,
        power=power,
        toughness=toughness,
        controller_seat_id=owner,
        owner_seat_id=owner,
        **extra,
    )


def board(spell="Lightning Strike", text="Lightning Strike deals 3 damage to any target."):
    return dict(
        local_seat_id=1,
        opponent_seat_id=2,
        turn=dict(turn_number=8, active_player=1, phase="Main1", step="None"),
        players=[
            dict(seat_id=1, is_local=True, life_total=18, lands_played=1),
            dict(seat_id=2, life_total=21),
        ],
        battlefield=[
            card(10, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain", owner=1),
            card(11, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain", owner=1),
            card(371, "Enduring Tenacity", TENACITY, "Enchantment Creature", 4, 3),
            card(360, "Healer's Hawk", "Flying\nLifelink", keywords=["Flying", "Lifelink"]),
        ],
        hand=[card(378, spell, text, "Instant", owner=1, mana_cost="{1}{R}")],
        graveyard=[],
        stack=[],
    )


def test_307_strike_removes_body_but_preserves_engine_and_hawk_drain():
    moves = Moves(build_board_model(board()), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    root = moves.root_node()
    target = next(b for b in root.theirs if b["name"] == "Enduring Tenacity")
    mid = moves.begin(root, None, ((moves.spells[0].plain, target),))
    assert [c["name"] for c in mid.persistent] == ["Enduring Tenacity"]
    assert all(c["name"] != "Enduring Tenacity" for c in mid.theirs)
    assert mid.persistent[0]["type_line"] == "Enchantment"
    after = moves.finish(mid, ("none", ()))
    assert after.our_life == 16  # Hawk 1 combat + 1 from still-present Tenacity
    assert after.opp_life == 22
    assert len(after.persistent) == 1
    assert moves.value(18, 21, (), (), (), persistent=after.persistent) < moves.value(18, 21, (), (), ())
    second = moves.finish(moves.begin(after, None, ()), ("none", ()))
    assert second.our_life == 14 and len(second.persistent) == 1


def test_307_exile_does_not_return_engine():
    moves = Moves(
        build_board_model(board("Exile fixture", "Exile target creature.")),
        survival=False,
        lethal_now=False,
        hard_ms=1e8,
        opponent=None,
    )
    root = moves.root_node()
    target = next(b for b in root.theirs if b["name"] == "Enduring Tenacity")
    mid = moves.begin(root, None, ((moves.spells[0].plain, target),))
    assert not mid.persistent
    after = moves.finish(mid, ("none", ()))
    assert after.our_life == 17


def test_307_loop_answer_destruction_is_not_permanent_engine_removal():
    from arenamcp.drain_loop import card_removes

    s = board()
    target = s["battlefield"][2]
    assert not card_removes(card(99, "Destroy", "Destroy target creature.", "Instant", owner=1), target, s)
    assert card_removes(card(99, "Exile", "Exile target creature.", "Instant", owner=1), target, s)
    assert returned_enchantment({**target, "type_line": "Enchantment"}) is None
    assert returned_enchantment({**target, "is_token": True}) is None


@pytest.mark.parametrize(
    "target,claim,expected",
    [
        (
            card(371, "Enduring Tenacity", TENACITY, "Enchantment Creature", 4, 3),
            "Lightning Strike kills the Enduring Tenacity engine (it returns as a harmless enchantment), removing their biggest threat and lifegain-drain combo before developing more creatures.",
            "not its persistent engine",
        ),
        (
            card(
                4,
                "Fleeting Effigy",
                "At the beginning of your end step, return Fleeting Effigy to its owner's hand.",
                owner=1,
            ),
            "Deploy Fleeting Effigy as a hasty blocker to trade with an attacker and reduce crackback damage, since no burn is in hand.",
            "cannot be held as a blocker",
        ),
        (
            card(
                5,
                "Infestation Sage",
                "When this creature dies, create a 1/1 black and green Insect creature token with flying.",
            ),
            "Kill Infestation Sage now with Burst Lightning before it can attack or be sacrificed, denying the opponent their flying Insect token.",
            "does not deny that token",
        ),
        (
            card(
                5,
                "Infestation Sage",
                "When this creature dies, create a 1/1 black and green Insect creature token with flying.",
            ),
            "Kills the 1/1 Infestation Sage before it can attack or leave a flying Insect token.",
            "does not deny that token",
        ),
        (
            card(6, "Attercop", "Reach\nDeathtouch", keywords=["Reach"]),
            "Casting Slickshot adds a hasty flier and prowess body for the lethal swing; opponent at 3 with no reach on fliers, so it advances the attack.",
            "can block a flyer",
        ),
    ],
)
def test_exact_false_explanation_replaced_before_reuse(target, claim, expected):
    planner = object.__new__(ActionPlanner)
    planner._last_decision_reasoning = claim
    planner._last_decision_trace = {}
    planner._note_false_claims(
        dict(local_seat_id=1, battlefield=[target], hand=[]), SimpleNamespace(options=[]), []
    )
    assert planner._last_decision_reasoning.startswith("Rules correction:")
    assert expected in planner._last_decision_reasoning
    assert claim not in planner._last_decision_reasoning
    assert planner._last_decision_trace["uncorrected_reasoning"] == claim


def test_41_reach_blocks_slickshot_in_actual_combat_math():
    from arenamcp.board_assessment import _body, _combat

    flyer = _body(
        card(
            7,
            "Slickshot Show-Off",
            "Flying\nHaste",
            power=1,
            toughness=2,
            owner=1,
            keywords=["Flying", "Haste"],
        ),
        8,
        {},
    )
    reach = _body(
        card(6, "Attercop", "Reach\nDeathtouch", power=1, toughness=3, keywords=["Reach", "Deathtouch"]),
        8,
        {},
    )
    assert _combat([flyer], [reach], 3)[0] == 0


def test_307_forced_mode_keeps_returned_engine_separate_from_creatures():
    model = build_board_model(board())
    target = next(b for b in model.theirs if b["name"] == "Enduring Tenacity")
    moves = Moves(
        model,
        survival=False,
        lethal_now=False,
        hard_ms=1e8,
        opponent=None,
        root_effect={"target": target, "source": {"oracle_text": "Deal 4 damage to target creature."}},
    )
    root = moves.root_node()
    assert len(root.persistent) == 1
    assert all(b["name"] != "Enduring Tenacity" for b in root.theirs)


def test_307_suppressed_death_trigger_does_not_return():
    s = board()
    s["battlefield"].append(
        card(
            90,
            "Hushbringer",
            "Creatures entering the battlefield or dying don't cause abilities to trigger.",
            owner=1,
        )
    )
    moves = Moves(build_board_model(s), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    root = moves.root_node()
    target = next(b for b in root.theirs if b["name"] == "Enduring Tenacity")
    assert not moves.begin(root, None, ((moves.spells[0].plain, target),)).persistent


def test_307_visible_return_engine_still_completes_lifelink_loop():
    from arenamcp.persistent_returns import gain_drain

    returned = returned_enchantment(board()["battlefield"][2])
    conqueror = card(
        20, "Bloodthirsty Conqueror", "Whenever an opponent loses life, you gain that much life."
    )
    assert gain_drain([returned, conqueror], 1, 18) == 18
    assert gain_drain([returned, conqueror], 0, 18) == 0


def test_correct_exile_and_expiry_explanations_are_preserved():
    from arenamcp.session_facts import corrected_reasoning

    sage = card(
        5,
        "Infestation Sage",
        "When this creature dies, create a 1/1 black and green Insect creature token with flying.",
    )
    text = "Exile Infestation Sage to deny the death token."
    assert corrected_reasoning(text, dict(local_seat_id=1, battlefield=[sage], hand=[])) == text
    effigy = card(
        4,
        "Fleeting Effigy",
        "At the beginning of your end step, return Fleeting Effigy to its owner's hand.",
        owner=1,
    )
    text = "Fleeting Effigy cannot block next turn because it returns to hand."
    assert corrected_reasoning(text, dict(local_seat_id=1, battlefield=[effigy], hand=[])) == text


def test_307_pending_strike_keeps_engine_in_projected_battlefield():
    from arenamcp.line_search import _after_our_pending

    s = board()
    s["stack"] = [{**s["hand"].pop(), "targeting": [371]}]
    model = build_board_model(s)
    assert 371 in model.our_stack_doomed
    after = _after_our_pending(model)
    engine = next(c for c in after.battlefield if c["instance_id"] == 371)
    assert engine["type_line"] == "Enchantment"
    assert all(c["instance_id"] != 371 for c in after.theirs)
    assert _after_our_pending(after) is after
