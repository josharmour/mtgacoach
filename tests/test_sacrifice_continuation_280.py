"""Equivalent legal public fixtures, not the missing #280 historical packet."""

from copy import deepcopy

import pytest

from arenamcp.sacrifice_continuation import UnsupportedContinuation, resolve, seat, unsupported

PACT = "Whenever a creature you control dies, each other player sacrifices a creature."
TERGRID = (
    "Menace\nWhenever an opponent sacrifices a nontoken permanent or discards a permanent card, "
    "you may put that card from a graveyard onto the battlefield under your control."
)


def card(i, controller, p=2, t=2, text="", **more):
    return dict(
        instance_id=i,
        name=f"Creature {i}",
        type_line="Creature",
        card_types=["Creature"],
        controller_seat_id=controller,
        owner_seat_id=controller,
        power=p,
        toughness=t,
        printed_power=p,
        printed_toughness=t,
        oracle_text=text,
        _can_attack=True,
        _can_block=True,
        _unblockable=False,
        **more,
    )


def pact(i=90, controller=1):
    return dict(
        instance_id=i,
        name="Public Pact",
        type_line="Enchantment",
        card_types=["Enchantment"],
        controller_seat_id=controller,
        owner_seat_id=controller,
        oracle_text=PACT,
    )


def tree(live, dead, permanents=(), **options):
    return resolve([*permanents, *live, *dead], live, dead, active=1, other=2, turn=6, **options)


def test_opponent_keeps_every_legal_victim_choice_and_tergrid_is_optional():
    live = [card(10, 1, 4, 5, TERGRID), card(2, 2, 1, 1, is_token=True), card(3, 2, 5, 5)]
    dead = [card(1, 1, is_token=True)]
    before = deepcopy((live, dead))
    choices = tree(live, dead, [pact()])
    outcomes = choices.outcomes()
    assert {o.sacrificed for o in outcomes} == {(2,), (3,)}
    assert any(o.sacrificed == (3,) and not o.transfers for o in outcomes)
    assert any(o.sacrificed == (3,) and o.transfers for o in outcomes)
    assert not any(o.transfers and o.transfers[0][0] == 2 for o in outcomes)
    chosen = choices.choose(lambda o: sum(c["power"] * (1 if seat(c) == 1 else -1) for c in o.bodies), 1)
    assert chosen.sacrificed == (2,)  # their choice, not our preferred stolen 5/5
    assert (live, dead) == before


def test_transfer_is_fresh_printed_body_with_owner_retained():
    victim = card(2, 2, 2, 3, counters={"CounterType_P1P1": 4}, damage=1, attachments=[77])
    victim.update(power=6, toughness=7, is_tapped=True, is_attacking=True, keywords=["flying"])
    outcomes = tree([card(10, 1, 4, 5, TERGRID), victim], [card(1, 1)], [pact()]).outcomes()
    taken = next(c for o in outcomes if o.transfers for c in o.bodies if c["instance_id"] < 0)
    assert (taken["power"], taken["toughness"]) == (2, 3)
    assert seat(taken) == 1 and taken["_card"]["owner_seat_id"] == 2
    assert taken["_card"]["counters"] == {} and taken["_card"]["damage"] == 0
    assert not taken["keywords"] and not taken["_tapped"] and taken["_sick"]


def test_two_pacts_generate_two_sacrifices_for_one_death():
    outcomes = tree([card(2, 2), card(3, 2), card(4, 2)], [card(1, 1)], [pact(), pact(91)]).outcomes()
    assert {frozenset(o.sacrificed) for o in outcomes} == {
        frozenset((2, 3)),
        frozenset((2, 4)),
        frozenset((3, 4)),
    }
    assert all(len(o.bodies) == 1 for o in outcomes)


def test_chained_sacrifice_deaths_trigger_both_players_pacts():
    outcomes = tree([card(2, 1), card(3, 2), card(4, 2)], [card(1, 1)], [pact(), pact(91, 2)]).outcomes()
    assert outcomes and all(not o.bodies and set(o.sacrificed) == {2, 3, 4} for o in outcomes)


