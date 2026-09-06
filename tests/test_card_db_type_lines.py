"""A card lookup that produced nothing must not look like one that succeeded.

Regression tests for the defect that silently corrupted 20.4% of the on-disk
play-decision gate corpus: ``MTGADatabaseAdapter`` handed callers MTGA's raw
``Cards.Types`` column ("5", "1,2") as ``CardInfo.type_line``. Every consumer
in the codebase tests type lines by substring (``"land" in type_line``), so a
Plains whose type line read "5" was not a land: it produced no mana, the
prompt rendered "Mana: 0", and every affordability tag computed from that pool
was wrong — with no exception, no warning, and no way for a reader of the
prompt to tell it apart from a genuinely empty board.
"""

import sqlite3

import pytest

from arenamcp.card_db import (
    CardInfo,
    MTGADatabaseAdapter,
    create_card_database,
    is_bogus_type_line,
    is_hidden_card_id,
)
from arenamcp.mtgadb import MTGADatabase
from arenamcp.rules_engine import RulesEngine

# GrpIds used by the fixture below.
PLAINS = 1001
BEAR = 1002
TOKEN = 1003
NO_TYPE_LOC = 1004  # row exists, localization missing
ABSENT = 4242  # not in the database at all


def _make_db(tmp_path, *, with_type_columns: bool = True):
    """A miniature MTGA CardDatabase in MTGA's real schema shape."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "carddb.mtga"
    conn = sqlite3.connect(path)
    type_cols = ", TypeTextId INTEGER, SubtypeTextId INTEGER" if with_type_columns else ""
    try:
        conn.executescript(
            f"""
            CREATE TABLE Cards (
                GrpId INTEGER PRIMARY KEY,
                TitleId INTEGER,
                Types TEXT,
                Power TEXT,
                Toughness TEXT,
                Colors TEXT,
                IsToken INTEGER,
                ExpansionCode TEXT,
                AbilityIds TEXT,
                Order_Title TEXT
                {type_cols}
            );
            CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT, Formatted INTEGER);
            CREATE TABLE Abilities (Id INTEGER PRIMARY KEY, TextId INTEGER);
            """
        )
        base_cols = (
            "GrpId, TitleId, Types, Power, Toughness, Colors, IsToken, ExpansionCode, AbilityIds, Order_Title"
        )
        rows = [
            # (grp, title, Types(numeric!), pow, tou, colors, token, set, abilities, order)
            (PLAINS, 2001, "5", "", "", "", 0, "TST", None, "Plains"),
            (BEAR, 2002, "2", "2", "2", "G", 0, "TST", None, "Bear"),
            (TOKEN, 2003, "1,2", "1", "1", "", 1, "TST", None, "Servo"),
            (NO_TYPE_LOC, 2004, "5", "", "", "", 0, "TST", None, "Mystery"),
        ]
        if with_type_columns:
            type_ids = {
                PLAINS: (3001, 3101),
                BEAR: (3002, 3102),
                TOKEN: (3003, 3103),
                NO_TYPE_LOC: (None, None),
            }
            conn.executemany(
                f"INSERT INTO Cards ({base_cols}, TypeTextId, SubtypeTextId) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [r + type_ids[r[0]] for r in rows],
            )
        else:
            conn.executemany(f"INSERT INTO Cards ({base_cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO Localizations_enUS (LocId, Loc, Formatted) VALUES (?,?,?)",
            [
                (2001, "Plains", 1),
                (2002, "Grizzly Bears", 1),
                (2003, "Servo", 1),
                (2004, "Mystery Card", 1),
                (3001, "Basic Land", 1),
                (3101, "Plains", 1),
                (3002, "Creature", 1),
                (3102, "Bear", 1),
                (3003, "Token Artifact Creature", 1),
                (3103, "Servo", 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return MTGADatabase(db_path=path)


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["5", "2", "1,2", " 10 , 4 ", "13"])
def test_numeric_type_codes_are_recognised_as_bogus(value):
    assert is_bogus_type_line(value) is True


@pytest.mark.parametrize(
    "value", ["Land", "Basic Land — Plains", "Creature — Bear", "Legendary Artifact", "", None]
)
def test_real_type_lines_are_not_bogus(value):
    assert is_bogus_type_line(value) is False


# ---------------------------------------------------------------------------
# A miss is distinguishable from a hit
# ---------------------------------------------------------------------------


def test_miss_returns_none_and_hit_returns_a_real_type_line(tmp_path):
    """The core requirement: absence and presence must not look the same."""
    adapter = MTGADatabaseAdapter(_make_db(tmp_path))

    miss = adapter.get_card_by_arena_id(ABSENT)
    hit = adapter.get_card_by_arena_id(PLAINS)

    assert miss is None, "a lookup failure must be None, never a placeholder card"
    assert hit is not None
    assert hit.type_line == "Basic Land — Plains"
    # And the thing that made them indistinguishable before:
    assert not is_bogus_type_line(hit.type_line)


def test_adapter_never_emits_the_raw_numeric_types_column(tmp_path):
    """``Cards.Types`` is a type-code list, not a type line. It must not leak."""
    db = _make_db(tmp_path)
    adapter = MTGADatabaseAdapter(db)

    for grp_id in (PLAINS, BEAR, TOKEN):
        raw = db.get_card(grp_id)
        info = adapter.get_card_by_arena_id(grp_id)
        assert is_bogus_type_line(raw.types), "fixture should carry MTGA's numeric codes"
        assert info.type_line != raw.types
        assert not is_bogus_type_line(info.type_line)


def test_missing_localization_yields_empty_not_a_number(tmp_path):
    """Unknown must be reported as unknown — "" — never as a made-up value."""
    info = MTGADatabaseAdapter(_make_db(tmp_path)).get_card_by_arena_id(NO_TYPE_LOC)
    assert info is not None
    assert info.type_line == ""


def test_schema_without_type_columns_degrades_instead_of_failing(tmp_path):
    """An MTGA build without the localization columns must still resolve cards."""
    adapter = MTGADatabaseAdapter(_make_db(tmp_path, with_type_columns=False))
    info = adapter.get_card_by_arena_id(PLAINS)
    assert info is not None, "cards must still resolve on an older/unknown schema"
    assert info.name == "Plains"
    assert info.type_line == "", "no type data available => unknown, not a numeric code"


def test_batch_lookup_matches_single_lookup(tmp_path):
    """``get_cards_batch`` is a separate SQL path and had the same gap."""
    db = _make_db(tmp_path)
    batch = db.get_cards_batch([PLAINS, BEAR])
    assert batch[PLAINS].type_line == "Basic Land — Plains"
    assert batch[BEAR].type_line == "Creature — Bear"

    fresh = _make_db(tmp_path / "second")
    assert fresh.get_card(PLAINS).type_line == batch[PLAINS].type_line


def test_prewarm_reports_real_type_lines(tmp_path):
    """Prewarming feeds the live snapshot path; it had the same defect."""
    warmed = MTGADatabaseAdapter(_make_db(tmp_path)).prewarm_cards([PLAINS, TOKEN])
    assert warmed[PLAINS].type_line == "Basic Land — Plains"
    assert not is_bogus_type_line(warmed[TOKEN].type_line)


# ---------------------------------------------------------------------------
# The consequence the bug actually had
# ---------------------------------------------------------------------------


def test_untapped_land_produces_mana(tmp_path):
    """The end-to-end symptom: a Plains on the battlefield must yield mana.

    This is the assertion that would have caught the corpus corruption. With
    a numeric type line, ``"land" in type_line`` is False and the pool is 0 —
    which reads exactly like a real empty board.
    """
    info = MTGADatabaseAdapter(_make_db(tmp_path)).get_card_by_arena_id(PLAINS)
    game_state = {
        "battlefield": [
            {
                "grp_id": PLAINS,
                "name": info.name,
                "type_line": info.type_line,
                "oracle_text": info.oracle_text,
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": -1,
            }
        ],
        "turn": {"turn_number": 3},
    }
    pool = RulesEngine._get_mana_pool(game_state, 1)
    assert pool["total"] == 1, "an untapped Plains must produce mana"
    assert pool["W"] == 1


def test_numeric_type_line_would_produce_no_mana():
    """Pins the failure mode itself, so the regression is unmistakable."""
    game_state = {
        "battlefield": [
            {
                "grp_id": PLAINS,
                "name": "",  # name-based rescue disabled: type line is the only signal
                "type_line": "5",
                "oracle_text": "",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": -1,
            }
        ],
        "turn": {"turn_number": 3},
    }
    assert RulesEngine._get_mana_pool(game_state, 1)["total"] == 0


# ---------------------------------------------------------------------------
# Hidden cards are not lookup failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grp_id", [3, 4])
def test_well_known_catalog_ids_are_hidden_not_missing(grp_id):
    """MTGA's StandardCardBack/Obscured placeholders are hidden by the rules."""
    assert is_hidden_card_id(grp_id) is True


@pytest.mark.parametrize("grp_id", [None, 0, 1, 2, PLAINS])
def test_ordinary_ids_are_not_hidden(grp_id):
    assert is_hidden_card_id(grp_id) is False


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


class _StubNameSource:
    """A name-indexed source, like MTGJSON, whose arena index is empty."""

    def __init__(self, by_name):
        self._by_name = by_name

    def get_card_by_arena_id(self, arena_id):
        return None

    def get_card_by_name(self, name):
        return self._by_name.get(name)


def test_fallback_prefers_a_real_type_line_over_a_numeric_one(tmp_path):
    """Enrichment must never overwrite a good type line with a code."""
    liar = _StubNameSource({"Plains": CardInfo(name="Plains", type_line="2", mana_cost="", source="liar")})
    db = create_card_database(mtgjson_db=None, mtga_db=_make_db(tmp_path), scryfall_cache=liar)
    info = db.get_card_by_arena_id(PLAINS)
    assert info is not None
    assert not is_bogus_type_line(info.type_line)
    assert "Land" in info.type_line
