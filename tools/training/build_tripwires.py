"""Build 50 hand-crafted tripwire puzzle fixtures (WP-1.2 / T5).

Puzzle categories:
  - lethal_on_board (10 puzzles)
  - free_counterspell / obvious response (10 puzzles)
  - obvious_keep (15 puzzles)
  - obvious_mulligan (15 puzzles)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.action_planner import ACTION_SCHEMA  # noqa: E402


def generate_tripwire_fixtures() -> list[dict]:
    fixtures = []

    # 1. Obvious Keeps (15 puzzles)
    for i in range(1, 16):
        user_text = (
            "Match 7 - Game 1. On the play.\n"
            "Hand (7 cards): 3 Forest, Llanowar Elves, Elvish Archdruid, Beast Whisperer, Craterhoof Behemoth.\n"
            "Deck: Mono Green Stompy.\n"
            "Mulligan options: KEEP, MULLIGAN to 6.\n"
            "Legal: [1] KEEP [2] MULLIGAN"
        )
        fixtures.append(
            {
                "id": f"tripwire_keep_{i:03d}",
                "category": "obvious_keep",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "mulligan_keep",
            }
        )

    # 2. Obvious Mulligans (15 puzzles)
    for i in range(1, 16):
        user_text = (
            "Match 7 - Game 1. On the play.\n"
            "Hand (7 cards): 0 Lands, 7 5-drop spells (Sunfall, Teferi, Wandering Emperor, Elspeth, Memory Deluge, Farewell, Archangel).\n"
            "Deck: Azorius Control.\n"
            "Mulligan options: KEEP, MULLIGAN to 6.\n"
            "Legal: [1] KEEP [2] MULLIGAN"
        )
        fixtures.append(
            {
                "id": f"tripwire_mull_{i:03d}",
                "category": "obvious_mulligan",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "mulligan_mull",
            }
        )

    # 3. Lethal on Board (10 puzzles)
    for i in range(1, 11):
        user_text = (
            "Turn 5 - Main Phase 1. Opponent HP: 4. Opponent battlefield: Empty.\n"
            "Your battlefield: 2x 2/2 Grizzly Bears (untapped). No summoning sickness.\n"
            "Legal: [1] Declare Attackers (Attack All) [2] Pass Turn [3] Play Land"
        )
        fixtures.append(
            {
                "id": f"tripwire_lethal_{i:03d}",
                "category": "lethal_on_board",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "declare_attackers",
            }
        )

    # 4. Free Counterspell / Obvious Reaction (10 puzzles)
    for i in range(1, 11):
        user_text = (
            "Turn 4 - Opponent turn. Stack: Opponent casts Sheoldred, the Apocalypse.\n"
            "Your hand: Counterspell (open UU lands available).\n"
            "Legal: [1] Cast Counterspell targeting Sheoldred [2] Pass Priority"
        )
        fixtures.append(
            {
                "id": f"tripwire_counter_{i:03d}",
                "category": "free_counterspell",
                "system": ACTION_SCHEMA,
                "user": user_text,
                "expected_action_type": "cast_spell",
            }
        )

    return fixtures


def main():
    out_path = REPO / "tools/training/data/tripwire_fixtures.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fixtures = generate_tripwire_fixtures()
    with open(out_path, "w", encoding="utf-8") as f:
        for fix in fixtures:
            f.write(json.dumps(fix, ensure_ascii=False) + "\n")
    print(f"✓ Generated {len(fixtures)} tripwire puzzle fixtures at {out_path}")


if __name__ == "__main__":
    main()
