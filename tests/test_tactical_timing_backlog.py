"""Timing/resource regressions from #322, #328 and #342 (no live client)."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from arenamcp.board_model import build_board_model
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search import evaluate_plan, search_lines
from arenamcp.tactical_timing import timing_notes


def card(iid, name, text, types, cost="", seat=1, **kw):
    return dict(
        instance_id=iid,
        name=name,
        oracle_text=text,
        type_line=types,
        mana_cost=cost,
        controller_seat_id=seat,
        owner_seat_id=seat,
        **kw,
    )


def board(mana=4, ward=3):
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 8, "active_player": 1, "phase": "Main1"},
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "lands_played": 1},
            {"seat_id": 2, "life_total": 20},
        ],
        "battlefield": [card(i, "Plains", "{T}: Add {W}.", "Basic Land — Plains") for i in range(1, mana + 1)]
        + [card(90, "Warded engine", f"Ward {{{ward}}}", "Creature", seat=2, power=5, toughness=5)],
        "hand": [
            card(20, "Development", "", "Creature", "{1}{W}", power=2, toughness=2),
            card(21, "Development", "", "Creature", "{1}{W}", power=2, toughness=2),
            card(30, "Answer", "Exile target creature.", "Instant", "{W}"),
        ],
        "stack": [],
    }


def replay(state, casts):
    result = search_lines(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e9)
    return evaluate_plan(result, [{"label": "T", "cast": casts, "attack": "none"}]), result


@pytest.mark.parametrize(("mana", "ward", "developments"), [(4, 3, 1), (5, 1, 2)])
def test_development_cannot_also_promise_unaffordable_removal_plus_ward(mana, ward, developments):
    evaluation, _ = replay(board(mana, ward), ["Development"] * developments + ["Answer (on Warded engine)"])
    assert any("Answer is unpayable" in issue and f"{ward} ward mana" in issue for issue in evaluation.issues)
    assert "Answer" not in evaluation.line.steps[0].casts


@pytest.mark.parametrize(("mana", "ward", "developments"), [(4, 3, 0), (6, 3, 1), (4, 1, 1)])
def test_affordable_removal_and_development_are_preserved(mana, ward, developments):
    evaluation, _ = replay(board(mana, ward), ["Development"] * developments + ["Answer (on Warded engine)"])
    assert not evaluation.issues
    assert "Answer" in evaluation.line.steps[0].casts


def test_earlier_removal_tax_reduces_later_development_budget():
    evaluation, _ = replay(board(), ["Answer (on Warded engine)", "Development"])
    assert any("Development is unpayable" in issue and "3 ward mana" in issue for issue in evaluation.issues)
    assert evaluation.line.steps[0].casts == ("Answer",)


def test_each_spell_in_combined_burn_pays_ward():
    s = board(3, 1)
    s["hand"] = [
        card(i, f"Burn {i}", f"Burn {i} deals 3 damage to target creature.", "Instant", "{W}")
        for i in (31, 32)
    ]
    evaluation, _ = replay(s, ["Burn 31 (on Warded engine)", "Burn 32 (on Warded engine)"])
    assert any("Burn 32 is unpayable" in issue and "2 ward mana" in issue for issue in evaluation.issues)


def test_plan_reports_replay_payment_error_even_with_unmodelled_effect(monkeypatch):
    from arenamcp import game_plan

    evaluation, result = replay(board(), ["Development", "Answer (on Warded engine)"])
    monkeypatch.setattr(game_plan, "_evaluate_plan_line", lambda *a, **kw: evaluation)
    monkeypatch.setattr(game_plan, "_unchecked_plan", lambda *a: "unpriced attack trigger")
    issues = []
    game_plan._check_plan_line(
        SimpleNamespace(turn_plan=[{}]), SimpleNamespace(line_search=result), board(), issues
    )
    assert any("including 3 ward mana" in issue for issue in issues)
    assert any("unpriced attack trigger" in issue for issue in issues)


def menu(*options):
    return PendingDecision(
        (1, 2), "ActionsAvailable", (*options, DecisionOption("pass", "Pass")), can_pass=True
    )


def cast(iid, payable=True, **meta):
    return DecisionOption(
        f"idx:{iid}", "Cast", payable, {"actionType": "ActionType_Cast", "instanceId": iid, **meta}
    )


def test_explicit_free_alternative_does_not_require_printed_cost_confirmation():
    s = board(0)
    s["exile"] = [
        card(
            40,
            "Free trigger creature",
            "Haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
            "Creature",
            "{1}{R}",
            power=1,
            toughness=2,
        )
    ]
    option = cast(
        40, None, alternativeGrpId=149, alternativeCostText="Cast without paying mana cost", manaCost=None
    )
    note = " ".join(timing_notes(menu(option), s))
    assert "Missing auto-tap confirmation is not evidence" in note
    assert "casting and resolving it before" in note
    assert "additional costs" in note and "urgent interaction or lethal" in note
    # We did not invent a complete payment solution or mutate live options.
    assert option.payable is None
    taxed = cast(40, None, **{**option.meta, "manaCost": [{"color": ["Generic"], "count": 2}]})
    assert "remaining mana cost is" in " ".join(timing_notes(menu(taxed), s))
    unknown = cast(40, None, alternativeGrpId=150, alternativeCostText="Sacrifice a creature")
    assert not timing_notes(menu(unknown), s)


def craft_board():
    s = board()
    s["hand"] = [
        card(
            30,
            "Seam Rip",
            "When this enchantment enters, exile target nonland permanent an opponent controls with mana value 2 or less until this enchantment leaves the battlefield.",
            "Enchantment",
            "{W}",
        )
    ]
    s["battlefield"].append(
        card(
            91,
            "Throne of the Grim Captain",
            "Craft with a Dinosaur, a Merfolk, a Pirate, and a Vampire {4} ({4}, Exile this artifact, Exile the four from among permanents you control and/or cards in your graveyard: Return this card transformed under its owner's control. Craft only as a sorcery.)",
            "Legendary Artifact",
            "{2}",
            seat=2,
            related_faces=[
                {"name": "The Grim Captain", "oracle_text": "Menace, trample, lifelink, hexproof"}
            ],
        )
    )
    return s


def test_sorcery_removal_names_the_pre_craft_window_without_forcing_cast():
    s = craft_board()
    original = deepcopy(s)
    note = " ".join(timing_notes(menu(cast(30)), s))
    assert "Seam Rip is sorcery-speed" in note
    assert "Throne of the Grim Captain now, if legal" in note
    assert "exiles its source as a cost" in note
    assert "The Grim Captain" in note and "hexproof" in note
    assert "Holding removal is valid" in note
    assert s == original


def test_instant_removal_and_other_windows_do_not_get_sorcery_claim():
    s = craft_board()
    s["hand"][0]["type_line"] = "Instant"
    assert not timing_notes(menu(cast(30)), s)
    s["hand"][0]["type_line"] = "Enchantment"
    s["stack"] = [card(50, "Response", "", "Instant", seat=2)]
    assert not timing_notes(menu(cast(30)), s)
    s["stack"] = []
    s["turn"]["active_player"] = 2
    assert not timing_notes(menu(cast(30)), s)


def test_missing_transform_text_does_not_invent_craft_or_a_back_face():
    s = craft_board()
    s["battlefield"][-1]["oracle_text"] = ""
    assert not timing_notes(menu(cast(30)), s)


def test_typed_prompt_receives_timing_facts(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    from arenamcp.action_planner import ActionPlanner

    backend = _CapturingBackend('{"option_ids": ["pass"], "reasoning": "A different line is better."}')
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", set()))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    planner._llm_decision_options(menu(cast(30)), craft_board())
    assert "REMOVAL WINDOW:" in backend.prompts[0]
    assert "RETURNED FACE: The Grim Captain" in backend.prompts[0]


def test_free_exiled_creature_is_not_ranked_as_a_worse_printed_cost_cast():
    from arenamcp.action_planner import ActionPlanner, _unmodelled_options
    from arenamcp.line_guard import unmodelled_cast

    s = board()
    s["exile"] = [
        card(
            40, "Trigger creature", "Haste\nProwess\nPlot {1}{R}", "Creature", "{1}{R}", power=2, toughness=2
        )
    ]
    option = cast(
        40, None, alternativeGrpId=149, alternativeCostText="Cast without paying mana cost", manaCost=None
    )
    _, result = replay(s, [])
    why = unmodelled_cast(result, option, s)
    assert "offered free alternative cost/zone not modelled" in why
    missing = _unmodelled_options(result, menu(option), s)
    assert missing[option.option_id] == why
    assert ActionPlanner._line_tag(result, option, s, missing) == "[LINE: effect not modelled]"
    # Ordinary casts retain the existing evaluation, without a blanket reorder.
    assert not unmodelled_cast(result, cast(20), s)


def test_predevelopment_menu_exposes_full_removal_reservation():
    s = board()
    answer = cast(30, manaCost=[{"color": ["White"], "count": 1}])
    development = cast(20, manaCost=[{"color": ["Generic"], "count": 1}, {"color": ["White"], "count": 1}])
    note = " ".join(timing_notes(menu(development, answer), s))
    assert "1 spell mana + 3 ward mana = 4 before development (4 mana available now)" in note
    assert "legally" in note and "colored mana" in note
    assert "If you choose development instead" in note
    # Unknown effective cost is not silently replaced by a printed cost.
    assert not timing_notes(menu(cast(30)), s)
