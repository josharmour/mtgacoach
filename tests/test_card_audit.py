from unittest.mock import Mock

import pytest

from arenamcp.card_audit import audit_records
from arenamcp.card_db import CardInfo, FallbackCardDatabase, is_unknown_card_name


@pytest.mark.parametrize(
    "name", ["Unknown", "Unknown_123", "Unknown(123)", "Unknown (123)", "Unknown Card #123", "", None]
)
def test_only_placeholder_names_are_unresolved(name):
    assert is_unknown_card_name(name)
    assert not is_unknown_card_name("Unknown Shores")


def test_unknown_shores_can_be_enriched_by_name():
    source = Mock()
    source.get_card_by_arena_id.return_value = CardInfo(name="Unknown Shores", arena_id=42)
    source.get_card_by_name.return_value = CardInfo(
        name="Unknown Shores", type_line="Land", oracle_text="{T}: Add {C}."
    )
    card = FallbackCardDatabase([source]).get_card_by_arena_id(42)
    assert card.type_line == "Land"
    assert card.oracle_text


def test_audit_separates_placeholders_markers_and_actual_missing_card_data():
    database = Mock()
    database.prewarm_cards.return_value = {
        20: CardInfo(name="Unknown Shores", type_line="Land", oracle_text="{T}: Add {C}."),
        21: CardInfo(name="The Monarch"),
        22: CardInfo(name="New spell", type_line="Sorcery"),
    }
    result = audit_records(
        [
            {"GrpId": 6},
            {"GrpId": 20, "Types": "5", "AbilityIds": "1"},
            {"GrpId": 21, "IsToken": 1, "IsPrimaryCard": 0},
            {"GrpId": 22, "Types": "7", "OldSchoolManaText": "oU", "AbilityIds": "2"},
            {"GrpId": 23, "Types": "2"},
        ],
        database,
    )
    assert result["catalog_placeholders"] == [6]
    assert result["effect_markers"] == [{"grp_id": 21, "name": "The Monarch"}]
    assert result["resolved_card_records"] == 1
    assert result["unresolved"] == [
        {"grp_id": 22, "missing": ["mana_cost", "rules_text"]},
        {"grp_id": 23, "missing": ["name", "type_line"]},
    ]
