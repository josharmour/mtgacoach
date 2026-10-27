import pytest

from arenamcp.draft_advisor import _plan_lane
from arenamcp.draft_plan import plan_colors


@pytest.mark.parametrize(
    "plan,stated,expected",
    [
        (
            "Stay flexible around green midrange with strong black interaction "
            "(Spin Out, Momentum Breaker) while keeping Simic's Skyserpent Seeker viable.",
            "UG",
            "",
        ),
        (
            "Green-black midrange using cheap removal, counters, and graveyard value, "
            "with Captain Howler as a potential splash-level finisher if blue-red fixing materializes.",
            "BG",
            "BG",
        ),
        ("Izzet or Golgari midrange", "UR", ""),
        ("Izzet or Azorius tempo", "UR", "U"),
        ("Izzet spells with a possible Golgari pivot", "BG", "UR"),
        ("blue, black and red goodstuff", "", ""),
        ("Efficient creatures and interaction", "BG", "BG"),
    ],
)
def test_alternative_colors_do_not_replace_committed_plan(plan, stated, expected):
    assert _plan_lane(plan, stated, [])["lane"] == expected


def test_definite_pair_survives_conditional_colors_in_another_clause():
    parsed = plan_colors("Green-black midrange, with blue-red fixing if it wheels.")
    assert parsed.main == "BG"
    assert not parsed.uncommitted
