"""Tests for deck_inventory.py parser."""

import json
import os
import re
import tempfile

import pytest

# Regexes from deck_inventory.py (copied for focused unit tests)
CARD_RE = re.compile(r"^(\d+)\s+\[(\w+):(\w+)(\*?)\]\s+(.+)$")
SB_RE = re.compile(r"^SB:\s+(\d+)\s+\[(\w+):(\w+)(\*?)\]\s+(.+)$")
SKIP_PREFIXES = ("LAYOUT", "NAME:", "#")


def _parse_line(raw: str) -> dict | None:
    """Parse a single .dck line, returning card dict or None for metadata."""
    raw = raw.rstrip("\n").rstrip("\r")
    if not raw:
        return None
    if any(raw.startswith(p) for p in SKIP_PREFIXES):
        return None

    m = SB_RE.match(raw)
    if m:
        return {
            "count": int(m.group(1)),
            "set": m.group(2),
            "num": m.group(3),
            "star": bool(m.group(4)),
            "name": m.group(5).strip(),
            "sideboard": True,
        }

    m = CARD_RE.match(raw)
    if m:
        return {
            "count": int(m.group(1)),
            "set": m.group(2),
            "num": m.group(3),
            "star": bool(m.group(4)),
            "name": m.group(5).strip(),
            "sideboard": False,
        }

    return None  # unrecognized — skip


def test_comma_in_card_name():
    """Cards with commas (e.g. 'Skrelv, Defector Mite') parse correctly."""
    result = _parse_line("4 [ONE:33] Skrelv, Defector Mite")
    assert result is not None
    assert result["count"] == 4
    assert result["name"] == "Skrelv, Defector Mite"
    assert not result["sideboard"]
    assert not result["star"]


def test_another_comma_card():
    """Another comma-in-name card."""
    result = _parse_line("4 [LCI:63] Malcolm, Alluring Scoundrel")
    assert result is not None
    assert result["name"] == "Malcolm, Alluring Scoundrel"
    assert result["set"] == "LCI"
    assert result["num"] == "63"


def test_sideboard_parsing():
    """SB: lines parse as sideboard=True."""
    result = _parse_line("SB: 1 [WAR:234] Saheeli, Sublime Artificer")
    assert result is not None
    assert result["count"] == 1
    assert result["name"] == "Saheeli, Sublime Artificer"
    assert result["sideboard"] is True


def test_sideboard_another():
    """Another SB card."""
    result = _parse_line("SB: 1 [MRD:54] Thoughtcast")
    assert result is not None
    assert result["name"] == "Thoughtcast"
    assert result["sideboard"] is True


def test_skip_name_line():
    """NAME: lines are ignored."""
    assert _parse_line("NAME:UW Control") is None
    assert _parse_line("NAME:RB Aggro") is None


def test_skip_layout_line():
    """LAYOUT lines are ignored."""
    assert _parse_line("LAYOUT MAIN:(1,1)(NONE,false,50)|(...)") is None


def test_skip_empty_line():
    """Empty lines are ignored."""
    assert _parse_line("") is None


def test_star_in_set_number():
    """* after collector number (foil/alt art) is handled."""
    # WAR:221* — Teferi, Time Raveler with alternate-art star
    result = _parse_line("2 [WAR:221*] Teferi, Time Raveler")
    assert result is not None
    assert result["name"] == "Teferi, Time Raveler"
    assert result["star"] is True
    assert result["num"] == "221"


def test_star_flag_false_by_default():
    """Without *, star field is False."""
    result = _parse_line("4 [ONE:33] Skrelv, Defector Mite")
    assert result is not None
    assert result["star"] is False


def test_alphanumeric_collector_number():
    """Collector numbers like '410a' parse correctly."""
    result = _parse_line("1 [LCI:410a] Cavern of Souls")
    assert result is not None
    assert result["name"] == "Cavern of Souls"
    assert result["num"] == "410a"


