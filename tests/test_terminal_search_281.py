from test_combat_loss_281 import board, creature


def search(snapshot):
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    return search_lines(build_board_model(snapshot), survival=False, lethal_now=False, hard_ms=1e9)


def test_search_toxic_win_and_incoming_poison_loss_at_positive_life():
    from arenamcp.line_search import DEAD, WIN

    a = creature(1, text="Toxic 1")
    win = search(board([a], opp_poison=9))
    assert win.best.cls == WIN and win.best.win_turn == 6
    assert win.best._leaf.opp_life == 29 and win.best._leaf.opponent_loss.poison == 10
    loss = search(board(theirs=[a], our_poison=9, active=2))
    assert loss.best.cls == DEAD and loss.best.dead_turn == 6
    assert loss.best._leaf.our_life == 29


def test_search_carries_poison_between_combats():
    from arenamcp.line_search import WIN

    win = search(board([creature(1, text="Toxic 1")], opp_poison=8))
    assert win.best.cls == WIN and win.best.win_turn == 8
    assert win.best._leaf.opponent_loss.poison == 10


def test_search_mill_win_waits_for_their_draw_and_mill_loss_for_ours():
    from arenamcp.line_search import DEAD, WIN

    a = creature(
        1, text="Whenever this creature deals combat damage to a player, that player mills three cards."
    )
    win = search(board([a], opp_library=3))
    assert win.best.cls == WIN and win.best.win_turn == 7
    loss = search(board(theirs=[a], library=3, active=2))
    assert loss.best.cls == DEAD and loss.best.dead_turn == 7
    assert loss.best._leaf.our_life > 0


def test_unknown_library_is_not_empty_and_last_card_draw_succeeds():
    from arenamcp.combat_loss import loss_state
    from arenamcp.line_search import ALIVE, DEAD

    legacy = board()
    legacy["zones"] = {"library_count": 0}
    assert loss_state(legacy).library is None
    assert search(legacy).best.cls == ALIVE
    one_card = search(board(library=1, active=2))
    assert one_card.best.cls == DEAD and one_card.best.dead_turn == 9


def test_search_names_alternate_win_and_loss_instead_of_lethal_damage():
    assert "win by poison" in search(board([creature(1, text="Toxic 1")], opp_poison=9)).best.outcome_text()
    assert (
        "dead to poison"
        in search(board(theirs=[creature(1, text="Toxic 1")], our_poison=9, active=2)).best.outcome_text()
    )


def test_assessment_names_failed_draw_without_recommending_lifegain():
    from arenamcp.board_assessment import assess
    a = creature(1, text="Whenever this creature deals combat damage to a player, that player mills three cards.")
    assessment = assess(board(theirs=[a], library=3, active=2))
    assert any("LOSS BY FAILED DRAW ON T7" in flag for flag in assessment.flags)
    assert not any("DEAD NEXT ATTACK" in flag for flag in assessment.flags)
    assert "stop the poison/mill source" in assessment.role_reason


def test_upkeep_failed_draw_precedes_a_potential_poison_attack():
    from arenamcp.line_search import DEAD, WIN
    snapshot = board([creature(1, text="Toxic 10")], library=0)
    snapshot["turn"].update(phase="Phase_Beginning", step="Step_Upkeep")
    result = search(snapshot)
    assert result.best.cls == DEAD and result.best.dead_turn == 6
    snapshot = board(theirs=[creature(1, text="Toxic 10")], opp_library=0, active=2)
    snapshot["turn"].update(phase="Phase_Beginning", step="Step_Upkeep")
    result = search(snapshot)
    assert result.best.cls == WIN and result.best.win_turn == 6
