"""Tests for canonical mana calculation and seat resolution utilities."""

from arenamcp.mana import get_local_seat_id, mana_cost_to_cmc, parse_color_identity


def test_mana_cost_to_cmc_standard():
    assert mana_cost_to_cmc("") == 0
    assert mana_cost_to_cmc(None) == 0
    assert mana_cost_to_cmc("{1}") == 1
    assert mana_cost_to_cmc("{W}") == 1
    assert mana_cost_to_cmc("{1}{W}{U}") == 3
    assert mana_cost_to_cmc("{2}{B}{B}") == 4


def test_mana_cost_to_cmc_two_digit_generic():
    """Verify two-digit generic costs (e.g. {10}{G}{G}) parse as 12, not 3."""
    assert mana_cost_to_cmc("{10}{G}{G}") == 12
    assert mana_cost_to_cmc("{15}") == 15
    assert mana_cost_to_cmc("{100}") == 100


def test_mana_cost_to_cmc_variable_and_hybrid():
    assert mana_cost_to_cmc("{X}{R}") == 1
    assert mana_cost_to_cmc("{X}{2}{U}") == 3
    assert mana_cost_to_cmc("{W/U}{W/U}") == 2
    assert mana_cost_to_cmc("{2/W}{2/W}") == 4
    assert mana_cost_to_cmc("{G/P}") == 1


def test_parse_color_identity():
    assert parse_color_identity("") == ""
    assert parse_color_identity(None) == ""
    assert parse_color_identity("{1}{U}{R}") == "UR"
    assert parse_color_identity("{2}{G}{W}") == "WG"
    assert parse_color_identity("{B}{G}{R}{U}{W}") == "WUBRG"
    assert parse_color_identity("{W/U}") == "WU"


def test_get_local_seat_id():
    assert get_local_seat_id(None) is None
    assert get_local_seat_id({}) is None
    assert get_local_seat_id({"local_seat_id": 1}) == 1
    assert get_local_seat_id({"player_seat": 2}) == 2
    assert (
        get_local_seat_id(
            {
                "players": [
                    {"seat_id": 1, "is_local": False},
                    {"seat_id": 2, "is_local": True},
                ]
            }
        )
        == 2
    )
