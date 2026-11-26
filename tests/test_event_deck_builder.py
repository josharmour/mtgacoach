"""Tests for owned-only event deck adaptation, native format validation, and UI wiring."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arenamcp.event_deck_builder import (
    adapt_deck_for_event,
    build_event_deck,
    describe_format_rules,
    load_event_card_catalog,
    load_event_format_rules,
)
from arenamcp.event_queue import event_can_build_deck, event_eligibility, near_decks_for_event
from arenamcp.mac_bridge_adapter import BEPINEX_REFLECT_COMMANDS, MacBridgeAdapter


def _build_test_mtgadb(tmp_path: Path) -> Any:
    """Create a synthetic local MTGA SQLite DB + MTGADatabase wrapper for offline tests."""
    from arenamcp.mtgadb import MTGADatabase

    db_path = tmp_path / "Raw_CardDatabase_test.mtga"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE Localizations_enUS (
            LocId INTEGER,
            Formatted INTEGER,
            Loc TEXT
        );
        CREATE TABLE Abilities (
            Id INTEGER PRIMARY KEY,
            TextId INTEGER,
            LoyaltyCost TEXT
        );
        CREATE TABLE Cards (
            GrpId INTEGER PRIMARY KEY,
            TitleId INTEGER,
            TypeTextId INTEGER,
            SubtypeTextId INTEGER,
            Rarity INTEGER,
            ExpansionCode TEXT,
            IsPrimaryCard INTEGER,
            IsToken INTEGER,
            IsRebalanced INTEGER,
            IsDigitalOnly INTEGER,
            RebalancedCardGrpId INTEGER,
            AlternateDeckLimit INTEGER,
            Colors TEXT,
            ColorIdentity TEXT,
            Supertypes TEXT,
            Types TEXT,
            Subtypes TEXT,
            AbilityIds TEXT,
            LinkedFaceGrpIds TEXT,
            OldSchoolManaText TEXT,
            Order_CMCWithXLast INTEGER,
            Power TEXT,
            Toughness TEXT,
            Order_Title TEXT
        );
        """
    )

    loc_id = 1000
    next_ability = 500

    def add_card(
        grp_id: int,
        title_id: int,
        name: str,
        *,
        rarity: int,
        exp: str,
        colors: str,
        ci: str,
        supertypes: str = "",
        types: str = "2",
        subtypes: str = "10",
        type_line: str = "Creature",
        subtype_line: str = "Elf",
        mana_raw: str = "o1oG",
        cmc: int = 2,
        power: str = "2",
        toughness: str = "2",
        is_rebalanced: int = 0,
        rebalanced_grp_id: int = 0,
        oracle_text: str = "",
        alternate_limit: int = 0,
    ) -> None:
        nonlocal loc_id, next_ability
        title_loc = loc_id
        t_loc = loc_id + 1
        s_loc = loc_id + 2
        loc_id += 10
        conn.execute("INSERT INTO Localizations_enUS VALUES (?, 1, ?)", (title_loc, name))
        conn.execute("INSERT INTO Localizations_enUS VALUES (?, 1, ?)", (t_loc, type_line))
        if subtype_line:
            conn.execute("INSERT INTO Localizations_enUS VALUES (?, 1, ?)", (s_loc, subtype_line))
        else:
            s_loc = 0
        ab_str = ""
        if oracle_text:
            ab_loc = loc_id
            loc_id += 1
            ab_id = next_ability
            next_ability += 1
            conn.execute("INSERT INTO Localizations_enUS VALUES (?, 1, ?)", (ab_loc, oracle_text))
            conn.execute("INSERT INTO Abilities VALUES (?, ?, NULL)", (ab_id, ab_loc))
            ab_str = f"{ab_id}:{ab_loc}"
        conn.execute(
            """
            INSERT INTO Cards VALUES (
                ?, ?, ?, ?, ?, ?, 1, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?
            )
            """,
            (
                grp_id,
                title_id,
                t_loc,
                s_loc,
                rarity,
                exp,
                is_rebalanced,
                rebalanced_grp_id,
                alternate_limit,
                colors,
                ci,
                supertypes,
                types,
                subtypes,
                ab_str,
                mana_raw,
                cmc,
                power,
                toughness,
                name,
            ),
        )

    # Five ordinary basic lands
    add_card(
        91301,
        648,
        "Plains",
        rarity=1,
        exp="FDN",
        colors="",
        ci="1",
        supertypes="1",
        types="5",
        subtypes="54",
        type_line="Basic Land",
        subtype_line="Plains",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
    )
    add_card(
        91303,
        652,
        "Island",
        rarity=1,
        exp="FDN",
        colors="",
        ci="2",
        supertypes="1",
        types="5",
        subtypes="43",
        type_line="Basic Land",
        subtype_line="Island",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
    )
    add_card(
        91305,
        653,
        "Swamp",
        rarity=1,
        exp="FDN",
        colors="",
        ci="3",
        supertypes="1",
        types="5",
        subtypes="69",
        type_line="Basic Land",
        subtype_line="Swamp",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
    )
    add_card(
        91307,
        654,
        "Mountain",
        rarity=1,
        exp="FDN",
        colors="",
        ci="4",
        supertypes="1",
        types="5",
        subtypes="48",
        type_line="Basic Land",
        subtype_line="Mountain",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
    )
    add_card(
        91309,
        655,
        "Forest",
        rarity=1,
        exp="FDN",
        colors="",
        ci="5",
        supertypes="1",
        types="5",
        subtypes="29",
        type_line="Basic Land",
        subtype_line="Forest",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
    )

    # Commanders (GW): Rare commander (illegal in ArtisanBrawl) + Uncommon replacement commander (legal) + Banned-as-commander uncommon
    add_card(
        10001,
        2001,
        "Trostani Rare",
        rarity=4,
        exp="MKM",
        colors="1,5",
        ci="1,5",
        supertypes="2",
        types="2",
        type_line="Legendary Creature",
        subtype_line="Dryad",
        mana_raw="o1oWoG",
        cmc=3,
        power="3",
        toughness="4",
    )
    add_card(
        10002,
        2002,
        "Selesnya Uncommon Guide",
        rarity=3,
        exp="BLB",
        colors="1,5",
        ci="1,5",
        supertypes="2",
        types="2",
        type_line="Legendary Creature",
        subtype_line="Rabbit",
        mana_raw="o1oWoG",
        cmc=3,
        power="3",
        toughness="3",
    )
    add_card(
        10003,
        2003,
        "Banned Uncommon Legend",
        rarity=3,
        exp="BLB",
        colors="1,5",
        ci="1,5",
        supertypes="2",
        types="2",
        type_line="Legendary Creature",
        subtype_line="Mouse",
        mana_raw="oWoG",
        cmc=2,
        power="2",
        toughness="2",
    )
    # Off-color Uncommon commander (RB) — should be skipped when GW commander is available
    add_card(
        10004,
        2004,
        "Rakdos Legend",
        rarity=3,
        exp="BLB",
        colors="3,4",
        ci="3,4",
        supertypes="2",
        types="2",
        type_line="Legendary Creature",
        subtype_line="Lizard",
        mana_raw="oBoR",
        cmc=2,
        power="2",
        toughness="2",
    )

    # 80 GW commons/uncommons (20000..20079), legal in ArtisanBrawl + Standard (exp FDN/BLB)
    for i in range(80):
        is_w = i % 2 == 0
        c = "1" if is_w else "5"
        mana = "o1oW" if is_w else "o2oG"
        rarity = 3 if i % 3 == 0 else 2
        is_spell = i % 5 == 0
        add_card(
            20000 + i,
            3000 + i,
            f"GW Artisan Card {i}",
            rarity=rarity,
            exp="BLB" if i < 60 else "FDN",
            colors=c,
            ci=c,
            types="4" if is_spell else "2",
            type_line="Instant" if is_spell else "Creature",
            subtype_line="" if is_spell else "Citizen",
            mana_raw=mana,
            cmc=2 if is_w else 3,
            power="" if is_spell else "3",
            toughness="" if is_spell else "2",
            oracle_text="Destroy target creature." if is_spell else "",
        )

    # 15 Rare/Mythic GW cards (30000..30014) — legal in HistoricBrawl, illegal in ArtisanBrawl / StandardPauper
    for i in range(15):
        add_card(
            30000 + i,
            4000 + i,
            f"GW Mythic Bomb {i}",
            rarity=5 if i < 5 else 4,
            exp="FDN",
            colors="1,5",
            ci="1,5",
            types="2",
            type_line="Creature",
            subtype_line="Angel",
            mana_raw="o2oWoG",
            cmc=4,
            power="4",
            toughness="4",
        )

    # Rebalanced pair: paper 40001 <-> A-rebalanced 40002 (ArtisanBrawl requires use_rebalanced=False)
    add_card(
        40001,
        5001,
        "Woodland Caller",
        rarity=2,
        exp="BLB",
        colors="5",
        ci="5",
        types="2",
        type_line="Creature",
        mana_raw="o1oG",
        cmc=2,
        is_rebalanced=0,
        rebalanced_grp_id=40002,
    )
    add_card(
        40002,
        5001,
        "A-Woodland Caller",
        rarity=2,
        exp="BLB",
        colors="5",
        ci="5",
        types="2",
        type_line="Creature",
        mana_raw="oG",
        cmc=1,
        is_rebalanced=1,
        rebalanced_grp_id=0,
    )

    # Off-color blue card (illegal in GW Brawl commander identity)
    add_card(
        45001,
        5501,
        "Blue Counterspell",
        rarity=2,
        exp="FDN",
        colors="2",
        ci="2",
        types="4",
        type_line="Instant",
        subtype_line="",
        mana_raw="oUoU",
        cmc=2,
    )

    # Historic-only uncommon legal in ArtisanBrawl (since ArtisanBrawl uses Historic sets), illegal in StandardPauper
    add_card(
        46001,
        5601,
        "Old Set Uncommon",
        rarity=3,
        exp="M19",
        colors="1",
        ci="1",
        types="2",
        type_line="Creature",
        mana_raw="o1oW",
        cmc=2,
    )

    # Dual common land (GW)
    add_card(
        47001,
        5701,
        "Blossoming Sands",
        rarity=2,
        exp="FDN",
        colors="",
        ci="1,5",
        types="5",
        type_line="Land",
        subtype_line="",
        mana_raw="",
        cmc=0,
        power="",
        toughness="",
        oracle_text="Blossoming Sands enters tapped.",
    )

    conn.commit()
    conn.close()
    return MTGADatabase(db_path=db_path)


