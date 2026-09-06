#!/usr/bin/env python3
"""Generate deck inventory from XMage .dck files.

Parses all 29 deck files under DECK_DIR, computes:
  - Per deck: distinct / total (main+sideboard) cards, colours, overlap with PRIMARY deck
  - Union card count across all decks
  - If card map exists, annotate mana_cost/type_line for color extraction

Outputs:
  - deck_inventory.json  — machine-readable inventory
  - deck_inventory.md    — human-readable markdown table
  - stdout summary       — union count + reconciliation vs 471
"""

import json
import os
import re
from collections import OrderedDict
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DECK_DIR = "/Volumes/repos/magezero/xmage/decks"
PRIMARY_DECK = "UWTempo.dck"
CARD_MAP_PATH = Path(__file__).resolve().parent.parent / "magezero_card_map.json"
OUTPUT_JSON = Path(__file__).resolve().parent / "deck_inventory.json"
OUTPUT_MD = Path(__file__).resolve().parent / "deck_inventory.md"

# Expected union from task description
EXPECTED_UNION = 471

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------
# Card line:   4 [SET:123] Card Name
# Card line:   4 [SET:123*] Card Name   (star = alternate art / foil)
# Card line:   4 [SET:410a] Card Name   (alphanumeric collector number)
CARD_RE = re.compile(r"^(\d+)\s+\[(\w+):(\w+)(\*?)\]\s+(.+)$")

# Sideboard line: SB: 1 [SET:123] Card Name
SB_RE = re.compile(r"^SB:\s+(\d+)\s+\[(\w+):(\w+)(\*?)\]\s+(.+)$")

# Skip these line prefixes
SKIP_PREFIXES = ("LAYOUT", "NAME:", "#")

# Mana-symbol-to-colour map (for colour extraction from mana_cost)
MANA_COLOUR_MAP = {
    "W": "W",
    "U": "U",
    "B": "B",
    "R": "R",
    "G": "G",
}