def test_dying_creature_source_sees_simultaneous_deaths_but_not_later_ones():
    source = card(
        1,
        1,
        text="Whenever this creature or another creature you control dies, each opponent sacrifices a creature.",
    )
    outcomes = tree([card(2, 2), card(3, 2), card(4, 2)], [source, card(5, 1)]).outcomes()
    assert all(len(o.sacrificed) == 2 and len(o.bodies) == 1 for o in outcomes)


def test_combat_death_is_not_itself_a_tergrid_sacrifice():
    outcomes = tree([card(10, 1, 4, 5, TERGRID)], [card(2, 2)], [pact()]).outcomes()
    assert len(outcomes) == 1 and not outcomes[0].transfers


def test_suppression_and_phased_out_pact_do_not_invent_sacrifices():
    suppressor = dict(
        instance_id=80,
        controller_seat_id=1,
        type_line="Enchantment",
        oracle_text="Creatures dying don't cause abilities to trigger.",
    )
    outcomes = tree([card(2, 2)], [card(1, 1)], [pact(), suppressor]).outcomes()
    assert not outcomes[0].sacrificed
    phased = {**pact(), "is_phased_out": True}
    assert not tree([card(2, 2)], [card(1, 1)], [phased]).outcomes()[0].sacrificed


@pytest.mark.parametrize(
    "extra",
    [
        "First strike",
        "Double strike",
        "When this creature dies, create a 1/1 creature token.",
        "When this creature dies, you gain 2 life.",
        "Sacrifice a creature: Draw a card.",
        "This creature can't be sacrificed.",
    ],
)
def test_unsupported_interactions_fail_explicitly_without_partial_results(extra):
    with pytest.raises(UnsupportedContinuation):
        tree([card(2, 2, text=extra)], [card(1, 1)], [pact()])


def test_missing_printed_characteristics_and_etb_are_not_guessed():
    victim = card(2, 2)
    victim.pop("printed_power")
    assert unsupported([pact(), card(10, 1, 4, 5, TERGRID), victim])
    victim = card(2, 2, text="When this creature enters, draw a card.")
    with pytest.raises(UnsupportedContinuation):
        tree([card(10, 1, 4, 5, TERGRID), victim], [card(1, 1)], [pact()])


def test_bound_exhaustion_never_selects_first_or_partial_outcome():
    with pytest.raises(UnsupportedContinuation, match="bound"):
        tree([card(2, 2), card(3, 2)], [card(1, 1)], [pact()], max_states=2)


def test_apnap_queued_transfer_resolves_after_tergrid_is_sacrificed():
    outcomes = tree([card(10, 1, 4, 5, TERGRID), card(2, 2)], [card(1, 1)], [pact(), pact(91, 2)]).outcomes()
    assert all(o.sacrificed == (2, 10) for o in outcomes)
    assert any(o.transfers and len(o.bodies) == 1 and seat(o.bodies[0]) == 1 for o in outcomes)
    assert any(not o.transfers and not o.bodies for o in outcomes)


def model_for(ours, theirs, permanents=(), active=1):
    from arenamcp.board_model import build_board_model
    from test_combat_loss_281 import board

    state = board(ours, theirs, active=active)
    state["battlefield"].extend(permanents)
    return build_board_model(state), state


def test_search_own_combat_keeps_forced_sacrifice_and_transfer_in_next_board():
    from arenamcp.line_search_moves import Moves

    model, state = model_for([card(1, 1), card(10, 1, 4, 5, TERGRID)], [card(2, 2, 3, 3)], [pact()])
    before = deepcopy(state)
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    after = moves.finish(moves.begin(moves.root_node(), None, ()), ("all", (1,)))
    assert not after.theirs and len(after.ours) == 2
    captured = next(c for c in after.ours if c["instance_id"] < 0)
    assert captured["power"] == 3 and seat(captured) == 1
    assert dict(after.entered)[captured["instance_id"]] == model.turn
    assert moves.sacrifice_choices and moves.choice_caveats
    assert state == before


