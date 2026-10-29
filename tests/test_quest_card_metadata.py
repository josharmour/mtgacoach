"""Real GetFormats casing and native SQLite columns, through MTGADatabase."""

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from arenamcp import quest_card_metadata as metadata
from arenamcp.mtgadb import MTGADatabase

# Shape captured from Player.log GetFormats (2026-10-29), including protobuf
# defaults omitted on the wire, TitleIds (NOT GrpIds), and actual Standard bans.
STANDARD = {
    "name": "Standard",
    "legalSets": [
        "WOE",
        "LCI",
        "MKM",
        "OTJ",
        "BIG",
        "BLB",
        "DSK",
        "FDN",
        "DFT",
        "TDM",
        "FIN",
        "EOE",
        "TLA",
        "ECL",
        "TMT",
        "SOS",
        "SPM",
        "MSH",
        "HOB",
        "FRA",
    ],
    "filterSets": ["FDN"],
    "bannedTitleIds": [
        737019,
        926968,
        811447,
        731463,
        812770,
        731627,
        748502,
        812819,
        756291,
        731808,
        928651,
        1070879,
        811206,
        1070468,
    ],
    "FormatTypeInternal": 3,
    "mainDeckQuota": {"min": 60, "max": 250},
    "sideBoardQuota": {"max": 15},
    "commandZoneQuota": {},
}


