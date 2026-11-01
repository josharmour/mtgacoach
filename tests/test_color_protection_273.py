"""Color protection and current-grant lifetime, including #273's mechanism.

The old full match packet is absent. These are explicit reconstructed rule
cases, not a claim that the historical attack was winning.
"""

from copy import deepcopy

import pytest

from arenamcp.combat_keywords import can_be_blocked_by
from arenamcp.combat_protection import (
    future_protection,
    protected_from_source,
    protection_qualities,
    source_colors,
)
from arenamcp.combat_solver import _card_key, _resolve_attacker, evaluate_attack, optimal_blocks


def body(i, p=2, t=2, text="", **extra):
    return dict(
        instance_id=i,
        name=f"Creature {i}",
        type_line="Creature",
        power=p,
        toughness=t,
        oracle_text=text,
        **extra,
    )


@pytest.mark.parametrize(
    "name,color,ability",
    [
        ("white", "W", 185),
        ("blue", "U", 186),
        ("black", "B", 187),
        ("red", "R", 188),
        ("green", "G", 189),
    ],
)
def test_printed_and_arena_granted_protection_stop_matching_blockers(name, color, ability):
    blocker = body(2, colors=[color])
    assert not can_be_blocked_by(body(1, text=f"Protection from {name}"), blocker)
    assert not can_be_blocked_by(body(1, ability_grp_ids=[ability]), blocker)
    assert not can_be_blocked_by(body(1, keywords=[f"Protection from {name}"]), blocker)
    assert can_be_blocked_by(body(1, ability_grp_ids=[ability]), body(2, colors=[]))


@pytest.mark.parametrize(
    "text",
    [
        "Other creatures you control have protection from white.",
        "{W}: This creature gains protection from white until end of turn.",
        "As long as you control an artifact, this creature has protection from white.",
        "Choose a color. White creatures you control gain protection from the chosen color until end of turn.",
        "Protection from the chosen color",
    ],
)
def test_inactive_grants_conditions_and_unknown_choices_are_not_invented(text):
    assert can_be_blocked_by(body(1, text=text), body(2, colors=["W"]))


def test_multiple_printed_colors_and_removed_abilities():
    card = body(1, text="Flying, protection from white and from blue")
    assert protection_qualities(card) == {"W", "U"}
    card["removed_abilities"] = ["Protection from white"]
    assert can_be_blocked_by(card, body(2, colors=["W"]))
    assert not can_be_blocked_by(card, body(2, colors=["U"]))


def test_formatted_oracle_text_still_finds_protection():
    """``_printed`` strips markup before matching; a raw-text fast path must not."""
    assert protection_qualities({"oracle_text": "Pro<b>tection</b> from red"}) == {"R"}
    assert protection_qualities({"oracle_text": "<i>Flying</i>"}) == frozenset()


def test_fight_memo_keys_top_level_fields_when_card_is_empty():
    """An empty ``_card`` falls back to the top-level body (combat_protection._raw).

    The fight memo must key the same fields the resolver reads: with
    ``_card={}`` the resolver reads top-level ``removed_abilities``, so a
    cached fight may not survive that field changing.
    """
    from arenamcp.combat_solver import (
        _RESOLVE_CACHE,
        _resolve_attacker_damage,
        _resolve_attacker_damage_uncached,
    )

    attacker = body(801, 4, 4, colors=["R"])
    blocker = body(802, 2, 2, "Protection from red", keywords=[], _card={})
    _RESOLVE_CACHE.clear()
    first = _resolve_attacker_damage(attacker, [blocker])
    assert not first.blockers_died
    blocker["removed_abilities"] = ["protection"]
    second = _resolve_attacker_damage(attacker, [blocker])
    uncached = _resolve_attacker_damage_uncached(attacker, [blocker])
    assert len(second.blockers_died) == len(uncached.blockers_died) == 1


def test_current_source_colors_override_printed_and_color_identity():
    card = body(1, text="Protection from white")
    assert protected_from_source(card, body(2, colors=["R", "W"]))
    assert not protected_from_source(card, body(2, colors=["W"], modified_colors=[]))
    assert protected_from_source(card, body(2, colors=["R"], modified_colors=["Color_White"]))
    assert not protected_from_source(card, body(2, color_identity=["W"]))
    assert source_colors({"colors": ["unrecognized"]}) is None
    assert protected_from_source(card, {"_source_colors": ["W"]})