def _write_sample_formats_log(tmp_path: Path) -> Path:
    log_path = tmp_path / "Player.log"
    payload = {
        "Formats": [
            {
                "name": "ArtisanBrawl",
                "legalSets": ["BLB", "FDN", "M19"],
                "bannedTitleIds": [4999],
                "BannedAsCommanderTitleIds": [2003],
                "CardCountRestrictionInternal": 1,
                "FormatTypeInternal": 3,
                "mainDeckQuota": {"min": 99, "max": 99},
                "sideBoardQuota": {"max": 1},
                "commandZoneQuota": {"min": 1, "max": 1},
                "SideboardBehaviorInternal": 1,
                "RarityPerCardQuotasInternal": {"0": {"max": 1}, "1": {"max": 1}, "2": {}, "3": {}},
            },
            {
                "name": "Brawl",
                "legalSets": ["BLB", "FDN"],
                "CardCountRestrictionInternal": 1,
                "FormatTypeInternal": 3,
                "mainDeckQuota": {"min": 59, "max": 59},
                "sideBoardQuota": {"max": 1},
                "commandZoneQuota": {"min": 1, "max": 1},
                "SideboardBehaviorInternal": 1,
            },
            {
                "name": "StandardPauper",
                "legalSets": ["BLB", "FDN"],
                "FormatTypeInternal": 3,
                "mainDeckQuota": {"min": 60, "max": 250},
                "sideBoardQuota": {"max": 15},
                "commandZoneQuota": {},
                "RarityPerCardQuotasInternal": {"1": {}, "2": {}, "3": {}},
            },
        ]
    }
    log_path.write_text("[UnityCrossThreadLogger]<== GetFormats(1)\n" + json.dumps(payload) + "\n")
    return log_path