def write_log(path, config=None, *, age=timedelta(), multiline=False):
    timestamp = metadata._utc_now() - age
    data = {"Formats": [STANDARD if config is None else config], "FormatGroups": []}
    path.write_text(
        '[UnityCrossThreadLogger]==> GetFormats {"request":"{ }"}\n'
        + "[UnityCrossThreadLogger]"
        + timestamp.strftime("%m/%d/%Y %I:%M:%S %p")
        + "\n<== GetFormats(1f331af0-d3e5-4adc-a373-66045e3f6490)\n"
        + json.dumps(data, indent=2 if multiline else None)
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def native(tmp_path, monkeypatch):
    from arenamcp import server

    path = tmp_path / "Raw_CardDatabase_test.mtga"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Cards (
            GrpId INT PRIMARY KEY, TitleId INT, Types TEXT DEFAULT '2',
            Power TEXT DEFAULT '2', Toughness TEXT DEFAULT '2',
            Colors TEXT DEFAULT '4', IsToken BOOLEAN DEFAULT 0,
            ExpansionCode TEXT DEFAULT 'FDN', AbilityIds TEXT DEFAULT '',
            Order_Title TEXT, TypeTextId INT DEFAULT 1, SubtypeTextId INT DEFAULT 0,
            OldSchoolManaText TEXT DEFAULT 'o1oR', Rarity INT DEFAULT 2,
            LinkedFaceGrpIds TEXT DEFAULT '', Supertypes TEXT DEFAULT '',
            Subtypes TEXT DEFAULT '', IsPrimaryCard BOOLEAN DEFAULT 1,
            IsRebalanced BOOLEAN DEFAULT 0, IsDigitalOnly BOOLEAN DEFAULT 0,
            AlternateDeckLimit INT DEFAULT 0
        );
        CREATE TABLE Localizations_enUS (LocId INT, Loc TEXT, Formatted INT);
        CREATE TABLE Abilities (Id INT, TextId INT);
        INSERT INTO Localizations_enUS VALUES (1, 'Creature', 1), (2, 'Basic Land', 1),
            (3, 'Mountain', 1), (4, 'Snow Basic Land', 1);
    """)

    def add(gid, title, name, **overrides):
        values = {"GrpId": gid, "TitleId": title, "Order_Title": name, **overrides}
        conn.execute(
            f"INSERT INTO Cards ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        conn.execute("INSERT INTO Localizations_enUS VALUES (?, ?, 1)", (title, name))

    # Reprinted title: old requested printing becomes legal via FDN sibling.
    add(100, 200, "Reprinted Red Spell", ExpansionCode="M19")
    add(101, 200, "Reprinted Red Spell")
    add(102, 737019, "Banned Spell")
    add(103, 203, "Rotated Spell", ExpansionCode="M19")
    add(104, 204, "A-Rebalanced Spell", IsRebalanced=1)
    add(105, 205, "Digital Spell", IsDigitalOnly=1)
    add(106, 206, "Token", IsToken=1)
    add(107, 207, "Back Face", IsPrimaryCard=0)
    add(108, 208, "Suspended Spell")
    add(109, 209, "Explicitly Allowed", ExpansionCode="M19")
    add(110, 210, "Unknown Cost", OldSchoolManaText="o1oRoQ")
    add(111, 211, "Linked Spell", LinkedFaceGrpIds="107")
    add(112, 212, "Hybrid Spell", Colors="2,4", OldSchoolManaText="o2o(U/R)")
    add(113, 213, "Unknown Color", Colors="99")
    add(114, 214, "Limited Copies", AlternateDeckLimit=1)
    # Ordinary basic absent from collection and outside current legal sets.
    add(
        300,
        400,
        "Mountain",
        Types="5",
        Supertypes="1",
        Subtypes="49",
        Colors="",
        OldSchoolManaText="",
        TypeTextId=2,
        SubtypeTextId=3,
        ExpansionCode="M19",
        Rarity=1,
        AlternateDeckLimit=250,
    )
    add(
        301,
        401,
        "Snow-Covered Mountain",
        Types="5",
        Supertypes="1,3",
        Subtypes="49",
        Colors="",
        OldSchoolManaText="",
        TypeTextId=4,
        SubtypeTextId=3,
        Rarity=1,
        ExpansionCode="M19",
    )
    add(
        302,
        402,
        "Wastes",
        Types="5",
        Supertypes="1",
        Subtypes="",
        Colors="",
        OldSchoolManaText="",
        TypeTextId=2,
        Rarity=1,
    )
    for gid in range(500, 509):
        add(gid, gid + 1000, f"Quest Red Spell {gid}")
    conn.commit()
    conn.close()
    raw = MTGADatabase(path)
    monkeypatch.setattr(server, "_get_mtgadb", lambda: raw)
    yield raw
    raw.close()


def test_native_schema_reprints_bans_colors_costs_and_free_basics(native, tmp_path):
    config = {**STANDARD, "suspendedTitleIds": [208], "allowedTitleIds": [209, 737019]}
    path = write_log(tmp_path / "Player.log", config, multiline=True)
    result = metadata.load_quest_card_metadata(list(range(100, 115)), path)
    assert result[100]["legalities"] == {"standard": "legal"}
    assert result[100]["colors"] == ["R"]
    assert result[100]["mana_cost"] == "{1}{R}"
    assert result[100]["type_line"] == "Creature"
    for gid in (102, 103, 108):
        assert result[gid]["legalities"]["standard"] == "not_legal"
    assert result[109]["legalities"]["standard"] == "legal"
    assert not {104, 105, 106, 107, 113, 114} & result.keys()
    assert result[110]["mana_cost"] == ""
    assert result[111]["layout"] == "multiface"
    assert result[112]["mana_cost"] == "{2}{U/R}"
    assert result[112]["colors"] == ["U", "R"]
    assert result[300]["name"] == "Mountain"
    assert result[300]["free_basic"] is True
    assert result[300]["legalities"] == {"standard": "legal"}
    assert 301 not in result and 302 not in result


def test_builder_uses_metadata_and_one_unlocked_basic_for_24_copies(native, tmp_path):
    from arenamcp.auto_quest import build_quest_deck

    collection = {gid: 4 for gid in range(500, 509)}
    cards = metadata.load_quest_card_metadata(list(collection), write_log(tmp_path / "Player.log"))
    assert build_quest_deck("Cast 20 red or green spells.", collection, cards) is None
    collection[300] = 1
    spec = build_quest_deck("Cast 20 red or green spells.", collection, cards)
    assert spec is not None
    assert spec.spell_count == 36
    assert sum(n for _, n in spec.main) == 60
    assert (300, 24) in spec.main
    assert collection[300] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"useRebalancedCards": True},
        {"individualCardQuotas": {"200": {"max": 1}}},
        {"RarityPerCardQuotasInternal": {"2": {"max": 1}}},
        {"RestrictToColorIdentityInternal": [1]},
        {"newRestriction": True},
        {"CardCountRestrictionInternal": 1},
        {"FormatTypeInternal": 2},
        {"legalSets": []},
        {"legalSets": "FDN"},
        {"bannedTitleIds": ["737019"]},
        {"suspendedTitleIds": None},
        {"commandZoneQuota": {"max": 1}},
        {"mainDeckQuota": {"min": 100, "max": 100}},
    ],
)
def test_unknown_or_unsupported_format_rules_fail_closed(tmp_path, changes):
    path = write_log(tmp_path / "Player.log", {**STANDARD, **changes})
    assert metadata.load_quest_card_metadata([100], path) == {}


def test_latest_response_replaces_previous_rules(native, tmp_path):
    path = write_log(tmp_path / "Player.log")
    assert metadata.load_quest_card_metadata([100], path)[100]["legalities"]["standard"] == "legal"
    newer = tmp_path / "new.log"
    write_log(newer, {**STANDARD, "bannedTitleIds": [200]})
    with path.open("a") as stream:
        stream.write(newer.read_text())
    assert metadata.load_quest_card_metadata([100], path)[100]["legalities"]["standard"] == "not_legal"
    with path.open("a") as stream:
        stream.write('<== GetFormats(broken)\n{"Formats": [')
    assert metadata.load_quest_card_metadata([100], path) == {}


def test_stale_response_is_not_refreshed_by_touching_log(tmp_path):
    path = write_log(tmp_path / "Player.log", age=timedelta(days=2))
    os.utime(path, None)
    assert metadata.load_quest_card_metadata([100], path) == {}


def test_missing_timestamp_and_missing_log_fail_closed(tmp_path):
    path = tmp_path / "Player.log"
    assert metadata.load_quest_card_metadata([100], path) == {}
    path.write_text("<== GetFormats(response)\n" + json.dumps({"Formats": [STANDARD]}))
    assert metadata.load_quest_card_metadata([100], path) == {}


def test_unknown_db_schema_fails_closed(native, tmp_path):
    native._card_columns.remove("IsRebalanced")
    assert metadata.load_quest_card_metadata([100], write_log(tmp_path / "Player.log")) == {}


def test_only_explicit_native_batches_used(native, tmp_path, monkeypatch):
    batches = []
    original = native.get_cards_batch

    def batch(ids):
        batches.append(ids)
        return original(ids)

    monkeypatch.setattr(native, "get_cards_batch", batch)
    result = metadata.load_quest_card_metadata(
        [100, 100, True, -1, "101"], write_log(tmp_path / "Player.log")
    )
    assert batches == [[100, 300]]
    assert set(result) == {100, 300}


def test_installed_log_and_native_database_read_only_smoke():
    """Optional real-install regression: no network, account or gameplay calls."""
    from arenamcp import server
    from arenamcp.auto_quest import build_quest_deck
    from arenamcp.mtgadb import find_mtga_database

    path = Path.home() / "Library/Logs/Wizards Of The Coast/MTGA/Player.log"
    if not path.exists() or not find_mtga_database() or not metadata._read_standard_format(path):
        pytest.skip("Fresh installed native GetFormats evidence unavailable")
    result = metadata.load_quest_card_metadata([], path)
    assert {c["name"] for c in result.values() if c["free_basic"]} == metadata._BASIC_NAMES
    assert all(c["legalities"]["standard"] == "legal" for c in result.values())

    raw = server._get_mtgadb()
    with raw._conn_lock:
        ids = [
            row[0]
            for row in raw._conn.execute(
                "SELECT GrpId FROM Cards WHERE ExpansionCode='FDN' AND Colors='4' "
                "AND IsPrimaryCard=1 AND IsToken=0"
            )
        ]
    cards = metadata.load_quest_card_metadata(ids, path)
    # Synthetic ownership only: proves the actual installed format and native
    # metadata feed the builder, without reading or changing account inventory.
    inventory = dict.fromkeys(ids, 4)
    basic = next(gid for gid, info in cards.items() if info["name"] == "Mountain")
    inventory[basic] = 1
    spec = build_quest_deck("Cast 20 red spells.", inventory, cards)
    assert spec is not None
    assert spec.spell_count == 36 and sum(n for _, n in spec.main) == 60
    assert all(cards[gid]["legalities"]["standard"] == "legal" for gid, _ in spec.main)