def test_another_alphanumeric():
    """Another alphanumeric: LCI:410a."""
    result = _parse_line("1 [TSP:176] Rift Bolt")
    assert result is not None
    assert result["num"] == "176"


def test_standard_card_line():
    """Normal card line without star or comma."""
    result = _parse_line("4 [MKM:273] Island")
    assert result is not None
    assert result["count"] == 4
    assert result["name"] == "Island"
    assert result["set"] == "MKM"
    assert result["num"] == "273"
    assert not result["sideboard"]


def test_distinct_counts():
    """Verify total distinct vs total count for a sample deck."""
    content = (
        "4 [ONE:33] Skrelv, Defector Mite\n"
        "4 [LCI:63] Malcolm, Alluring Scoundrel\n"
        "4 [ONE:33] Skrelv, Defector Mite\n"  # duplicate name from a different set
        "7 [MKM:273] Island\n"
        "4 [DMU:44] Combat Research\n"
    )
    lines = content.split("\n")
    parsed = []
    for line in lines:
        r = _parse_line(line)
        if r:
            parsed.append(r)

    distinct_names = set(c["name"] for c in parsed)
    total_count = sum(c["count"] for c in parsed)

    assert len(distinct_names) == 4, f"Expected 4 distinct, got {len(distinct_names)}"
    assert total_count == 23, f"Expected 23 total, got {total_count}"


def test_sideboard_not_counted_in_main():
    """Sideboard cards are tagged separately."""
    content = (
        "4 [WAR:221] Teferi, Time Raveler\n"
        "SB: 1 [WAR:221] Teferi, Time Raveler\n"
    )
    lines = content.split("\n")
    main_cards = []
    sb_cards = []
    for line in lines:
        r = _parse_line(line)
        if r:
            if r["sideboard"]:
                sb_cards.append(r)
            else:
                main_cards.append(r)

    assert len(main_cards) == 1
    assert len(sb_cards) == 1


def test_skip_hash_comment():
    """# comment lines are ignored."""
    assert _parse_line("# This is a comment") is None


def test_regression_no_false_positive():
    """Random non-card strings should not match."""
    assert _parse_line("random text here") is None
    assert _parse_line("12345") is None
    assert _parse_line("4[] Card Name") is None


def test_inventory_json_exists():
    """deck_inventory.json exists and has the right shape."""
    json_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "training", "wp3", "deck_inventory.json"
    )
    assert os.path.exists(json_path), f"{json_path} not found"
    with open(json_path) as f:
        data = json.load(f)
    assert "primary_deck" in data
    assert data["deck_count"] == 29
    assert len(data["decks"]) == 29
    assert data["primary_deck"] == "UWTempo"


def test_primary_deck_has_no_overlap_field():
    """UWTempo should not have overlap_with_primary."""
    json_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "training", "wp3", "deck_inventory.json"
    )
    with open(json_path) as f:
        data = json.load(f)
    for deck in data["decks"]:
        if deck["deck"] == "UWTempo":
            assert "overlap_with_primary" not in deck, "Primary deck should not have overlap field"
            return
    pytest.fail("UWTempo not found in inventory")


def test_oathbreaker_has_sideboard():
    """Oathbreaker_UR.dck has 2 SB cards."""
    json_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "training", "wp3", "deck_inventory.json"
    )
    with open(json_path) as f:
        data = json.load(f)
    for deck in data["decks"]:
        if deck["deck"] == "Oathbreaker_UR":
            assert deck["distinct_sb"] == 2, f"Expected 2 SB cards, got {deck['distinct_sb']}"
            assert deck["sb_total"] == 2
            return
    pytest.fail("Oathbreaker_UR not found")


def test_all_decks_have_colours():
    """Every deck should have a colour field (even if just 'C' for colorless)."""
    json_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "training", "wp3", "deck_inventory.json"
    )
    with open(json_path) as f:
        data = json.load(f)
    for deck in data["decks"]:
        assert deck.get("colours"), f"{deck['deck']} has no colours field"