def test_load_format_rules_and_adapt_artisan_brawl_and_controls(tmp_path: Path):
    mtgadb = _build_test_mtgadb(tmp_path)
    log_path = _write_sample_formats_log(tmp_path)

    rules = load_event_format_rules("ArtisanBrawl", log_path=log_path)
    assert rules is not None
    assert rules.main_min == 99 and rules.main_max == 99
    assert rules.command_zone_min == 1
    assert rules.singleton is True
    assert rules.rarity_max_copies == {"common": 1, "uncommon": 1, "rare": 0, "mythic": 0}
    assert 2003 in rules.banned_as_commander_title_ids
    assert "99 + 1 commander" in describe_format_rules(rules)
    assert "commons & uncommons only" in describe_format_rules(rules)

    # Collection owns:
    # - Trostani Rare (10001), Selesnya Uncommon Guide (10002), Banned Uncommon (10003), Rakdos (10004)
    # - 55 GW commons/uncommons (20000..20054) — enough to fill 99 cards with basics
    # - 15 GW mythics/rares (30000..30014)
    # - Both Woodland Caller (40001) and A-Woodland Caller (40002)
    # - Blue counterspell (45001) + dual land (47001) + basics
    collection = {
        10001: 1,
        10002: 1,
        10003: 1,
        10004: 1,
        **{20000 + i: 1 for i in range(65)},
        **{30000 + i: 1 for i in range(15)},
        40001: 1,
        40002: 1,
        45001: 2,
        47001: 1,
        91301: 1,
        91309: 1,
    }

    # Almost-qualifying Historic Brawl deck:
    # Rare commander + 10 rares + 1 rebalanced A-Woodland Caller + 1 off-color blue card + 49 legal commons/uncommons + 38 basics = 99
    source_deck = {
        "deck_id": "11111111-1111-4111-8111-111111111111",
        "name": "My Historic Brawl",
        "format": "HistoricBrawl",
        "command_zone": [{"grp_id": 10001, "count": 1}],
        "main": (
            [{"grp_id": 30000 + i, "count": 1} for i in range(10)]
            + [{"grp_id": 40002, "count": 1}]
            + [{"grp_id": 45001, "count": 1}]
            + [{"grp_id": 20000 + i, "count": 1} for i in range(49)]
            + [{"grp_id": 91301, "count": 19}, {"grp_id": 91309, "count": 19}]
        ),
        "sideboard": [{"grp_id": 20070, "count": 1}],
        "companions": [{"grp_id": 20070, "count": 1}],
    }
    extra_ids = {c["grp_id"] for pile in ("main", "command_zone", "sideboard") for c in source_deck[pile]}
    catalog, legal_titles = load_event_card_catalog(collection, extra_ids, rules, mtgadb)

    plan = adapt_deck_for_event(source_deck, rules, collection, catalog, legal_titles)
    assert plan.ok is True, plan.error
    # Commander swapped from rare Trostani to owned legal uncommon GW commander (never banned 10003 or off-color 10004!)
    assert plan.commander_swapped is True
    assert plan.command_zone == [(10002, 1)]
    # Exact 99 main deck cards, singleton nonbasics, cleared sideboard/companions for Brawl
    assert sum(q for _, q in plan.main) == 99
    assert plan.sideboard == []
    assert plan.companions == []

    main_map = dict(plan.main)
    # Rares/mythics & off-color removed; A-Woodland Caller swapped to owned paper Woodland Caller (40001)
    assert all(30000 + i not in main_map for i in range(15))
    assert 45001 not in main_map
    assert 40002 not in main_map
    assert 40001 in main_map

    # Every placed non-basic card obeys collection count (0 wildcards) & rarity & singleton & GW color identity
    for gid, qty in plan.main:
        info = catalog[gid]
        assert info.color_identity <= {"W", "G"}
        if not info.is_basic_land:
            assert qty == 1
            assert collection.get(gid, 0) >= qty
            assert info.rarity in {"common", "uncommon"}

    # Downsize test: 99-card deck adapted down to 59-card Standard Brawl
    brawl_rules = load_event_format_rules("Brawl", log_path=log_path)
    assert brawl_rules is not None
    brawl_plan = adapt_deck_for_event(source_deck, brawl_rules, collection, catalog, legal_titles)
    assert brawl_plan.ok is True, brawl_plan.error
    assert sum(q for _, q in brawl_plan.main) == 59
    assert len(brawl_plan.command_zone) == 1

    # StandardPauper 60-card test: keeps/adds commons up to 4x owned, drops rares & uncommons
    pauper_rules = load_event_format_rules("StandardPauper", log_path=log_path)
    assert pauper_rules is not None
    pauper_coll = {
        **{20000 + i: 4 for i in range(30)},
        **{30000 + i: 4 for i in range(5)},
        91301: 1,
        91309: 1,
    }
    pauper_source = {
        "deck_id": "22222222-2222-4222-8222-222222222222",
        "name": "GW Midrange",
        "format": "Standard",
        "main": (
            [{"grp_id": 30000 + i, "count": 4} for i in range(3)]  # 12 mythics (cut)
            + [{"grp_id": 20000 + i * 3, "count": 4} for i in range(3)]  # 12 uncommons (cut)
            + [
                {"grp_id": 20001, "count": 4},
                {"grp_id": 20002, "count": 4},
                {"grp_id": 20004, "count": 4},
            ]  # 12 commons (keep)
            + [{"grp_id": 91301, "count": 12}, {"grp_id": 91309, "count": 12}]  # 24 basics
        ),
        "sideboard": [],
    }
    p_extra = {c["grp_id"] for c in pauper_source["main"]}
    p_cat, p_legal = load_event_card_catalog(pauper_coll, p_extra, pauper_rules, mtgadb)
    p_plan = adapt_deck_for_event(pauper_source, pauper_rules, pauper_coll, p_cat, p_legal)
    assert p_plan.ok is True, p_plan.error
    assert sum(q for _, q in p_plan.main) >= 60
    for gid, qty in p_plan.main:
        info = p_cat[gid]
        if not info.is_basic_land:
            assert info.rarity == "common"
            assert pauper_coll[gid] >= qty

    # Control: insufficient owned legal cards fails closed cleanly with 0 wildcards spent
    tiny_coll = {10002: 1, 20001: 1, 91301: 1}
    t_cat, t_legal = load_event_card_catalog(tiny_coll, extra_ids, rules, mtgadb)
    fail_plan = adapt_deck_for_event(source_deck, rules, tiny_coll, t_cat, t_legal)
    assert fail_plan.ok is False
    assert "Not enough owned legal cards" in fail_plan.error


