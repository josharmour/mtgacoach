"""#322: explicit free exile casts participate in search at their current cost."""

from copy import deepcopy

from tests.test_tactical_timing_backlog import board, card, cast, menu

from arenamcp.board_assessment import _signature
from arenamcp.board_model import build_board_model
from arenamcp.line_guard import unmodelled_cast
from arenamcp.line_search import action_key, evaluate_plan, search_lines
from arenamcp.offered_casts import bind_offered_casts, free_exile_casts
from arenamcp.play_safety import _effective_cost


def offered_board(life=6):
    state = board(0)
    state["players"][1]["life_total"] = life
    state["battlefield"] = [card(1, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain")]
    state["hand"] = [card(30, "Boltwave", "Boltwave deals 3 damage to each opponent.", "Sorcery", "{R}")]
    state["exile"] = [
        card(
            40,
            "Slickshot Show-Off",
            "Flying, haste\nWhenever you cast a noncreature spell, Slickshot Show-Off gets +2/+0 until end of turn.\nPlot {1}{R}",
            "Creature — Bird Wizard",
            "{1}{R}",
            power=1,
            toughness=2,
        )
    ]
    decision = menu(
        cast(
            40, None, alternativeGrpId=149, alternativeCostText="Cast without paying mana cost", manaCost=None
        ),
        cast(30, manaCost=[{"color": ["Red"], "count": 1}]),
    )
    return state, decision


def test_explicit_free_exile_cast_enables_creature_then_burn_lethal():
    state, decision = offered_board()
    original = deepcopy(state)
    bound, state = bind_offered_casts(decision, state)
    model = build_board_model(state)
    free = next(s for s in model.spells if s.name == "Slickshot Show-Off")
    assert free.zone == "exile" and free.mana_value == 0 and free.pips == ()
    assert free.card["mana_cost"] == "{1}{R}"  # printed mana value is still two
    assert bound.find("idx:40").payable is True and decision.find("idx:40").payable is None
    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9)
    assert result.best.steps[0].casts == ("Slickshot Show-Off", "Boltwave")
    assert result.best.win_at == 1
    assert action_key(bound.find("idx:40"), state) in result.best.first_actions
    assert not unmodelled_cast(result, bound.find("idx:40"), state)
    assert original["exile"] == state["exile"]
    assert _effective_cost(original["exile"][0], decision.find("idx:40").meta) == "{0}"


def test_current_offer_does_not_grant_future_free_casts():
    state, decision = offered_board(20)
    _, state = bind_offered_casts(decision, state)
    result = search_lines(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e9)
    evaluation = evaluate_plan(
        result,
        [{"label": "T", "cast": [], "attack": "none"}, {"label": "T+1", "cast": ["Slickshot Show-Off"]}],
    )
    assert any("Slickshot Show-Off is not in hand" in issue for issue in evaluation.issues)
    assert all("Slickshot Show-Off" not in step.casts for step in evaluation.line.steps)


def test_signature_and_bind_discard_stale_menu_permission():
    state, decision = offered_board()
    _, bound = bind_offered_casts(decision, state)
    assert _signature(state) != _signature(bound)
    _, gone = bind_offered_casts(menu(cast(30)), bound)
    assert not free_exile_casts(gone)
    assert _signature(gone) == _signature(state)
    # No offer: an exiled card is never inferred castable from its name or plot keyword.
    assert all(s.zone != "exile" for s in build_board_model(gone).spells)


def test_positive_remaining_mana_cost_and_colors_are_preserved():
    state, decision = offered_board()
    option = cast(40, None, **{**decision.find("idx:40").meta, "manaCost": [{"color": ["Blue"], "count": 1}]})
    _, bound = bind_offered_casts(menu(option, cast(30)), state)
    free = next(s for s in build_board_model(bound).spells if s.zone == "exile")
    assert free.mana_value == 1 and free.pips == (frozenset({"U"}),)
    result = search_lines(build_board_model(bound), survival=False, lethal_now=False, hard_ms=1e9)
    assert "Slickshot Show-Off" not in result.best.steps[0].casts


def test_uncertain_additional_cost_wrong_owner_and_other_windows_stay_unmodelled():
    state, decision = offered_board()
    state["exile"][0]["oracle_text"] += "\nAs an additional cost to cast this spell, sacrifice a creature."
    bound, changed = bind_offered_casts(decision, state)
    assert not free_exile_casts(changed) and bound.find("idx:40").payable is None
    state, decision = offered_board()
    for change in ("opponent", "stack", "owner"):
        bad = deepcopy(state)
        if change == "opponent":
            bad["turn"]["active_player"] = 2
        if change == "stack":
            bad["stack"] = [card(99, "Pending", "", "Creature", seat=2)]
        if change == "owner":
            bad["exile"][0]["owner_seat_id"] = 2
        _, bad = bind_offered_casts(decision, bad)
        assert not free_exile_casts(bad)


def test_typed_planner_uses_offered_free_cast_in_search(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    from arenamcp.action_planner import ActionPlanner

    state, decision = offered_board()
    backend = _CapturingBackend(
        '{"option_ids": ["idx:40"], "reasoning": "Free creature before burn enables lethal."}'
    )
    planner = ActionPlanner(backend=backend)
    chosen = planner.plan_decision_options(decision, state)
    assert chosen == ["idx:40"]
    if backend.prompts:
        assert "Slickshot Show-Off" in backend.prompts[-1]
        assert "offered free alternative cost/zone not modelled" not in backend.prompts[-1]
