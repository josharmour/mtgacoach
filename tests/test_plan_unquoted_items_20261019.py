"""A plan answer with unquoted list items is repaired (ledger #114 follow-up).

2026-10-19 16:21:40: the high-effort plan's "assumptions" list held two bare
items ('no instant trick or fog disrupts the committed T13 swing,' and
'Thalia blocks at most one attacker; ...'); the whole 2643-char plan was
dropped as unusable.
"""

from pathlib import Path

from arenamcp.game_plan import _plan_object

FIXTURE = Path(__file__).parent / "fixtures" / "game_plan_unquoted_list_items_20261019.txt"


def test_the_real_answer_with_bare_list_items_parses():
    data, _text = _plan_object(FIXTURE.read_text())
    assert data["role"] == "aggressor"
    assert data["turns"]
    assert any("instant trick or fog" in item for item in data["assumptions"])
    assert any(item.startswith("Thalia blocks at most one attacker") for item in data["assumptions"])


def test_a_bare_item_at_the_end_of_a_one_line_list():
    data, _ = _plan_object('{"role": "race", "turns": [{"turn": "T"}], "assumptions": ["a", b c]}')
    assert data["assumptions"] == ["a", "b c"]