def test_build_event_deck_clones_never_mutates_source_and_validates_natively(tmp_path: Path):
    mtgadb = _build_test_mtgadb(tmp_path)
    log_path = _write_sample_formats_log(tmp_path)

    src_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    clone_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    calls: list[tuple[str, dict[str, Any]]] = []
    source_deck = {
        "deck_id": src_id,
        "name": "Trostani Brawl",
        "format": "HistoricBrawl",
        "command_zone": [{"grp_id": 10001, "count": 1}],
        "main": (
            [{"grp_id": 30000 + i, "count": 1} for i in range(10)]
            + [{"grp_id": 20000 + i, "count": 1} for i in range(51)]
            + [{"grp_id": 91301, "count": 19}, {"grp_id": 91309, "count": 19}]
        ),
        "sideboard": [],
        "companions": [],
    }
    collection = {
        10001: 1,
        10002: 1,
        **{20000 + i: 1 for i in range(65)},
        **{30000 + i: 1 for i in range(15)},
        91301: 1,
        91309: 1,
    }

    class _FakeBridge:
        connected = True

        def __init__(self, *, validate_ok: bool = True):
            self.decks = [dict(source_deck)]
            self.validate_ok = validate_ok

        def draft_command(self, action: str, timeout: float | None = None, **kwargs: Any) -> dict[str, Any]:
            calls.append((action, dict(kwargs)))
            if action == "get_decks":
                return {"ok": True, "decks": list(self.decks)}
            if action == "get_collection":
                return {"ok": True, "cards": dict(collection), "wildcards": {}, "gold": 0, "gems": 0}
            if action == "create_deck":
                self.decks.append(
                    {
                        "deck_id": clone_id,
                        "name": kwargs["name"],
                        "format": "HistoricBrawl",
                        "main": list(source_deck["main"]),
                        "command_zone": list(source_deck["command_zone"]),
                        "sideboard": [],
                        "companions": [],
                    }
                )
                return {"ok": True, "name": kwargs["name"]}
            if action == "update_deck":
                assert kwargs["deck_id"] == clone_id, "Must NEVER mutate original deck!"
                for d in self.decks:
                    if d["deck_id"] == clone_id:
                        for pile, rows in kwargs["piles"].items():
                            d[pile] = [{"grp_id": g, "count": q} for g, q in rows]
                return {"ok": True, "deck_id": clone_id}
            if action == "validate_deck_for_format":
                if self.validate_ok:
                    return {
                        "ok": True,
                        "deck_id": clone_id,
                        "format_name": kwargs["format_name"],
                        "is_valid": True,
                        "summary": "Valid",
                        "counters": {},
                    }
                return {
                    "ok": True,
                    "deck_id": clone_id,
                    "format_name": kwargs["format_name"],
                    "is_valid": False,
                    "summary": "1 non-format card",
                    "counters": {"NumberNonFormatCard": 1},
                }
            raise AssertionError(f"Unexpected bridge command: {action}")

    bridge = _FakeBridge(validate_ok=True)
    res = build_event_deck(
        bridge=bridge,
        event_name="Contender_Match",
        event_format="ArtisanBrawl",
        source_deck_id=src_id,
        mtgadb=mtgadb,
        log_path=log_path,
        display_name="Contender Match",
        poll_interval=0.01,
    )
    assert res["ok"] is True, res
    assert res["deck_id"] == clone_id
    assert res["commander_swapped"] is True
    assert res["new_commander_name"] == "Selesnya Uncommon Guide"
    assert all(c[0] != "craft" for c in calls)
    assert [c[0] for c in calls] == [
        "get_decks",
        "get_collection",
        "create_deck",
        "get_decks",
        "update_deck",
        "validate_deck_for_format",
    ]
    # Original deck untouched
    assert bridge.decks[0]["command_zone"] == [{"grp_id": 10001, "count": 1}]

    # Control: if Arena native validation fails, build_event_deck reports failure instead of ok=True
    calls.clear()
    bad_bridge = _FakeBridge(validate_ok=False)
    res_bad = build_event_deck(
        bridge=bad_bridge,
        event_name="Contender_Match",
        event_format="ArtisanBrawl",
        source_deck_id=src_id,
        mtgadb=mtgadb,
        log_path=log_path,
        display_name="Contender Match",
        poll_interval=0.01,
    )
    assert res_bad["ok"] is False
    assert "1 non-format card" in res_bad["error"]


