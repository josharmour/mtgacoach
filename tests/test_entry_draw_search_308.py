"""#308 search acceptance: same-state entry-draw order comparison.

The engine and punisher use the real Enduring Innocence / Orcish Bowmasters
text. "Test Recruit" and "Test Maker" are fictional simplified cards used only
to exercise the transition; this reconstructs mechanics and is not a historical
replay.
"""

from tests.test_entry_draw_transition_308 import bowmasters, innocence, recruit
from tests.test_tactical_timing_backlog import card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import evaluate_plan, search_lines


def answer() -> dict:
    return card(72, "Answer", "Exile target creature.", "Instant", "{W}")


def maker() -> dict:
    return card(
        74,
        "Test Maker",
        "When this creature enters, create a 1/1 white Soldier creature token.",
        "Creature — Human",
        "{W}",
        power=1,
        toughness=1,
    )


def state(*, hand=None, extra=(), punisher=True) -> dict:
    battlefield = [card(i, "Plains", "{T}: Add {W}.", "Basic Land — Plains") for i in range(1, 7)]
    if punisher:
        battlefield.append(bowmasters())
    battlefield.extend(extra)
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 12, "active_player": 1, "phase": "Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 8, "lands_played": 1},
            {"seat_id": 2, "life_total": 20},
        ],
        "battlefield": battlefield,
        "hand": [innocence(), recruit(power=2, toughness=2), answer()] if hand is None else hand,
        "stack": [],
    }


def search(**kwargs):
    return search_lines(build_board_model(state(**kwargs)), survival=False, lethal_now=False, hard_ms=1e9)


def plan(result, casts):
    return evaluate_plan(result, [{"label": "T", "cast": casts, "attack": "none"}])


def test_same_state_search_ranks_removal_before_entry_above_entry_first():
    result = search()
    assert result.best.steps[0].casts == ("Answer", "Enduring Innocence", "Test Recruit")
    assert result.best.steps[0].draws == 1
    assert result.best._leaf.draws == 1

    removal_first = plan(
        result,
        ["Answer (on Orcish Bowmasters)", "Enduring Innocence", "Test Recruit"],
    )
    entry_first = plan(
        result,
        ["Enduring Innocence", "Test Recruit", "Answer (on Orcish Bowmasters)"],
    )
    entrant_first = plan(
        result,
        ["Test Recruit", "Enduring Innocence", "Answer (on Orcish Bowmasters)"],
    )
    assert removal_first.line is not None
    assert entry_first.line is not None
    assert entrant_first.line is not None

    # The same initial public state ranks differently by legal cast order alone.
    assert removal_first.line.v > entry_first.line.v
    assert entrant_first.line.v > entry_first.line.v
    assert removal_first.line._leaf.draws == 1
    assert entrant_first.line._leaf.draws == 0

    # The beneficial entrant-first line survives the search's own ranking.
    assert any(
        line.steps and line.steps[0].casts == ("Test Recruit", "Enduring Innocence") for line in result.lines
    )


def test_entry_first_preserves_ping_and_amass_after_late_punisher_removal():
    line = plan(
        search(),
        ["Enduring Innocence", "Test Recruit", "Answer (on Orcish Bowmasters)"],
    ).line
    assert line is not None
    leaf = line._leaf

    # The draw happened while the punisher was still present; the later Answer
    # removes it without erasing the already-triggered ping/amass continuation.
    assert leaf.draws == 1
    assert not any(body["name"] == "Orcish Bowmasters" for body in leaf.theirs)
    army = next(body for body in leaf.theirs if body["name"] == "Orc Army")
    assert army["power"] == army["toughness"] == 1
    assert army["counters"] == {"+1/+1": 1}

    # The ping killed the engine; its death trigger returned it as an enchantment.
    assert not any(body["name"] == "Enduring Innocence" for body in leaf.ours)
    returned = next(body for body in leaf.persistent if body["name"] == "Enduring Innocence")
    assert returned["type_line"] == "Enchantment"