def test_search_opponent_combat_retains_captured_blocker_for_next_turn():
    from arenamcp.line_search_moves import Moves

    attacker = card(2, 2, is_attacking=True)
    model, _ = model_for([card(1, 1, 3, 3)], [attacker, card(10, 2, 4, 5, TERGRID)], [pact(90, 2)], active=2)
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    nodes = [n for _, n in moves.roots() if not n.terminal]
    assert any(not n.ours and any(c["instance_id"] < 0 and seat(c) == 2 for c in n.theirs) for n in nodes)


def test_supported_choices_are_advisory_and_all_alternatives_remain_available():
    from arenamcp.line_guard import _usable
    from arenamcp.line_search import search_lines

    model, _ = model_for(
        [card(1, 1), card(10, 1, 4, 5, TERGRID)],
        [card(2, 2, 3, 3), card(3, 2, 1, 1, is_token=True)],
        [pact()],
    )
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e8)
    assert not result.unsupported and result.choice_caveats
    assert not result.exact_first_attack and not _usable(result)
    assert "advisory" in result.prompt_line()
    assert any(len(t.outcomes()) > 1 for t in result._search.sacrifice_choices.values())


def test_unsupported_search_does_not_fall_back_to_a_fabricated_lethal_forecast():
    from arenamcp.board_assessment import assess
    from arenamcp.line_guard import _usable
    from arenamcp.line_search import evaluate_plan, search_lines

    model, state = model_for([card(1, 1, 50, 50, "First strike")], [card(2, 2)], [pact()])
    result = search_lines(model, survival=False, lethal_now=True, hard_ms=1e8)
    assert result.unsupported and not result.lines and not result.first_action
    assert result.best.outcome == "unknown" and not _usable(result)
    assert "UNSUPPORTED" in result.prompt_line()
    assert evaluate_plan(result, []).unmodelled
    assessment = assess(state)
    assert assessment is not None and assessment.line_search.unsupported
    assert not assessment.lethal_now and not assessment.lethal_next_turn and not assessment.all_in
    assert assessment.dead_in is None and assessment.our_clock is None
    assert any("sacrifice continuation unsupported" in s for s in assessment.unknowns)


def test_runtime_bound_failure_discards_the_whole_search(monkeypatch):
    from arenamcp import sacrifice_continuation as sc
    from arenamcp.line_search import search_lines

    real = sc.resolve
    monkeypatch.setattr(sc, "resolve", lambda *a, **kw: real(*a, **kw, max_states=1))
    model, _ = model_for([card(1, 1)], [card(2, 2, 3, 3)], [pact()])
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e8)
    assert result.unsupported and "bound" in result.unsupported[0]
    assert not result.lines and result.best.outcome == "unknown"


def test_sacrifice_ignores_hexproof_shroud_and_indestructible():
    outcome = tree([card(2, 2, text="Hexproof, shroud, indestructible")], [card(1, 1)], [pact()]).outcomes()[
        0
    ]
    assert outcome.sacrificed == (2,) and not outcome.bodies


def test_opponent_sacrifice_prevention_is_not_ignored():
    prevention = dict(
        instance_id=70,
        controller_seat_id=2,
        type_line="Enchantment",
        oracle_text="Spells and abilities your opponents control can't cause you to sacrifice permanents.",
    )
    with pytest.raises(UnsupportedContinuation, match="prevention"):
        tree([card(2, 2)], [card(1, 1)], [pact(), prevention])


def test_pending_public_trigger_is_not_replayed_as_resolved_combat():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    _, state = model_for([card(1, 1)], [card(2, 2)], [pact()])
    state["stack"] = [
        dict(instance_id=77, name="Public Pact", oracle_text="Each opponent sacrifices a creature.")
    ]
    result = search_lines(build_board_model(state), survival=False, lethal_now=False)
    assert result.unsupported and "stack" in result.unsupported[0]