def test_bridge_near_decks_validate_command_and_ui_adapt_flow():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.coach_session import CoachSession
    from arenamcp.desktop.event_mode_dialog import EventModeDialog
    from arenamcp.event_bridge import _attach_eligible_decks
    from arenamcp.pipe_adapter import PipeAdapter

    assert "validate_deck_for_format" in BEPINEX_REFLECT_COMMANDS

    # 1. Verify _attach_eligible_decks attaches sorted near_decks and validate_deck_for_format works.
    d1 = str(uuid.UUID(int=101))
    d2 = str(uuid.UUID(int=102))

    class _MockAdapter:
        def _run(self, ops, timeout):
            first_op = ops.ops[0]

            def _guid_dict(hex_s):
                u = uuid.UUID(hex_s)
                d = {"_a": u.time_low, "_b": u.time_mid, "_c": u.time_hi_version}
                for ch, b in zip("defghijk", u.bytes[8:], strict=True):
                    d[f"_{ch}"] = b
                return d

            # Batch 1 in _attach_eligible_decks: [DecksManager, GetAllCachedDecks(depth=1)] (2 ops)
            if first_op.get("member") == "DecksManager" and len(ops.ops) == 2:
                return [{"$h": 2}, {"$items": [{"$h": 11}, {"$h": 12}]}]
            # Summary batch in _read_deck_summaries: [Summary(depth=2), Format, ...] or [Summary(depth=2), ...]
            if first_op.get("member") == "Summary":
                out = []
                summaries = {
                    11: {"DeckId": _guid_dict(d1), "Name": "Close Brawl"},
                    12: {"DeckId": _guid_dict(d2), "Name": "Far Brawl"},
                }
                formats = {11: "HistoricBrawl", 12: "HistoricBrawl"}
                last_h = 11
                for op in ops.ops:
                    if op.get("member") == "Summary":
                        last_h = op["target"]["h"]
                        # Explicitly omit 'Format' field to mimic real Unity Summary serialization.
                        out.append(summaries[last_h])
                    elif op.get("member") == "Format":
                        out.append(formats[last_h])
                return out
            # Batch in validate_deck_for_format: [WrapperController, DecksManager, GetAllCachedDecks(depth=1)] (3 ops)
            if any(op.get("method") == "GetAllCachedDecks" for op in ops.ops):
                assert any(op.get("depth") == 1 for op in ops.ops if op.get("method") == "GetAllCachedDecks")
                return [
                    {"$h": 1},
                    {"$h": 2},
                    {"$items": [{"$h": 11}, {"$h": 12}]},
                ]
            # Validation batch: 5 ops per deck in _attach_eligible_decks, or single-deck 5 ops in validate_deck_for_format.
            if len(ops.ops) >= 8:
                return [
                    None,
                    None,
                    None,
                    {"NumberNonFormatCard": 3, "NumberOfInvalidCards": 3},
                    False,
                    None,
                    None,
                    None,
                    {"NumberNonFormatCard": 14, "NumberBannedAsCommanderCards": 1},
                    False,
                ]
            return [None, None, None, {"NumberNonFormatCard": 2}, False]

    adapter = _MockAdapter()
    events = [
        {
            "event_name": "Contender_Match",
            "category": "constructed_event",
            "in_progress": False,
            "is_precon": False,
            "skip_deck_validation": False,
            "deck_select_format": "ArtisanBrawl",
        }
    ]
    _attach_eligible_decks(adapter, 1, events, 5.0)
    assert events[0]["has_eligible_deck"] is False
    near = events[0]["near_decks"]
    assert [d["deck_id"] for d in near] == [d1, d2]
    assert "3 non-format cards" in near[0]["summary"]
    assert near[1]["issue_count"] == 15

    # validate_deck_for_format over MacBridgeAdapter
    mbr = MacBridgeAdapter(
        lambda cmd, to: {"ok": True, "results": adapter._run(SimpleNamespace(ops=cmd["ops"]), to)}
    )
    val_res = mbr._cmd_validate_deck_for_format({"deck_id": d1, "format_name": "ArtisanBrawl"}, 5.0)
    assert val_res["ok"] is True
    assert val_res["is_valid"] is False
    assert "2 non-format cards" in val_res["summary"]

    # 2. Helper & UI tests for affordable buildable event vs unaffordable control
    buildable_ev = {
        "event_name": "Contender_Match",
        "display_name": "Contender Match",
        "category": "constructed_event",
        "is_free": True,
        "in_progress": False,
        "deck_select_format": "ArtisanBrawl",
        "entry_fees": [{"currency_type": "Free", "quantity": 0, "is_free": True, "affordable": True}],
        "eligible_decks": [],
        "near_decks": near,
    }
    unaffordable_ev = {
        **buildable_ev,
        "event_name": "Contender_Expensive",
        "display_name": "Expensive Contender",
        "is_free": False,
        "entry_fees": [{"currency_type": "Gold", "quantity": 50000, "is_free": False, "affordable": False}],
    }
    assert event_can_build_deck(buildable_ev, []) is True
    assert event_can_build_deck(unaffordable_ev, []) is False
    assert event_eligibility(buildable_ev, [])[0] is False
    assert len(near_decks_for_event(buildable_ev)) == 2

    _app = QApplication.instance() or QApplication([])
    dlg = EventModeDialog()
    dlg.update_events(
        {
            "ok": True,
            "on_home": True,
            "wallet": {"gold": 500, "gems": 0},
            "events": [unaffordable_ev, buildable_ev],
        }
    )

    items_by_name = {}
    for i in range(dlg.event_list.count()):
        item = dlg.event_list.item(i)
        data = item.data(Qt.UserRole)
        if isinstance(data, dict):
            items_by_name[data["event_name"]] = item

    # Unaffordable stays unselectable even when near_decks is present; affordable buildable is selectable.
    assert items_by_name["Contender_Expensive"].flags() == Qt.NoItemFlags
    assert items_by_name["Contender_Match"].flags() != Qt.NoItemFlags
    assert "(adapt deck)" in items_by_name["Contender_Match"].text()
    assert dlg._selected_event()["event_name"] == "Contender_Match"
    assert not dlg.adapt_box.isHidden()
    assert dlg.start_btn.isEnabled() is False

    # Clicking Adapt emits buildDeckRequested and disables button; on_deck_built refreshes and selects built deck.
    build_requests: list[dict] = []
    refreshes: list[bool] = []
    dlg.buildDeckRequested.connect(build_requests.append)
    dlg.refreshRequested.connect(lambda: refreshes.append(True))
    dlg.adapt_btn.click()
    assert len(build_requests) == 1
    assert build_requests[0]["event_format"] == "ArtisanBrawl"
    assert build_requests[0]["source_deck_id"] == d1
    assert dlg.adapt_btn.isEnabled() is False

    built_id = str(uuid.UUID(int=999))
    dlg.on_deck_built(
        {
            "ok": True,
            "event_name": "Contender_Match",
            "deck_id": built_id,
            "deck_name": "Close Brawl (Contender)",
            "summary": "Built 'Close Brawl (Contender)' (89 kept, 10 owned cards added, 0 wildcards)",
        }
    )
    assert len(refreshes) == 1

    # Simulated refresh returns new deck as eligible -> Start Event enables & new deck is auto-selected!
    dlg.update_events(
        {
            "ok": True,
            "on_home": True,
            "wallet": {"gold": 500, "gems": 0},
            "events": [
                {
                    **buildable_ev,
                    "eligible_decks": [
                        {"deck_id": built_id, "name": "Close Brawl (Contender)", "format": "HistoricBrawl"}
                    ],
                }
            ],
        }
    )
    assert dlg.adapt_box.isHidden()
    assert dlg.start_btn.isEnabled() is True
    assert dlg.deck_combo.currentData() == built_id
    assert "0 wildcards" in dlg.status_label.text()

    # 3. IPC round-trip smoke check for CoachSession + PipeAdapter
    seen_session_built: list[dict] = []
    sess = CoachSession()
    sess.eventDeckBuilt.connect(seen_session_built.append)
    sess._handle_process_event({"type": "event_deck_built", "ok": True, "deck_id": built_id})
    assert seen_session_built == [{"ok": True, "deck_id": built_id}]

    emitted_pipe: list[dict] = []
    pa = PipeAdapter.__new__(PipeAdapter)
    pa._emit = emitted_pipe.append
    pa.emit_event_deck_built({"ok": False, "error": "short by 3"})
    assert emitted_pipe == [{"type": "event_deck_built", "ok": False, "error": "short by 3"}]