def test_removal_before_entry_avoids_ping_and_amass():
    line = plan(
        search(),
        ["Answer (on Orcish Bowmasters)", "Enduring Innocence", "Test Recruit"],
    ).line
    assert line is not None
    leaf = line._leaf

    assert leaf.draws == 1
    assert not any(body["name"] == "Orcish Bowmasters" for body in leaf.theirs)
    assert not any(body["name"] == "Orc Army" for body in leaf.theirs)
    assert {body["name"] for body in leaf.ours} == {"Enduring Innocence", "Test Recruit"}
    assert leaf.persistent == ()


def test_entrant_before_engine_is_legal_and_draws_nothing():
    line = plan(
        search(),
        ["Test Recruit", "Enduring Innocence", "Answer (on Orcish Bowmasters)"],
    ).line
    assert line is not None
    leaf = line._leaf

    # Engine-before-entry is a trigger condition, not cast legality: this order
    # intentionally avoids the draw/ping chain.
    assert leaf.draws == 0
    assert not any(body["name"] == "Orcish Bowmasters" for body in leaf.theirs)
    assert not any(body["name"] == "Orc Army" for body in leaf.theirs)
    assert {body["name"] for body in leaf.ours} == {"Enduring Innocence", "Test Recruit"}
    assert leaf.persistent == ()


def test_no_punisher_still_draws_without_ping_or_amass():
    result = search(punisher=False, hand=[innocence(), recruit(power=2, toughness=2)])
    line = plan(result, ["Enduring Innocence", "Test Recruit"]).line
    assert line is not None
    leaf = line._leaf

    assert leaf.draws == 1
    assert not any(body["name"] == "Orcish Bowmasters" for body in leaf.theirs)
    assert not any(body["name"] == "Orc Army" for body in leaf.theirs)
    assert {body["name"] for body in leaf.ours} == {"Enduring Innocence", "Test Recruit"}


def test_later_turn_entry_draw_is_explicitly_unmodelled():
    result = search()
    later = evaluate_plan(
        result,
        [
            {"label": "T", "cast": [], "attack": "none"},
            {"label": "T+1", "cast": ["Enduring Innocence", "Test Recruit"], "attack": "none"},
        ],
    )
    assert later.line is None
    assert any("outside the current turn" in item for item in later.unmodelled)

    # A later-turn omission must not become a complete or forced-survival claim.
    assert result.best.complete is False
    assert result.exact_first_attack is False
    assert result.only_survivor is False
    assert any("outside the current turn" in caveat for caveat in result.choice_caveats)


def test_multiple_qualifying_entrants_abstain():
    result = search(hand=[innocence(), recruit(iid=71), recruit(iid=73)])
    assert not result.unsupported
    assert any("multiple qualifying entrants" in caveat for caveat in result.choice_caveats)

    replay = evaluate_plan(
        result,
        [{"label": "T", "cast": ["Enduring Innocence", "Test Recruit", "Test Recruit"], "attack": "none"}],
    )
    assert replay.line is None
    assert any("multiple qualifying entrants" in item for item in replay.unmodelled)


def test_token_producing_entry_in_a_chain_abstains():
    result = search(hand=[innocence(), maker()])
    assert not result.unsupported
    assert any("token-producing entry" in caveat for caveat in result.choice_caveats)

    replay = evaluate_plan(
        result,
        [{"label": "T", "cast": ["Enduring Innocence", "Test Maker"], "attack": "none"}],
    )
    assert replay.line is None
    assert any("token-producing entry" in item for item in replay.unmodelled)


def test_deadline_result_is_bounded_and_not_a_complete_survival_claim():
    result = search_lines(
        build_board_model(state()),
        survival=False,
        lethal_now=False,
        hard_ms=0.001,
        budget=1,
    )
    assert result.bounded is True
    assert result.truncated is True
    assert result.best.complete is False
    assert result.exact_first_attack is False
    assert result.only_survivor is False


def test_adversarial_collapse_is_reported_not_hidden():
    result = search()
    assert result.bounded is True
    assert result.truncated is True
    assert result.exact_first_attack is False
    assert result.only_survivor is False
    assert any("collapsed adversarially" in caveat for caveat in result.choice_caveats)
