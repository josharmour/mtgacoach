"""Evaluates oracle text parser coverage across gauntlet decks and recorded match history.

Reports percentage of ability lines parsed into structured IR, Unknown leaves,
and the most frequent unparsed phrases.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from arenamcp.ability_synthesizer import AbilitySynthesizer, UnknownEffect


def run_coverage_analysis() -> None:
    deck_pools_path = Path("src/arenamcp/data/gauntlet_card_pools.json")
    if not deck_pools_path.exists():
        print("Gauntlet card pools not found.")
        return

    pools = json.loads(deck_pools_path.read_text(encoding="utf-8"))
    unique_cards: set[str] = set()
    for cards in pools.values():
        unique_cards.update(cards)

    # Also add known Brawl commanders / staples
    unique_cards.update(
        [
            "The Notary Hobbits",
            "Samwise Gamgee",
            "Gilded Goose",
            "Adeline, Resplendent Cathar",
            "Rosie Cotton of South Lane",
            "Pippin, Guard of the Citadel",
            "Tireless Provisioner",
            "Academy Manufactor",
            "Jaheira, Friend of the Forest",
        ]
    )

    total_cards = len(unique_cards)
    total_lines = 0
    parsed_lines = 0
    unknown_effects: Counter[str] = Counter()

    print(f"Analyzing ability coverage for {total_cards} unique cards...")

    # Enrich from local MTGA db or Scryfall cache if available
    try:
        from arenamcp.scryfall import ScryfallClient

        scryfall = ScryfallClient()
    except Exception:
        scryfall = None

    for card_name in sorted(unique_cards):
        oracle = ""
        if scryfall:
            info = scryfall.get_card_by_name(card_name)
            if info:
                oracle = info.get("oracle_text", "")

        if not oracle:
            continue

        res = AbilitySynthesizer.parse_card(card_name, oracle)
        lines = [l for l in oracle.split("\n") if l.strip()]
        total_lines += len(lines)
        parsed_lines += int(round(res.coverage * len(lines)))

        for u in res.unparsed:
            unknown_effects[u[:50]] += 1

    cov_pct = (parsed_lines / max(1, total_lines)) * 100
    print(f"\n--- ABILITY SYNTHESIZER COVERAGE ---")
    print(f"Cards evaluated: {total_cards}")
    print(f"Total lines: {total_lines}")
    print(f"Parsed lines: {parsed_lines} ({cov_pct:.1f}%)")
    if unknown_effects:
        print("\nTop unparsed clauses:")
        for phrase, count in unknown_effects.most_common(5):
            print(f"  [{count}x] {phrase}...")


if __name__ == "__main__":
    run_coverage_analysis()
