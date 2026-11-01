"""#254: public subtype counts generalize beyond the former Halfling exception."""

from tests.test_tactical_timing_backlog import board, card

from arenamcp.play_safety import affordable_x, exact_mana_total


def forest_board():
    state = board(0)
    state["battlefield"] = [card(i, "Forest", "{T}: Add {G}.", "Basic Land — Forest") for i in range(1, 4)]
    state["battlefield"].append(
        card(
            10,
            "Counted mana creature",
            "{T}: Add {G} for each Forest you control.",
            "Creature — Elf Druid",
            summoning_sickness=False,
        )
    )
    return state


def test_forest_count_prices_hydra_x_and_excludes_opponent_sources():
    state = forest_board()
    state["battlefield"].append(card(20, "Forest", "{T}: Add {G}.", "Basic Land — Forest", seat=2))
    assert exact_mana_total(state) == 6
    assert affordable_x(state, {"mana_cost": "{X}{G}"}) == 5
    state["battlefield"][0]["is_tapped"] = True
    assert exact_mana_total(state) == 5  # tapped Forest still counts for the producer
    state["battlefield"][3]["is_tapped"] = True
    assert exact_mana_total(state) == 2


def test_elf_count_uses_subtype_not_name_or_controller():
    state = forest_board()
    state["battlefield"][3]["oracle_text"] = "{T}: Add {G} for each Elf you control."
    state["battlefield"].append(card(30, "Another elf", "", "Creature — Elf"))
    state["battlefield"].append(card(31, "Elf impersonator", "", "Creature — Human"))
    state["battlefield"].append(card(32, "Opponent elf", "", "Creature — Elf", seat=2))
    assert exact_mana_total(state) == 5


def test_unknown_types_costly_and_multiple_abilities_remain_unknown():
    for text in (
        "{2}, {T}: Add {G} for each Forest you control.",
        "{T}: Add {G} for each Forest you control.\n{T}: Add {C}.",
        "{T}: Add {G} for each Forest you control. Spend this mana only to cast creatures.",
    ):
        state = forest_board()
        state["battlefield"][3]["oracle_text"] = text
        assert exact_mana_total(state) is None
    state = forest_board()
    state["battlefield"][0].pop("type_line")
    assert exact_mana_total(state) is None
