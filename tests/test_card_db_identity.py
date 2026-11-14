"""Cross-source numeric ID collisions must not splice different cards/faces."""

from arenamcp.card_db import CardInfo, FallbackCardDatabase


class Source:
    def __init__(self, by_id=None, by_name=None):
        self.by_id = by_id
        self.by_name = by_name

    def get_card_by_arena_id(self, arena_id):
        return self.by_id

    def get_card_by_name(self, name):
        return self.by_name


def card(name, text="", **kwargs):
    return CardInfo(name=name, oracle_text=text, type_line="Land", **kwargs)


def test_collision_uses_identity_matched_name_fallback():
    island = card("Island", source="mtgadb", arena_id=58441)
    plains = card("Plains", "({T}: Add {W}.)", arena_id=58441)
    correct = card("Island", "({T}: Add {U}.)")
    db = FallbackCardDatabase([Source(island), Source(plains, plains), Source(None, correct)])
    result = db.get_card_by_arena_id(58441)
    assert result.name == "Island"
    assert result.oracle_text == "({T}: Add {U}.)"
    assert plains.oracle_text == "({T}: Add {W}.)"


def test_collision_without_named_evidence_stays_incomplete():
    first = card("Island")
    wrong = card("Plains", "white mana", colors=["W"], cmc=7)
    result = FallbackCardDatabase([Source(first), Source(wrong, wrong)]).get_card_by_arena_id(1)
    assert result.name == "Island"
    assert result.oracle_text == ""
    assert result.colors == []
    assert result.cmc == 0


def test_same_card_enrichment_retained():
    first = card(" Island ")
    second = card("island", "blue mana")
    assert (
        FallbackCardDatabase([Source(first), Source(second)]).get_card_by_arena_id(1).oracle_text
        == "blue mana"
    )


def test_front_back_and_combined_faces_are_not_interchangeable():
    first = card("Front")
    first.related_faces = [{"name": "Back", "oracle_text": "back rules"}]
    db = FallbackCardDatabase(
        [Source(first), Source(card("Back", "back rules")), Source(card("Front // Back", "both rules"))]
    )
    result = db.get_card_by_arena_id(1)
    assert result.oracle_text == ""
    assert result.related_faces == [{"name": "Back", "oracle_text": "back rules"}]


def test_same_combined_identity_and_same_named_face_still_enrich():
    for name in ("Front", "Back", "Front // Back"):
        result = FallbackCardDatabase(
            [Source(card(name)), Source(card(name, "correct rules"))]
        ).get_card_by_arena_id(1)
        assert result.oracle_text == "correct rules"


def test_unknown_name_is_not_identity_evidence():
    assert not FallbackCardDatabase._same_identity("Unknown", "Unknown")
    assert not FallbackCardDatabase._same_identity("", "")


def test_name_lookup_rejects_other_face_and_continues():
    db = FallbackCardDatabase(
        [Source(by_name=card("Front // Back", "combined")), Source(by_name=card("Back", "right"))]
    )
    assert db.get_card_by_name("Back").oracle_text == "right"
