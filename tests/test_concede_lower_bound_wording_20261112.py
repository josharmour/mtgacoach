"""The concede reason labels the optimistic damage figure as a lower bound.

Match 6deb0e48-c65d-4162-a58f-1db24b7b1be8, 2026-11-12 02:12:49 UTC
(DarkWarrior): we were at 8 life facing Hydra-Behemoth 17/17 trample,
Chocobo 10/11, Jadzia 9/10 and Ouroboroid 7/9 with Cindermaw 4/3,
Slickshot Sequence-ish 1/2 and Hired Claw-ish 1/2 to block with and an empty
mana pool. The block-solver max flow (supplies [10, 10, 10], capacities
[[3, 10, 9, 7], [2, 10, 9, 7], [2, 10, 9, 7]], demands [17, 10, 9, 7])
stops 30 of the incoming 43 — "13 through" — an optimistic relaxed lower
bound (splitting one blocker's absorption across attackers is not a legal
assignment; the owner-derived legal minimum was 17). The spoken reason must
present that number as "at least", never as exact.
"""

from tests.test_concede import board, creature

from arenamcp.concede import DEFAULT_THRESHOLD, _concede_reason, _most_chumped_life, estimate_loss


def _darkwarrior_attackers() -> list[dict]:
    return [
        creature(101, "Hydra-Behemoth", 2, 17, 17, keywords=("trample",)),
        creature(102, "Chocobo Knight", 2, 10, 11),
        creature(103, "Jadzia", 2, 9, 10),
        creature(104, "Ouroboroid", 2, 7, 9),
    ]


def _darkwarrior_blockers() -> list[dict]:
    return [
        creature(201, "Cindermaw", 1, 4, 3),
        creature(202, "Slickshot Singer", 1, 1, 2),
        creature(203, "Hired Claw", 1, 1, 2),
    ]


def test_the_darkwarrior_max_flow_is_unchanged_thirteen_through():
    # Upper bound on retained life: at least sum(damage) - stopped = at least
    # (17 + 10 + 9 + 7) - (10 + ... ) => exactly the relaxed flow's stopped=30.
    after = _most_chumped_life(_darkwarrior_attackers(), _darkwarrior_blockers(), life=8)
    assert after == -5


def test_the_relaxed_flow_never_understates_retained_life():
    # Dropping every blocker can only lose more life than the relaxed flow.
    with_none = _most_chumped_life(_darkwarrior_attackers(), [], life=8)
    assert with_none <= -5


def test_the_reason_says_at_least_when_nothing_is_evasive():
    reason = _concede_reason([], through=13, our_life=8, when="next attack")
    assert reason.startswith("Concede: at least 13 damage gets through your best blocks")


def test_the_evasive_remainder_also_reads_as_a_lower_bound():
    fliers = [
        {
            "name": "Exemplar of Light",
            "power": 6,
            "toughness": 6,
            "keywords": ["flying"],
            "_unblockable": False,
        },
        {
            "name": "Lyra Dawnbringer",
            "power": 5,
            "toughness": 5,
            "keywords": ["flying"],
            "_unblockable": False,
        },
    ]
    reason = _concede_reason(fliers[:1], through=13, our_life=11, when="this attack")
    assert reason.startswith(
        "Concede: their 6/6 flying Exemplar of Light and at least 7 more damage get through"
    )


def test_the_darkwarrior_board_concedes_with_lower_bound_wording():
    state = board(
        _darkwarrior_attackers() + _darkwarrior_blockers(),
        life=8,
        opp_life=20,
        active=2,
    )
    estimate = estimate_loss(state)
    assert estimate.confidence >= DEFAULT_THRESHOLD, estimate.blockers
    assert "at least" in estimate.reason
