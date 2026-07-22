"""Tests for draft evaluation color-pair preference locking and draft pool synergy badges."""

from unittest.mock import MagicMock
import pytest

from arenamcp.draft_eval import (
    CardEvaluation,
    check_synergy,
    evaluate_pack,
)


def test_check_synergy_badge():
    """Test check_synergy returns score, reason, and human-readable synergy badge."""
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

    score, reason, badge = check_synergy(card_elf, [101, 102], mock_scryfall)
    assert score > 0
    assert "elf" in reason.lower()
    assert badge is not None
    assert "⚡ Elf" in badge


def test_evaluate_pack_color_pair_preference_locking():
    """Test evaluate_pack prioritizes locked color pair when locked_color_pair is set."""
    mock_scryfall = MagicMock()

    # Red/Blue card
    card_ur = MagicMock()
    card_ur.name = "Expressive Iteration"
    card_ur.colors = ["U", "R"]
    card_ur.type_line = "Sorcery"
    card_ur.oracle_text = "Look at top three cards"

    # White/Green card
    card_wg = MagicMock()
    card_wg.name = "Selesnya Evangel"
    card_wg.colors = ["W", "G"]
    card_wg.type_line = "Creature - Human"
    card_wg.oracle_text = "Create Saproling"

    def get_card(grp_id):
        if grp_id == 201:
            return card_ur
        if grp_id == 202:
            return card_wg
        return None

    mock_scryfall.get_card_by_arena_id.side_effect = get_card

    # Evaluate pack with locked color pair "UR"
    evals = evaluate_pack(
        cards_in_pack=[201, 202],
        picked_cards=[],
        set_code="",
        scryfall=mock_scryfall,
        locked_color_pair="UR",
    )

    assert len(evals) == 2
    ur_eval = next(e for e in evals if e.grp_id == 201)
    wg_eval = next(e for e in evals if e.grp_id == 202)

    assert ur_eval.locked_color_pair == "RU" or ur_eval.locked_color_pair == "UR"
    assert ur_eval.is_locked is True
    assert "fits locked" in ur_eval.reason.lower() or "locked" in ur_eval.reason.lower()
    assert ur_eval.score > wg_eval.score