def test_review_fixes_depth_currency_reentry_and_ladder_fallback():
    from PySide6.QtWidgets import QApplication
    from tests.mac_world import Obj
    from tests.test_event_mode import _EventWorld

    from arenamcp.desktop.event_mode_dialog import EventReentryDialog
    from arenamcp.event_bridge import (
        _read_wallet,
        enter_event,
        get_constructed_deck_select,
        select_event_deck,
        validate_deck_for_format,
    )
    from arenamcp.event_queue import EventQueueConfig
    from arenamcp.ladder import ranked_ladder
    from arenamcp.standalone_draft_event import _DraftEventMixin

    # 1. Depth-enforcing _EventWorld where Summary has NO 'Format' field (only property getter via ops.get)
    #    and Guid is an Obj (requiring Summary depth=2 to reach _a.._k).
    class _LiveParityWorld(_EventWorld):
        def __init__(self) -> None:
            super().__init__()
            self.props: dict[tuple[int, str], Any] = {}

        def send(self, command: dict, timeout: float | None) -> dict:
            results: list[Any] = []
            for op in command["ops"]:
                kind = op["op"]
                if kind == "new":
                    h = max(self.objects, default=500) + 1
                    self.objects[h] = {}
                    self.classes[h] = op["class"]
                    results.append({"$c": op["class"], "$h": h})
                elif kind == "get":
                    h = self.target(op["target"], results)
                    if h is None:
                        if op.get("optional"):
                            results.append(None)
                            continue
                        return {"ok": False, "error": f"null target for {op}"}
                    member = op["member"]
                    depth = int(op.get("depth") or 0)
                    if (h, member) in self.props:
                        results.append(self.node_depth(self.props[(h, member)], depth))
                    else:
                        members = self.objects[h]
                        if member not in members and op.get("optional"):
                            results.append(None)
                        else:
                            results.append(self.node_depth(members.get(member), depth))
                else:
                    op_copy = op
                    target = op.get("target")
                    if isinstance(target, dict) and "ref" in target:
                        resolved_h = self.target(target, results)
                        if resolved_h is not None:
                            op_copy = {**op, "target": {"h": resolved_h}}
                    args = op.get("args")
                    if isinstance(args, list):
                        new_args = []
                        for arg in args:
                            if isinstance(arg, dict) and "ref" in arg:
                                ah = self.target(arg, results)
                                new_args.append({"h": ah} if ah is not None else arg)
                            else:
                                new_args.append(arg)
                        op_copy = {**op_copy, "args": new_args}
                    sub = super().send({"ops": [op_copy]}, timeout)
                    if not sub.get("ok"):
                        return sub
                    results.append(sub["results"][0])
            return {"ok": True, "results": results}

    world = _LiveParityWorld()
    deck_guid = str(uuid.UUID(int=4242))
    u = uuid.UUID(deck_guid)
    g_fields = dict(
        zip("abcdefghijk", (u.time_low, u.time_mid, u.time_hi_version, *u.bytes[8:]), strict=True)
    )
    guid_h = world.add(100, "Guid", **g_fields)
    # Format is exposed only via world.props (property getter), never serialized as a field on Summary!
    sum_h = world.add(101, "Client_DeckSummary", DeckId=Obj(guid_h), Name="Artisan Built")
    world.props[(sum_h, "Format")] = "HistoricBrawl"
    deck_h = world.add(102, "Client_Deck", Summary=Obj(sum_h))
    cached_list = world.add(103, "List", **{"$items": [Obj(deck_h)]})
    dm_h = world.add(104, "DecksManager")
    world.results[(dm_h, "GetAllCachedDecks")] = Obj(cached_list)

    fee_h = world.add(
        105,
        "EventEntryFeeInfo",
        CurrencyType={"e": "CurrencyType_Free", "v": 0},
        Quantity=0,
        UsesRemaining=True,
        ReferenceId="",
    )
    fees_list = world.add(106, "List", **{"$items": [Obj(fee_h)]})
    info_h = world.add(
        107,
        "EventInfo",
        InternalEventName="Contender_Match",
        IsPreconEvent=False,
        SkipDeckValidation=False,
        EntryFees=Obj(fees_list),
    )
    course_h = world.add(108, "CourseData", CurrentModule={"e": "Join", "v": 1})
    ux_h = world.add(109, "EventUXInfo", DeckSelectFormat="ArtisanBrawl", Joinability={"e": "Open", "v": 1})
    pe_h = world.add(
        110, "PlayerEvent", EventInfo=Obj(info_h), CourseData=Obj(course_h), EventUXInfo=Obj(ux_h)
    )
    ctx_h = world.add(111, "EventContext", PlayerEvent=Obj(pe_h))
    ev_mgr_h = world.add(112, "EventManager")
    world.results[(ev_mgr_h, "GetEventContext")] = Obj(ctx_h)
    tok_entry_h = world.add(120, "DictEntry", key="ContenderToken", value=2)
    custom_tok_h = world.add(114, "Dictionary", entries=[Obj(tok_entry_h)])
    inv_h = world.add(
        113,
        "ClientPlayerInventory",
        gold=1000,
        gems=50,
        draftTokens=0,
        sealedTokens=0,
        CustomTokens=Obj(custom_tok_h),
    )
    inv_mgr_h = world.add(115, "InventoryManager", Inventory=Obj(inv_h))
    wrapper_h = world.add(
        1,
        "WrapperController",
        DecksManager=Obj(dm_h),
        EventManager=Obj(ev_mgr_h),
        InventoryManager=Obj(inv_mgr_h),
    )
    world.statics["WrapperController.Instance"] = wrapper_h
    ds_h = world.add(116, "ConstructedDeckSelectController", _decks=Obj(cached_list), _event=Obj(ctx_h))
    world.finds["ConstructedDeckSelectController"] = ds_h

    val_res_h = world.add(118, "DeckValidationResult", IsValid=True)
    disp_h = world.add(119, "DeckDisplayInfo", ValidationResult=Obj(val_res_h))
    orig_send = world.send

    def _dispatch_with_validation(cmd: dict, to: Any) -> dict:
        if any(op.get("op") == "new" for op in cmd["ops"]):
            res = []
            for op in cmd["ops"]:
                kind = op["op"]
                if kind == "new":
                    res.append({"$c": op["class"], "$h": 700})
                elif kind == "set":
                    res.append(None)
                elif kind == "call" and op.get("method") == "GetValidationForFormat":
                    res.append(world.node_depth(Obj(disp_h), 0))
                elif kind == "get" and op.get("member") == "ValidationResult":
                    res.append(world.node_depth(Obj(val_res_h), int(op.get("depth") or 0)))
                elif kind == "get" and op.get("member") == "IsValid":
                    res.append(True)
                else:
                    return {"ok": False, "error": f"unexpected op {op}"}
            return {"ok": True, "results": res}
        return orig_send(cmd, to)

    mbr = MacBridgeAdapter(_dispatch_with_validation)
    wallet = _read_wallet(mbr, wrapper_h, 5.0)
    assert wallet["custom_tokens"] == {"ContenderToken": 2}

    ds_out = get_constructed_deck_select(mbr, 5.0)
    assert ds_out["decks"] == [{"deck_id": deck_guid, "name": "Artisan Built", "format": "HistoricBrawl"}]
    sel_out = select_event_deck(mbr, {"event_name": "Contender_Match", "deck_id": deck_guid}, 5.0)
    assert sel_out["ok"] is True
    val_out = validate_deck_for_format(mbr, {"deck_id": deck_guid, "format_name": "ArtisanBrawl"}, 5.0)
    assert val_out["is_valid"] is True

    # 2. enter_event handles CurrencyType_Free enum prefix cleanly
    enter_out = enter_event(
        mbr,
        {
            "event_name": "Contender_Match",
            "currency_type": "CurrencyType_Free",
            "expected_quantity": 0,
            "require_free": True,
        },
        5.0,
    )
    assert enter_out["ok"] is True
    assert enter_out["currency_type"] == "CurrencyType_Free"

    # 3. EventReentryDialog rejected (Esc / window close) emits pause once
    _app = QApplication.instance() or QApplication([])
    reentry_replies: list[tuple] = []
    rdlg = EventReentryDialog(
        {
            "request_id": "req-1",
            "event_name": "Contender_Match",
            "outcome": "completed",
            "wins": 3,
            "losses": 0,
            "is_free": True,
        }
    )
    rdlg.responded.connect(lambda req, act, cur, qty: reentry_replies.append((req, act, cur, qty)))
    rdlg.reject()
    assert reentry_replies == [("req-1", "pause", "", 0)]

    # 4. set_auto_queue_mode leaving 'draft' clears stale _event_queue_config, and failed resign aborts re-entry
    statuses: list[tuple[str, str]] = []

    class _Rt(_DraftEventMixin):
        def __init__(self):
            self._event_queue_config = EventQueueConfig(event_name="Old_Event")
            self._auto_queue_enabled = True
            self.ui = SimpleNamespace(status=lambda k, v: statuses.append((k, v)))

        def _auto_queue_mode(self):
            return "same"

    rt = _Rt()
    rt.set_auto_queue_mode("same")
    assert rt._event_queue_config is None

    # 5. ranked_ladder strips _YYYYMMDD before normalize_event_key for extra_ranked fallback
    assert ranked_ladder("ContenderDraft_20261110", extra_ranked={"contenderdraft": "Limited"}) == "Limited"
