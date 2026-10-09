"""bug_20261008_172415 (game 2, T18, 17:23:15): a planned attacker with no recipient.

The opponent controlled a Jace, so every attacker had two legal recipients;
``choose_recipient`` raised for the unnamed Screeching Soulbreaker, the window
went MANUAL REQUIRED, an Oops emote was sent and the user attacked by hand.
"""

from __future__ import annotations

import pytest
from tests.fra_quickdraft_states import state

from arenamcp.combat_targets import attack_target_prompt, choose_recipient, default_recipient, recipient_key


def _window():
    window = state("G2_T18_ATTACK")
    raw = window["decision_context"]["raw_attackers"]
    legal = next(entry for entry in raw if entry["attackerInstanceId"] == 303)["legalDamageRecipients"]
    soulbreaker = next(card for card in window["battlefield"] if card["instance_id"] == 303)
    return window, legal, soulbreaker


def test_an_unnamed_attacker_hits_the_opponent_player():
    window, legal, soulbreaker = _window()
    assert recipient_key(choose_recipient("", legal, window, soulbreaker)) == ("player", 1)


def test_a_named_planeswalker_is_still_honoured():
    window, legal, soulbreaker = _window()
    assert recipient_key(choose_recipient("Jace [216]", legal, window, soulbreaker)) == ("planeswalker", 216)


def test_a_sure_lethal_hit_on_the_planeswalker_defaults_to_it():
    window, legal, soulbreaker = _window()
    # Nobody untapped can block a 2-power flyer when their flyer is tapped, and Jace sits at 2.
    for card in window["battlefield"]:
        if card["instance_id"] == 245:  # their Surveillance Phantasm
            card["is_tapped"] = True
    soulbreaker = dict(soulbreaker, power=2)
    assert recipient_key(default_recipient(legal, window, soulbreaker)) == ("planeswalker", 216)


def test_a_blockable_or_non_lethal_hit_keeps_the_player():
    window, legal, soulbreaker = _window()
    # Their flyer is untapped: the hit is not certain.
    assert recipient_key(default_recipient(legal, window, dict(soulbreaker, power=2))) == ("player", 1)
    for card in window["battlefield"]:
        if card["instance_id"] == 245:
            card["is_tapped"] = True
    # Unblockable but 1 power vs loyalty 2: not lethal to Jace.
    assert recipient_key(default_recipient(legal, window, soulbreaker)) == ("player", 1)


def test_a_single_legal_recipient_needs_no_attacker():
    assert choose_recipient("", [{"type": "Player", "playerSystemSeatId": 1}], {}) == {
        "type": "Player",
        "playerSystemSeatId": 1,
    }


def test_no_recipient_at_all_still_raises():
    with pytest.raises(ValueError):
        choose_recipient("", [], {})


def test_the_prompt_states_the_default():
    window, _legal, _soulbreaker = _window()
    text = attack_target_prompt(window, window["decision_context"])
    assert "attacks the opponent player" in text
    assert "Screeching Soulbreaker: Opponent; Jace [216]" in text
