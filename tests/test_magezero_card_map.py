#!/usr/bin/env python3
"""Tests for the XMage↔Scryfall card map and action classifier."""

import json
import os

import pytest

# ── Paths ──────────────────────────────────────────────────────────────────────

HERE = os.path.dirname(__file__) or "."
REPO_ROOT = os.path.normpath(os.path.join(HERE, ".."))
TOOLS_TRAINING = os.path.join(REPO_ROOT, "tools", "training")
CARD_MAP_PATH = os.path.join(TOOLS_TRAINING, "magezero_card_map.json")
SCRYFALL_CACHE = os.path.join(TOOLS_TRAINING, "wp3", "scryfall_cache.json")
ACTIONS_MODULE = os.path.join(TOOLS_TRAINING, "magezero_actions.py")

# Known deck file names (6 decks, ~83 unique card names)
DECK_FILES = {
    "UWTempo": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "UWTempo.dck"),
    "Standard-MonoR": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "Standard-MonoR.dck"),
    "Standard-MonoG": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "Standard-MonoG.dck"),
    "Standard-MonoB": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "Standard-MonoB.dck"),
    "Standard-MonoW": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "Standard-MonoW.dck"),
    "Standard-MonoU": os.path.join(REPO_ROOT, "..", "..", "magezero", "xmage", "decks", "Standard-MonoU.dck"),
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def count_deck_names() -> set[str]:
    """Parse all 6 deck files and return the set of card names found."""
    import re

    names: set[str] = set()
    for _deck_name, path in DECK_FILES.items():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip().rstrip("\r")
                    if not line or line.startswith("LAYOUT"):
                        continue
                    m = re.match(r"^(\d+)\s+\[(\w+):(\w+)\]\s+(.+)$", line)
                    if m:
                        names.add(m.group(4).strip())
        except FileNotFoundError:
            pass  # deck files might not be accessible in CI
    return names


def load_card_map() -> dict:
    with open(CARD_MAP_PATH) as f:
        return json.load(f)


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestCardMapCompleteness:
    """Every card name from the 6 deck files is resolved in the map."""

    @pytest.fixture(scope="class")
    def deck_names(self) -> set[str]:
        return count_deck_names()

    @pytest.fixture(scope="class")
    def card_map(self) -> dict:
        return load_card_map()

    def test_map_exists(self):
        assert os.path.exists(CARD_MAP_PATH), f"Card map not found at {CARD_MAP_PATH}"

    def test_all_deck_names_in_map(self, deck_names, card_map):
        """Every name from the deck files is a key in the output map."""
        missing = deck_names - set(card_map.keys())
        assert not missing, f"{len(missing)} deck card names missing from map: {sorted(missing)}"

    def test_all_names_have_scryfall_field(self, card_map):
        """Every entry has a non-empty scryfall canonical name."""
        bad = [k for k, v in card_map.items() if not v.get("scryfall")]
        assert not bad, f"Entries without scryfall name: {bad}"

    def test_all_names_found(self, card_map):
        """Every card was found (exact or fuzzy) on Scryfall, except entries
        the local-bulk enricher explicitly recorded as having no local source
        (oracle_source == "none_local") — those are counted, not guessed."""
        missing = [
            k for k, v in card_map.items() if not v.get("found") and v.get("oracle_source") != "none_local"
        ]
        assert not missing, f"{len(missing)} cards not found on Scryfall: {missing}"

    def test_exact_count(self, card_map):
        """At most 2 non-enricher entries may be fuzzy (the layout-only refs).

        Entries added by enrich_card_map.py (tokens, DFC faces — tagged with
        an oracle_source) are name-normalized by construction ("Map Token" ->
        token "Map") and are exempt from the exactness invariant.
        """
        fuzzy = [
            k
            for k, v in card_map.items()
            if v.get("found")
            and not v.get("exact")
            and not str(v.get("oracle_source", "")).startswith("local_bulk:")
        ]
        assert len(fuzzy) <= 2, f"Expected ≤2 fuzzy matches, got {len(fuzzy)}: {fuzzy}"

    def test_oracle_text_enrichment(self, card_map):
        """The map carries oracle text for the training decks: every found
        entry either has an oracle_text field or is explicitly marked
        none_local. Guards against re-running resolve_cards.py without the
        enrichment step (which would silently strip oracle coverage)."""
        unenriched = [
            k
            for k, v in card_map.items()
            if v.get("found") and "oracle_text" not in v and v.get("oracle_source") != "none_local"
        ]
        assert not unenriched, (
            f"{len(unenriched)} found entries lack oracle_text (run "
            f"tools/training/wp3/enrich_card_map.py): {unenriched[:10]}"
        )

    def test_at_least_83_entries(self, card_map):
        """At least 83 entries in the map (the number of deck card names)."""
        assert len(card_map) >= 83, f"Expected ≥83 entries in card map, got {len(card_map)}"


