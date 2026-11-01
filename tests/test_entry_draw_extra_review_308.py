"""Independent behavior checks for the staged #308 search integration."""

import copy

import pytest
from tests.test_entry_draw_transition_308 import bowmasters, innocence, recruit
from tests.test_tactical_timing_backlog import card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import _Search, evaluate_plan, search_lines
from arenamcp.line_search_moves import Deadline


def state():
    return dict(
        local_seat_id=1,
        turn=dict(turn_number=12, active_player=1, phase="Main1"),
        players=[
            dict(seat_id=1, is_local=True, life_total=8, lands_played=1),
            dict(seat_id=2, life_total=20),
        ],
        battlefield=[card(i, "Plains", "{T}: Add {W}.", "Basic Land — Plains") for i in range(1, 7)]
        + [bowmasters()],
        hand=[
            innocence(),
            recruit(power=2, toughness=2),
            card(72, "Answer", "Exile target creature.", "Instant", "{W}"),
        ],
        stack=[],
    )


def moves():
    m = _Search(
        build_board_model(state()),
        survival=False,
        lethal_now=False,
        hard_ms=1e9,
        groups=16,
        width=2,
        k_next=4,
        opponent=None,
    )
    m.free = True
    return m


def chosen(m, names):
    root = m.root_node()
    _, sources = m.sources(root, None)
    choices = m.cast_sets(root, sources, instant_only=False)
    casts = next(c for c in choices if tuple(m.spells[v.spell].name for v, _ in c) == names)
    return root, casts


def test_same_state_preserves_legal_no_trigger_order_and_late_removal():
    m = moves()
    root, casts = chosen(m, ("Test Recruit", "Enduring Innocence", "Answer"))
    no_trigger = m.begin(root, None, casts)
    assert not no_trigger.responses and no_trigger.draws == 0
    assert no_trigger.our_life == 8
    assert not no_trigger.theirs
    root, casts = chosen(m, ("Enduring Innocence", "Test Recruit", "Answer"))
    triggered = m.begin(root, None, casts)
    assert len(triggered.responses) >= 2
    for branch in triggered.responses:
        assert branch.draws == 1
        assert all(b["instance_id"] != 320 for b in branch.theirs)
        assert any("Army" in b["name"] for b in branch.theirs)


def test_full_search_prefers_removal_before_entry_without_claiming_exhaustive_forecast():
    s = state()
    before = copy.deepcopy(s)
    r = search_lines(build_board_model(s), survival=False, lethal_now=False, hard_ms=1e9)
    assert not r.unsupported
    assert r.best.steps[0].casts == ("Answer", "Enduring Innocence", "Test Recruit")
    assert r.best.steps[0].draws == 1
    late = evaluate_plan(
        r,
        [
            dict(
                label="T",
                cast=["Enduring Innocence", "Test Recruit", "Answer (on Orcish Bowmasters)"],
                attack="none",
            )
        ],
    )
    assert late.line is not None and r.best.v > late.line.v
    assert not r.best.complete and not r.exact_first_attack and not r.only_survivor
    assert s == before


def test_engine_created_on_prior_turn_does_not_silently_omit_later_trigger():
    r = search_lines(build_board_model(state()), survival=False, lethal_now=False, hard_ms=1e9)
    later = evaluate_plan(
        r,
        [
            dict(label="T", cast=["Enduring Innocence"], attack="none"),
            dict(label="T+1", cast=["Test Recruit"], attack="none"),
        ],
    )
    assert later.unmodelled or later.issues or later.line is None, (
        "Later entry with a surviving engine was silently treated as no trigger"
    )
    assert later.line is None or not later.line.complete, (
        "Unsupported later entry was still labelled complete"
    )


def test_deadline_cannot_cache_an_optimistic_partial_response():
    m = moves()
    root, casts = chosen(m, ("Enduring Innocence", "Test Recruit", "Answer"))
    mid = m.begin(root, None, casts)
    assert mid.responses
    m.free = False
    m.hard = 0
    m.started = 0
    with pytest.raises(Deadline):
        m.finish(mid, ("none", ()))
    assert mid.response_cache == {}


def test_two_draw_engines_are_also_qualifying_entries_and_must_abstain():
    s = state()
    s["hand"] = [innocence(70), innocence(73)]
    r = search_lines(build_board_model(s), survival=False, lethal_now=False, hard_ms=1e9)
    replay = evaluate_plan(
        r, [dict(label="T", cast=["Enduring Innocence", "Enduring Innocence"], attack="none")]
    )
    assert replay.line is None and any(
        "multiple" in str(x).lower() for x in (*r.unsupported, *replay.unmodelled, *replay.issues)
    ), "Second engine entry incorrectly treated as harmless"


def test_returned_enchantment_engine_also_requires_unknown_availability_abstention():
    from dataclasses import replace

    from arenamcp.persistent_returns import returned_enchantment
    from arenamcp.sacrifice_continuation import UnsupportedContinuation

    m = moves()
    root, casts = chosen(m, ("Test Recruit",))
    returned = returned_enchantment(innocence())
    assert returned is not None
    root = replace(root, persistent=(returned,), k=1, abs_turn=root.abs_turn + 2)
    m.replay = True
    with pytest.raises(UnsupportedContinuation):
        m.begin(root, None, casts)
