"""#240/#287 shorthand leak; equivalent fixtures, historical full packets unavailable."""

import pytest
from tests.test_plan_availability_feedback import state

from arenamcp.board_assessment import assess
from arenamcp.game_plan import GamePlan, validate_plan


def plan_with(text):
    return GamePlan(
        win_conditions=["Akroma's Will dual-mode alpha if drawn"],
        path=text,
        develop_next=text,
        resource_priorities=[text],
        active_mechanisms=[text],
        turn_plan=[{"turn": "T", "hold": text}],
    )


@pytest.mark.parametrize(
    "text",
    ["Keep Leonardo alive for Will", "Will alpha finishes", "Preserve Leonardo as Will enabler", "Cast Will"],
)
def test_library_dependency_shorthand_is_grounded_in_one_known_card(text):
    game = state()
    plan = plan_with(text)
    validate_plan(plan, assess(game), game)
    assert plan.path == ""
    assert plan.develop_next == ""
    assert plan.resource_priorities == []
    assert plan.active_mechanisms == []
    assert "Will" not in plan.turn_plan[0]["hold"]
    assert any("akroma s will" in issue for issue in plan.issues)


@pytest.mark.parametrize(
    "text",
    [
        "Will attack with Leonardo next turn",
        "Will plan to attack with Leonardo next turn",
        "Leonardo will enable combat damage",
        "Do not hold Leonardo for Will",
        "Will unavailable",
        "If drawn: Will alpha finishes",
        "Tutor for Will",
    ],
)
def test_ordinary_prose_conditional_lines_and_negative_advice_survive(text):
    game = state()
    plan = plan_with(text)
    validate_plan(plan, assess(game), game)
    assert plan.path == text


@pytest.mark.parametrize("zone", ["hand", "battlefield", "stack", "graveyard", "exile", "command"])
def test_visible_card_does_not_become_library_only_through_alias(zone):
    game = state()
    game[zone] = [dict(game["deck_catalog"][1], owner_seat_id=1)]
    plan = plan_with("Keep Leonardo alive for Will")
    validate_plan(plan, assess(game), game)
    assert plan.path == "Keep Leonardo alive for Will"


@pytest.mark.parametrize("location", ["deck", "hand", "actions"])
def test_ambiguous_will_does_not_resolve_to_akromas_will(location):
    game = state()
    other = {"name": "Will, Scion of Peace", "type_line": "Legendary Creature", "owner_seat_id": 1}
    if location == "deck":
        game["deck_catalog"][2] = other
    elif location == "hand":
        game["hand"] = [other]
    else:
        game["_bridge_actions"] = [{"actionType": "Cast", "name": other["name"], "instanceId": 80}]
    plan = plan_with("Cast Will")
    validate_plan(plan, assess(game), game)
    assert plan.path == "Cast Will"


def test_unknown_alias_is_not_guessed_from_historical_knowledge():
    game = state()
    game["deck_catalog"] = {}
    plan = plan_with("Keep Leonardo alive for Will")
    validate_plan(plan, assess(game), game)
    assert plan.path == "Keep Leonardo alive for Will"


def test_alias_in_win_condition_is_rejected_with_full_identity_in_feedback():
    game = state()
    plan = GamePlan(win_conditions=["Will alpha finishes"])
    validate_plan(plan, assess(game), game)
    assert "Will alpha finishes" not in plan.win_conditions
    assert any("needs akroma s will" in issue for issue in plan.issues)


def test_first_name_will_is_not_mistaken_for_future_tense():
    game = state()
    game["deck_catalog"] = {2: {"name": "Will, Scion of Peace", "type_line": "Creature"}}
    plan = plan_with("Will attack with Leonardo next turn")
    validate_plan(plan, assess(game), game)
    assert plan.path == "Will attack with Leonardo next turn"
    plan = plan_with("Cast Will")
    validate_plan(plan, assess(game), game)
    assert plan.path == ""
    assert any("will scion of peace" in issue for issue in plan.issues)


def test_shared_word_does_not_hide_an_explicit_full_name_dependency():
    game = state()
    game["deck_catalog"][2] = {"name": "Will, Scion of Peace", "type_line": "Creature"}
    plan = plan_with("Preserve Leonardo for Akroma's Will")
    validate_plan(plan, assess(game), game)
    assert plan.path == ""


@pytest.mark.parametrize("text", ["Fblthp unblockable chip damage", "Fblthp combat damage", "Fblthp beats"])
def test_unique_named_win_subject_requires_the_library_card(text):
    game = state()
    game["deck_catalog"] = {3: {"name": "Fblthp, Impossibly Lost", "type_line": "Legendary Creature"}}
    plan = GamePlan(win_conditions=[text, "Fblthp beats if drawn"])
    validate_plan(plan, assess(game), game)
    assert plan.win_conditions == ["Fblthp beats if drawn"]
    assert any("fblthp impossibly lost" in issue for issue in plan.issues)


@pytest.mark.parametrize("kind", ["ambiguous", "visible", "ordinary_verb", "not_win_context"])
def test_named_win_subject_does_not_guess_other_contexts(kind):
    from arenamcp.plan_dependencies import library_dependencies

    game = state()
    game["deck_catalog"] = {3: {"name": "Fblthp, Impossibly Lost", "type_line": "Creature"}}
    text = "Fblthp unblockable chip damage"
    library = {"fblthp impossibly lost"}
    if kind == "ambiguous":
        game["deck_catalog"][4] = {"name": "Fblthp, the Lost", "type_line": "Creature"}
    elif kind == "visible":
        game["hand"] = [game["deck_catalog"][3]]
        library = set()
    elif kind == "ordinary_verb":
        game["deck_catalog"] = {3: {"name": "Make Mischief", "type_line": "Sorcery"}}
        text, library = "Make unblockable attackers", {"make mischief"}
    assert not library_dependencies(text, game, library, win_condition=kind != "not_win_context")
