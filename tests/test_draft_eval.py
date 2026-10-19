"""Tests for the draft_eval baseline: synergy with the drafted pool."""

from unittest.mock import MagicMock

from arenamcp.draft_eval import check_synergy


def test_check_synergy_finds_tribal_overlap_with_the_pool():
    """A creature sharing a tribe with drafted cards scores a named synergy."""
    mock_scryfall = MagicMock()

    card_elf = MagicMock()
    card_elf.name = "Llanowar Elves"
    card_elf.oracle_text = "Tap to add Green mana"
    card_elf.type_line = "Creature - Elf Druid"

    picked_elf1 = MagicMock()
    picked_elf1.name = "Elvish Archdruid"
    picked_elf1.oracle_text = "Elves get +1/+1"
    picked_elf1.type_line = "Creature - Elf Druid"

    picked_elf2 = MagicMock()
    picked_elf2.name = "Imperious Perfect"
    picked_elf2.oracle_text = "Other Elf creatures get +1/+1"
    picked_elf2.type_line = "Creature - Elf Shaman"

    def get_card(grp_id):
        if grp_id == 101:
            return picked_elf1
        if grp_id == 102:
            return picked_elf2
        return None

    mock_scryfall.get_card_by_arena_id.side_effect = get_card

    score, reason = check_synergy(card_elf, [101, 102], mock_scryfall)
    assert score > 0
    assert "elf" in reason.lower()
