"""A distant win no longer outranks damage now (ledger #158, match be1a945f T10).

T10, 14:16:52: we are at 25 with Leonardo 16/3, three able Hare Apparents and
three Rabbits (25 power); they are at 12 with Lathliss 6/6 and Ureni 8/8 (both
fliers) and a 1/3 Servant. The line search ranked any win class above any
alive one, so "hold, lethal on T14" (a greedy third turn, their new cards
unmodelled) beat "attack, opponent at 5", the low-effort model followed it and
we declared no attackers. They untapped into Ugin (exile Leonardo, +3 life)
and stabilised. Nothing of ours could block their fliers anyway: the creatures
kept home only waited.
"""

from __future__ import annotations

from tests import leonardo_boards_20261019 as lb

from arenamcp.board_model import build_board_model
from arenamcp.line_search import ALIVE, WIN, Line, _order, _Search, distant_win, search_lines


def _search(state):
    return search_lines(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e9)


def test_t10_holding_back_blocks_nothing_so_the_posture_is_attack():
    result = _search(lb.t10_declare_attackers())
    assert result.posture == "attack", result.posture_reason
    assert "holding back blocks nothing (none of ours can block Lathliss, Dragon Queen, Ureni" in (
        result.posture_reason
    )


def test_holding_still_wins_when_their_attackers_can_be_blocked():
    state = lb.t10_declare_attackers()
    for card in state["battlefield"]:
        if card["controller_seat_id"] == lb.OPPONENT:
            card["keywords"] = [k for k in card.get("keywords") or [] if k != "flying"]
            card["oracle_text"] = ""
    result = _search(state)
    assert "holding back blocks nothing" not in result.posture_reason


def test_a_win_on_our_third_turn_ranks_as_alive_with_only_the_damage_dealt_now():
    state = lb.t10_declare_attackers()
    state["players"][1]["life_total"] = 30
    result = _search(state)
    best = result.best
    # Lethal only on T14 (T+2, greedy): an alive line valued with T's damage.
    assert (best.cls, best.win_at) == (WIN, 3), best.summary(200)
    assert best.distant_v is not None and best.ranked[0] == ALIVE
    assert best.outcome_text() == "lethal on T14"  # the facts still say what the line does


def test_ranking_keys():
    def line(cls, timing, v, *, win_at=None, distant_v=None):
        return Line(steps=(), cls=cls, timing=timing, v=v, win_at=win_at, distant_v=distant_v)

    near = line(WIN, -2, 0.5, win_at=2)
    distant = line(WIN, -3, 0.5, win_at=3, distant_v=20.0)
    alive = line(ALIVE, 0, 25.0)
    assert near.ranked == near.score and distant.ranked == (ALIVE, 0, 20.0)
    ordered = sorted([distant, alive, near], key=_order, reverse=True)
    assert ordered == [near, alive, distant]


def test_distant_wins_are_the_third_turns_and_the_greedy_tails():
    state = lb.t12_main1()
    search = _Search(build_board_model(state), survival=False, lethal_now=False, hard_ms=1e9, groups=16,
                     width=2, k_next=4, opponent=None)  # fmt: skip
    root = search.root_node()
    import dataclasses

    assert not distant_win(root)
    assert not distant_win(dataclasses.replace(root, win_at=2))
    assert distant_win(dataclasses.replace(root, win_at=3))
    assert distant_win(dataclasses.replace(root, win_at=2, rolled=True))
    assert search.distant_value(dataclasses.replace(root, win_at=1)) is None