class TestClassifyAction:
    """Tests for classify_action in magezero_actions.py."""

    @pytest.fixture(scope="class", autouse=True)
    def _import_module(self):
        """Import once, before any test in the class."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("magezero_actions", ACTIONS_MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pytest.classify_action = mod.classify_action
        pytest.classify_actions = mod.classify_actions
        pytest.card_map = load_card_map()
        return mod

    # ── Known action strings ──

    def test_play_land(self):
        r = pytest.classify_action("Play Adarkar Wastes")
        assert r["verb"] == "play"
        assert r["card"] == "Adarkar Wastes"

    def test_play_land_comment(self):
        """'Play' followed by a basic land."""
        r = pytest.classify_action("Play Forest")
        assert r["verb"] == "play"
        assert r["card"] == "Forest"

    def test_cast_creature(self):
        r = pytest.classify_action("Cast Kitsa, Otterball Elite")
        assert r["verb"] == "cast"
        assert r["card"] == "Kitsa, Otterball Elite"

    def test_cast_creature_with_ok(self):
        """[OK] suffix indicating affordable cast."""
        r = pytest.classify_action("Cast Kitsa, Otterball Elite [OK]")
        assert r["verb"] == "cast"
        assert r["card"] == "Kitsa, Otterball Elite"

    def test_cast_instant(self):
        r = pytest.classify_action("Cast Lightning Strike")
        assert r["verb"] == "cast"
        assert r["card"] == "Lightning Strike"

    def test_cast_dfc(self):
        """Double-faced card with // separator."""
        r = pytest.classify_action("Cast Unholy Annex // Ritual Chamber")
        assert r["verb"] == "cast"
        assert r["card"] == "Unholy Annex // Ritual Chamber"

    def test_pass(self):
        r = pytest.classify_action("Pass")
        assert r["verb"] == "pass"
        assert r["card"] is None

    def test_activate_ability(self):
        r = pytest.classify_action("use Kitsa, Otterball Elite")
        assert r["verb"] == "activate"
        assert r["card"] == "Kitsa, Otterball Elite"

    def test_attack_with(self):
        r = pytest.classify_action("Attack with Kitsa, Otterball Elite")
        assert r["verb"] == "attack"
        assert r["card"] == "Kitsa, Otterball Elite"

    def test_block_with(self):
        r = pytest.classify_action("Block with Axebane Ferox")
        assert r["verb"] == "block"
        assert r["card"] == "Axebane Ferox"

    def test_unknown_action_verb(self):
        r = pytest.classify_action("Unknown menu item")
        assert r["verb"] == "other"
        assert r["card"] is None

    def test_non_existent_card(self):
        """Unknown card name in a recognized verb pattern."""
        r = pytest.classify_action("Cast Completely MadeUp Card, Test")
        assert r["verb"] == "cast"
        assert r["card"] is None  # card not in map

    def test_comma_in_name_not_split(self):
        """Comma-inclusive card names are matched whole, not split on comma."""
        r = pytest.classify_action("Cast Malcolm, Alluring Scoundrel")
        assert r["verb"] == "cast"
        assert r["card"] == "Malcolm, Alluring Scoundrel"

    def test_longest_name_wins(self):
        """Longest card name substring should match first."""
        # "Skrelv, Defector Mite" and "Skrelv" — should match the full name
        r = pytest.classify_action("Cast Skrelv, Defector Mite")
        assert r["verb"] == "cast"
        assert r["card"] == "Skrelv, Defector Mite"

    def test_activate_with_prefix_use(self):
        r = pytest.classify_action("use Skrelv, Defector Mite")
        assert r["verb"] == "activate"
        assert r["card"] == "Skrelv, Defector Mite"

    def test_classify_actions_batch(self):
        """Batch classify_actions returns one result per input."""
        items = ["Play Island", "Cast Lightning Strike", "Pass"]
        results = pytest.classify_actions(items)
        assert len(results) == 3
        assert results[0]["verb"] == "play"
        assert results[1]["verb"] == "cast"
        assert results[2]["verb"] == "pass"


class TestCardNameResolution:
    """Edge cases in Scryfall resolution of the 83 deck names."""

    def test_brutal_cathar_dfc(self):
        """Brutal Cathar is a DFC — scryfall name includes both sides."""
        cm = load_card_map()
        entry = cm.get("Brutal Cathar")
        assert entry is not None
        assert "//" in entry["scryfall"], f"Expected DFC notation, got: {entry['scryfall']}"

    def test_unholy_annex_has_separator(self):
        """Unholy Annex // Ritual Chamber is already a DFC name."""
        cm = load_card_map()
        entry = cm.get("Unholy Annex // Ritual Chamber")
        assert entry is not None
        assert entry["exact"] is True

    def test_basic_lands_resolved(self):
        """Basic lands (Island, Plains, Mountain, Forest, Swamp) are present."""
        cm = load_card_map()
        for name in ["Island", "Plains", "Mountain", "Forest", "Swamp"]:
            assert name in cm, f"Basic land '{name}' missing from card map"
            assert cm[name]["found"] is True