# Basic lands from type_line
BASIC_LAND_COLOURS = {
    "Plains": "W",
    "Island": "U",
    "Swamp": "B",
    "Mountain": "R",
    "Forest": "G",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_deck(filepath: str) -> dict:
    """Parse a .dck file, returning structured card data.

    Returns:
      {
        "name": str (basename stem),
        "cards": [ { "count": int, "set": str, "num": str, "star": bool, "name": str, "sideboard": bool }, ... ],
      }
    """
    filename = os.path.basename(filepath)
    name = os.path.splitext(filename)[0]

    cards = []
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            raw = line.rstrip("\n").rstrip("\r")

            if not raw:
                continue

            # Skip metadata lines
            if any(raw.startswith(p) for p in SKIP_PREFIXES):
                continue

            # Try sideboard line first (it starts with SB:)
            m = SB_RE.match(raw)
            if m:
                cards.append(
                    {
                        "count": int(m.group(1)),
                        "set": m.group(2),
                        "num": m.group(3),
                        "star": bool(m.group(4)),
                        "name": m.group(5).strip(),
                        "sideboard": True,
                    }
                )
                continue

            # Try normal card line
            m = CARD_RE.match(raw)
            if m:
                cards.append(
                    {
                        "count": int(m.group(1)),
                        "set": m.group(2).strip(),
                        "num": m.group(3).strip(),
                        "star": bool(m.group(4)),
                        "name": m.group(5).strip(),
                        "sideboard": False,
                    }
                )
                continue

            # If we get here, it's an unrecognized line — skip silently
    return {"name": name, "filename": filename, "cards": cards}


def load_card_map() -> dict:
    """Load the resolved card map (mana_cost, type_line), or empty dict."""
    if CARD_MAP_PATH.exists():
        with open(CARD_MAP_PATH) as f:
            return json.load(f)
    return {}


def extract_colours(card_name: str, mana_cost: str, type_line: str) -> set:
    """Determine colour identity of a card from its Scryfall data."""
    colours: set = set()

    # Basic lands
    if type_line and "Basic Land" in type_line:
        for bname, c in BASIC_LAND_COLOURS.items():
            if bname in type_line or bname == card_name:
                colours.add(c)
        return colours

    # Dual lands / typed lands: "Land — Plains Island"
    if type_line and "Land" in type_line and "—" in type_line:
        after_dash = type_line.split("—", 1)[1].strip()
        for word in after_dash.split():
            if word in BASIC_LAND_COLOURS:
                colours.add(BASIC_LAND_COLOURS[word])
        if colours:
            return colours

    # Extract from mana_cost: {W}, {U}, {B}, {R}, {G}
    if mana_cost:
        for symbol in re.findall(r"\{(\w)\}", mana_cost):
            if symbol in MANA_COLOUR_MAP:
                colours.add(MANA_COLOUR_MAP[symbol])
        if colours:
            return colours

    return colours


def colour_code(colours: set) -> str:
    """Return colour identity code, sorted WUBRG."""
    order = ["W", "U", "B", "R", "G"]
    return "".join(c for c in order if c in colours) if colours else "C"  # C = colorless


def deck_stats(deck: dict, card_map: dict) -> dict:
    """Compute statistics for a single deck."""
    all_cards = deck["cards"]
    # Separate main and sideboard
    main_cards = [c for c in all_cards if not c["sideboard"]]
    sb_cards = [c for c in all_cards if c["sideboard"]]

    # Distinct card names (unique by name, ignoring count)
    all_distinct = set(c["name"] for c in all_cards)
    main_distinct = set(c["name"] for c in main_cards)
    sb_distinct = set(c["name"] for c in sb_cards)

    # Total card count
    total_cards = sum(c["count"] for c in all_cards)
    main_total = sum(c["count"] for c in main_cards)
    sb_total = sum(c["count"] for c in sb_cards)

    # Colours: aggregate from all non-land cards + land cards
    deck_colours: set = set()
    unknown_cards = []
    for c in all_cards:
        cm = card_map.get(c["name"])
        if cm:
            mana_cost = cm.get("mana_cost", "")
            type_line = cm.get("type_line", "")
            c_colours = extract_colours(c["name"], mana_cost, type_line)
        else:
            c_colours = extract_colours(c["name"], "", "")
            if not c_colours:
                unknown_cards.append(c["name"])

        deck_colours |= c_colours

        # Also check by card name for basic lands not in card map
        if c["name"] in BASIC_LAND_COLOURS:
            deck_colours.add(BASIC_LAND_COLOURS[c["name"]])

    return {
        "deck": deck["name"],
        "filename": deck["filename"],
        "distinct_total": len(all_distinct),
        "distinct_main": len(main_distinct),
        "distinct_sb": len(sb_distinct),
        "total_cards": total_cards,
        "main_total": main_total,
        "sb_total": sb_total,
        "colours": colour_code(deck_colours),
        "colour_set": sorted(deck_colours),
        "unknown_colour_cards": unknown_cards,
    }


def compute_overlap(deck1_cards: list, deck2_cards: list) -> dict:
    """Compute card-level overlap between two decks."""
    names1 = set(c["name"] for c in deck1_cards)
    names2 = set(c["name"] for c in deck2_cards)
    common = names1 & names2
    union = names1 | names2
    only2 = names2 - names1
    return {
        "common_count": len(common),
        "deck2_only": len(only2),
        "union_count": len(union),
        "overlap_pct": round(len(common) / len(names2) * 100, 1) if names2 else 0,
        "common_cards": sorted(common),
        "deck2_only_cards": sorted(only2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    # Load card map
    card_map = load_card_map()
    print(f"Loaded card map: {len(card_map)} entries")

    # Find all .dck files
    deck_files = sorted([f for f in os.listdir(DECK_DIR) if f.endswith(".dck")])
    print(f"Found {len(deck_files)} deck files")

    # Parse all decks
    all_decks = {}  # name -> parsed deck dict
    for fname in deck_files:
        fpath = os.path.join(DECK_DIR, fname)
        deck = parse_deck(fpath)
        all_decks[deck["name"]] = deck

    # Compute stats per deck
    stats_by_deck = OrderedDict()
    union_all: set = set()
    for dname, deck in sorted(all_decks.items()):
        stats = deck_stats(deck, card_map)
        stats_by_deck[dname] = stats
        union_all |= set(c["name"] for c in deck["cards"])

    print(f"\nUnion card count (all 29 decks): {len(union_all)}")
    print(f"Expected (from task): {EXPECTED_UNION}")
    if len(union_all) == EXPECTED_UNION:
        print("✓ RECONCILES perfectly.")
    else:
        diff = len(union_all) - EXPECTED_UNION
        print(f"Δ: {diff:+d} cards. See notes below.")

    # Overlap with PRIMARY deck
    primary_name = os.path.splitext(PRIMARY_DECK)[0]
    primary_cards = all_decks[primary_name]["cards"]
    print(f"\nPrimary deck: {primary_name}")

    overlaps = {}
    for dname, deck in sorted(all_decks.items()):
        if dname == primary_name:
            continue
        ov = compute_overlap(primary_cards, deck["cards"])
        overlaps[dname] = ov

    # Per-deck overlap summary for context
    print("\nOverlap with primary deck (new distinct cards per opponent):")
    overlap_rows = []
    for dname in sorted(overlaps):
        ov = overlaps[dname]
        overlap_rows.append((dname, ov["deck2_only"], ov["common_count"], ov["overlap_pct"]))
    overlap_rows.sort(key=lambda x: -x[1])  # sort by new cards descending
    for dname, new, common, pct in overlap_rows:
        print(f"  {dname:30s}  {new:3d} new cards  {common:3d} shared  ({pct}% overlap)")

    # -----------------------------------------------------------------------
    # Write JSON
    # -----------------------------------------------------------------------
    output = OrderedDict()
    output["primary_deck"] = primary_name
    output["deck_count"] = len(all_decks)
    output["union_card_count"] = len(union_all)
    output["expected_union"] = EXPECTED_UNION
    output["reconciles"] = len(union_all) == EXPECTED_UNION

    output["decks"] = []
    for dname, stats in stats_by_deck.items():
        entry = dict(stats)
        del entry["unknown_colour_cards"]  # verbose, keep in markdown
        if dname != primary_name:
            ov = overlaps.get(dname, {})
            entry["overlap_with_primary"] = {
                "common_cards": ov.get("common_count", 0),
                "new_cards_vs_primary": ov.get("deck2_only", 0),
                "overlap_pct": ov.get("overlap_pct", 0),
            }
        output["decks"].append(entry)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2, sort_keys=False)
    print(f"\nWrote {OUTPUT_JSON}")

    # -----------------------------------------------------------------------
    # Write Markdown
    # -----------------------------------------------------------------------
    md_lines = []
    md_lines.append("# Deck Inventory — All 29 XMage Decks\n")
    md_lines.append(f"**Primary deck**: `{primary_name}`  ")
    md_lines.append(f"**Total distinct cards (union)**: {len(union_all)}  ")
    md_lines.append(f"**Expected**: {EXPECTED_UNION}  ")
    md_lines.append(
        f"**Reconciliation**: {'✓ matches' if len(union_all) == EXPECTED_UNION else f'Δ {len(union_all) - EXPECTED_UNION:+d}'}\n"
    )

    md_lines.append("## Per-Deck Summary\n")
    md_lines.append(
        "| Deck | Distinct Cards | Total Cards | Main | SB | Colours | New vs Primary | Overlap% | Unknown Colours |"
    )
    md_lines.append(
        "|------|---------------|-------------|------|----|---------|---------------|----------|----------------|"
    )

    rows = []
    for dname, stats in stats_by_deck.items():
        unknown_count = len(stats.get("unknown_colour_cards", []))
        unknown_str = str(unknown_count) if unknown_count else "—"
        if dname == primary_name:
            new_str = "—"
            ovpct_str = "—"
        else:
            ov = overlaps.get(dname, {})
            new_str = str(ov.get("deck2_only", "?"))
            ovpct_str = f"{ov.get('overlap_pct', 0):.1f}%"
        rows.append(
            (
                dname,
                stats["distinct_total"],
                stats["total_cards"],
                stats["main_total"],
                stats["sb_total"],
                stats["colours"],
                new_str,
                ovpct_str,
                unknown_str,
            )
        )
        # Print unknown cards for diagnosis
        if unknown_count:
            uc = stats["unknown_colour_cards"]
            print(f"  [WARN] {dname}: {unknown_count} card(s) with unknown colour: {uc}")

    rows.sort(key=lambda x: -x[1])  # sort by distinct cards descending
    for r in rows:
        md_lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} | {r[8]} |")

    # Current opponents section
    md_lines.append("\n## Current Opponents (in `configs/run.yml`)\n")
    current_opponents = [
        "Standard-MonoR",
        "Standard-MonoG",
        "Standard-MonoB",
        "Standard-MonoW",
        "Standard-MonoU",
    ]
    md_lines.append("| Deck | Distinct Cards | Colours | New vs Primary |")
    md_lines.append("|------|---------------|---------|---------------|")
    cur_distinct = set()
    for dname in current_opponents:
        stats = stats_by_deck.get(dname)
        if not stats:
            continue
        ov = overlaps.get(dname, {})
        md_lines.append(
            f"| {dname} | {stats['distinct_total']} | {stats['colours']} | {ov.get('deck2_only', '?')} |"
        )
        for c in all_decks[dname]["cards"]:
            cur_distinct.add(c["name"])

    # Current pool stats
    cur_pool = set()
    for dname in current_opponents:
        for c in all_decks[dname]["cards"]:
            cur_pool.add(c["name"])
    cur_pool |= set(c["name"] for c in primary_cards)

    md_lines.append(f"\n**Current pool**: {primary_name} + {', '.join(current_opponents)}  ")
    md_lines.append(f"**Distinct cards in current pool**: {len(cur_pool)}  ")

    # Coverage rank for remaining decks
    remaining = [d for d in sorted(all_decks) if d != primary_name and d not in current_opponents]
    md_lines.append("\n## Remaining Decks (25) — Coverage Rank\n")
    md_lines.append("| Rank | Deck | New Cards | Colours | Cumulative New | Cumulative Pool |")
    md_lines.append("|------|------|-----------|---------|---------------|----------------|")
    coverage = []
    for dname in remaining:
        ov = overlaps.get(dname, {})
        coverage.append((dname, ov["deck2_only"], ov["common_count"], stats_by_deck[dname]["colours"]))
    coverage.sort(key=lambda x: -x[1])

    cum_pool = set(cur_pool)
    cum_new = 0
    for i, (dname, new, common, colours) in enumerate(coverage, 1):
        deck_cards = set(c["name"] for c in all_decks[dname]["cards"])
        actually_new = len(deck_cards - cum_pool)
        cum_new += actually_new
        cum_pool |= deck_cards
        md_lines.append(f"| {i} | {dname} | {actually_new} | {colours} | {cum_new} | {len(cum_pool)} |")

    md_lines.append(f"\n**Potential pool with all 25 remaining decks**: {len(cum_pool)} distinct cards\n")

    # -----------------------------------------------------------------------
    # Cards NOT in the union that contribute to the 471 claim
    # -----------------------------------------------------------------------
    md_lines.append("## Reconciliation Detail\n")
    md_lines.append(f"The union of all 29 decks yields **{len(union_all)}** distinct card names.  ")
    md_lines.append(f"The task states **{EXPECTED_UNION}** cards on disk.  ")
    diff = len(union_all) - EXPECTED_UNION
    if diff == 0:
        md_lines.append("✓ **Reconciles perfectly**  ")
    elif diff > 0:
        md_lines.append(f"Union is **{diff} cards higher** than expected. Possible explanations:  ")
        md_lines.append("- Some cards counted here are not in the 471 (e.g., basic lands counted per-set)  ")
        md_lines.append("- The 471 may exclude certain cards (sideboard-only, etc.)  ")
    else:
        md_lines.append(
            f"Union is **{diff} cards lower** than expected ({len(union_all)} vs {EXPECTED_UNION}).  "
        )

    with open(OUTPUT_MD, "w") as f:
        f.write("\n".join(md_lines) + "\n")
    print(f"Wrote {OUTPUT_MD}")

    # -----------------------------------------------------------------------
    # Summary to stdout
    # -----------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("DECK INVENTORY SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Decks parsed:     {len(all_decks)}")
    print(f"  Union (distinct): {len(union_all)}")
    print(f"  Expected:         {EXPECTED_UNION}")
    reconciled = "YES" if len(union_all) == EXPECTED_UNION else "NO"
    print(f"  Reconciled:       {reconciled}")
    print(f"  Primary deck:     {primary_name}")
    print(f"  Card map:         {len(card_map)} entries")
    print(f"  Output JSON:      {OUTPUT_JSON}")
    print(f"  Output MD:        {OUTPUT_MD}")


if __name__ == "__main__":
    main()
