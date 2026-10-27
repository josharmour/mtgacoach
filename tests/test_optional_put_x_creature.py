"""Ghalta's optional deployment must not imply an X creature is free value."""

from dataclasses import replace

from arenamcp.decisions import DecisionOption, PendingDecision, select_n_note


def selection():
    return PendingDecision(
        request_id=(176, 221),
        request_type="SelectN",
        options=(DecisionOption("sel:20", "Goldvein Hydra"), DecisionOption("sel:21", "Woodfall Primus")),
        min_select=0,
        max_select=2,
        source_label="Ghalta, Stampede Tyrant",
        source_oracle_text="When Ghalta enters, put any number of creature cards from your hand onto the battlefield.",
    )


def hand():
    return {
        "hand": [
            {"instance_id": 20, "name": "Goldvein Hydra", "mana_cost": "{X}{G}"},
            {"instance_id": 21, "name": "Woodfall Primus", "mana_cost": "{5}{G}{G}{G}"},
        ]
    }


def test_optional_put_explains_card_loss_and_zero_x_without_forbidding_useful_death_triggers():
    decision = selection()
    note = select_n_note(decision, hand())
    assert "0 to 2 cards" in note and "Unselected cards stay in your hand" in note
    assert "Goldvein Hydra has X=0" in note
    assert "spends the card" in note and "concrete entry/death payoff" in note
    assert decision.selection_is_valid(["sel:21"])
    assert decision.selection_is_valid(["sel:20", "sel:21"])


def test_unknown_hand_selection_does_not_invent_a_put_effect():
    assert "X=0" not in select_n_note(replace(selection(), source_oracle_text=""), hand())


def test_recorded_ghalta_prompt_is_enough_when_source_rules_are_missing():
    decision = replace(
        selection(),
        source_oracle_text="",
        prompt_text="Put any number of creature cards onto the battlefield.",
    )
    assert "Goldvein Hydra has X=0" in select_n_note(decision, hand())


def test_discard_still_uses_loss_framing_instead_of_deployment():
    decision = replace(selection(), source_oracle_text="Discard two cards.", prompt_text="Discard two cards.")
    note = select_n_note(decision, hand())
    assert "DISCARD" in note
    assert "X=0" not in note


def test_required_deployment_does_not_offer_an_illegal_skip():
    decision = replace(selection(), min_select=2)
    note = select_n_note(decision, hand())
    assert "0 to 2" not in note
    assert "X=0" in note
    assert not decision.selection_is_valid(["sel:21"])
