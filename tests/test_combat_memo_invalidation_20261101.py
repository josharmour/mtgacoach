"""In-search combat memos must notice in-place refreshes of their inputs.

``combat_detail`` keys its result by ``protection_key`` of every attacker and
blocker and decides whether the defender's library matters from the attackers'
oracle text. Both are memoised per search; a nested ``_card`` refresh or an
oracle-text refresh between two lookups must produce the fresh result.
"""

import pytest

from arenamcp import combat_solver as cs
from arenamcp.board_model import build_board_model
from arenamcp.combat_loss import LossState
from arenamcp.combat_protection import protection_key, protection_signature
from arenamcp.line_search_moves import Moves
from test_color_protection_273 import body
from test_combat_loss_281 import board


def moves_for(ours, theirs=()):
    snapshot = board(ours, theirs)
    return Moves(build_board_model(snapshot), survival=False, lethal_now=False, hard_ms=1e8, opponent=None)


def backed_body(text="Protection from white"):
    """A body whose printed fields live in its nested ``_card`` backing dict."""
    return {
        **body(1),
        "_card": {
            "oracle_text": text,
            "colors": ["W"],
            "keywords": [],
            "ability_grp_ids": [],
            "granted_abilities": [],
            "removed_abilities": [],
        },
    }


def test_block_search_rekeys_a_body_changed_between_searches():
    """The per-search fight-key cache must not outlive the search that built it."""
    attacker = body(1, text="Protection from white")
    blocker = body(2, 3, 3, colors=["W"])
    first = cs._search_blocks([attacker], [blocker], 20)
    assert first.damage_through == 2
    blocker["colors"] = ["R"]
    second = cs._search_blocks([attacker], [blocker], 20)
    assert second.damage_through == 0


def test_nested_raw_color_refresh_changes_the_cached_combat_result():
    moves = moves_for([body(1, text="Protection from white")], [body(2, 3, 3, colors=["W"])])
    root = moves.root_node()
    before = moves.combat_life(root.ours, root.theirs, 20)
    size = len(moves._protection_memo)  # attacker and blocker are both keyed
    assert size == 2
    assert moves.combat_life(root.ours, root.theirs, 20) == before
    assert len(moves._protection_memo) == size  # equal content reuses the entries
    blocker = root.theirs[0]
    assert protection_key(blocker)[0] == ("W",)
    blocker["_card"]["modified_colors"] = ["R"]
    assert protection_key(blocker)[0] == ("R",)
    after = moves.combat_life(root.ours, root.theirs, 20)
    assert (before[0], after[0]) == (2, 0)


def test_nested_raw_removal_refresh_changes_the_cached_combat_result():
    moves = moves_for([body(1, text="Protection from white")], [body(2, 3, 3, colors=["W"])])
    root = moves.root_node()
    before = moves.combat_life(root.ours, root.theirs, 20)
    attacker = root.ours[0]
    attacker["_card"]["removed_abilities"] = ["all abilities"]
    assert protection_key(attacker)[1] == ()
    after = moves.combat_life(root.ours, root.theirs, 20)
    assert (before[0], after[0]) == (2, 0)


def test_future_flag_refresh_drops_a_current_grant_in_the_cached_result():
    moves = moves_for([body(1, ability_grp_ids=[185])], [body(2, 3, 3, colors=["W"])])
    root = moves.root_node()
    before = moves.combat_life(root.ours, root.theirs, 20)
    attacker = root.ours[0]
    assert protection_key(attacker)[1] == ("W",)
    attacker["_protection_future"] = True
    assert protection_key(attacker)[1] == ()
    after = moves.combat_life(root.ours, root.theirs, 20)
    assert (before[0], after[0]) == (2, 0)


def test_oracle_text_refresh_changes_the_cached_mill_result():
    moves = moves_for([body(1)])
    root = moves.root_node()
    defender = LossState(library=5)
    before = moves.combat_detail(root.ours, [], 20, defender=defender)
    assert before.loss.library == 5
    attacker = root.ours[0]
    attacker["oracle_text"] = (
        "Whenever this creature deals combat damage to a player, that player mills two cards."
    )
    after = moves.combat_detail(root.ours, [], 20, defender=defender)
    assert after.loss.library == 3


def test_protection_signature_is_stable_for_equal_content():
    """The memo must hit across equal bodies: identity is not part of the signature."""
    assert protection_signature(backed_body()) == protection_signature(backed_body())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda card: card["_card"].__setitem__("modified_colors", ["R"]),
        lambda card: card["_card"].__setitem__("removed_abilities", ["all abilities"]),
        lambda card: card.__setitem__("granted_abilities", ["Protection from green"]),
        lambda card: card["_card"].__setitem__("keywords", ["Protection from blue"]),
        lambda card: card.__setitem__("damage_cant_be_prevented", True),
        lambda card: card["_card"].__setitem__("oracle_text", ""),
        lambda card: card.__setitem__("colors", ["G"]),
        lambda card: card.__setitem__("ability_grp_ids", [188]),
        lambda card: card["_card"].__setitem__("granted_abilities", ["Protection from black"]),
        lambda card: card["_card"].__setitem__("ability_grp_ids", [189]),
        lambda card: card["_card"].__setitem__("damage_cant_be_prevented", True),
        lambda card: card["_card"].__setitem__("removed_abilities", ["protection"]),
        lambda card: card.__setitem__("keywords", ["Protection from red"]),
        lambda card: card.__setitem__("colors", []),
        lambda card: card["_card"].__setitem__("colors", []),
        lambda card: card.__setitem__("modified_colors", []),
        lambda card: card["_card"].__setitem__("modified_colors", []),
        lambda card: card["_card"].__setitem__("colors", None),
        lambda card: card.__setitem__("_source_colors", ["B"]),
        lambda card: card["_card"].__setitem__("_source_colors", ["B"]),
        lambda card: card["_card"].__setitem__("oracle_text", "Protection from red"),
        lambda card: card.__setitem__("keywords", ["Protection from green"]),
        lambda card: card["_card"].__setitem__("keywords", ["Protection from green"]),
        lambda card: card["_card"].__setitem__("oracle_text", "Protection from white, protection from blue"),
        lambda card: card.__setitem__("modified_colors", ["U"]),
        lambda card: card["_card"].__setitem__("modified_colors", ["U"]),
        lambda card: card.__setitem__("colors", ["R"]),
        lambda card: card["_card"].__setitem__("colors", ["R"]),
    ],
)
def test_protection_signature_changes_whenever_the_key_changes(mutate):
    """Every field ``protection_key`` reads is part of ``protection_signature``."""
    card = backed_body()
    before_key, before_signature = protection_key(card), protection_signature(card)
    mutate(card)
    assert protection_key(card) != before_key
    assert protection_signature(card) != before_signature


def test_protection_signature_tracks_the_future_flag():
    """The future flag drops current grants without changing printed protection."""
    card = backed_body(text="")
    card["ability_grp_ids"] = [185]
    before_key, before_signature = protection_key(card), protection_signature(card)
    assert before_key[1] == ("W",)
    card["_protection_future"] = True
    assert protection_key(card)[1] == ()
    assert protection_signature(card) != before_signature