def test_assessment_cache_refreshes_when_printed_capture_stats_arrive():
    from arenamcp.board_assessment import assess

    _, state = model_for([card(1, 1), card(10, 1, 4, 5, TERGRID)], [card(2, 2)], [pact()])
    missing = deepcopy(state)
    missing["battlefield"][0].pop("printed_power")
    first, second = assess(missing), assess(state)
    assert first.line_search.unsupported and not second.line_search.unsupported


def test_disabled_search_does_not_republish_legacy_sacrifice_forecast(monkeypatch):
    from arenamcp.board_assessment import assess

    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    _, state = model_for([card(1, 1, 50, 50)], [card(2, 2)], [pact()])
    assessment = assess(state)
    assert assessment.line_search is None and not assessment.lethal_now
    assert not assessment.lookahead and assessment.our_clock is None
    assert any("sacrifice continuation" in s for s in assessment.unknowns)


def test_unrelated_death_and_sacrifice_abilities_do_not_activate_this_model():
    from arenamcp.sacrifice_continuation import relevant

    source = card(8, 2, text="Whenever a creature dies, draw a card.\nSacrifice a creature: Add {B}.")
    assert not relevant([source]) and not unsupported([source])


def test_two_queued_transfers_cannot_take_the_same_graveyard_object_twice():
    # Two nonlegendary token copies with this public ability are legal.
    thieves = [card(i, 1, 4, 5, TERGRID, is_token=True) for i in (10, 11)]
    outcomes = tree([*thieves, card(2, 2)], [card(1, 1)], [pact()]).outcomes()
    assert any(o.transfers for o in outcomes)
    assert all(len(o.transfers) <= 1 for o in outcomes)


def test_public_choice_policy_cannot_become_an_automatic_concede_fact():
    from arenamcp.board_assessment import assess

    _, state = model_for([card(1, 1), card(10, 1, 4, 5, TERGRID)], [card(2, 2, 3, 3)], [pact()])
    assessment = assess(state)
    assert assessment.line_search.choice_caveats
    assert assessment.dead_in is None and not assessment.opp_lethal_on_board and not assessment.all_in


def test_other_controller_is_explicitly_unsupported():
    with pytest.raises(UnsupportedContinuation, match="two known controllers"):
        tree([card(2, 3)], [card(1, 1)], [pact()])


def test_named_public_equivalent_fixture_does_not_keep_the_sacrificed_giant():
    import json
    from pathlib import Path

    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves

    state = json.loads((Path(__file__).parent / "fixtures/sacrifice_280_equivalent.json").read_text())
    assert "Not a captured game" in state["fixture_note"]
    before = deepcopy(state)
    moves = Moves(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    node = moves.finish(moves.begin(moves.root_node(), None, ()), ("all", (1,)))
    assert not node.theirs
    assert {c["name"] for c in node.ours} == {"Tergrid, God of Fright", "Hill Giant"}
    assert state == before


@pytest.mark.parametrize("changes", [{"is_commander": True}, {"face_down": True}])
def test_unknown_zone_or_face_choices_are_explicitly_unsupported(changes):
    with pytest.raises(UnsupportedContinuation):
        tree([card(2, 2, **changes)], [card(1, 1)], [pact()])


def test_delayed_sacrifice_is_not_silently_skipped_between_turns():
    with pytest.raises(UnsupportedContinuation, match="outside"):
        tree(
            [card(2, 2, text="At the beginning of the end step, sacrifice this creature.")],
            [card(1, 1)],
            [pact()],
        )


def test_face_down_source_does_not_reveal_or_trigger_its_printed_ability():
    source = card(10, 1, text=PACT, face_down=True)
    outcomes = tree([source, card(2, 2)], [card(1, 1)]).outcomes()
    assert not outcomes[0].sacrificed