def test_protection_prevents_damage_and_lifelink_but_not_trample_assignment():
    attacker = body(1, 5, 5, "Trample\nLifelink", colors=["R"])
    blocker = body(2, 2, 2, "Protection from red")
    fight = _resolve_attacker(attacker, [blocker])
    assert fight.damage_through == 3 and fight.life_gained == 3
    assert not fight.blockers_died
    attacker["oracle_text"] = "Lifelink"
    fight = _resolve_attacker(attacker, [blocker])
    assert fight.damage_through == 0 and fight.life_gained == 0


def test_damage_prevention_applies_in_both_directions_and_both_strikes():
    attacker = body(1, 3, 3, "Protection from white", colors=["R"])
    blocker = body(2, 2, 4, "Double strike\nLifelink", colors=["W"])
    fight = _resolve_attacker(attacker, [blocker])  # already-declared block, protection gained later
    assert not fight.attacker_died and fight.blocker_life_gained == 0


def test_unpreventable_damage_does_not_remove_blocking_restriction():
    attacker = body(1, 5, 5, "Lifelink\nDamage can't be prevented.", colors=["R"])
    blocker = body(2, 2, 2, "Protection from red")
    fight = _resolve_attacker(attacker, [blocker])
    assert fight.blockers_died == [blocker] and fight.life_gained == 5
    assert not can_be_blocked_by(blocker, attacker)


def test_colors_active_grants_and_expiration_invalidate_combat_memo():
    attacker = body(1, text="Protection from white")
    blocker = body(2, 3, 3, colors=["W"])
    assert optimal_blocks([attacker], [blocker], 20).damage_through == 2
    old_key = _card_key(blocker)
    blocker["modified_colors"] = ["R"]
    assert _card_key(blocker) != old_key
    assert optimal_blocks([attacker], [blocker], 20).damage_through == 0
    attacker["oracle_text"] = ""
    attacker["ability_grp_ids"] = [188]
    assert optimal_blocks([attacker], [blocker], 20).damage_through == 2
    attacker["ability_grp_ids"] = []
    assert optimal_blocks([attacker], [blocker], 20).damage_through == 0


def test_future_projection_retains_printed_not_current_only_grants():
    card = body(1, text="Protection from red", ability_grp_ids=[185], keywords=["protection from white"])
    snapshot = deepcopy(card)
    future = future_protection(card)
    assert protection_qualities(card) == {"W", "R"}
    assert protection_qualities(future) == {"R"}
    assert _card_key(card) != _card_key(future)
    assert card == snapshot


@pytest.mark.parametrize("printed,expected", [(False, 2), (True, 0)])
def test_crackback_does_not_keep_brave_grant_past_our_end_step(printed, expected):
    ours = body(1, text="Lifelink" + ("\nProtection from white" if printed else ""), ability_grp_ids=[185])
    theirs = body(2, 4, 4, "First strike\nTrample", colors=["W"])
    plan = evaluate_attack([], [ours], [], 20, 4, [theirs])
    assert plan.worst_case_crackback == expected


def test_current_gre_grant_survives_serialization_then_disappears_on_refresh():
    from arenamcp.gamestate_models import GameObject

    obj = GameObject(1, 2, 3, 1, ability_grp_ids=[185], modified_colors=[])
    card = obj.to_dict()
    card["colors"] = ["W"]
    assert source_colors(card) == frozenset()
    assert not can_be_blocked_by(card, body(2, colors=["W"]))
    obj.ability_grp_ids = []
    assert can_be_blocked_by(obj.to_dict(), body(2, colors=["W"]))


def test_line_search_expires_grants_and_does_not_reuse_current_combat_cache():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search_moves import Moves
    from test_combat_loss_281 import board

    snapshot = board(
        [body(1, text="Lifelink", ability_grp_ids=[185])],
        [body(2, 4, 4, "First strike\nTrample", colors=["W"])],
    )
    snapshot["players"][0]["life_total"] = 4
    moves = Moves(build_board_model(snapshot), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)
    root = moves.root_node()
    current = moves.combat_life(root.theirs, root.ours, 4)
    future = moves.combat_life(moves._future_bodies(root.theirs), moves._future_bodies(root.ours), 4)
    assert current[4] == 2 and future[4] == 0
    after = moves.finish(moves.begin(root, None, ()), ("none", ()))
    assert after.our_life == 2
    assert protection_qualities(snapshot["battlefield"][0]) == {"W"}


def test_untargeted_destroy_is_not_prevented_by_active_protection():
    from arenamcp.board_assessment import _protected_from

    card, spell = body(1, ability_grp_ids=[185]), {"colors": ["W"]}
    assert _protected_from(card, spell, targeted=True, damage=False)
    assert not _protected_from(card, spell, targeted=False, damage=False)
